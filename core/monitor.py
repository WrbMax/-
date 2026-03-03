"""
DTRS Trading Engine - Position Monitor
Monitors open positions and executes exit strategies:
1. ATR-based stop loss (initial)
2. EMA20 trailing stop (every 15min)
3. MA20 crossover exit (2 consecutive closes below/above MA20)
4. Tiered take profit (TP1: 40%, TP2: 30%)
5. TP1 hit -> move SL to TP1 price (lock profit)
"""

import logging
import numpy as np
from typing import List, Dict
from datetime import datetime, timedelta

from config import config
from core.binance_client import client
from core.indicators import parse_klines, ema, calculate_all_indicators
from core.database import (
    get_open_positions, update_position, add_log, get_position_by_id
)
from core.executor import Executor

logger = logging.getLogger("dtrs.monitor")


class PositionMonitor:
    """Monitors and manages open positions"""

    def __init__(self):
        self.executor = Executor()

    def check_all_positions(self):
        """Run all exit checks on open positions"""
        positions = get_open_positions()
        if not positions:
            return

        for pos in positions:
            try:
                self._check_position(pos)
            except Exception as e:
                logger.error(f"Error checking position {pos['id']} {pos['symbol']}: {e}")

    def _check_position(self, pos: Dict):
        """Check a single position against all exit conditions"""
        symbol = pos["symbol"]
        direction = pos["direction"]
        period = pos["period"]
        entry_price = pos["entry_price"]

        # Get current price
        try:
            mark_data = client.get_mark_price(symbol)
            current_price = float(mark_data["markPrice"])
        except Exception as e:
            logger.error(f"Failed to get price for {symbol}: {e}")
            return

        # 0. Check ATR stop loss first
        if self._check_stop_loss(pos, current_price):
            return  # Position closed, no further checks
        # 1. Check take profit levels
        self._check_take_profits(pos, current_price)

        # 2. Check EMA20 trailing stop
        self._check_ema_trailing_stop(pos, current_price)

        # 3. Check MA20 crossover exit
        self._check_ma20_crossover(pos, current_price)


    def _check_stop_loss(self, pos: Dict, current_price: float):
        """Check ATR-based stop loss"""
        direction = pos["direction"]
        stop_loss = pos.get("stop_loss")
        if not stop_loss or stop_loss <= 0:
            return

        sl_hit = (
            (direction == "LONG" and current_price <= stop_loss) or
            (direction == "SHORT" and current_price >= stop_loss)
        )

        if sl_hit:
            logger.info(f"Stop loss hit for {pos['symbol']} {pos['period']} @ ${current_price:.6f} (SL: ${stop_loss:.6f})")
            self.executor.close_position_full(
                pos["id"], current_price,
                f"ATR止损 @ ${current_price:.6f} (止损价: ${stop_loss:.6f})"
            )
            add_log("warning", "MONITOR",
                    f"{pos['symbol']} {pos['direction']} {pos['period']}: "
                    f"ATR止损触发, 价格 ${current_price:.6f} {'跌破' if direction == 'LONG' else '突破'} "
                    f"止损价 ${stop_loss:.6f}")
            return True
        return False

    def _check_take_profits(self, pos: Dict, current_price: float):
        """Check TP1 and TP2 levels"""
        direction = pos["direction"]
        tp1 = pos["take_profit_1"]
        tp2 = pos["take_profit_2"]
        tp1_hit = bool(pos["tp1_hit"])
        tp2_hit = bool(pos["tp2_hit"])

        # TP1 Check
        if not tp1_hit:
            tp1_reached = (
                (direction == "LONG" and current_price >= tp1) or
                (direction == "SHORT" and current_price <= tp1)
            )
            if tp1_reached:
                logger.info(f"TP1 reached for {pos['symbol']} {pos['period']}")
                # Close TP1 portion
                success = self.executor.close_position_partial(
                    pos["id"], config.exit.tp1_close_ratio, current_price,
                    f"TP1达成 @ ${current_price:.2f}"
                )
                if success:
                    # Move stop loss to TP1 price (trailing take-profit: lock in profit)
                    # This is better than breakeven: guarantees at least TP1 profit on remaining position
                    new_sl = tp1  # TP1 price = new stop loss
                    update_position(pos["id"], {
                        "tp1_hit": 1,
                        "stop_loss": new_sl,
                    })
                    add_log("info", "MONITOR",
                            f"{pos['symbol']} {pos['direction']} {pos['period']}: "
                            f"TP1 达成 (${tp1:.2f}), 已平仓 {config.exit.tp1_close_ratio*100:.0f}%, "
                            f"止损上移至 TP1 价格 ${new_sl:.6f} (锁定利润)")
                return

        # TP2 Check (also handle price skipping TP1 and going directly to TP2)
        if not tp2_hit:
            tp2_reached = (
                (direction == "LONG" and current_price >= tp2) or
                (direction == "SHORT" and current_price <= tp2)
            )
            if tp2_reached:
                logger.info(f"TP2 reached for {pos['symbol']} {pos['period']}")
                # If TP1 was skipped (price jumped over TP1), first execute TP1 close
                if not tp1_hit:
                    logger.info(f"Price skipped TP1, executing TP1 close first for {pos['symbol']}")
                    success1 = self.executor.close_position_partial(
                        pos["id"], config.exit.tp1_close_ratio, current_price,
                        f"TP1跳过补平 @ ${current_price:.2f}"
                    )
                    if success1:
                        # Also move SL to TP1 price when TP1 is executed as part of skip-to-TP2
                        update_position(pos["id"], {
                            "tp1_hit": 1,
                            "stop_loss": tp1,  # Lock in profit at TP1 level
                        })
                        tp1_hit = True
                # Now execute TP2 close on remaining position
                remaining_after_tp1 = 1 - config.exit.tp1_close_ratio if tp1_hit else 1.0
                tp2_adjusted_ratio = config.exit.tp2_close_ratio / remaining_after_tp1 if remaining_after_tp1 > 0 else 1.0
                tp2_adjusted_ratio = min(tp2_adjusted_ratio, 1.0)  # Cap at 100%
                success = self.executor.close_position_partial(
                    pos["id"], tp2_adjusted_ratio,
                    current_price, f"TP2达成 @ ${current_price:.2f}"
                )
                if success:
                    update_position(pos["id"], {"tp2_hit": 1})
                    add_log("info", "MONITOR",
                            f"{pos['symbol']} {pos['direction']} {pos['period']}: "
                            f"TP2 达成 (${tp2:.2f}), 已平仓 {config.exit.tp2_close_ratio*100:.0f}%")

    def _check_ema_trailing_stop(self, pos: Dict, current_price: float):
        """Check EMA20 trailing stop - close if price crosses EMA20 against position"""
        symbol = pos["symbol"]
        direction = pos["direction"]
        period = pos["period"]

        interval_map = {"1h": "1h", "4h": "4h", "1d": "1d"}
        interval = interval_map.get(period, "1h")

        try:
            raw_klines = client.get_klines(symbol, interval, limit=30)
            klines = parse_klines(raw_klines)
            ema20 = ema(klines["close"], config.exit.ema_period)
            current_ema = ema20[-1]

            if np.isnan(current_ema):
                return

            # Check if price has crossed EMA20 against position direction
            should_close = False
            if direction == "LONG" and current_price < current_ema:
                should_close = True
            elif direction == "SHORT" and current_price > current_ema:
                should_close = True

            if should_close:
                # Confirm with 2 consecutive closes below/above EMA
                prev_close = klines["close"][-2]
                prev_ema = ema20[-2]
                if not np.isnan(prev_ema):
                    confirmed = (
                        (direction == "LONG" and prev_close < prev_ema) or
                        (direction == "SHORT" and prev_close > prev_ema)
                    )
                    if confirmed:
                        self.executor.close_position_full(
                            pos["id"], current_price,
                            f"EMA20趋势止损 @ ${current_price:.2f} (EMA20: ${current_ema:.2f})"
                        )
                        add_log("info", "MONITOR",
                                f"{symbol} {direction} {period}: EMA20趋势止损, "
                                f"价格 ${current_price:.2f} {'跌破' if direction == 'LONG' else '突破'} "
                                f"EMA20 ${current_ema:.2f}")

        except Exception as e:
            logger.error(f"EMA check failed for {symbol}: {e}")

    def _check_ma20_crossover(self, pos: Dict, current_price: float):
        """Check MA20 crossover exit - close if price crosses MA20 against position direction.
        Requires 2 consecutive candle closes below/above MA20 to confirm (avoid false signals).
        """
        symbol = pos["symbol"]
        direction = pos["direction"]
        period = pos["period"]

        interval_map = {"1h": "1h", "4h": "4h", "1d": "1d"}
        interval = interval_map.get(period, "1h")

        try:
            raw_klines = client.get_klines(symbol, interval, limit=30)
            klines = parse_klines(raw_klines)
            indicators = calculate_all_indicators(klines)
            ma20 = indicators["ma20"]

            # Use confirmed candles: idx=-1 (latest closed) and idx=-2 (previous closed)
            current_ma = ma20[-1]
            prev_ma = ma20[-2]
            current_close = klines["close"][-1]
            prev_close = klines["close"][-2]

            if np.isnan(current_ma) or np.isnan(prev_ma):
                return

            # Check 2 consecutive closes crossing MA20 against position direction
            if direction == "LONG":
                crossed = current_close < current_ma and prev_close < prev_ma
            else:  # SHORT
                crossed = current_close > current_ma and prev_close > prev_ma

            if crossed:
                self.executor.close_position_full(
                    pos["id"], current_price,
                    f"MA20穿线止盈 @ ${current_price:.6f} (MA20: ${current_ma:.6f})"
                )
                add_log("info", "MONITOR",
                        f"{symbol} {direction} {period}: MA20穿线止盈, "
                        f"价格 ${current_price:.6f} {'跌破' if direction == 'LONG' else '突破'} "
                        f"MA20 ${current_ma:.6f} (连续2根K线确认)")

        except Exception as e:
            logger.error(f"MA20 crossover check failed for {symbol}: {e}")
