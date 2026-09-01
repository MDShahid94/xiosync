from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

import redis

logger = logging.getLogger(__name__)


class RateLimiterNotAvailable(RuntimeError):  # noqa: N818 — public API name is normative
    """Raised when the shared Redis rate-limit store cannot be reached."""


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    reset_after_seconds: int


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    limit: int
    window_seconds: int


def get_rate_limit_config(route_class: str, config: Any) -> RateLimitConfig:
    if route_class == "auth":
        return RateLimitConfig(config.rate_limit_auth_limit, config.rate_limit_window_seconds)
    return RateLimitConfig(config.rate_limit_api_limit, config.rate_limit_window_seconds)


class RateLimiter:
    def __init__(self, client: redis.Redis) -> None:
        self.client = client

    def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("limit and window_seconds must be positive")
        try:
            with self.client.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, window_seconds)
                count, _ = pipe.execute()
                ttl = self.client.ttl(key)
        except (redis.RedisError, OSError) as exc:
            raise RateLimiterNotAvailable from exc
        count_int = cast(int, count)
        reset = max(cast(int, ttl), 0)
        result = RateLimitResult(count_int <= limit, max(limit - count_int, 0), reset)
        logger.info(
            "rate_limit_decision",
            extra={"allowed": result.allowed, "key": key, "remaining": result.remaining},
        )
        return result


def create_rate_limiter(redis_url: str) -> RateLimiter:
    return RateLimiter(redis.Redis.from_url(redis_url, decode_responses=True))


def close_rate_limiter(rate_limiter: RateLimiter) -> None:
    rate_limiter.client.close()
