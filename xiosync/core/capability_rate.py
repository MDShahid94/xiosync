"""Per-capability rate checking for authorization constraint evaluation.

Bridges the domain-layer ``RateChecker`` protocol with the infrastructure-layer
``RateLimiter`` (Redis sliding-window counter).

Usage::

    from xiosync.core.capability_rate import build_rate_checker

    checker = build_rate_checker(rate_limiter, actor_id, capability)
    # checker is a Callable[[Mapping[str, Any]], bool]

    # The authorize() domain function passes the rate constraint config:
    #   {"limit": 100, "window_seconds": 60}
    decision = authorize(..., rate_checker=checker)
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

from xiosync.core.rate_limit import RateLimiter, RateLimiterNotAvailable
from xiosync.domain.authorization import RateChecker

__all__ = ["build_rate_checker"]

logger = logging.getLogger(__name__)


def build_rate_checker(
    rate_limiter: RateLimiter,
    actor_id: uuid.UUID,
    capability: str,
) -> RateChecker:
    """Create a per-actor, per-capability rate checker closure.

    The returned callable matches the ``RateChecker`` signature:
    ``(Mapping[str, Any]) -> bool``.

    The constraint config dict is expected to have:
    - ``limit`` (int): Maximum invocations allowed in the window.
    - ``window_seconds`` (int): Sliding window size in seconds.

    Returns ``True`` if under the rate limit, ``False`` if exceeded.
    On Redis errors, returns ``False`` (fail-closed).
    """

    def _check(config: Mapping[str, Any]) -> bool:
        limit = config.get("limit")
        window = config.get("window_seconds")

        if not isinstance(limit, int) or not isinstance(window, int):
            logger.warning(
                "rate_constraint_invalid_config",
                extra={"actor_id": str(actor_id), "capability": capability, "config": dict(config)},
            )
            return False

        if limit <= 0 or window <= 0:
            return False

        key = f"xiosync:rate:{actor_id}:{capability}"

        try:
            result = rate_limiter.check(key, limit, window)
            return result.allowed
        except RateLimiterNotAvailable:
            logger.warning(
                "rate_checker_redis_unavailable",
                extra={"actor_id": str(actor_id), "capability": capability},
            )
            return False  # Fail-closed: deny if Redis is down.

    return _check
