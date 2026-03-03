#!/usr/bin/env python3.11
"""
DTRS Position Monitor - WebSocket-based Real-time Price Feed
Replaces REST API polling with Binance WebSocket mark price stream.
All exit logic (stop loss, take profit, EMA, MA20) is unchanged.

Architecture:
- WebSocket subscribes to markPrice streams for all open positions
- On each price tick, immediately check stop loss / take profit
- EMA/MA20 checks (which require K-line data) still run on a 60s timer
  to avoid excessive K-line API calls
- When positions change (new open / close), WebSocket subscriptions
  are refreshed automatically
"""
import sys
import time
import json
import logging
import threading
import websocket  # websocket-client library
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [MONITOR] %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/opt/dtrs-engine/data/monitor.log', mode='a'),
    ]
)
logger = logging.getLogger('monitor')

sys.path.insert(0, '/opt/dtrs-engine')
from core.binance_client import client
from core.database import get_open_positions, update_position, create_trade, add_log
from core.monitor import PositionMonitor

# ── Globals ──────────────────────────────────────────────────────────────────
_monitor = PositionMonitor()

# Latest mark prices received from WebSocket: { "BTCUSDT": 65000.0, ... }
_mark_prices: dict = {}
_mark_prices_lock = threading.Lock()

# Currently subscribed symbols
_subscribed_symbols: set = set()
_ws_instance = None
_ws_lock = threading.Lock()

# Guard: prevent duplicate close/TP orders
# Stores pos_id of positions currently being processed to prevent re-entry
_processing_positions: set = set()
_processing_lock = threading.Lock()

# Cooldown: track last time each position was checked (pos_id -> timestamp)
# Prevents hammering the same position every second
_last_checked: dict = {}
_check_cooldown_sec = 3  # minimum seconds between checks for same position

# ── Entry price sync (unchanged from original) ────────────────────────────────
def get_binance_positions():
    """Get all active positions from Binance (REST, used only for entry price sync)"""
    positions = client._request('GET', '/fapi/v2/positionRisk', {}, signed=True)
    return {p['symbol']: p for p in positions if float(p.get('positionAmt', 0)) != 0}

def sync_entry_prices():
    """Sync entry prices from Binance into local DB (REST, runs every 5 min)"""
    try:
        binance_pos = get_binance_positions()
        db_positions = get_open_positions()
        updated = 0
        for pos in db_positions:
            sym = pos['symbol']
            if sym in binance_pos:
                bp = binance_pos[sym]
                real_entry = float(bp['entryPrice'])
                if real_entry > 0 and pos['entry_price'] == 0.0:
                    update_position(pos['id'], {'entry_price': real_entry})
                    logger.info(f"Synced entry price for {sym}: {real_entry}")
                    updated += 1
        if updated:
            logger.info(f"Synced {updated} entry prices from Binance")
    except Exception as e:
        logger.error(f"Failed to sync entry prices: {e}")

# ── Price-tick handler ────────────────────────────────────────────────────────
def _on_price_tick(symbol: str, mark_price: float):
    """
    Called on every WebSocket mark price update.
    Only checks stop loss and take profit (price-based checks).
    EMA/MA20 checks run separately on a timer to avoid K-line API spam.
    """
    positions = get_open_positions()
    for pos in positions:
        if pos['symbol'] != symbol:
            continue

        pos_id = pos['id']
        direction = pos['direction']
        stop_loss = pos.get('stop_loss', 0)
        entry_price = pos.get('entry_price', 0)
        tp1 = pos.get('take_profit_1', 0)
        tp2 = pos.get('take_profit_2', 0)
        tp1_hit = bool(pos.get('tp1_hit', 0))
        tp2_hit = bool(pos.get('tp2_hit', 0))

        # ── Cooldown guard: skip if checked too recently ─────────────────────
        now = time.time()
        with _processing_lock:
            last = _last_checked.get(pos_id, 0)
            if now - last < _check_cooldown_sec:
                continue
            # Also skip if already being processed
            if pos_id in _processing_positions:
                continue
            _last_checked[pos_id] = now
            _processing_positions.add(pos_id)

        try:
            # ── Stop loss check ──────────────────────────────────────────────
            if stop_loss and stop_loss > 0:
                sl_triggered = (
                    (direction == 'LONG' and mark_price <= stop_loss) or
                    (direction == 'SHORT' and mark_price >= stop_loss)
                )
                if sl_triggered:
                    logger.warning(f"SL TRIGGERED: {symbol} {direction} mark={mark_price:.6f} SL={stop_loss:.6f}")
                    _monitor._check_stop_loss(pos, mark_price)
                    continue  # Position closed, skip TP checks

            # ── Emergency stop: loss > 40% of margin ────────────────────────
            if entry_price > 0:
                qty = pos.get('quantity', 0)
                margin = entry_price * qty / pos.get('leverage', 20)
                if direction == 'LONG':
                    pnl = (mark_price - entry_price) * qty
                else:
                    pnl = (entry_price - mark_price) * qty
                pnl_pct = pnl / margin * 100 if margin > 0 else 0
                if pnl_pct < -40:
                    logger.error(f"EMERGENCY STOP: {symbol} loss {pnl_pct:.1f}% > -40%")
                    from core.executor import Executor
                    Executor().close_position_full(
                        pos_id, mark_price,
                        f"紧急止损: 亏损{pnl_pct:.1f}%"
                    )
                    add_log('error', 'MONITOR', f"{symbol} {direction} 紧急止损: 亏损{pnl_pct:.1f}%")
                    continue

            # ── Take profit checks ───────────────────────────────────────────
            if tp1 and tp2:
                _monitor._check_take_profits(pos, mark_price)

        finally:
            with _processing_lock:
                _processing_positions.discard(pos_id)

