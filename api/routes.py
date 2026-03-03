"""
DTRS Trading Engine - REST API Routes
Provides endpoints for the frontend dashboard to communicate with the engine.
All configuration changes are persisted to disk via config.save().
"""

import logging
from typing import Optional, List
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import config
from core.database import (
    get_open_positions, get_recent_signals, get_recent_logs,
    get_performance_stats, get_state, set_state, get_position_by_id
)
from core.binance_client import client

logger = logging.getLogger("dtrs.api")

app = FastAPI(title="DTRS Trading Engine API", version="1.0.0")

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- Request/Response Models ----

class StatusResponse(BaseModel):
    engine_status: str
    engine_start_time: Optional[str] = None
    wallet_balance: float = 0
    margin_ratio: float = 0
    open_positions_count: int = 0
    scan_pool_size: int = 0
    last_heartbeat: Optional[str] = None
    api_connected: bool = False
    api_key_set: bool = False
    testnet: bool = True


class ConfigUpdate(BaseModel):
    # Binance API
    binance_api_key: Optional[str] = None
    binance_api_secret: Optional[str] = None
    binance_testnet: Optional[bool] = None
    # Margin
    leverage: Optional[int] = None
    margin_1h: Optional[float] = None
    margin_4h: Optional[float] = None
    margin_1d: Optional[float] = None
    max_open_positions: Optional[int] = None
    margin_warning_threshold: Optional[float] = None
    margin_circuit_break_threshold: Optional[float] = None
    # Exit
    atr_stop_multiplier: Optional[float] = None
    tp1_close_ratio: Optional[float] = None
    tp2_close_ratio: Optional[float] = None
    # Entry
    volume_threshold: Optional[float] = None
    rsi_enabled: Optional[bool] = None
    atr_filter_enabled: Optional[bool] = None
    # Scan
    scan_scope: Optional[int] = None
    exclude_list: Optional[List[str]] = None
    auto_blacklist_enabled: Optional[bool] = None


# ---- Endpoints ----

@app.get("/api/status", response_model=StatusResponse)
async def get_status():
    """Get engine status and key metrics"""
    try:
        positions = get_open_positions()
        balance = 0
        margin_ratio = 0
        api_connected = False

        if config.binance.api_key:
            try:
                balance = client.get_wallet_balance()
                margin_ratio = client.get_margin_ratio()
                api_connected = True
            except Exception as e:
                logger.debug(f"Binance API check failed: {e}")

        return StatusResponse(
            engine_status=get_state("engine_status") or "running",
            engine_start_time=get_state("engine_start_time"),
            wallet_balance=balance,
            margin_ratio=margin_ratio,
            open_positions_count=len(positions),
            scan_pool_size=int(get_state("scan_pool_size") or 0),
            last_heartbeat=get_state("last_heartbeat"),
            api_connected=api_connected,
            api_key_set=bool(config.binance.api_key),
            testnet=config.binance.testnet,
        )
    except Exception as e:
        logger.error(f"Status endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/positions")
