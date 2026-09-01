"""Unit tests for platform.telemetry (doc 09 §6 — structured JSON logging)."""

from __future__ import annotations

import io
import json
import logging
from typing import Any

from xiosync.platform.telemetry import (
    JsonFormatter,
    bound_context,
    configure_logging,
    get_logger,
    log_context,
)


def _fresh_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel("DEBUG")
    return logger, stream


def _last_line(stream: io.StringIO) -> dict[str, Any]:
    lines = [line for line in stream.getvalue().splitlines() if line]
    parsed: dict[str, Any] = json.loads(lines[-1])
    return parsed


class TestJsonLine:
    def test_line_is_json_with_required_fields(self) -> None:
        logger, stream = _fresh_logger("t.required")
        logger.info("hello %s", "world")
        line = _last_line(stream)
        assert line["message"] == "hello world"
        assert line["level"] == "INFO"
        assert line["logger"] == "t.required"
        assert "timestamp" in line
        assert "request_id" in line  # always present, null when unbound

    def test_unbound_request_id_is_null_and_optional_ids_absent(self) -> None:
        logger, stream = _fresh_logger("t.unbound")
        logger.info("no context")
        line = _last_line(stream)
        assert line["request_id"] is None
        assert "organization_id" not in line
        assert "actor_id" not in line

    def test_extra_fields_are_included(self) -> None:
        logger, stream = _fresh_logger("t.extra")
        logger.info("evt", extra={"workflow_run_id": "wr-1"})
        assert _last_line(stream)["workflow_run_id"] == "wr-1"

    def test_exception_is_rendered(self) -> None:
        logger, stream = _fresh_logger("t.exc")
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("failed")
        line = _last_line(stream)
        assert "ValueError: boom" in line["exception"]


class TestCorrelationContext:
    def test_bound_ids_appear_on_lines(self) -> None:
        logger, stream = _fresh_logger("t.bound")
        with bound_context(request_id="req-1", organization_id="org-1", actor_id="act-1"):
            logger.info("inside")
        line = _last_line(stream)
        assert line["request_id"] == "req-1"
        assert line["organization_id"] == "org-1"
        assert line["actor_id"] == "act-1"

    def test_context_restores_on_exit_and_nests(self) -> None:
        with bound_context(request_id="outer"):
            with bound_context(request_id="inner", actor_id="act-9"):
                assert log_context()["request_id"] == "inner"
                assert log_context()["actor_id"] == "act-9"
            assert log_context()["request_id"] == "outer"
            assert log_context()["actor_id"] is None
        assert log_context()["request_id"] is None


class TestSecretRedaction:
    def test_secret_like_extra_keys_are_redacted(self) -> None:
        logger, stream = _fresh_logger("t.secrets")
        logger.info(
            "login",
            extra={
                "session_token": "tok-abc",
                "db_password": "hunter2",
                "authorization": "Bearer xyz",
                "plain_field": "visible",
            },
        )
        line = _last_line(stream)
        assert line["session_token"] == "[REDACTED]"
        assert line["db_password"] == "[REDACTED]"
        assert line["authorization"] == "[REDACTED]"
        assert line["plain_field"] == "visible"
        assert "tok-abc" not in stream.getvalue()


class TestConfigureLogging:
    def test_configure_replaces_handlers_and_sets_level(self) -> None:
        root = logging.getLogger("t.configure.root")
        root.addHandler(logging.NullHandler())
        stream = io.StringIO()
        configure_logging("WARNING", stream=stream, root=root)
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert root.level == logging.WARNING
        root.warning("warned")
        assert _last_line(stream)["message"] == "warned"

    def test_get_logger_returns_named_logger(self) -> None:
        assert get_logger("t.named").name == "t.named"
