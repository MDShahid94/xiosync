"""XIOSYNC background worker entrypoint.

Usage::

    python -m xiosync.worker.main                    # Run all loops
    python -m xiosync.worker.main --mode ticker      # Cron trigger ticker only
    python -m xiosync.worker.main --mode reaper      # Lease reaper only
    python -m xiosync.worker.main --mode dispatcher   # Webhook dispatcher only
    python -m xiosync.worker.main --mode events       # Event trigger evaluator only

Environment variables::

    DATABASE_URL          — PostgreSQL connection string (required)
    WORKER_TICKER_INTERVAL   — Cron ticker poll interval in seconds (default: 15)
    WORKER_REAPER_INTERVAL   — Lease reaper interval in seconds (default: 30)
    WORKER_DISPATCH_INTERVAL — Webhook dispatch interval in seconds (default: 10)
    WORKER_EVENT_INTERVAL    — Event trigger evaluation interval in seconds (default: 5)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from typing import Callable

from sqlalchemy import Engine
from sqlalchemy.orm import Session as OrmSession

from xiosync.persistence.database import create_database_engine
from xiosync.platform.telemetry import configure_logging

logger = logging.getLogger("xiosync.worker")

# Graceful shutdown flag.
_shutdown = threading.Event()


def _signal_handler(signum: int, frame: object) -> None:
    logger.info("shutdown_signal_received", extra={"signal": signum})
    _shutdown.set()


def _run_loop(
    name: str,
    fn: Callable[[OrmSession], int],
    engine: Engine,
    interval: float,
) -> None:
    """Run a worker function in a loop until shutdown is signaled."""
    logger.info(f"worker_loop_started", extra={"loop": name, "interval": interval})
    while not _shutdown.is_set():
        try:
            with OrmSession(engine) as session:
                count = fn(session)
                if count > 0:
                    logger.debug(
                        "worker_loop_tick",
                        extra={"loop": name, "processed": count},
                    )
        except Exception:
            logger.exception("worker_loop_error", extra={"loop": name})

        _shutdown.wait(timeout=interval)

    logger.info("worker_loop_stopped", extra={"loop": name})


def main() -> None:
    """Parse args and launch worker loops."""
    parser = argparse.ArgumentParser(
        description="XIOSYNC background worker",
        prog="python -m xiosync.worker.main",
    )
    parser.add_argument(
        "--mode",
        choices=["all", "ticker", "reaper", "dispatcher", "run-dispatcher", "events"],
        default="all",
        help="Which worker loop(s) to run (default: all)",
    )
    args = parser.parse_args()

    # Setup.
    configure_logging("INFO")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    engine = create_database_engine(database_url)
    logger.info(
        "worker_starting",
        extra={"mode": args.mode, "database": database_url.split("@")[-1]},
    )

    # Register signal handlers for graceful shutdown.
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Read intervals from environment.
    ticker_interval = float(os.environ.get("WORKER_TICKER_INTERVAL", "15"))
    reaper_interval = float(os.environ.get("WORKER_REAPER_INTERVAL", "30"))
    dispatch_interval = float(os.environ.get("WORKER_DISPATCH_INTERVAL", "10"))
    event_interval = float(os.environ.get("WORKER_EVENT_INTERVAL", "5"))

    # Import worker functions.
    from xiosync.worker.ticker import tick_cron_triggers
    from xiosync.worker.reaper import reap_expired_leases
    from xiosync.worker.webhook_dispatcher import dispatch_pending_webhooks
    from xiosync.worker.event_router import evaluate_event_triggers
    from xiosync.worker.run_dispatcher import dispatch_pending_runs
    from xiosync.worker.pppoe_health import tick_pppoe_health
    from xiosync.worker.pppoe_warmpool import tick_pppoe_warmpool
    from xiosync.subsystems.xiorun.health_loop import tick_xiorun_health

    # PPPoE health check interval (default 60s)
    pppoe_health_interval = float(os.environ.get("WORKER_PPPOE_HEALTH_INTERVAL", "60"))
    # Warm pool top-up interval (default 120s — slower, each provision takes ~30s)
    pppoe_warmpool_interval = float(os.environ.get("WORKER_PPPOE_WARMPOOL_INTERVAL", "120"))
    # XIORUN browser health check interval (default 30s)
    xiorun_health_interval = float(os.environ.get("WORKER_XIORUN_HEALTH_INTERVAL", "30"))

    # Build the set of loops to run.
    loops: list[tuple[str, Callable[[OrmSession], int], float]] = []
    if args.mode in ("all", "ticker"):
        loops.append(("ticker", tick_cron_triggers, ticker_interval))
    if args.mode in ("all", "reaper"):
        loops.append(("reaper", reap_expired_leases, reaper_interval))
    if args.mode in ("all", "dispatcher"):
        loops.append(("dispatcher", dispatch_pending_webhooks, dispatch_interval))
    if args.mode in ("all", "events"):
        loops.append(("events", evaluate_event_triggers, event_interval))
    # PPPoE health check + warm pool — always enabled in "all" mode
    if args.mode in ("all", "run-dispatcher"):
        run_dispatch_interval = float(os.environ.get("WORKER_RUN_DISPATCH_INTERVAL", "5"))
        loops.append(("run-dispatcher", dispatch_pending_runs, run_dispatch_interval))
    if args.mode in ("all",):
        loops.append(("pppoe-health",   tick_pppoe_health,   pppoe_health_interval))
        loops.append(("pppoe-warmpool", tick_pppoe_warmpool, pppoe_warmpool_interval))
        loops.append(("xiorun-health",  tick_xiorun_health,  xiorun_health_interval))

    if not loops:
        logger.error("no_loops_selected")
        sys.exit(1)

    # Launch each loop in a daemon thread (except the last which runs in main).
    threads: list[threading.Thread] = []
    for name, fn, interval in loops[:-1]:
        t = threading.Thread(
            target=_run_loop,
            args=(name, fn, engine, interval),
            name=f"xiosync-{name}",
            daemon=True,
        )
        t.start()
        threads.append(t)

    # Run the last loop in the main thread (blocks until shutdown).
    last_name, last_fn, last_interval = loops[-1]
    _run_loop(last_name, last_fn, engine, last_interval)

    # Wait for daemon threads to finish.
    for t in threads:
        t.join(timeout=5)

    # Cleanup.
    engine.dispose()
    logger.info("worker_stopped")


if __name__ == "__main__":
    main()
