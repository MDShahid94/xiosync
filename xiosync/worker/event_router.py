"""Event trigger evaluator — matches new events against event-type triggers.

Listens for newly appended events and evaluates them against active
event-type triggers. When a match is found, creates a workflow run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from xiosync.worker.context import system_context
from xiosync.persistence.models.authorization import Event
from xiosync.persistence.models.identity import Organization
from xiosync.services.triggers import TriggerService
from xiosync.services.workflows import WorkflowService

logger = logging.getLogger("xiosync.worker.event_router")

# Track the last processed event timestamp to avoid reprocessing.
_last_processed_at: datetime | None = None


def evaluate_event_triggers(session: Session, *, limit: int = 100) -> int:
    """Match recent events against event-type triggers. Returns runs created."""
    global _last_processed_at

    now = datetime.now(timezone.utc)
    if _last_processed_at is None:
        # On first run, only process events from the last 60 seconds.
        from datetime import timedelta
        _last_processed_at = now - timedelta(seconds=60)

    # Find events created since our last check, excluding system events.
    stmt = (
        select(Event)
        .where(
            Event.created_at > _last_processed_at,
            Event.event_type.notin_(["webhook.dispatch", "webhook.delivered", "webhook.failed"]),
        )
        .order_by(Event.created_at.asc())
        .limit(limit)
    )
    events = list(session.scalars(stmt).all())

    if not events:
        return 0

    runs_created = 0
    for event in events:
        try:
            context = system_context(event.organization_id)
            trigger_svc = TriggerService(session)

            matching = trigger_svc.evaluate_event_triggers(
                context,
                event_type=event.event_type,
                event_payload=event.payload,
            )

            for trigger in matching:
                try:
                    wf_svc = WorkflowService(session)
                    run_id = wf_svc.start_run(
                        context,
                        trigger.workflow_id,
                        initiated_by=event.actor_id or event.organization_id,
                    )
                    runs_created += 1
                    logger.info(
                        "event_trigger_fired",
                        extra={
                            "trigger_id": str(trigger.id),
                            "event_id": str(event.id),
                            "event_type": event.event_type,
                            "run_id": str(run_id),
                        },
                    )
                except Exception:
                    logger.exception(
                        "event_trigger_run_failed",
                        extra={
                            "trigger_id": str(trigger.id),
                            "event_id": str(event.id),
                        },
                    )
        except Exception:
            logger.exception(
                "event_trigger_eval_failed",
                extra={"event_id": str(event.id)},
            )

    # Update watermark to the latest event we processed.
    _last_processed_at = events[-1].created_at

    if runs_created:
        session.commit()

    return runs_created
