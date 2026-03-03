"""
DTRS Trading Engine - Technical Indicators Calculator
Uses the 'ta' library (https://github.com/bukosabino/ta) for reliable,
battle-tested indicator calculations from Binance OHLCV kline data.

Binance provides raw OHLCV data; indicators are computed here using pandas-based ta library.
"""

import numpy as np
import pandas as pd
import ta
from typing import List, Dict


def parse_klines(raw_klines: List) -> Dict[str, np.ndarray]:
    """Parse Binance kline data into numpy arrays"""
    data = np.array(raw_klines, dtype=object)
    return {
        "open_time": data[:, 0].astype(np.int64),
        "open": data[:, 1].astype(np.float64),
        "high": data[:, 2].astype(np.float64),
        "low": data[:, 3].astype(np.float64),
        "close": data[:, 4].astype(np.float64),
        "volume": data[:, 5].astype(np.float64),
        "close_time": data[:, 6].astype(np.int64),
    }


def calculate_all_indicators(klines: Dict[str, np.ndarray], cfg=None) -> Dict[str, np.ndarray]:
    """
    Calculate all indicators needed for signal generation using the 'ta' library.
    
    Binance API returns raw OHLCV kline data. This function computes:
    - MA (Simple Moving Average)
    - EMA (Exponential Moving Average)
    - MACD (DIF, DEA/Signal, Histogram)
    - RSI (Relative Strength Index)
    - ATR (Average True Range)
    - Volume SMA
    """
    from config import config
    if cfg is None:
        cfg = config.entry

    close = pd.Series(klines["close"])
    high = pd.Series(klines["high"])
    low = pd.Series(klines["low"])
    volume = pd.Series(klines["volume"])

    # --- Moving Averages ---
    ma20 = ta.trend.SMAIndicator(close, window=cfg.ma_period).sma_indicator().values
    ema20 = ta.trend.EMAIndicator(close, window=cfg.ma_period).ema_indicator().values

    # --- MACD ---
    macd_obj = ta.trend.MACD(
        close,
        window_fast=cfg.macd_fast,
        window_slow=cfg.macd_slow,
        window_sign=cfg.macd_signal,
    )
    dif = macd_obj.macd().values          # DIF line (MACD line)
    dea = macd_obj.macd_signal().values   # DEA line (Signal line)
    hist = macd_obj.macd_diff().values    # Histogram (DIF - DEA)

    # --- RSI ---
    rsi_values = ta.momentum.RSIIndicator(close, window=cfg.rsi_period).rsi().values

    # --- ATR ---
    atr_values = ta.volatility.AverageTrueRange(
        high, low, close, window=cfg.atr_period
    ).average_true_range().values

    # --- Volume SMA ---
    vol_avg = ta.trend.SMAIndicator(volume, window=cfg.volume_lookback).sma_indicator().values

    return {
        "open_time": klines["open_time"],
        "close": klines["close"],
        "high": klines["high"],
        "low": klines["low"],
        "volume": klines["volume"],
        "ma20": ma20,
        "ema20": ema20,
        "dif": dif,
        "dea": dea,
        "macd_hist": hist,
        "rsi": rsi_values,
        "atr": atr_values,
        "vol_avg": vol_avg,
    }


def ema(data: np.ndarray, period: int) -> np.ndarray:
    """
    Compatibility wrapper: compute EMA using ta library.
    Returns numpy array of same length as input, with NaN for initial periods.
    """
    series = pd.Series(data)
    result = ta.trend.EMAIndicator(series, window=period).ema_indicator().values
    return result
