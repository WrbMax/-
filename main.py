"""
DTRS Trading Engine - Main Entry Point
Starts both the FastAPI server and the trading scheduler.
"""

import asyncio
import logging
import sys
import os
import signal
import uvicorn
from threading import Thread

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import config
from core.database import init_db
from core.scheduler import DTRSScheduler
from api.routes import app

# Configure logging
logging.basicConfig(
    level=getattr(logging, config.system.log_level),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/dtrs.log", mode="a"),
    ]
)
logger = logging.getLogger("dtrs.main")


scheduler = DTRSScheduler()
_scheduler_started = False


@app.on_event("startup")
async def startup_event():
    """Start the scheduler when FastAPI starts"""
    global _scheduler_started
    if _scheduler_started:
        logger.warning("Scheduler already started, skipping duplicate startup")
        return
    _scheduler_started = True
    logger.info("DTRS Trading Engine starting...")
    init_db()
    # Start scheduler in background
    asyncio.create_task(scheduler.start())


@app.on_event("shutdown")
async def shutdown_event():
    """Stop the scheduler when FastAPI shuts down"""
    logger.info("DTRS Trading Engine shutting down...")
    await scheduler.stop()


def main():
    """Main entry point"""
    os.makedirs("logs", exist_ok=True)
    os.makedirs("data", exist_ok=True)

    logger.info(f"Starting DTRS Engine on {config.system.api_host}:{config.system.api_port}")
    logger.info(f"Binance Testnet: {config.binance.testnet}")
    logger.info(f"Leverage: {config.margin.leverage}x")
    logger.info(f"Scan Scope: {config.scan.scan_scope}")

    uvicorn.run(
        "main:app",
        host=config.system.api_host,
        port=config.system.api_port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
