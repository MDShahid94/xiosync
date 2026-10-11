"""Retry backoff for DOM actions during DAG execution."""

import asyncio
import logging

logger = logging.getLogger("xiorun_agent")


# ── Retry Backoff for DOM Actions (Phase 4 audit — Fix 26) ───────────────────
async def _dom_retry_backoff(
    coro_fn, *, max_retries: int = 3, base_delay: float = 1.0, label: str = "dom_action"
):
    """Execute a DOM action coroutine with exponential backoff retry.

    Args:
        coro_fn: Async callable (no args) returning the result.
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay in seconds (doubles each retry).
        label: Human-readable label for logging.

    Raises the last exception if all retries fail.
    """
    _delay = base_delay
    for _attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except Exception as _e:
            if _attempt == max_retries:
                raise
            logger.debug(f"{label}: attempt {_attempt + 1} failed ({_e}) — retrying in {_delay}s")
            await asyncio.sleep(_delay)
            _delay = min(_delay * 2, 10.0)
