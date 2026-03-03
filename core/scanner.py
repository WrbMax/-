"""
DTRS Trading Engine - Multi-Timeframe Scanner
Scans assets across 1h, 4h, 1d timeframes and generates entry signals.
"""

import logging
import numpy as np
from typing import List, Dict, Optional, Tuple
from datetime import datetime

from config import config
from core.binance_client import client
from core.indicators import parse_klines, calculate_all_indicators
from core.database import (
    create_signal, get_open_positions, get_positions_by_symbol_period,
    add_log, get_state, set_state
)

logger = logging.getLogger("dtrs.scanner")


class Scanner:
    """Multi-timeframe asset scanner"""

    def __init__(self):
        self.scan_pool: List[str] = []
        self.blacklist: List[str] = list(config.scan.exclude_list)

    def refresh_scan_pool(self):
        """Refresh the scan pool based on 24h volume ranking"""
        try:
            tickers = client.get_ticker_24h()
            # Filter USDT perpetual pairs
            usdt_pairs = [
                t for t in tickers
                if t["symbol"].endswith("USDT")
                and t["symbol"] not in self.blacklist
            ]

            # Sort by quote volume (USDT volume)
            usdt_pairs.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)

            # Take top N
            self.scan_pool = [t["symbol"] for t in usdt_pairs[:config.scan.scan_scope]]

            # Auto-blacklist: extreme volatility
            if config.scan.auto_blacklist_enabled:
                for t in usdt_pairs:
                    change = abs(float(t.get("priceChangePercent", 0)))
                    if change > config.scan.auto_blacklist_volatility_threshold:
                        symbol = t["symbol"]
                        if symbol in self.scan_pool:
                            self.scan_pool.remove(symbol)
                            logger.warning(f"Auto-blacklisted {symbol}: 24h change {change:.1f}%")
                            add_log("warning", "SCANNER", f"自动黑名单: {symbol} 24h波动 {change:.1f}%")

            set_state("scan_pool_size", str(len(self.scan_pool)))
            set_state("scan_pool_updated", datetime.utcnow().isoformat())
            add_log("info", "SCANNER", f"扫描池更新完成: {len(self.scan_pool)} 个标的")
            logger.info(f"Scan pool refreshed: {len(self.scan_pool)} symbols")

        except Exception as e:
            logger.error(f"Failed to refresh scan pool: {e}")
            add_log("error", "SCANNER", f"扫描池更新失败: {e}")

    def scan_timeframe(self, period: str) -> List[Dict]:
        """Scan all symbols for a specific timeframe and generate signals"""
        signals = []
        interval_map = {"1h": "1h", "4h": "4h", "1d": "1d"}
        interval = interval_map.get(period, period)

        logger.info(f"Starting {period} scan for {len(self.scan_pool)} symbols...")
        add_log("info", "SCANNER", f"开始 {period} 周期扫描... ({len(self.scan_pool)} 个标的)")

        executed = 0
        filtered_count = 0
        conflict_count = 0

        for symbol in self.scan_pool:
            try:
                signal = self._analyze_symbol(symbol, interval, period)
                if signal:
                    signals.append(signal)
                    if signal["status"] == "executed":
                        executed += 1
                    elif signal["status"] == "filtered":
                        filtered_count += 1
                    elif signal["status"] == "conflict":
                        conflict_count += 1
            except Exception as e:
                logger.error(f"Error scanning {symbol} {period}: {e}")

        summary = (f"{period} 扫描完成: 扫描 {len(self.scan_pool)} 个标的, "
                   f"发现 {len(signals)} 个信号, 执行 {executed} 个, "
                   f"过滤 {filtered_count} 个, 冲突 {conflict_count} 个")
        add_log("info", "SCANNER", summary)
        logger.info(summary)

        return signals

    def _analyze_symbol(self, symbol: str, interval: str, period: str) -> Optional[Dict]:
        """Analyze a single symbol and return signal if conditions met"""
        # Fetch klines (need enough data for indicators)
        raw_klines = client.get_klines(symbol, interval, limit=100)
        if len(raw_klines) < 50:
            return None

        klines = parse_klines(raw_klines)

        # === VALIDITY CHECK: Filter stale/invalid klines ===
        # If last 5 candles all have identical close prices, data is invalid
        closes_check = klines["close"][-5:]
        if len(set(round(float(c), 8) for c in closes_check)) <= 1:
            logger.warning(f"{symbol} {period}: Invalid kline data (all same price), skipping")
            return None

        # === VOLUME CHECK: Skip if recent volume is near zero ===
        volumes_check = klines["volume"][-5:]
        if sum(float(v) for v in volumes_check) < 1.0:
            logger.warning(f"{symbol} {period}: Near-zero volume, skipping")
            return None

        indicators = calculate_all_indicators(klines)

        # Get latest values (second to last for confirmed candle)
        idx = -2  # Use confirmed (closed) candle
        close = indicators["close"][idx]
        close_prev = indicators["close"][idx - 1]   # Previous candle close
        high_prev = indicators["high"][idx - 1]     # Previous candle high (for full-body MA20 check)
        low_prev  = indicators["low"][idx - 1]      # Previous candle low (for full-body MA20 check)
        ma20 = indicators["ma20"][idx]
        ma20_prev = indicators["ma20"][idx - 1]     # Previous candle MA20
        # 上上根K线数据（用于验证MA20穿越时效性）
        high_prev2 = indicators["high"][idx - 2]    # 上上根K线最高价
        low_prev2  = indicators["low"][idx - 2]     # 上上根K线最低价
        close_prev2 = indicators["close"][idx - 2]  # 上上根K线收盘价
        ma20_prev2 = indicators["ma20"][idx - 2]    # 上上根K线MA20
        ema20 = indicators["ema20"][idx]
        # MACD: DIF, DEA, Histogram (current + previous)
        dif = indicators["dif"][idx]
        dea = indicators["dea"][idx]
        macd_hist = indicators["macd_hist"][idx]       # Current histogram bar
        macd_hist_prev = indicators["macd_hist"][idx - 1]  # Previous histogram bar
        rsi_val = indicators["rsi"][idx]
        atr_val = indicators["atr"][idx]
        volume = indicators["volume"][idx]
        vol_avg = indicators["vol_avg"][idx]

        # === CANDLE FRESHNESS CHECK: Reject signals from stale candles ===
        # The confirmed candle (idx=-2) must be the MOST RECENTLY closed candle.
        # Rule: candle_close_time = candle_open_time + period_seconds
        # The candle is fresh ONLY IF: (now - candle_close_time) <= 1 period
        # i.e. the candle closed within the last 1 period duration.
        # Example (1h): candle opened at 13:00, closed at 14:00.
        #   Scan at 14:00:05 -> age_since_close = 5s -> FRESH (ok)
        #   Scan at 15:00:05 -> age_since_close = 3605s > 3600s -> STALE (reject)
        try:
            from datetime import datetime, timezone as tz
            candle_open_ms = int(indicators["open_time"][idx])
            period_seconds_map = {"1h": 3600, "4h": 14400, "1d": 86400}
            period_seconds = period_seconds_map.get(period, 3600)
            # Calculate candle close time (open_time + period)
            candle_close_ms = candle_open_ms + (period_seconds * 1000)
            candle_close_dt = datetime.fromtimestamp(candle_close_ms / 1000, tz=tz.utc)
            now_utc = datetime.now(tz=tz.utc)
            # How many seconds have passed since this candle CLOSED?
            age_since_close = (now_utc - candle_close_dt).total_seconds()
            # Allow up to 1 full period after close (generous buffer for execution)
            # If more than 1 period has passed since close, the signal is stale
            max_age_since_close = period_seconds  # exactly 1 period
            if age_since_close > max_age_since_close:
                candle_open_dt = datetime.fromtimestamp(candle_open_ms / 1000, tz=tz.utc)
                reason = (f"K线信号已过期: 信号K线收盘于 {candle_close_dt.strftime('%H:%M')} UTC, "
                          f"距收盘已过 {age_since_close/3600:.1f}h, 超过 {max_age_since_close/3600:.1f}h 限制 (仅允许最新一根K线)")
                logger.warning(f"{symbol} {period}: STALE signal rejected (closed {age_since_close:.0f}s ago > max {max_age_since_close:.0f}s)")
                add_log("warning", "SCANNER", f"{symbol} {period}: {reason}")
                return None  # Silently drop stale signals - do NOT create DB record to avoid clutter
        except Exception as e:
            logger.warning(f"{symbol} {period}: Candle freshness check failed: {e}, skipping signal")
            return None  # Cannot verify candle freshness, skip to avoid stale signals

        # 提取K线开盘时间（用于信号去重）
        candle_open_time_str = None
        try:
            candle_open_time_str = str(int(indicators["open_time"][idx]))
        except Exception:
            pass

        # Check for NaN (including DIF/DEA needed for MACD crossover check)
        if any(np.isnan(v) for v in [ma20, dif, dea, macd_hist, macd_hist_prev, rsi_val, atr_val, vol_avg] if v is not None):
            return None

        # Volume ratio
        volume_ratio = volume / vol_avg if vol_avg > 0 else 0

        # ============================================================
        # ---- Entry Conditions (ALL THREE must be satisfied) ----
        # ============================================================
        # Condition 1: Price Position (MA20 Crossover)
        # Condition 2: Momentum (MACD DIF/DEA + Histogram)
        # Condition 3: Volume Surge
        # ALL THREE conditions must be TRUE simultaneously to open a position.
        # ============================================================
        direction = None

        # === CONDITION 1: Price Position (MA20 Full-Body Confirmation + 穿越时效性) ===
        # 核心逻辑：上一根K线是「第一根」完全穿越MA20的K线
        #
        # 做多新信号条件：
        #   1. 上上根K线整根在MA20下方（high_prev2 <= ma20_prev2）—— 还没穿越，不能是穿越中
        #   2. 上一根K线整根站上MA20（low_prev > ma20_prev）—— 第一根穿越
        #   3. 当前K线收盘 > 当前MA20 —— 确认延续
        #
        # 做空新信号条件：
        #   1. 上上根K线整根在MA20上方（low_prev2 >= ma20_prev2）—— 还没穿越，不能是穿越中
        #   2. 上一根K线整根站下MA20（high_prev < ma20_prev）—— 第一根穿越
        #   3. 当前K线收盘 < 当前MA20 —— 确认延续
        #
        # 关键：上上根必须是「整根」在另一侧，如果上上根是穿越中（影线碰到MA20）也不算新信号
        # 因为那说明已经在MA20附近震荡多根了，不是新鲜的第一次穿越
        prev2_fully_below_ma20 = (high_prev2 <= ma20_prev2)   # 上上根整根在MA20下方（做多前提）
        prev2_fully_above_ma20 = (low_prev2  >= ma20_prev2)   # 上上根整根在MA20上方（做空前提）
        long_cond_price  = prev2_fully_below_ma20 and (low_prev  > ma20_prev) and (close > ma20)
        short_cond_price = prev2_fully_above_ma20 and (high_prev < ma20_prev) and (close < ma20)

        # === CONDITION 2: Momentum (MACD) ===
        # LONG requires ALL of:
        #   a) DIF > DEA (golden cross state / above signal line)
        #   b) Histogram > 0 (green bar)
        #   c) Current histogram > previous histogram (green bar growing = 1 increase)
        #   d) DIF and DEA are near the zero axis: |DIF/close| < 3% AND |DEA/close| < 3%
        # SHORT requires ALL of:
        #   a) DIF < DEA (death cross state / below signal line)
        #   b) Histogram < 0 (red bar)
        #   c) Current histogram < previous histogram (red bar growing in magnitude)
        #   d) DIF and DEA are near the zero axis: |DIF/close| < 3% AND |DEA/close| < 3%
        # Zero-axis proximity threshold: 3% of current close price (relative, works for all coins)
        zero_axis_threshold = 0.03  # 3%
        near_zero_axis = (
            abs(dif / close) < zero_axis_threshold and
            abs(dea / close) < zero_axis_threshold
        ) if close > 0 else False
        long_cond_macd = (
            (dif > dea) and              # a) DIF above DEA (golden cross state)
            (macd_hist > 0) and          # b) Histogram is positive (green bar)
            (macd_hist > macd_hist_prev) and  # c) Green bar is growing (1 increase)
            near_zero_axis               # d) Cross occurs near zero axis
        )
        short_cond_macd = (
            (dif < dea) and              # a) DIF below DEA (death cross state)
            (macd_hist < 0) and          # b) Histogram is negative (red bar)
            (macd_hist < macd_hist_prev) and  # c) Red bar is growing in magnitude
            near_zero_axis               # d) Cross occurs near zero axis
        )

        # === CONDITION 3: Volume Surge ===
        # Current candle volume > 10-period average volume * threshold (default 1.5x)
        long_cond_vol = volume_ratio >= config.entry.volume_threshold
        short_cond_vol = volume_ratio >= config.entry.volume_threshold

        # === FINAL: ALL THREE conditions must be TRUE ===
        if long_cond_price and long_cond_macd and long_cond_vol:
            direction = "LONG"
        elif short_cond_price and short_cond_macd and short_cond_vol:
            direction = "SHORT"
        else:
            return None  # No signal - not all 3 conditions met

        # ---- Filters ----
        reason = None

        # RSI filter
        if config.entry.rsi_enabled:
            if direction == "LONG" and (rsi_val > config.entry.rsi_long_max):
                reason = f"RSI超买 ({rsi_val:.1f} > {config.entry.rsi_long_max}), 过滤多头信号"
            elif direction == "LONG" and (rsi_val < config.entry.rsi_long_min):
                reason = f"RSI过低 ({rsi_val:.1f} < {config.entry.rsi_long_min}), 过滤多头信号"
            elif direction == "SHORT" and (rsi_val < config.entry.rsi_short_min):
                reason = f"RSI超卖 ({rsi_val:.1f} < {config.entry.rsi_short_min}), 过滤空头信号"
            elif direction == "SHORT" and (rsi_val > config.entry.rsi_short_max):
                reason = f"RSI过高 ({rsi_val:.1f} > {config.entry.rsi_short_max}), 过滤空头信号"

            if reason:
                signal_data = self._build_signal(symbol, period, direction, "filtered",
                                                  close, ma20, macd_hist, volume_ratio, atr_val, rsi_val, reason,
                                                  candle_open_time=candle_open_time_str)
                create_signal(signal_data)
                return signal_data

        # ATR volatility filter
        if config.entry.atr_filter_enabled:
            atr_100 = np.nanmean(indicators["atr"][-100:])
            if atr_val < atr_100 * config.entry.atr_min_ratio:
                reason = f"波动率不足 (ATR {atr_val:.4f} < 均值{config.entry.atr_min_ratio}x)"
                signal_data = self._build_signal(symbol, period, direction, "filtered",
                                                  close, ma20, macd_hist, volume_ratio, atr_val, rsi_val, reason,
                                                  candle_open_time=candle_open_time_str)
                create_signal(signal_data)
                return signal_data

        # ---- Conflict Check (Higher timeframe priority) ----
        status = "executed"
        conflict_reason = self._check_conflicts(symbol, period, direction)
        if conflict_reason:
            status = "conflict"
            reason = conflict_reason

        # ---- Circuit Breaker Check ----
        if status == "executed":
            try:
                margin_ratio = client.get_margin_ratio()
                if margin_ratio >= config.margin.margin_circuit_break_threshold * 100:
                    status = "circuit_break"
                    reason = f"保证金率 {margin_ratio:.1f}% > {config.margin.margin_circuit_break_threshold * 100}% 熔断阈值"
                elif margin_ratio >= config.margin.margin_warning_threshold * 100 and period == "1h":
                    status = "circuit_break"
                    reason = f"保证金率 {margin_ratio:.1f}% > {config.margin.margin_warning_threshold * 100}% 预警, 禁止1h新开仓"
            except Exception as e:
                logger.error(f"Failed to check margin ratio: {e}")

        # ---- Max Position Check ----
        if status == "executed":
            open_positions = get_open_positions()
            if len(open_positions) >= config.margin.max_open_positions:
                status = "circuit_break"
                reason = f"持仓数 {len(open_positions)} >= 最大限制 {config.margin.max_open_positions}"

        # ---- Duplicate Check ----
        if status == "executed":
            existing = get_positions_by_symbol_period(symbol, period)
            if existing:
                status = "filtered"
                reason = f"{symbol} {period} 已有持仓, 跳过"

        # === STALENESS CHECK: Verify current price hasn't moved too far from signal candle ===
        # 放宽偏离阈值：1h=3%, 4h=5%, 1d=8%（原来1%/2%/3%过于严格导致开单延迟）
        # 扫描在K线收盘后数秒内执行，正常波动不应超过这个范围
        if status == "executed":
            try:
                current_ticker = client.get_ticker_price(symbol)
                current_price = float(current_ticker.get("price", close))
                price_deviation = abs(current_price - close) / close * 100
                deviation_map = {
                    "1h": getattr(config.entry, "max_price_deviation_pct_1h", 3.0),
                    "4h": getattr(config.entry, "max_price_deviation_pct_4h", 5.0),
                    "1d": getattr(config.entry, "max_price_deviation_pct_1d", 8.0),
                }
                max_deviation = deviation_map.get(period, 3.0)
                if price_deviation > max_deviation:
                    status = "filtered"
                    reason = f"价格偏离信号点 {price_deviation:.2f}% > {max_deviation}%, 入场时机已过"
                    logger.warning(f"{symbol} {period}: Price deviation {price_deviation:.2f}% > {max_deviation}%, signal stale - skipping")
                    add_log("warning", "SCANNER", f"{symbol} {period}: 价格偏离 {price_deviation:.2f}% > {max_deviation}%, 信号已过期, 跳过开仓")
            except Exception as e:
                logger.warning(f"Failed to check current price for {symbol}: {e}")

        signal_data = self._build_signal(symbol, period, direction, status,
                                          close, ma20, macd_hist, volume_ratio, atr_val, rsi_val, reason,
                                          candle_open_time=candle_open_time_str)
        create_signal(signal_data)

        if status == "executed":
            add_log("info", "SCANNER", f"信号生成: {symbol} {direction} {period} @ ${close}")
        elif status == "conflict":
            add_log("warning", "CONFLICT", f"{symbol} {period} {direction} 被覆盖: {reason}")
        elif status == "circuit_break":
            add_log("error", "CIRCUIT_BREAKER", f"{symbol} {period} {direction} 被拒绝: {reason}")

        return signal_data

    def _check_conflicts(self, symbol: str, period: str, direction: str) -> Optional[str]:
        """Check for conflicts with higher timeframe positions"""
        priority = {"1d": 3, "4h": 2, "1h": 1}
        current_priority = priority.get(period, 0)

        # Check all higher timeframe positions for this symbol
        for tf in ["1d", "4h", "1h"]:
            if priority.get(tf, 0) > current_priority:
                positions = get_positions_by_symbol_period(symbol, tf)
                for pos in positions:
                    if pos["direction"] != direction:
                        return f"{tf} 周期持有 {pos['direction']} 方向, 大周期优先"
        return None

    def _build_signal(self, symbol, period, direction, status, price, ma20,
                       macd_hist, volume_ratio, atr_val, rsi_val, reason,
                       candle_open_time=None) -> Dict:
        return {
            "symbol": symbol,
            "period": period,
            "direction": direction,
            "status": status,
            "price": price,
            "ma20": ma20,
            "macd_hist": macd_hist,
            "volume_ratio": volume_ratio,
            "atr": atr_val,
            "rsi": rsi_val,
            "reason": reason,
            "candle_open_time": candle_open_time,
        }