# ── WebSocket management ──────────────────────────────────────────────────────
def _build_stream_url(symbols: list) -> str:
    """Build combined stream URL for multiple mark price streams"""
    streams = '/'.join(f"{s.lower()}@markPrice@1s" for s in symbols)
    return f"wss://fstream.binance.com/stream?streams={streams}"

def _on_ws_message(ws, message):
    try:
        data = json.loads(message)
        # Combined stream wraps payload in {"stream": "...", "data": {...}}
        payload = data.get('data', data)
        if payload.get('e') == 'markPriceUpdate':
            symbol = payload['s']
            price = float(payload['p'])
            with _mark_prices_lock:
                _mark_prices[symbol] = price
            # Trigger price-based checks immediately
            _on_price_tick(symbol, price)
    except Exception as e:
        logger.error(f"WS message error: {e}")

def _on_ws_error(ws, error):
    logger.error(f"WebSocket error: {error}")

def _on_ws_close(ws, close_status_code, close_msg):
    logger.warning(f"WebSocket closed: {close_status_code} {close_msg}")

def _on_ws_open(ws):
    symbols = list(_subscribed_symbols)
    logger.info(f"WebSocket connected, subscribed to {len(symbols)} symbols: {symbols}")

def _start_websocket(symbols: list):
    """Start WebSocket connection for given symbols"""
    global _ws_instance
    if not symbols:
        logger.info("No open positions, WebSocket not started")
        return
    url = _build_stream_url(symbols)
    logger.info(f"Starting WebSocket: {url[:80]}...")
    ws = websocket.WebSocketApp(
        url,
        on_message=_on_ws_message,
        on_error=_on_ws_error,
        on_close=_on_ws_close,
        on_open=_on_ws_open,
    )
    _ws_instance = ws
    # Run in background thread, auto-reconnect on disconnect
    def run():
        while True:
            try:
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.error(f"WebSocket run_forever error: {e}")
            logger.warning("WebSocket disconnected, reconnecting in 5s...")
            time.sleep(5)
    t = threading.Thread(target=run, daemon=True)
    t.start()

def _refresh_subscriptions():
    """
    Check if open positions have changed.
    If new symbols appeared or old ones closed, restart WebSocket with updated list.
    """
    global _ws_instance, _subscribed_symbols
    positions = get_open_positions()
    current_symbols = set(p['symbol'] for p in positions)

    if current_symbols == _subscribed_symbols:
        return  # No change, nothing to do

    logger.info(f"Position change detected. Old: {_subscribed_symbols} → New: {current_symbols}")
    _subscribed_symbols = current_symbols

    # Close old WebSocket
    with _ws_lock:
        if _ws_instance:
            try:
                _ws_instance.close()
            except Exception:
                pass
            _ws_instance = None

    if current_symbols:
        _start_websocket(list(current_symbols))
    else:
        logger.info("No open positions, WebSocket stopped")

# ── Periodic K-line based checks (EMA / MA20) ────────────────────────────────
def _run_kline_checks():
    """
    Run EMA20 trailing stop and MA20 crossover checks.
    These require K-line data (REST API), so run every 60s to avoid rate limits.
    Uses latest cached mark price instead of calling get_mark_price() REST API.
    """
    positions = get_open_positions()
    for pos in positions:
        sym = pos['symbol']
        with _mark_prices_lock:
            current_price = _mark_prices.get(sym)
        if current_price is None:
            continue  # No price yet from WebSocket, skip
        try:
            _monitor._check_ema_trailing_stop(pos, current_price)
            _monitor._check_ma20_crossover(pos, current_price)
        except Exception as e:
            logger.error(f"K-line check error for {sym}: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    logger.info("=== DTRS Position Monitor Started (WebSocket Mode) ===")
    logger.info("Price-based checks: real-time via WebSocket")
    logger.info("K-line checks (EMA/MA20): every 60 seconds")

    # Initial entry price sync
    sync_entry_prices()

    # Initial WebSocket subscription
    _refresh_subscriptions()

    cycle = 0
    while True:
        time.sleep(60)
        cycle += 1
        logger.info(f"--- Periodic cycle {cycle} ---")

        try:
            # Refresh subscriptions if positions changed
            _refresh_subscriptions()

            # Entry price sync every 5 cycles (~5 min)
            if cycle % 5 == 0:
                sync_entry_prices()

            # K-line based checks (EMA20, MA20 crossover)
            _run_kline_checks()

        except Exception as e:
            logger.error(f"Periodic cycle error: {e}")

if __name__ == '__main__':
    main()
