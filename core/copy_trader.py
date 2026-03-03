"""
DTRS Copy Trading Module
Executes the same trades on follower accounts when master opens/closes positions.
"""
import logging
import json
import hmac
import hashlib
import time
import requests
import sqlite3
from typing import List, Dict, Optional
from urllib.parse import urlencode

logger = logging.getLogger("dtrs.copy_trader")
DB_PATH = "/opt/dtrs-engine/data/dtrs.db"
BINANCE_FUTURES_URL = "https://fapi.binance.com"


def _sign(params: dict, secret: str) -> str:
    query = urlencode(params)
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + "&signature=" + sig


def _api_request(method: str, endpoint: str, params: dict, api_key: str, api_secret: str) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    signed = _sign(params, api_secret)
    url = f"{BINANCE_FUTURES_URL}{endpoint}"
    headers = {"X-MBX-APIKEY": api_key}
    if method == "POST":
        resp = requests.post(url, data=signed, headers=headers, timeout=10)
    else:
        resp = requests.get(f"{url}?{signed}", headers=headers, timeout=10)
    return resp.json()


def get_follower_accounts() -> List[Dict]:
    """Load all active follower accounts from database"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT id, name, api_key, api_secret, leverage_multiplier, active
            FROM copy_accounts WHERE active=1
        """)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        logger.debug(f"No copy accounts table or error: {e}")
        return []


class CopyTrader:
    """Executes copy trades on follower accounts"""

    def execute_open(self, symbol: str, direction: str, side: str,
                     quantity: float, entry_price: float,
                     stop_loss: float, tp1: float, tp2: float, period: str):
        """Open position on all follower accounts"""
        accounts = get_follower_accounts()
        if not accounts:
            return

        for account in accounts:
            try:
                self._open_on_account(account, symbol, direction, side,
                                       quantity, entry_price, stop_loss, tp1, tp2)
                logger.info(f"Copy trade opened on {account['name']}: {symbol} {direction}")
            except Exception as e:
                logger.error(f"Copy trade failed on {account['name']} for {symbol}: {e}")

    def _open_on_account(self, account: Dict, symbol: str, direction: str, side: str,
                          quantity: float, entry_price: float,
                          stop_loss: float, tp1: float, tp2: float):
        """Open position on a single follower account"""
        api_key = account["api_key"]
        api_secret = account["api_secret"]
        multiplier = float(account.get("leverage_multiplier", 1.0))
        copy_qty = round(quantity * multiplier, 8)

        # Set margin type (ignore errors)
        try:
            _api_request("POST", "/fapi/v1/marginType",
                         {"symbol": symbol, "marginType": "CROSSED"},
                         api_key, api_secret)
        except Exception:
            pass

        # Place market order
        result = _api_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": copy_qty,
        }, api_key, api_secret)

        if result.get("orderId"):
            logger.info(f"Copy order placed: {symbol} {side} qty={copy_qty}, orderId={result['orderId']}")
        else:
            logger.warning(f"Copy order response: {result}")

    def execute_close(self, symbol: str, direction: str,
                       close_ratio: float, reason: str):
        """Close position on all follower accounts"""
        accounts = get_follower_accounts()
        if not accounts:
            return

        close_side = "SELL" if direction == "LONG" else "BUY"

        for account in accounts:
            try:
                # Get current position on follower account
                positions = _api_request("GET", "/fapi/v2/positionRisk",
                                          {"symbol": symbol},
                                          account["api_key"], account["api_secret"])
                pos_amt = 0
                for p in positions:
                    if p.get("symbol") == symbol:
                        pos_amt = abs(float(p.get("positionAmt", 0)))
                        break

                if pos_amt <= 0:
                    continue

                close_qty = round(pos_amt * close_ratio, 8)
                result = _api_request("POST", "/fapi/v1/order", {
                    "symbol": symbol,
                    "side": close_side,
                    "type": "MARKET",
                    "quantity": close_qty,
                    "reduceOnly": "true",
                }, account["api_key"], account["api_secret"])

                if result.get("orderId"):
                    logger.info(f"Copy close on {account['name']}: {symbol} {close_ratio*100:.0f}%")
                else:
                    logger.warning(f"Copy close response: {result}")

            except Exception as e:
                logger.error(f"Copy close failed on {account['name']} for {symbol}: {e}")


# Singleton
copy_trader = CopyTrader()