async def list_positions(status: str = "open", limit: int = 200, offset: int = 0):
    """List positions: status=open|closed|partial|all"""
    try:
        from core.database import get_db
        conn = get_db()
        if status == "open":
            positions = get_open_positions()
        elif status == "closed":
            rows = conn.execute(
                "SELECT * FROM positions WHERE status = 'CLOSED' ORDER BY close_time DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
            positions = [dict(r) for r in rows]
        elif status == "partial":
            rows = conn.execute(
                "SELECT * FROM positions WHERE status = 'PARTIAL' ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
            positions = [dict(r) for r in rows]
        else:
            rows = conn.execute(
                "SELECT * FROM positions ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
            positions = [dict(r) for r in rows]
        conn.close()
        # Enrich open/partial positions with current prices
        for pos in positions:
            if pos.get("status") in ("OPEN", "PARTIAL"):
                try:
                    mark = client.get_mark_price(pos["symbol"])
                    current_price = float(mark["markPrice"])
                    pos["current_price"] = current_price
                    remaining = pos.get("remaining_ratio", 1) or 1
                    lev = pos.get("leverage", 1) or 1
                    if pos["direction"] == "LONG":
                        pos["unrealized_pnl"] = (current_price - pos["entry_price"]) * pos["quantity"] * remaining
                        pos["unrealized_pnl_pct"] = round(((current_price / pos["entry_price"]) - 1) * 100 * lev, 2)
                    else:
                        pos["unrealized_pnl"] = (pos["entry_price"] - current_price) * pos["quantity"] * remaining
                        pos["unrealized_pnl_pct"] = round((1 - (current_price / pos["entry_price"])) * 100 * lev, 2)
                except Exception:
                    pos["current_price"] = pos.get("entry_price", 0)
                    pos["unrealized_pnl"] = 0
                    pos["unrealized_pnl_pct"] = 0
            else:
                # For closed/partial positions, compute realized_pnl_pct
                margin = pos.get("margin_used") or 0
                realized = pos.get("realized_pnl") or 0
                pos["realized_pnl_pct"] = round(realized / margin * 100, 2) if margin > 0 else 0
        return {"positions": positions, "total": len(positions)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
@app.get("/api/signals")
async def list_signals(limit: int = 50, period: Optional[str] = None, status: Optional[str] = None):
    """List recent signals with optional filters"""
    try:
        from core.database import get_db
        conn = get_db()
        query = "SELECT * FROM signals WHERE 1=1"
        params = []
        if period:
            query += " AND period = ?"
            params.append(period)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return {"signals": [dict(r) for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/logs")
async def list_logs(limit: int = 100, level: Optional[str] = None, module: Optional[str] = None):
    """List recent system logs"""
    try:
        logs = get_recent_logs(limit, level, module)
        return {"logs": logs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/performance")
async def get_performance():
    """Get enriched performance statistics"""
    try:
        from core.database import get_db
        import math
        conn = get_db()

        # Basic stats
        total = conn.execute("SELECT COUNT(*) as cnt FROM positions WHERE status = 'CLOSED'").fetchone()["cnt"]
        wins = conn.execute("SELECT COUNT(*) as cnt FROM positions WHERE status = 'CLOSED' AND realized_pnl > 0").fetchone()["cnt"]
        losses_cnt = total - wins
        total_pnl = conn.execute("SELECT COALESCE(SUM(realized_pnl), 0) as v FROM positions WHERE status = 'CLOSED'").fetchone()["v"]
        avg_win = conn.execute("SELECT COALESCE(AVG(realized_pnl), 0) as v FROM positions WHERE status = 'CLOSED' AND realized_pnl > 0").fetchone()["v"]
        avg_loss = conn.execute("SELECT COALESCE(AVG(realized_pnl), 0) as v FROM positions WHERE status = 'CLOSED' AND realized_pnl < 0").fetchone()["v"]
        total_margin = conn.execute("SELECT COALESCE(SUM(margin_used), 0) as v FROM positions WHERE status = 'CLOSED'").fetchone()["v"]
        total_pnl_pct = (total_pnl / total_margin * 100) if total_margin > 0 else 0

        # Max drawdown calculation
        rows = conn.execute("SELECT realized_pnl FROM positions WHERE status='CLOSED' ORDER BY close_time ASC").fetchall()
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        drawdown_curve = []
        for i, r in enumerate(rows):
            cumulative += r["realized_pnl"]
            if cumulative > peak:
                peak = cumulative
            dd = ((peak - cumulative) / abs(peak) * 100) if peak != 0 else 0
            if dd > max_dd:
                max_dd = dd
            drawdown_curve.append({"index": i + 1, "dd": round(-dd, 2), "pnl": round(cumulative, 2)})

        # Monthly returns
        monthly_rows = conn.execute("""
            SELECT strftime('%Y-%m', close_time) as month,
                   SUM(realized_pnl) as pnl,
                   SUM(margin_used) as margin
            FROM positions WHERE status='CLOSED' AND close_time IS NOT NULL
            GROUP BY month ORDER BY month
        """).fetchall()
        monthly_returns = []
        for r in monthly_rows:
            pnl = r["pnl"] or 0
            margin = r["margin"] or 1
            monthly_returns.append({
                "month": r["month"],
                "return": round(pnl / margin * 100, 2),
                "pnl": round(pnl, 2)
            })

        # Per-period stats
        period_rows = conn.execute("""
            SELECT period,
                   COUNT(*) as trades,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(realized_pnl) as total_pnl,
                   AVG(realized_pnl) as avg_pnl,
                   SUM(margin_used) as total_margin
            FROM positions WHERE status='CLOSED'
            GROUP BY period
        """).fetchall()
        period_stats = []
        for r in period_rows:
            trades = r["trades"] or 1
            total_m = r["total_margin"] or 1
            period_stats.append({
                "period": r["period"],
                "trades": r["trades"],
                "wins": r["wins"],
                "losses": r["trades"] - r["wins"],
                "win_rate": round(r["wins"] / trades * 100, 1),
                "total_pnl": round(r["total_pnl"] or 0, 2),
                "avg_pnl": round((r["avg_pnl"] or 0) / (r["total_margin"] / r["trades"] if r["trades"] > 0 else 1) * 100, 2),
                "avg_hold": "N/A"
            })

        # Top/worst performers by symbol
        symbol_rows = conn.execute("""
            SELECT symbol,
                   COUNT(*) as trades,
                   SUM(realized_pnl) as total_pnl,
                   SUM(margin_used) as total_margin
            FROM positions WHERE status='CLOSED'
            GROUP BY symbol
            ORDER BY total_pnl DESC
        """).fetchall()
        performers = []
        for r in symbol_rows:
            margin = r["total_margin"] or 1
            performers.append({
                "symbol": r["symbol"],
                "trades": r["trades"],
                "total_pnl": round(r["total_pnl"] or 0, 2),
                "pnl_pct": round((r["total_pnl"] or 0) / margin * 100, 2)
            })
        top_performers = performers[:5]
        worst_performers = list(reversed(performers))[:5]

        conn.close()
        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses_cnt,
            "win_rate": round(wins / total * 100, 2) if total > 0 else 0,
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(abs(avg_win / avg_loss), 3) if avg_loss != 0 else 0,
            "max_drawdown": round(max_dd, 2),
            "monthly_returns": monthly_returns,
            "drawdown_curve": drawdown_curve[-60:],  # last 60 trades
            "period_stats": period_stats,
            "top_performers": top_performers,
            "worst_performers": worst_performers,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/config")
async def get_config():
    """Get current configuration (API secrets are masked)"""
    api_key_display = ""
    if config.binance.api_key:
        k = config.binance.api_key
        api_key_display = k[:8] + "..." + k[-4:] if len(k) > 12 else "***"

    return {
        "binance": {
            "testnet": config.binance.testnet,
            "api_key_set": bool(config.binance.api_key),
            "api_key_display": api_key_display,
        },
        "scan": {
            "scan_scope": config.scan.scan_scope,
            "exclude_list": config.scan.exclude_list,
            "auto_blacklist_enabled": config.scan.auto_blacklist_enabled,
        },
        "margin": {
            "leverage": config.margin.leverage,
            "margin_1h": config.margin.margin_1h,
            "margin_4h": config.margin.margin_4h,
            "margin_1d": config.margin.margin_1d,
            "max_open_positions": config.margin.max_open_positions,
            "margin_warning_threshold": config.margin.margin_warning_threshold,
            "margin_circuit_break_threshold": config.margin.margin_circuit_break_threshold,
        },
        "entry": {
            "volume_threshold": config.entry.volume_threshold,
            "rsi_enabled": config.entry.rsi_enabled,
            "atr_filter_enabled": config.entry.atr_filter_enabled,
        },
        "exit": {
            "atr_stop_multiplier": config.exit.atr_stop_multiplier,
            "tp1_close_ratio": config.exit.tp1_close_ratio,
            "tp2_close_ratio": config.exit.tp2_close_ratio,
            "ema_check_interval_minutes": config.exit.ema_check_interval_minutes,
        },
    }


@app.post("/api/config")
async def update_config(update: ConfigUpdate):
    """Update configuration and persist to disk"""
    try:
        binance_changed = False

        # Binance API keys
        if update.binance_api_key is not None and update.binance_api_key.strip():
            config.binance.api_key = update.binance_api_key.strip()
            binance_changed = True
        if update.binance_api_secret is not None and update.binance_api_secret.strip():
            config.binance.api_secret = update.binance_api_secret.strip()
            binance_changed = True
        if update.binance_testnet is not None:
            config.binance.testnet = update.binance_testnet
            binance_changed = True

        # Margin config
        if update.leverage is not None:
            config.margin.leverage = update.leverage
        if update.margin_1h is not None:
            config.margin.margin_1h = update.margin_1h
        if update.margin_4h is not None:
            config.margin.margin_4h = update.margin_4h
        if update.margin_1d is not None:
            config.margin.margin_1d = update.margin_1d
        if update.max_open_positions is not None:
            config.margin.max_open_positions = update.max_open_positions
        if update.margin_warning_threshold is not None:
            config.margin.margin_warning_threshold = update.margin_warning_threshold
        if update.margin_circuit_break_threshold is not None:
            config.margin.margin_circuit_break_threshold = update.margin_circuit_break_threshold

        # Exit config
        if update.atr_stop_multiplier is not None:
            config.exit.atr_stop_multiplier = update.atr_stop_multiplier
        if update.tp1_close_ratio is not None:
            config.exit.tp1_close_ratio = update.tp1_close_ratio
        if update.tp2_close_ratio is not None:
            config.exit.tp2_close_ratio = update.tp2_close_ratio

        # Entry config
        if update.volume_threshold is not None:
            config.entry.volume_threshold = update.volume_threshold
        if update.rsi_enabled is not None:
            config.entry.rsi_enabled = update.rsi_enabled
        if update.atr_filter_enabled is not None:
            config.entry.atr_filter_enabled = update.atr_filter_enabled

        # Scan config
        if update.scan_scope is not None:
            config.scan.scan_scope = update.scan_scope
        if update.exclude_list is not None:
            config.scan.exclude_list = update.exclude_list
        if update.auto_blacklist_enabled is not None:
            config.scan.auto_blacklist_enabled = update.auto_blacklist_enabled

        # Persist to disk
        config.save()

        # Reinitialize Binance client if API keys changed
        if binance_changed:
            client.reinitialize()
            logger.info("Binance client reinitialized after config update")

        from core.database import add_log
        add_log("info", "API", "配置已更新并保存到磁盘")

        # Test connection if API keys were updated
        connection_result = None
        if binance_changed and config.binance.api_key:
            connection_result = client.test_connection()

        return {
            "status": "ok",
            "message": "Configuration saved and persisted",
            "binance_connection": connection_result,
        }
    except Exception as e:
        logger.error(f"Config update error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/test-connection")
async def test_binance_connection():
    """Test Binance API connection with current credentials"""
    if not config.binance.api_key:
        return {
            "connected": False,
            "balance": 0,
            "error": "API Key 未配置，请先在设置中填入币安 API Key",
        }
    result = client.test_connection()
    return result


@app.post("/api/engine/{action}")
async def control_engine(action: str):
    """Control engine: start/stop/restart"""
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail="Invalid action")

    set_state("engine_status", "running" if action in ("start", "restart") else "stopped")
    from core.database import add_log
    add_log("info", "API", f"引擎控制: {action}")
    return {"status": "ok", "action": action}


@app.post("/api/positions/{pos_id}/close")
async def close_position(pos_id: int):
    """Manually close a position"""
    try:
        pos = get_position_by_id(pos_id)
        if not pos:
            raise HTTPException(status_code=404, detail="Position not found")

        mark = client.get_mark_price(pos["symbol"])
        current_price = float(mark["markPrice"])

        from core.executor import Executor
        executor = Executor()
        success = executor.close_position_full(pos_id, current_price, "手动平仓")

        if success:
            return {"status": "ok", "message": f"Position {pos_id} closed"}
        else:
            raise HTTPException(status_code=500, detail="Failed to close position")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/account")
async def get_account():
    """Get Binance account info"""
    try:
        if not config.binance.api_key:
            return {
                "total_wallet_balance": 0,
                "total_unrealized_profit": 0,
                "total_margin_balance": 0,
                "total_maint_margin": 0,
                "available_balance": 0,
                "error": "API Key 未配置",
            }
        account = client.get_account_info()
        return {
            "total_wallet_balance": float(account.get("totalWalletBalance", 0)),
            "total_unrealized_profit": float(account.get("totalUnrealizedProfit", 0)),
            "total_margin_balance": float(account.get("totalMarginBalance", 0)),
            "total_maint_margin": float(account.get("totalMaintMargin", 0)),
            "available_balance": float(account.get("availableBalance", 0)),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Global scan lock to prevent concurrent manual + scheduled scans
import asyncio as _asyncio
_global_scan_lock = _asyncio.Lock()

@app.post("/api/scan/{period}")
async def manual_scan(period: str):
    """Manually trigger a scan for a specific timeframe (1h, 4h, 1d)"""
    if period not in ("1h", "4h", "1d"):
        raise HTTPException(status_code=400, detail="Invalid period. Use 1h, 4h or 1d")
    
    # Prevent concurrent scans (manual + scheduled running simultaneously)
    if _global_scan_lock.locked():
        return {
            "status": "busy",
            "period": period,
            "message": "扫描正在进行中，请稍后再试",
            "scanned": 0,
            "signals_total": 0,
            "executed": 0,
            "filtered": 0,
            "conflict": 0,
            "signal_details": []
        }
    
    async with _global_scan_lock:
        try:
            from core.scanner import Scanner
            from core.database import add_log
            add_log("info", "API", f"手动触发 {period} 扫描")
            scanner = Scanner()
            scanner.refresh_scan_pool()
            loop = _asyncio.get_event_loop()
            signals = await loop.run_in_executor(None, scanner.scan_timeframe, period)
            executed = [s for s in signals if s["status"] == "executed"]
            filtered = [s for s in signals if s["status"] == "filtered"]
            conflict = [s for s in signals if s["status"] == "conflict"]
            # Execute signals
            if executed:
                from core.executor import Executor
                executor = Executor()
                for sig in executed:
                    await loop.run_in_executor(None, executor.execute_signal, sig)
            return {
                "status": "ok",
                "period": period,
                "scanned": scanner.scan_pool.__len__(),
                "signals_total": len(signals),
                "executed": len(executed),
                "filtered": len(filtered),
                "conflict": len(conflict),
                "signal_details": signals[:20],
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))



# ─── Copy Trading Account Management ─────────────────────────────────────────

class CopyAccountCreate(BaseModel):
    name: str
    api_key: str
    api_secret: str
    ratio: float = 1.0  # 1.0 = 100%

class CopyAccountUpdate(BaseModel):
    enabled: bool = None
    ratio: float = None

@app.get("/api/copy-accounts")
def get_copy_accounts():
    """Get all copy trading accounts"""
    import sqlite3 as _sqlite3
    from core.database import get_db
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, api_key, leverage_multiplier, active, created_at FROM copy_accounts ORDER BY id")
        rows = []
        for r in cur.fetchall():
            row = dict(r)
            key = row.get("api_key", "")
            rows.append({
                "id": row["id"],
                "name": row["name"],
                "api_key_display": key[:8] + "..." + key[-4:] if len(key) > 12 else "***",
                "enabled": bool(row["active"]),
                "ratio": float(row.get("leverage_multiplier", 1.0)),
                "status": "active" if row["active"] else "disabled",
                "last_sync": None,
                "total_trades": 0,
                "error_message": None,
            })
        return {"accounts": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.post("/api/copy-accounts")
def add_copy_account(data: CopyAccountCreate):
    """Add a copy trading account"""
    import hmac, hashlib, time, requests as _requests
    from urllib.parse import urlencode
    from core.database import get_db

    name = data.name.strip()
    api_key = data.api_key.strip()
    api_secret = data.api_secret.strip()
    ratio = float(data.ratio)

    if not name or not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="name, api_key, api_secret are required")

    # Test Binance API connection
    try:
        ts = int(time.time() * 1000)
        params = {"timestamp": ts}
        query = urlencode(params)
        sig = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = f"https://fapi.binance.com/fapi/v2/balance?{query}&signature={sig}"
        resp = _requests.get(url, headers={"X-MBX-APIKEY": api_key}, timeout=8)
        result = resp.json()
        if isinstance(result, dict) and result.get("code"):
            raise HTTPException(status_code=400, detail=f"API验证失败: {result.get('msg', str(result))}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"连接测试失败: {e}")

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO copy_accounts (name, api_key, api_secret, leverage_multiplier, active) VALUES (?, ?, ?, ?, 1)",
            (name, api_key, api_secret, ratio)
        )
        conn.commit()
        return {"status": "ok", "message": f"跟单账户 '{name}' 添加成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.patch("/api/copy-accounts/{account_id}")
def update_copy_account(account_id: int, data: CopyAccountUpdate):
    """Update copy trading account (enable/disable, change ratio)"""
    from core.database import get_db
    conn = get_db()
    try:
        updates = []
        params = []
        if data.enabled is not None:
            updates.append("active = ?")
            params.append(1 if data.enabled else 0)
        if data.ratio is not None:
            updates.append("leverage_multiplier = ?")
            params.append(float(data.ratio))
        if not updates:
            return {"status": "ok", "message": "nothing to update"}
        updates.append("updated_at = datetime('now')")
        params.append(account_id)
        conn.execute(f"UPDATE copy_accounts SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.delete("/api/copy-accounts/{account_id}")
def delete_copy_account(account_id: int):
    """Delete a copy trading account"""
    from core.database import get_db
    conn = get_db()
    try:
        conn.execute("DELETE FROM copy_accounts WHERE id = ?", (account_id,))
        conn.commit()
        return {"status": "ok", "message": "跟单账户已删除"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
