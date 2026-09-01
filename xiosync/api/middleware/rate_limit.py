from __future__ import annotations

import json
import logging
from collections.abc import Callable

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from xiosync.core.rate_limit import RateLimitConfig, RateLimiter, RateLimiterNotAvailable

logger = logging.getLogger(__name__)
ConfigFn = Callable[[str], RateLimitConfig]


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, rate_limiter: RateLimiter, config_fn: ConfigFn) -> None:
        self.app = app
        self.rate_limiter = rate_limiter
        self.config_fn = config_fn

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in {"/live", "/ready"}:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        route_class = "auth" if "/auth/" in path else "api"
        config = self.config_fn(route_class)
        context = scope.get("state", {}).get("org_context")
        identity = (
            f"org:{context.organization_id}"
            if context is not None
            else f"ip:{self._client_ip(scope)}"
        )
        try:
            result = self.rate_limiter.check(
                f"xiosync:rate:{route_class}:{identity}",
                config.limit,
                config.window_seconds,
            )
        except RateLimiterNotAvailable:
            logger.warning("rate_limit_degraded", extra={"key": identity})
            await self.app(scope, receive, send)
            return
        if not result.allowed:
            body = json.dumps(
                {"detail": "rate_limit_exceeded", "retry_after": result.reset_after_seconds}
            ).encode()
            headers = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"retry-after", str(result.reset_after_seconds).encode()),
                (b"x-ratelimit-limit", str(config.limit).encode()),
                (b"x-ratelimit-remaining", b"0"),
                (b"x-ratelimit-reset", str(result.reset_after_seconds).encode()),
            ]
            await send({"type": "http.response.start", "status": 429, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-RateLimit-Limit"] = str(config.limit)
                headers["X-RateLimit-Remaining"] = str(result.remaining)
                headers["X-RateLimit-Reset"] = str(result.reset_after_seconds)
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    def _client_ip(scope: Scope) -> str:
        client = scope.get("client")
        return str(client[0]) if client else "unknown"
