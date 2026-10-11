"""XIOFlow DAG adaptive polling loop."""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger("xiorun_agent")

try:
    from agent.dag.executor import _execute_dag_run
except ImportError:
    try:
        from agent.dag.executor import _execute_dag_run
    except ImportError:
        pass


async def _dag_poll_loop() -> None:
    """Background task — poll XIOSYNC for pending DAG runs with adaptive backoff."""
    import httpx as _hx

    _XIOSYNC = os.environ.get("XIORUN_XIOSYNC_BASE", os.environ.get("XIOSYNC_BASE", ""))
    _SECRET = os.environ.get(
        "XIORUN_INTERNAL_SECRET", os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
    )

    if not _XIOSYNC or not _SECRET:
        logger.warning("dag_poll: XIORUN_XIOSYNC_BASE or XIORUN_INTERNAL_SECRET not set — disabled")
        return

    # Unified auth: send both headers for compatibility
    _HDRS = {"X-XIOSYNC-Internal": _SECRET, "X-Worker-Secret": _SECRET}
    _active: set[str] = set()
    _poll_interval = 5.0  # Start at 5s
    _MIN_POLL = 2.0  # Fast poll after claiming a run
    _MAX_POLL = 15.0  # Slow poll when idle
    _idle_ticks = 0  # Count consecutive empty polls
    logger.info(
        f"dag_poll: polling {_XIOSYNC}/api/v1/xioflow/events/runs/pending-dag-internal (adaptive {_MIN_POLL}-{_MAX_POLL}s)"
    )

    while True:
        await asyncio.sleep(_poll_interval)
        try:
            async with _hx.AsyncClient(timeout=10) as _cl:
                _resp = await _cl.get(
                    f"{_XIOSYNC}/api/v1/xioflow/events/runs/pending-dag-internal",
                    headers=_HDRS,
                )
            if _resp.status_code == 204:
                _idle_ticks += 1
                _poll_interval = min(_MAX_POLL, _poll_interval + 1.0)
                continue  # nothing pending
            if _resp.status_code != 200:
                logger.debug(f"dag_poll: status {_resp.status_code}")
                _idle_ticks += 1
                _poll_interval = min(_MAX_POLL, _poll_interval + 2.0)
                continue
            _run = _resp.json()
            _rid = _run.get("run_id")
            if not _rid or _rid in _active:
                continue
            _active.add(_rid)
            _idle_ticks = 0
            _poll_interval = _MIN_POLL  # Speed up: work available
            logger.info(
                f"dag_poll: claimed run_id={_rid} domain={_run.get('dag_domain')} intent={_run.get('dag_root_intent')}"
            )
            asyncio.create_task(
                _execute_dag_run(
                    run_id=_rid,
                    task_id=_run.get("task_id"),
                    dag_domain=_run.get("dag_domain", ""),
                    dag_root_intent=_run.get("dag_root_intent", ""),
                    context=_run.get("context", {}),
                )
            ).add_done_callback(lambda _t: _active.discard(_rid))
        except asyncio.CancelledError:
            break
        except Exception as _pe:
            logger.debug(f"dag_poll: error {_pe}")
            _poll_interval = min(_MAX_POLL, _poll_interval + 3.0)
