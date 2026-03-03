"""
DTRS Trading Engine - Configuration Settings
All strategy parameters are centralized here for easy management.
Supports persistent save/load via JSON config file.
"""
import os
import json
import logging
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger("dtrs.config")

CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "config.json")


@dataclass
class BinanceConfig:
    """Binance API configuration"""
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = True
    testnet_base_url: str = "https://testnet.binancefuture.com"
    testnet_ws_url: str = "wss://stream.binancefuture.com"
    prod_base_url: str = "https://fapi.binance.com"
    prod_ws_url: str = "wss://fstream.binance.com"

    @property
    def base_url(self) -> str:
        return self.testnet_base_url if self.testnet else self.prod_base_url

    @property
    def ws_url(self) -> str:
        return self.testnet_ws_url if self.testnet else self.prod_ws_url


@dataclass
class ScanConfig:
    """Asset scanning configuration"""
    scan_scope: int = 100
    exclude_list: List[str] = field(default_factory=lambda: ["LUNAUSDT", "USTCUSDT"])
    auto_blacklist_enabled: bool = True
    auto_blacklist_volatility_threshold: float = 30.0
    auto_blacklist_volume_decay_threshold: float = 50.0
    refresh_interval_hours: int = 24


@dataclass
class EntryConfig:
    """Entry signal configuration"""
    ma_period: int = 20
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    volume_threshold: float = 1.5
    volume_lookback: int = 10
    rsi_enabled: bool = True
    rsi_period: int = 14
    rsi_long_min: float = 40.0
    rsi_long_max: float = 80.0
    rsi_short_min: float = 20.0
    rsi_short_max: float = 60.0
    atr_filter_enabled: bool = True
    atr_period: int = 14
    atr_min_ratio: float = 0.7
    # Signal freshness: max price deviation from signal candle close
    # If current price moved more than this % from signal price, skip entry
    max_price_deviation_pct_1h: float = 1.0
    max_price_deviation_pct_4h: float = 2.0
    max_price_deviation_pct_1d: float = 3.0


@dataclass
class MarginConfig:
    """Position sizing and margin configuration"""
    leverage: int = 20
    margin_1h: float = 0.02
    margin_4h: float = 0.05
    margin_1d: float = 0.15
    max_open_positions: int = 12
    margin_warning_threshold: float = 0.45
    margin_circuit_break_threshold: float = 0.60


@dataclass
class ExitConfig:
    """Exit strategy configuration"""
    atr_stop_multiplier: float = 1.5
    ema_period: int = 20
    ema_check_interval_minutes: int = 15
    tp1_atr_multiplier: float = 1.5
    tp1_close_ratio: float = 0.40
    tp2_atr_multiplier: float = 3.0
    tp2_close_ratio: float = 0.30
    max_hold_1h: int = 24
    max_hold_4h: int = 120
    max_hold_1d: int = 480


@dataclass
class SystemConfig:
    """System-level configuration"""
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8888
    db_path: str = "data/dtrs.db"
    heartbeat_interval: int = 60
    scan_offset_seconds: int = 5  # seconds to wait AFTER candle close before scanning


