"""Pure Event semantics — append-only provenance structure (doc 03 §2.8).

An Event is XIOSYNC's append-only telemetry/audit record. This module is pure
domain (RULE-ARCH-1): no I/O, no framework imports. It fixes two things the
service and persistence layers depend on:

* **The canonical event vocabulary and severities** — the bootstrap
  ``event_type`` set (doc 03 §2.8). The Type Registry is authoritative at
  runtime (doc 03 §8), but this module seeds the closed set a fresh install
  starts from and lets the service reject an obviously bogus type before it
  reaches the (append-only) table.
* **The canonical payload shape** for structured events. Because the ``events``
  table stores structured detail in a single ``payload`` JSONB column, the
  *structure* of that column is what makes an Event well formed (INV-EVENT-1);
  ``build_state_change_payload`` produces the one true shape for the
  ``state_change`` events that every lifecycle transition emits (INV-LC-2), so
  no two call sites invent their own layout.

Immutability itself (no UPDATE/DELETE path) is enforced at the database by the
revision-0004 append-only trigger and INSERT/SELECT grants — this module only
governs what a *valid new* Event looks like.
"""

from __future__ import annotations

import uuid
from typing import Any

__all__ = [
    "EVENT_TYPES",
    "SEVERITIES",
    "InvalidEventError",
    "InvalidEventTypeError",
    "InvalidSeverityError",
    "build_state_change_payload",
    "extract_routing_fields",
    "validate_event_type",
    "validate_severity",
]

# -- Bootstrap event vocabulary (doc 03 §2.8) ------------------------------
ACTION_EXECUTED = "action_executed"
ERROR = "error"
STATE_CHANGE = "state_change"
HEARTBEAT = "heartbeat"
TOOL_INVOKED = "tool_invoked"
AUTH_EVENT = "auth_event"
METRIC = "metric"
POLICY_DECISION = "policy_decision"

#: The closed ``event_type`` set a fresh install seeds the registry with. At
#: runtime the Type Registry is authoritative (doc 03 §8); this is the seed.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        ACTION_EXECUTED,
        ERROR,
        STATE_CHANGE,
        HEARTBEAT,
        TOOL_INVOKED,
        AUTH_EVENT,
        METRIC,
        POLICY_DECISION,
        # Wave 3 — platform event types
        "task.output",         # R-3: streaming task output chunks
        "webhook.dispatch",    # R-2: webhook delivery intent
        "webhook.delivered",   # R-2: successful webhook delivery
        "webhook.failed",      # R-2: failed webhook delivery
    }
)

# -- Severity ladder (doc 03 §2.8) -----------------------------------------
DEBUG = "debug"
INFO = "info"
WARN = "warn"
SEVERITY_ERROR = "error"
CRITICAL = "critical"

#: The closed severity set for structured event payloads.
SEVERITIES: frozenset[str] = frozenset({DEBUG, INFO, WARN, SEVERITY_ERROR, CRITICAL})


class InvalidEventError(ValueError):
    """Base class for a malformed Event caught before it reaches the table."""


class InvalidEventTypeError(InvalidEventError):
    """The ``event_type`` is not part of the known vocabulary (doc 03 §2.8)."""

    def __init__(self, event_type: str) -> None:
        super().__init__(
            f"event_type {event_type!r} is not a known event type; "
            "register it in the Type Registry before use (INV-TYPE-2)"
        )
        self.event_type = event_type


class InvalidSeverityError(InvalidEventError):
    """The severity is not one of the declared levels (doc 03 §2.8)."""

    def __init__(self, severity: str) -> None:
        super().__init__(
            f"severity {severity!r} is not one of {sorted(SEVERITIES)}"
        )
        self.severity = severity


def validate_event_type(event_type: str) -> str:
    """Return ``event_type`` if it is a known type, else raise.

    This is the cheap, always-available guard on the bootstrap vocabulary; the
    Type Registry remains the runtime authority (doc 03 §8) for tenant/plugin
    namespaces.
    """
    if event_type not in EVENT_TYPES:
        raise InvalidEventTypeError(event_type)
    return event_type


def validate_severity(severity: str) -> str:
    """Return ``severity`` if it is a declared level, else raise."""
    if severity not in SEVERITIES:
        raise InvalidSeverityError(severity)
    return severity


def build_state_change_payload(
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    from_state: str,
    to_state: str,
    operation_id: uuid.UUID,
    trigger: str,
    rationale: str | None = None,
    severity: str = INFO,
) -> dict[str, Any]:
    """Build the canonical payload for a ``state_change`` Event (INV-EVENT-1).

    Every lifecycle transition emits exactly this structure (INV-LC-2) so the
    audit stream is uniform: a human-readable ``summary``, the ``severity``, the
    subject (``entity_type``/``entity_id``), the ``from_state``/``to_state``
    pair, the ``operation_id`` that ties the Event back to its Operation, and
    the ``trigger`` that caused it. ``entity_id``/``operation_id`` are stringified
    because the value lands in JSONB.
    """
    validate_severity(severity)
    payload: dict[str, Any] = {
        "summary": (
            f"{entity_type} {entity_id} transitioned from {from_state} to {to_state}"
        ),
        "severity": severity,
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "from_state": from_state,
        "to_state": to_state,
        "operation_id": str(operation_id),
        "trigger": trigger,
    }
    if rationale is not None:
        payload["rationale"] = rationale
    return payload


def extract_routing_fields(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Extract R-4 top-level routing fields from a payload dict.

    Gap R-4: the service layer calls this to populate the first-class indexed
    columns (severity, correlation_id, operation_id, entity_type, entity_id)
    from a payload that may contain them.  Returns only non-None fields.

    >>> extract_routing_fields({"severity": "warn", "entity_type": "task"})
    {'severity': 'warn', 'entity_type': 'task'}
    """
    routing: dict[str, Any] = {}
    if "severity" in payload:
        routing["severity"] = payload["severity"]
    if "operation_id" in payload:
        val = payload["operation_id"]
        routing["operation_id"] = uuid.UUID(val) if isinstance(val, str) else val
    if "entity_type" in payload:
        routing["entity_type"] = payload["entity_type"]
    if "entity_id" in payload:
        val = payload["entity_id"]
        routing["entity_id"] = uuid.UUID(val) if isinstance(val, str) else val
    if "correlation_id" in payload:
        val = payload["correlation_id"]
        routing["correlation_id"] = uuid.UUID(val) if isinstance(val, str) else val
    return routing
