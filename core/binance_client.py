"""
DTRS Trading Engine - Binance Futures API Client
Handles all communication with Binance USDT-M Perpetual Futures API.
Supports runtime re-initialization when API keys are updated.
"""
import time
import hmac
import hashlib
import logging
from urllib.parse import urlencode
from typing import Optional, Dict, List, Any

import requests
from config import config

logger = logging.getLogger("dtrs.binance")


class BinanceFuturesClient:
    """Binance USDT-M Perpetual Futures REST API Client"""

    def __init__(self):
        self.session = requests.Session()
        self._apply_config()

    def _apply_config(self):
        """Apply current config to client (called on init and reinit)"""
        self.api_key = config.binance.api_key
        self.api_secret = config.binance.api_secret
        self.base_url = config.binance.base_url
        self.session.headers.update({
            "X-MBX-APIKEY": self.api_key,
            "Content-Type": "application/x-www-form-urlencoded",
        })
        if self.api_key:
            masked = self.api_key[:8] + "..." + self.api_key[-4:] if len(self.api_key) > 12 else "***"
            logger.info(f"Binance client configured: key={masked}, url={self.base_url}")
        else:
            logger.warning("Binance client: No API key configured")

    def reinitialize(self):
        """Re-read config and reinitialize the client (called after config update)"""
        logger.info("Reinitializing Binance client with updated config...")
        self._apply_config()

    def _sign(self, params: Dict) -> Dict:
        """Add timestamp and signature to request params"""
        params["timestamp"] = int(time.time() * 1000)
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    def _request(self, method: str, endpoint: str, params: Dict = None, signed: bool = False) -> Any:
        """Execute API request with error handling"""
        if signed and not self.api_key:
            raise ValueError("Binance API key not configured. Please set API key in Settings.")

        url = f"{self.base_url}{endpoint}"
        params = params or {}

        if signed:
            params = self._sign(params)

        try:
            if method == "GET":
                resp = self.session.get(url, params=params, timeout=10)
            elif method == "POST":
                resp = self.session.post(url, data=params, timeout=10)
            elif method == "DELETE":
                resp = self.session.delete(url, params=params, timeout=10)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"Binance API error: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            raise

    # ---- Market Data ----

    def get_exchange_info(self) -> Dict:
        """Get exchange trading rules and symbol info"""
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> List:
        """Get kline/candlestick data"""
        return self._request("GET", "/fapi/v1/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        })

    def get_ticker_price(self, symbol: str) -> dict:
        """Get current price for a single symbol"""
        return self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)

    def get_ticker_24h(self, symbol: str = None) -> Any:
        """Get 24h ticker price change statistics"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/fapi/v1/ticker/24hr", params)

    def get_mark_price(self, symbol: str = None) -> Any:
        """Get mark price and funding rate"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/fapi/v1/premiumIndex", params)

    # ---- Account ----

    def get_account_info(self) -> Dict:
        """Get current account information"""
        return self._request("GET", "/fapi/v2/account", signed=True)

    def get_balance(self) -> List:
        """Get futures account balance"""
        return self._request("GET", "/fapi/v2/balance", signed=True)

    def get_position_risk(self, symbol: str = None) -> List:
        """Get current position information"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/fapi/v2/positionRisk", params, signed=True)

    # ---- Trading ----

    def set_leverage(self, symbol: str, leverage: int) -> Dict:
        """Change initial leverage"""
        return self._request("POST", "/fapi/v1/leverage", {
            "symbol": symbol,
            "leverage": leverage,
        }, signed=True)

    def set_margin_type(self, symbol: str, margin_type: str = "CROSSED") -> Dict:
        """Change margin type (ISOLATED or CROSSED)"""
        try:
            return self._request("POST", "/fapi/v1/marginType", {
                "symbol": symbol,
                "marginType": margin_type,
            }, signed=True)
        except Exception as e:
            if "No need to change margin type" in str(e):
                return {"msg": "Already set"}
            raise

    def place_market_order(self, symbol: str, side: str, quantity: float,
                           reduce_only: bool = False) -> Dict:
        """Place a market order"""
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def place_stop_market_order(self, symbol: str, side: str, stop_price: float,
                                 close_position: bool = True) -> Dict:
        """Place a stop-market order for stop loss"""
        params = {
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET",
            "stopPrice": stop_price,
            "closePosition": "true" if close_position else "false",
        }
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def cancel_all_orders(self, symbol: str) -> Dict:
        """Cancel all open orders for a symbol"""
        return self._request("DELETE", "/fapi/v1/allOpenOrders", {
            "symbol": symbol,
        }, signed=True)

    def get_open_orders(self, symbol: str = None) -> List:
        """Get all open orders"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/fapi/v1/openOrders", params, signed=True)

    def get_all_orders(self, symbol: str, limit: int = 50) -> List:
        """Get all orders for a symbol"""
        return self._request("GET", "/fapi/v1/allOrders", {
            "symbol": symbol,
            "limit": limit,
        }, signed=True)

    # ---- Utility ----

    def get_symbol_precision(self, symbol: str) -> Dict:
        """Get quantity and price precision for a symbol"""
        info = self.get_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                qty_precision = s["quantityPrecision"]
                price_precision = s["pricePrecision"]
                min_qty = 0
                step_size = 0
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        min_qty = float(f["minQty"])
                        step_size = float(f["stepSize"])
                return {
                    "quantity_precision": qty_precision,
                    "price_precision": price_precision,
                    "min_qty": min_qty,
                    "step_size": step_size,
                }
        raise ValueError(f"Symbol {symbol} not found")

    def calculate_quantity(self, symbol: str, usdt_amount: float, price: float) -> float:
        """Calculate order quantity based on USDT amount and current price"""
        precision = self.get_symbol_precision(symbol)
        raw_qty = usdt_amount / price
        step = precision["step_size"]
        if step > 0:
            qty = round(raw_qty - (raw_qty % step), precision["quantity_precision"])
        else:
            qty = round(raw_qty, precision["quantity_precision"])
        return max(qty, precision["min_qty"])

    def get_wallet_balance(self) -> float:
        """Get total USDT wallet balance"""
        balances = self.get_balance()
        for b in balances:
            if b["asset"] == "USDT":
                return float(b["balance"])
        return 0.0

    def get_margin_ratio(self) -> float:
        """Get current margin ratio"""
        account = self.get_account_info()
        total_margin = float(account.get("totalMaintMargin", 0))
        total_equity = float(account.get("totalMarginBalance", 1))
        if total_equity == 0:
            return 0
        return (total_margin / total_equity) * 100

    def test_connection(self) -> dict:
        """Test API connection and return account summary"""
        try:
            balance = self.get_wallet_balance()
            return {"connected": True, "balance": balance, "error": None}
        except Exception as e:
            return {"connected": False, "balance": 0, "error": str(e)}


# Global client instance
client = BinanceFuturesClient()