@dataclass
class DTRSConfig:
    """Master configuration"""
    binance: BinanceConfig = field(default_factory=BinanceConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    margin: MarginConfig = field(default_factory=MarginConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)
    system: SystemConfig = field(default_factory=SystemConfig)

    def save(self):
        """Persist current configuration to JSON file"""
        try:
            os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
            data = {
                "binance": {
                    "api_key": self.binance.api_key,
                    "api_secret": self.binance.api_secret,
                    "testnet": self.binance.testnet,
                },
                "scan": {
                    "scan_scope": self.scan.scan_scope,
                    "exclude_list": self.scan.exclude_list,
                    "auto_blacklist_enabled": self.scan.auto_blacklist_enabled,
                },
                "entry": {
                    "volume_threshold": self.entry.volume_threshold,
                    "rsi_enabled": self.entry.rsi_enabled,
                    "atr_filter_enabled": self.entry.atr_filter_enabled,
                },
                "margin": {
                    "leverage": self.margin.leverage,
                    "margin_1h": self.margin.margin_1h,
                    "margin_4h": self.margin.margin_4h,
                    "margin_1d": self.margin.margin_1d,
                    "max_open_positions": self.margin.max_open_positions,
                    "margin_warning_threshold": self.margin.margin_warning_threshold,
                    "margin_circuit_break_threshold": self.margin.margin_circuit_break_threshold,
                },
                "exit": {
                    "atr_stop_multiplier": self.exit.atr_stop_multiplier,
                    "tp1_close_ratio": self.exit.tp1_close_ratio,
                    "tp2_close_ratio": self.exit.tp2_close_ratio,
                    "ema_check_interval_minutes": self.exit.ema_check_interval_minutes,
                },
            }
            with open(CONFIG_FILE, "w") as f:
                json.dump(data, f, indent=2)
            logger.info(f"Configuration saved to {CONFIG_FILE}")
        except Exception as e:
            logger.error(f"Failed to save configuration: {e}")

    def load(self):
        """Load configuration from JSON file if it exists, fallback to env vars"""
        # First check env vars for backward compatibility
        env_key = os.getenv("BINANCE_API_KEY", "")
        env_secret = os.getenv("BINANCE_API_SECRET", "")
        if env_key:
            self.binance.api_key = env_key
        if env_secret:
            self.binance.api_secret = env_secret
        env_testnet = os.getenv("BINANCE_TESTNET", "")
        if env_testnet:
            self.binance.testnet = env_testnet.lower() == "true"

        # Then override with saved config file if exists
        if not os.path.exists(CONFIG_FILE):
            logger.info("No saved config file found, using defaults/env vars")
            return

        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)

            # Binance
            b = data.get("binance", {})
            if b.get("api_key"):
                self.binance.api_key = b["api_key"]
            if b.get("api_secret"):
                self.binance.api_secret = b["api_secret"]
            if "testnet" in b:
                self.binance.testnet = b["testnet"]

            # Scan
            s = data.get("scan", {})
            if "scan_scope" in s:
                self.scan.scan_scope = s["scan_scope"]
            if "exclude_list" in s:
                self.scan.exclude_list = s["exclude_list"]
            if "auto_blacklist_enabled" in s:
                self.scan.auto_blacklist_enabled = s["auto_blacklist_enabled"]

            # Entry
            e = data.get("entry", {})
            if "volume_threshold" in e:
                self.entry.volume_threshold = e["volume_threshold"]
            if "rsi_enabled" in e:
                self.entry.rsi_enabled = e["rsi_enabled"]
            if "atr_filter_enabled" in e:
                self.entry.atr_filter_enabled = e["atr_filter_enabled"]
            for key in ["max_price_deviation_pct_1h", "max_price_deviation_pct_4h", "max_price_deviation_pct_1d"]:
                if key in e:
                    setattr(self.entry, key, e[key])

            # Margin
            m = data.get("margin", {})
            for key in ["leverage", "margin_1h", "margin_4h", "margin_1d",
                        "max_open_positions", "margin_warning_threshold",
                        "margin_circuit_break_threshold"]:
                if key in m:
                    setattr(self.margin, key, m[key])

            # Exit
            x = data.get("exit", {})
            for key in ["atr_stop_multiplier", "tp1_close_ratio", "tp2_close_ratio",
                        "ema_check_interval_minutes"]:
                if key in x:
                    setattr(self.exit, key, x[key])

            logger.info(f"Configuration loaded from {CONFIG_FILE}")
            if self.binance.api_key:
                masked = self.binance.api_key[:8] + "..." + self.binance.api_key[-4:]
                logger.info(f"Binance API Key: {masked}, Testnet: {self.binance.testnet}")
        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")


# Global config instance
config = DTRSConfig()
config.load()
