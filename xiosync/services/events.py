"""Event use cases — append-only writes with a canonical structure (doc 03 §2.8).

``EventService`` is the sanctioned way to append to the ``events`` table. It
validates the ``event_type`` against the known vocabulary (``domain/events``)
and writes exactly one row per call; it never updates or deletes, mirroring the
database's own guarantee — the revision-0004 append-only trigger and
INSERT/SELECT grants make destructive paths impossible at the DB (INV-EVENT-1),
and this service never even attempts one.

The caller owns the transaction (via ``org_scoped_session``); every write here
flushes within it so an Event and the Operation it accompanies (INV-LC-2) commit
or roll back atomically together. Reads return frozen ``EventRecord`` values so
the service layer holds no live ORM state.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.domain.events import (
    STATE_CHANGE,
    InvalidEventTypeError,
    build_state_change_payload,
    extract_routing_fields,
    validate_event_type,
)
from xiosync.persistence.models.authorization import Event
from xiosync.platform.ids import new_id

__all__ = ["EventRecord", "EventService", "InvalidEventTypeError"]


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One ``events`` row, detached from the ORM."""

    id: uuid.UUID
    organization_id: uuid.UUID
    actor_id: uuid.UUID | None
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
    # Gap R-4: first-class routing columns.
    severity: str
    correlation_id: uuid.UUID | None
    operation_id: uuid.UUID | None
    entity_type: str | None
    entity_id: uuid.UUID | None


def _record(row: Event) -> EventRecord:
    return EventRecord(
        id=row.id,
        organization_id=row.organization_id,
        actor_id=row.actor_id,
        event_type=row.event_type,
        payload=row.payload,
        created_at=row.created_at,
        severity=row.severity,
        correlation_id=row.correlation_id,
        operation_id=row.operation_id,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
    )


class EventService:
    """Append-only use-case orchestration for Events (doc 04 §2.1)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        context: OrgContext,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        actor_id: uuid.UUID | None = None,
        severity: str | None = None,
        correlation_id: uuid.UUID | None = None,
        operation_id: uuid.UUID | None = None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Append one Event, returning its id.

        The ``event_type`` is validated against the known vocabulary before the
        write so a bogus type is rejected with a clean domain error rather than
        polluting the append-only stream (INV-TYPE-2). ``organization_id`` comes
        from the context, never the caller, so an Event cannot be misattributed.

        Gap R-4: routing columns (severity, correlation_id, operation_id,
        entity_type, entity_id) can be passed explicitly. When not provided,
        they are auto-extracted from the payload dict if present.
        """
        validate_event_type(event_type)

        # M-1: quota enforcement — check daily event limit before appending.
        from xiosync.services.quotas import QuotaService
        QuotaService(self._session).check_daily_events(context.organization_id)

        event_id = new_id()

        # Gap R-4: auto-extract routing fields from payload when not explicit.
        routing = extract_routing_fields(dict(payload))
        effective_severity = severity or routing.get("severity", "info")
        effective_correlation_id = correlation_id or routing.get("correlation_id")
        effective_operation_id = operation_id or routing.get("operation_id")
        effective_entity_type = entity_type or routing.get("entity_type")
        effective_entity_id = entity_id or routing.get("entity_id")

        self._session.add(
            Event(
                id=event_id,
                organization_id=context.organization_id,
                actor_id=actor_id,
                event_type=event_type,
                payload=dict(payload),
                severity=effective_severity,
                correlation_id=effective_correlation_id,
                operation_id=effective_operation_id,
                entity_type=effective_entity_type,
                entity_id=effective_entity_id,
            )
        )
        self._session.flush()

        # M-3: metering instrumentation — record event_count metric.
        try:
            from xiosync.services.metering import MeteringService
            MeteringService(self._session).record_usage(
                context, metric_type="event_count",
            )
        except Exception:
            pass  # Metering is best-effort; never fail the event append.

        # R-2: webhook dispatch — find matching subscriptions and log deliveries.
        try:
            from xiosync.services.webhooks import WebhookService, sign_payload as _sign
            wh_svc = WebhookService(self._session)
            matching = wh_svc.get_matching_subscriptions(
                context, event_type=event_type,
            )
            for sub in matching:
                dispatch_payload = {
                    "event_type": event_type,
                    "event_id": str(event_id),
                    "payload": dict(payload),
                }
                signature = _sign(sub.signing_secret, dispatch_payload)
                # Record dispatch intent as an event for the background worker to pick up.
                self._session.add(
                    Event(
                        id=new_id(),
                        organization_id=context.organization_id,
                        actor_id=None,
                        event_type="webhook.dispatch",
                        payload={
                            "subscription_id": str(sub.id),
                            "target_url": sub.url,
                            "source_event_id": str(event_id),
                            "signature": signature,
                        },
                        severity="info",
                        entity_type="webhook_subscription",
                        entity_id=sub.id,
                    )
                )
            if matching:
                self._session.flush()
        except Exception:
            pass  # Webhook dispatch is best-effort; never fail the event append.

        return event_id

    def append_state_change(
        self,
        context: OrgContext,
        *,
        actor_id: uuid.UUID,
        from_state: str,
        to_state: str,
        operation_id: uuid.UUID,
        trigger: str,
        entity_type: str = "actor",
        rationale: str | None = None,
    ) -> uuid.UUID:
        """Append the ``state_change`` Event that accompanies a transition.

        This is the single call site's shortcut for the lifecycle path
        (INV-LC-2): it builds the canonical payload (``domain/events``) tying the
        Event to its Operation, then appends it.
        """
        payload = build_state_change_payload(
            entity_type=entity_type,
            entity_id=actor_id,
            from_state=from_state,
            to_state=to_state,
            operation_id=operation_id,
            trigger=trigger,
            rationale=rationale,
        )
        return self.append(
            context,
            event_type=STATE_CHANGE,
            payload=payload,
            actor_id=actor_id,
        )

    def get_event(self, context: OrgContext, event_id: uuid.UUID) -> EventRecord | None:
        """Fetch one Event in this org, or ``None`` if it does not exist."""
        row = self._session.scalar(
            select(Event).where(
                Event.organization_id == context.organization_id,
                Event.id == event_id,
            )
        )
        return None if row is None else _record(row)

    def list_by_type(self, context: OrgContext, event_type: str) -> list[EventRecord]:
        """All Events of a given type in this org, oldest first."""
        rows = self._session.scalars(
            select(Event)
            .where(
                Event.organization_id == context.organization_id,
                Event.event_type == event_type,
            )
            .order_by(Event.created_at)
        ).all()
        return [_record(row) for row in rows]
