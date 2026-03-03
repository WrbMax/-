"""
DTRS Trading Engine - SQLite Database Manager
Stores positions, trades, signals, and system state.
"""

import sqlite3
import json
import os
from datetime import datetime
from typing import Optional, List, Dict, Any
from config import config

DB_PATH = config.system.db_path


def get_db() -> sqlite3.Connection:
    """Get database connection with row factory"""
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Initialize database tables"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('LONG', 'SHORT')),
        period TEXT NOT NULL CHECK(period IN ('1h', '4h', '1d')),
        entry_price REAL NOT NULL,
        quantity REAL NOT NULL,
        leverage INTEGER NOT NULL,
        margin_used REAL NOT NULL,
        stop_loss REAL,
        take_profit_1 REAL,
        take_profit_2 REAL,
        tp1_hit INTEGER DEFAULT 0,
        tp2_hit INTEGER DEFAULT 0,
        remaining_ratio REAL DEFAULT 1.0,
        status TEXT DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'CLOSED', 'PARTIAL')),
        open_time TEXT NOT NULL,
        close_time TEXT,
        close_price REAL,
        close_reason TEXT,
        realized_pnl REAL DEFAULT 0,
        binance_order_id TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        period TEXT NOT NULL,
        direction TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('executed', 'filtered', 'conflict', 'circuit_break')),
        price REAL NOT NULL,
        ma20 REAL,
        macd_hist REAL,
        volume_ratio REAL,
        atr REAL,
        rsi REAL,
        reason TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id INTEGER REFERENCES positions(id),
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
        order_type TEXT NOT NULL,
        quantity REAL NOT NULL,
        price REAL NOT NULL,
        commission REAL DEFAULT 0,
        binance_order_id TEXT,
        binance_trade_id TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS scan_pool (
        symbol TEXT PRIMARY KEY,
        volume_24h REAL,
        market_cap REAL,
        volatility_7d REAL,
        rank_score REAL,
        updated_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS system_state (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT NOT NULL,
        module TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
    CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
    CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
    CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);
    """)

    conn.commit()
    conn.close()


# ---- Position CRUD ----

def create_position(data: Dict[str, Any]) -> int:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO positions (symbol, direction, period, entry_price, quantity, leverage,
            margin_used, stop_loss, take_profit_1, take_profit_2, open_time, binance_order_id)
        VALUES (:symbol, :direction, :period, :entry_price, :quantity, :leverage,
            :margin_used, :stop_loss, :take_profit_1, :take_profit_2, :open_time, :binance_order_id)
    """, data)
    pos_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return pos_id


def get_open_positions() -> List[Dict]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM positions WHERE status IN ('OPEN', 'PARTIAL') ORDER BY open_time DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_position_by_id(pos_id: int) -> Optional[Dict]:
    conn = get_db()
    row = conn.execute("SELECT * FROM positions WHERE id = ?", (pos_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_positions_by_symbol_period(symbol: str, period: str) -> List[Dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM positions WHERE symbol = ? AND period = ? AND status IN ('OPEN', 'PARTIAL')",
        (symbol, period)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_position(pos_id: int, data: Dict[str, Any]):
    conn = get_db()
    sets = ", ".join(f"{k} = :{k}" for k in data.keys())
    data["id"] = pos_id
    data["updated_at"] = datetime.utcnow().isoformat()
    conn.execute(f"UPDATE positions SET {sets}, updated_at = :updated_at WHERE id = :id", data)
    conn.commit()
    conn.close()


def close_position(pos_id: int, close_price: float, close_reason: str, realized_pnl: float):
    update_position(pos_id, {
        "status": "CLOSED",
        "close_price": close_price,
        "close_time": datetime.utcnow().isoformat(),
        "close_reason": close_reason,
        "realized_pnl": realized_pnl,
    })


# ---- Signal CRUD ----

def create_signal(data: Dict[str, Any]) -> int:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO signals (symbol, period, direction, status, price, ma20, macd_hist,
            volume_ratio, atr, rsi, reason)
        VALUES (:symbol, :period, :direction, :status, :price, :ma20, :macd_hist,
            :volume_ratio, :atr, :rsi, :reason)
    """, data)
    sig_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return sig_id


def get_recent_signals(limit: int = 50) -> List[Dict]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- Trade CRUD ----

def create_trade(data: Dict[str, Any]) -> int:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO trades (position_id, symbol, side, order_type, quantity, price,
            commission, binance_order_id, binance_trade_id)
        VALUES (:position_id, :symbol, :side, :order_type, :quantity, :price,
            :commission, :binance_order_id, :binance_trade_id)
    """, data)
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id


# ---- Log CRUD ----

def add_log(level: str, module: str, message: str):
    conn = get_db()
    conn.execute("INSERT INTO logs (level, module, message) VALUES (?, ?, ?)", (level, module, message))
    conn.commit()
    conn.close()


def get_recent_logs(limit: int = 100, level: str = None, module: str = None) -> List[Dict]:
    conn = get_db()
    query = "SELECT * FROM logs WHERE 1=1"
    params = []
    if level:
        query += " AND level = ?"
        params.append(level)
    if module:
        query += " AND module = ?"
        params.append(module)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- System State ----

def set_state(key: str, value: str):
    conn = get_db()
    conn.execute("""
        INSERT INTO system_state (key, value, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
    """, (key, value))
    conn.commit()
    conn.close()


def get_state(key: str) -> Optional[str]:
    conn = get_db()
    row = conn.execute("SELECT value FROM system_state WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


# ---- Stats ----

def get_performance_stats() -> Dict:
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) as cnt FROM positions WHERE status = 'CLOSED'").fetchone()["cnt"]
    wins = conn.execute("SELECT COUNT(*) as cnt FROM positions WHERE status = 'CLOSED' AND realized_pnl > 0").fetchone()["cnt"]
    total_pnl = conn.execute("SELECT COALESCE(SUM(realized_pnl), 0) as total FROM positions WHERE status = 'CLOSED'").fetchone()["total"]
    avg_win = conn.execute("SELECT COALESCE(AVG(realized_pnl), 0) as avg FROM positions WHERE status = 'CLOSED' AND realized_pnl > 0").fetchone()["avg"]
    avg_loss = conn.execute("SELECT COALESCE(AVG(realized_pnl), 0) as avg FROM positions WHERE status = 'CLOSED' AND realized_pnl < 0").fetchone()["avg"]
    conn.close()

    return {
        "total_trades": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate": (wins / total * 100) if total > 0 else 0,
        "total_pnl": total_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": abs(avg_win / avg_loss) if avg_loss != 0 else 0,
    }
