"""Structured JSON logging with request-scoped correlation context.

Normative sources: doc 09 §6 (M7 remediation — structured logging), doc 04
§2.1/§6 (platform layer), GLOSSARY (canonical terms: ``request_id``,
``organization_id``, ``actor_id``).

Rules enforced here:
- Every log line is a single JSON object containing ``timestamp``, ``level``,
  ``logger``, ``message``, and ``request_id`` plus ``organization_id`` /
  ``actor_id`` when resolved (doc 09 §6).
- Correlation identifiers live in ``contextvars`` so they propagate across
  async task boundaries without threading parameters through every call.
- No secrets or full tokens in logs: extra fields whose keys look like
  secrets are redacted defensively (M2 / doc 09 §6).
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_REDACTED = "[REDACTED]"

_SECRET_KEY_MARKERS = ("token", "secret", "password", "authorization", "api_key", "cookie")

_RESERVED_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
    | {"message", "asctime", "taskName"}
)

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
organization_id_var: ContextVar[str | None] = ContextVar("organization_id", default=None)
actor_id_var: ContextVar[str | None] = ContextVar("actor_id", default=None)


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_KEY_MARKERS)


@contextmanager
def bound_context(
    *,
    request_id: str | None = None,
    organization_id: str | None = None,
    actor_id: str | None = None,
) -> Iterator[None]:
    """Bind correlation identifiers for the duration of the ``with`` block.

    Only identifiers passed explicitly are (re)bound; the previous values are
    restored on exit, so nested scopes compose correctly.
    """
    tokens = []
    if request_id is not None:
        tokens.append((request_id_var, request_id_var.set(request_id)))
    if organization_id is not None:
        tokens.append((organization_id_var, organization_id_var.set(organization_id)))
    if actor_id is not None:
        tokens.append((actor_id_var, actor_id_var.set(actor_id)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


class JsonFormatter(logging.Formatter):
    """Render every record as one JSON object per line (doc 09 §6)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        organization_id = organization_id_var.get()
        if organization_id is not None:
            payload["organization_id"] = organization_id
        actor_id = actor_id_var.get()
        if actor_id is not None:
            payload["actor_id"] = actor_id
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key in payload:
                continue
            payload[key] = _REDACTED if _is_secret_key(key) else value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(
    log_level: str, *, stream: Any | None = None, root: logging.Logger | None = None
) -> None:
    """Install the JSON formatter on the root logger, replacing any handlers.

    ``log_level`` must already be validated (``platform.config.load_config``
    is the only sanctioned source — INV-CFG-1).
    """
    target = logging.getLogger() if root is None else root
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter())
    for existing in list(target.handlers):
        target.removeHandler(existing)
    target.addHandler(handler)
    target.setLevel(log_level)


def get_logger(name: str) -> logging.Logger:
    """Return the named logger; a thin seam so call sites avoid raw logging."""
    return logging.getLogger(name)


def log_context() -> Mapping[str, str | None]:
    """Snapshot the currently bound correlation identifiers (for tests/debug)."""
    return {
        "request_id": request_id_var.get(),
        "organization_id": organization_id_var.get(),
        "actor_id": actor_id_var.get(),
    }
