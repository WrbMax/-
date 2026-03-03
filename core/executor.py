"""
DTRS Trading Engine - Trade Executor
Handles order placement, position sizing, and trade execution.
"""
import logging
import threading
from typing import Dict, Optional
from datetime import datetime
from config import config
from core.binance_client import client
from core.database import (
    create_position, create_trade, update_position, add_log, get_db
)

logger = logging.getLogger("dtrs.executor")

def _mark_signal_failed(signal: Dict, reason: str):
    """Mark a signal as failed (reuse 'filtered' status with failure reason)"""
    sig_id = signal.get("id")
    if not sig_id:
        return
    try:
        conn = get_db()
        conn.execute(
            "UPDATE signals SET status='filtered', reason=? WHERE id=?",
            (f"下单失败: {reason}", sig_id)
        )
        conn.commit()
        conn.close()
        logger.info(f"Signal {sig_id} marked as failed: {reason}")
    except Exception as e:
        logger.error(f"Failed to mark signal {sig_id} as failed: {e}")

# In-memory lock to prevent concurrent duplicate orders for the same symbol+period
_position_locks: Dict[str, threading.Lock] = {}
_global_lock = threading.Lock()

def _get_position_lock(key: str) -> threading.Lock:
    """Get or create a per-symbol-period lock"""
    with _global_lock:
        if key not in _position_locks:
            _position_locks[key] = threading.Lock()
        return _position_locks[key]


