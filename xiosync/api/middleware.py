"""Fail-closed HTTP middleware for correlation, hardening, and tenancy."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from contextlib import AbstractContextManager
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as OrmSession
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from xiosync.persistence.tenancy import org_scoped_session
from xiosync.platform.clock import Clock
from xiosync.platform.telemetry import bound_context
from xiosync.services.identity import AuthenticationError, SessionService

DEFAULT_MAX_BODY_BYTES = 1_048_576
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_PUBLIC_AUTH_PATHS = frozenset({"/api/v1/auth/login", "/api/v1/auth/refresh"})


def _problem(status: int, code: str, title: str, request_id: str) -> dict[str, Any]:
    return {
        "type": f"https://xiosync.dev/problems/{code}",
        "title": title,
        "status": status,
        "code": code,
        "request_id": request_id,
    }


async def _send_problem(send: Send, status: int, code: str, title: str, request_id: str) -> None:
    import json

    body = json.dumps(_problem(status, code, title, request_id), separators=(",", ":")).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RequestIDMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = Headers(scope=scope).get("x-request-id", "")
        request_id = supplied if _REQUEST_ID_PATTERN.fullmatch(supplied) else str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
            await send(message)

        with bound_context(request_id=request_id):
            await self.app(scope, receive, send_with_id)


class SecurityHeadersMiddleware:
    _HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Cache-Control": "no-store",
    }

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_hardened(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in self._HEADERS.items():
                    headers[key] = value
            await send(message)

        await self.app(scope, receive, send_hardened)


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = scope.get("state", {}).get("request_id", "unknown")
        raw_length = Headers(scope=scope).get("content-length")
        try:
            declared = int(raw_length) if raw_length is not None else None
        except ValueError:
            declared = None
        if declared is not None and declared > self.max_body_bytes:
            await _send_problem(send, 413, "body_too_large", "Request body too large", request_id)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _BodyTooLargeError
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLargeError:
            await _send_problem(send, 413, "body_too_large", "Request body too large", request_id)


class _BodyTooLargeError(Exception):
    pass


class AuthenticationMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        session_service: SessionService,
        engine: Engine,
        clock: Clock,
        public_paths: Iterable[str] = _PUBLIC_AUTH_PATHS,
    ) -> None:
        self.app = app
        self.session_service = session_service
        self.engine = engine
        self.clock = clock
        self.public_paths = frozenset(public_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.public_paths:
            await self.app(scope, receive, send)
            return
        request_id = scope.get("state", {}).get("request_id", "unknown")
        authorization = Headers(scope=scope).get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            await _send_problem(
                send, 401, "authentication_failed", "Authentication failed", request_id
            )
            return
        try:
            context = self.session_service.validate_access_token(
                token.strip(), now=self.clock.now()
            )
        except AuthenticationError:
            await _send_problem(
                send, 401, "authentication_failed", "Authentication failed", request_id
            )
            return

        scope.setdefault("state", {})["org_context"] = context
        manager: AbstractContextManager[OrmSession] = org_scoped_session(self.engine, context)
        with (
            bound_context(
                organization_id=str(context.organization_id), actor_id=str(context.actor_id)
            ),
            manager as session,
        ):
            scope["state"]["org_session"] = session
            await self.app(scope, receive, send)
