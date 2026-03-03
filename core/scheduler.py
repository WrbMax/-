"""
DTRS Trading Engine - Task Scheduler
Manages timing for scans, monitoring, and maintenance tasks.
Scan timing: Execute 10 seconds before each candle close.
"""

import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from config import config
from core.scanner import Scanner
from core.executor import Executor
from core.monitor import PositionMonitor
from core.database import add_log, set_state, init_db

logger = logging.getLogger("dtrs.scheduler")


class DTRSScheduler:
    """Main scheduler for all DTRS tasks"""

    def __init__(self):
        self.scanner = Scanner()
        self.executor = Executor()
        self.monitor = PositionMonitor()
        self.running = False
        self._tasks = []
        self._scan_lock = asyncio.Lock()  # Prevent concurrent async scans
        self._scan_thread_lock = threading.Lock()  # Prevent concurrent thread-pool scans

    async def start(self):
        """Start all scheduled tasks"""
        self.running = True
        init_db()

        logger.info("DTRS Scheduler starting...")
        add_log("info", "SYSTEM", "DTRS 引擎启动")
        set_state("engine_status", "running")
        set_state("engine_start_time", datetime.utcnow().isoformat())

        # Initial scan pool refresh
        self.scanner.refresh_scan_pool()

        # Start all task loops
        self._tasks = [
            asyncio.create_task(self._scan_loop_1h()),
            asyncio.create_task(self._scan_loop_4h()),
            asyncio.create_task(self._scan_loop_1d()),
            asyncio.create_task(self._monitor_loop()),
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._daily_maintenance()),
        ]

        logger.info("All task loops started")
        add_log("info", "SYSTEM", "所有定时任务已启动")

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            logger.info("Scheduler tasks cancelled")

    async def stop(self):
        """Stop all scheduled tasks"""
        self.running = False
        set_state("engine_status", "stopped")
        add_log("info", "SYSTEM", "DTRS 引擎停止")

        for task in self._tasks:
            task.cancel()

    async def _wait_until_next(self, interval_seconds: int, offset_seconds: int = 0):
        """Wait until the next interval boundary minus offset"""
        now = datetime.now(timezone.utc)
        # Calculate seconds since epoch
        epoch_seconds = now.timestamp()
        # Time until next interval
        elapsed = epoch_seconds % interval_seconds
        wait_time = interval_seconds - elapsed - offset_seconds

        if wait_time < 0:
            wait_time += interval_seconds

        logger.debug(f"Waiting {wait_time:.1f}s until next {interval_seconds}s interval")
        await asyncio.sleep(wait_time)

    async def _scan_loop_1h(self):
        """Scan 1h timeframe every hour, 10s before candle close"""
        while self.running:
            try:
                await self._wait_until_next(3600, config.system.scan_offset_seconds)
                if not self.running:
                    break
                logger.info("Running 1h scan...")
                async with self._scan_lock:  # Prevent concurrent async scans
                    if not self._scan_thread_lock.acquire(blocking=False):
                        logger.warning("1h scan skipped: another scan is already running")
                        continue
                    try:
                        signals = await asyncio.get_event_loop().run_in_executor(
                            None, self.scanner.scan_timeframe, "1h"
                        )
                        # Execute signals sequentially
                        for signal in signals:
                            if signal["status"] == "executed":
                                await asyncio.get_event_loop().run_in_executor(
                                    None, self.executor.execute_signal, signal
                                )
                    finally:
                        self._scan_thread_lock.release()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"1h scan error: {e}")
                add_log("error", "SCHEDULER", f"1h 扫描异常: {e}")
                await asyncio.sleep(60)

    async def _scan_loop_4h(self):
        """Scan 4h timeframe every 4 hours"""
        while self.running:
            try:
                await self._wait_until_next(14400, config.system.scan_offset_seconds)
                if not self.running:
                    break
                logger.info("Running 4h scan...")
                async with self._scan_lock:
                    if not self._scan_thread_lock.acquire(blocking=False):
                        logger.warning("4h scan skipped: another scan is already running")
                        continue
                    try:
                        signals = await asyncio.get_event_loop().run_in_executor(
                            None, self.scanner.scan_timeframe, "4h"
                        )
                        for signal in signals:
                            if signal["status"] == "executed":
                                await asyncio.get_event_loop().run_in_executor(
                                    None, self.executor.execute_signal, signal
                                )
                    finally:
                        self._scan_thread_lock.release()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"4h scan error: {e}")
                add_log("error", "SCHEDULER", f"4h 扫描异常: {e}")
                await asyncio.sleep(60)

    async def _scan_loop_1d(self):
        """Scan 1d timeframe once daily at UTC 00:00"""
        while self.running:
            try:
                await self._wait_until_next(86400, config.system.scan_offset_seconds)
                if not self.running:
                    break
                logger.info("Running 1d scan...")
                async with self._scan_lock:
                    if not self._scan_thread_lock.acquire(blocking=False):
                        logger.warning("1d scan skipped: another scan is already running")
                        continue
                    try:
                        signals = await asyncio.get_event_loop().run_in_executor(
                            None, self.scanner.scan_timeframe, "1d"
                        )
                        for signal in signals:
                            if signal["status"] == "executed":
                                await asyncio.get_event_loop().run_in_executor(
                                    None, self.executor.execute_signal, signal
                                )
                    finally:
                        self._scan_thread_lock.release()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"1d scan error: {e}")
                add_log("error", "SCHEDULER", f"1d 扫描异常: {e}")
                await asyncio.sleep(60)

    async def _monitor_loop(self):
        """Monitor positions every 15 minutes (EMA check interval)"""
        while self.running:
            try:
                await asyncio.sleep(config.exit.ema_check_interval_minutes * 60)
                if not self.running:
                    break
                logger.debug("Running position monitor...")
                await asyncio.get_event_loop().run_in_executor(
                    None, self.monitor.check_all_positions
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Monitor error: {e}")
                add_log("error", "SCHEDULER", f"持仓监控异常: {e}")
                await asyncio.sleep(60)

    async def _heartbeat_loop(self):
        """System heartbeat - log status periodically"""
        while self.running:
            try:
                await asyncio.sleep(config.system.heartbeat_interval)
                if not self.running:
                    break

                # Update system state
                set_state("last_heartbeat", datetime.utcnow().isoformat())

                try:
                    balance = await asyncio.get_event_loop().run_in_executor(
                        None, client.get_wallet_balance
                    )
                    set_state("wallet_balance", str(balance))
                except Exception:
                    pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
                await asyncio.sleep(60)

    async def _daily_maintenance(self):
        """Daily maintenance: refresh scan pool, cleanup old logs"""
        while self.running:
            try:
                # Wait until next UTC midnight
                now = datetime.now(timezone.utc)
                tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
                wait_seconds = (tomorrow - now).total_seconds()
                await asyncio.sleep(wait_seconds)

                if not self.running:
                    break

                logger.info("Running daily maintenance...")
                add_log("info", "SYSTEM", "开始每日维护任务")

                # Refresh scan pool
                await asyncio.get_event_loop().run_in_executor(
                    None, self.scanner.refresh_scan_pool
                )

                add_log("info", "SYSTEM", "每日维护完成")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Daily maintenance error: {e}")
                add_log("error", "SCHEDULER", f"每日维护异常: {e}")
                await asyncio.sleep(3600)


# Need to import client at module level for heartbeat
from core.binance_client import client