class Executor:
    """Trade execution engine"""

    def execute_signal(self, signal: Dict) -> Optional[int]:
        """Execute a trading signal - open a new position"""
        if signal["status"] != "executed":
            return None

        symbol = signal["symbol"]
        direction = signal["direction"]
        period = signal["period"]
        price = signal["price"]
        atr_val = signal.get("atr", 0)

        # === DEDUP LOCK: Per-symbol-period threading lock ===
        lock_key = f"{symbol}:{period}"
        position_lock = _get_position_lock(lock_key)

        if not position_lock.acquire(blocking=False):
            logger.warning(f"DEDUP LOCK: {symbol} {period} is being processed by another thread, skipping")
            add_log("warning", "EXECUTOR", f"并发去重: {symbol} {period} 正在被另一线程处理, 跳过")
            return None

        try:
            # === DB CHECK: Verify no open position exists ===
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT COUNT(*) FROM positions WHERE symbol=? AND period=? AND status IN ("OPEN","PARTIAL")',
                (symbol, period)
            )
            count = cursor.fetchone()[0]
            conn.close()

            if count > 0:
                logger.warning(f"DEDUP DB: {symbol} {period} already has {count} open position(s), skipping")
                add_log("warning", "EXECUTOR", f"数据库去重: {symbol} {period} 已有 {count} 个持仓, 跳过")
                return None

            # === MAX POSITIONS CHECK ===
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM positions WHERE status="OPEN"')
            total_open = cursor.fetchone()[0]
            conn.close()

            max_pos = config.margin.max_open_positions
            if total_open >= max_pos:
                logger.info(f"Max positions reached ({total_open}/{max_pos}), skipping {symbol}")
                add_log("info", "EXECUTOR", f"达到最大持仓数 ({total_open}/{max_pos}), 跳过 {symbol}")
                return None

            return self._do_execute(signal, symbol, direction, period, price, atr_val)

        finally:
            position_lock.release()

    def _do_execute(self, signal: Dict, symbol: str, direction: str, period: str,
                    price: float, atr_val: float) -> Optional[int]:
        """Actually execute the trade after all checks pass"""
        try:
            # 1. Calculate position size
            wallet_balance = client.get_wallet_balance()
            margin_ratio_map = {
                "1h": config.margin.margin_1h,
                "4h": config.margin.margin_4h,
                "1d": config.margin.margin_1d,
            }
            margin_ratio = margin_ratio_map.get(period, config.margin.margin_1h)
            margin_amount = wallet_balance * margin_ratio
            position_size = margin_amount * config.margin.leverage

            # 2. Set leverage and margin type (ignore already-set errors)
            try:
                client.set_margin_type(symbol, "CROSSED")
            except Exception:
                pass  # -4046: Already CROSSED, ignore
            try:
                client.set_leverage(symbol, config.margin.leverage)
            except Exception as e:
                logger.warning(f"Set leverage warning for {symbol}: {e}")

            # 3. Calculate quantity
            quantity = client.calculate_quantity(symbol, position_size, price)
            if quantity <= 0:
                logger.error(f"Invalid quantity {quantity} for {symbol}")
                return None

            # 4. Calculate stop loss and take profits using ATR
            # NOTE: Will be recalculated from actual fill price after order execution
            def calc_sl_tp(ref_price, direction, atr_val):
                if atr_val and atr_val > 0:
                    if direction == "LONG":
                        sl = ref_price - (atr_val * config.exit.atr_stop_multiplier)
                        t1 = ref_price + (atr_val * config.exit.tp1_atr_multiplier)
                        t2 = ref_price + (atr_val * config.exit.tp2_atr_multiplier)
                    else:
                        sl = ref_price + (atr_val * config.exit.atr_stop_multiplier)
                        t1 = ref_price - (atr_val * config.exit.tp1_atr_multiplier)
                        t2 = ref_price - (atr_val * config.exit.tp2_atr_multiplier)
                else:
                    pct = 0.03
                    if direction == "LONG":
                        sl = ref_price * (1 - pct)
                        t1 = ref_price * (1 + pct * 1.5)
                        t2 = ref_price * (1 + pct * 3)
                    else:
                        sl = ref_price * (1 + pct)
                        t1 = ref_price * (1 - pct * 1.5)
                        t2 = ref_price * (1 - pct * 3)
                return sl, t1, t2
            stop_loss, tp1, tp2 = calc_sl_tp(price, direction, atr_val)

            # 5. Place market order
            side = "BUY" if direction == "LONG" else "SELL"
            order = client.place_market_order(symbol, side, quantity)
            order_id = str(order.get("orderId", ""))

            # 6. Get actual fill price (query order if avgPrice is 0)
            avg_price = float(order.get("avgPrice", 0))
            if avg_price == 0 or avg_price < 0.0000001:
                import time
                time.sleep(0.5)
                try:
                    filled = client.get_order(symbol, order_id)
                    avg_price = float(filled.get("avgPrice", price))
                    if avg_price == 0:
                        avg_price = price
                except Exception:
                    avg_price = price

            # === RECALCULATE SL/TP from actual fill price ===
            # Ensures SL/TP are anchored to real entry, not signal price
            if avg_price > 0 and abs(avg_price - price) / max(price, 0.0000001) > 0.001:
                stop_loss, tp1, tp2 = calc_sl_tp(avg_price, direction, atr_val)
                logger.info(f"SL/TP recalculated from fill ${avg_price:.6f} (signal: ${price:.6f})")
            logger.info(f"Order executed: {symbol} {direction} {period} qty={quantity} @ ${avg_price:.6f}")

            # 7. Save position to database
            pos_id = create_position({
                "symbol": symbol,
                "direction": direction,
                "period": period,
                "entry_price": avg_price,
                "quantity": quantity,
                "leverage": config.margin.leverage,
                "margin_used": margin_amount,
                "stop_loss": stop_loss,
                "take_profit_1": tp1,
                "take_profit_2": tp2,
                "open_time": datetime.utcnow().isoformat(),
                "binance_order_id": order_id,
            })

            # 8. Save trade record
            create_trade({
                "position_id": pos_id,
                "symbol": symbol,
                "side": side,
                "order_type": "MARKET",
                "quantity": quantity,
                "price": avg_price,
                "commission": float(order.get("commission", 0)),
                "binance_order_id": order_id,
                "binance_trade_id": "",
            })

            add_log("success", "EXECUTOR",
                    f"{symbol} {direction} {period} 开仓成功 @ ${avg_price:.6f}, "
                    f"数量: {quantity}, 保证金: ${margin_amount:.0f}, "
                    f"止损: ${stop_loss:.6f}, TP1: ${tp1:.6f}, TP2: ${tp2:.6f}")

            # 9. Execute copy trading if configured
            self._execute_copy_trades(symbol, direction, side, quantity, avg_price,
                                       stop_loss, tp1, tp2, period)

            return pos_id

        except Exception as e:
            err_msg = str(e)
            logger.error(f"Failed to execute signal {symbol} {direction} {period}: {err_msg}")
            add_log("error", "EXECUTOR", f"执行失败 {symbol} {direction} {period}: {err_msg}")
            # Mark signal as failed in DB so frontend shows correct status
            _mark_signal_failed(signal, err_msg)
            return None

    def _execute_copy_trades(self, symbol, direction, side, quantity, avg_price,
                              stop_loss, tp1, tp2, period):
        """Execute copy trades on follower accounts"""
        try:
            from core.copy_trader import copy_trader
            copy_trader.execute_open(
                symbol=symbol, direction=direction, side=side,
                quantity=quantity, entry_price=avg_price,
                stop_loss=stop_loss, tp1=tp1, tp2=tp2, period=period
            )
        except ImportError:
            pass  # Copy trader not yet initialized
        except Exception as e:
            logger.warning(f"Copy trade failed for {symbol}: {e}")
            add_log("warning", "EXECUTOR", f"跟单执行失败 {symbol}: {e}")

    def close_position_partial(self, pos_id: int, close_ratio: float,
                                current_price: float, reason: str) -> bool:
        """Partially close a position"""
        from core.database import get_position_by_id
        pos = get_position_by_id(pos_id)
        if not pos:
            return False

        symbol = pos["symbol"]
        direction = pos["direction"]
        remaining = pos["remaining_ratio"]
        quantity = pos["quantity"]
        close_qty = quantity * remaining * close_ratio

        if close_qty <= 0:
            return False

        try:
            side = "SELL" if direction == "LONG" else "BUY"
            precision = client.get_symbol_precision(symbol)
            step = precision["step_size"]
            if step > 0:
                close_qty = round(close_qty - (close_qty % step), precision["quantity_precision"])

            order = client.place_market_order(symbol, side, close_qty, reduce_only=True)
            avg_price = float(order.get("avgPrice", current_price))
            if avg_price == 0:
                avg_price = current_price

            if direction == "LONG":
                pnl = (avg_price - pos["entry_price"]) * close_qty
            else:
                pnl = (pos["entry_price"] - avg_price) * close_qty

            new_remaining = remaining * (1 - close_ratio)
            new_status = "CLOSED" if new_remaining <= 0.01 else "PARTIAL"

            update_data = {
                "remaining_ratio": new_remaining,
                "status": new_status,
                "realized_pnl": pos["realized_pnl"] + pnl,
            }
            if new_status == "CLOSED":
                update_data["close_price"] = avg_price
                update_data["close_time"] = datetime.utcnow().isoformat()
                update_data["close_reason"] = reason

            update_position(pos_id, update_data)

            create_trade({
                "position_id": pos_id,
                "symbol": symbol,
                "side": side,
                "order_type": "MARKET",
                "quantity": close_qty,
                "price": avg_price,
                "commission": float(order.get("commission", 0)),
                "binance_order_id": str(order.get("orderId", "")),
                "binance_trade_id": "",
            })

            add_log("success", "EXECUTOR",
                    f"{symbol} {direction} {pos['period']} 部分平仓 {close_ratio*100:.0f}% @ ${avg_price:.6f}, "
                    f"盈亏: ${pnl:.2f}, 原因: {reason}")

            # === 撤销该标的所有残留挂单（止盈/止损单）===
            # 平仓后立即撤销，避免挂单数量超过持仓或产生反向开仓
            try:
                open_orders = client.get_open_orders(symbol)
                if open_orders:
                    client.cancel_all_orders(symbol)
                    add_log("info", "EXECUTOR",
                            f"{symbol} 平仓后撤销 {len(open_orders)} 个残留挂单")
                    logger.info(f"{symbol}: Cancelled {len(open_orders)} open orders after close")
            except Exception as cancel_err:
                logger.warning(f"{symbol}: Failed to cancel open orders after close: {cancel_err}")
                add_log("warning", "EXECUTOR", f"{symbol} 撤销挂单失败（不影响平仓）: {cancel_err}")

            # Copy trade close
            try:
                from core.copy_trader import copy_trader
                copy_trader.execute_close(symbol=symbol, direction=direction,
                                           close_ratio=close_ratio, reason=reason)
            except Exception:
                pass

            return True

        except Exception as e:
            logger.error(f"Failed to close position {pos_id}: {e}")
            add_log("error", "EXECUTOR", f"平仓失败 {symbol}: {e}")
            return False

    def close_position_full(self, pos_id: int, current_price: float, reason: str) -> bool:
        """Fully close a position"""
        return self.close_position_partial(pos_id, 1.0, current_price, reason)
