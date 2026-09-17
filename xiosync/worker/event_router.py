"""Event trigger evaluator — matches fired events to xioflow_triggers and creates runs."""
from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def evaluate_event_triggers(session: Session, *, limit: int = 100) -> int:
    """Find unprocessed events and check them against enabled event triggers.

    For each matching (event, trigger) pair, create an xioflow_run in PENDING state.

    The events table is the existing ``xiosync`` events store (operation events).
    Triggers are matched on ``event_name`` column.

    Returns the number of runs created.
    """
    from xiosync.platform.ids import new_id

    created = 0

    # Find recent unprocessed events that have matching enabled triggers.
    # We match on events.event_type (the actual column name) against triggers.event_name.
    # Events are processed if there's already a run with trigger_id pointing to this trigger
    # fired AFTER the event's created_at — simple idempotency guard.
    matches = session.execute(
        text("""
            SELECT DISTINCT ON (e.id, tr.id)
                e.id         AS event_id,
                e.event_type AS event_type,
                e.organization_id,
                e.payload,
                tr.id        AS trigger_id,
                tr.template_id,
                tr.context_defaults
            FROM events e
            JOIN xioflow_triggers tr
              ON tr.event_name = e.event_type
             AND tr.organization_id = e.organization_id
             AND tr.trigger_type = 'event'
             AND tr.enabled = true
            WHERE e.created_at > now() - interval '5 minutes'
              -- Idempotency: no run already fired for this trigger after this event
              AND NOT EXISTS (
                  SELECT 1 FROM xioflow_runs r
                  WHERE r.trigger_id = tr.id
                    AND r.started_at >= e.created_at
              )
            LIMIT :lim
        """),
        {"lim": limit},
    ).fetchall()

    for row in matches:
        run_id = new_id()
        ctx = {**(row.context_defaults or {}), "event_type": row.event_type, "event_id": str(row.event_id)}
        session.execute(
            text("""
                INSERT INTO xioflow_runs
                  (id, organization_id, template_id, trigger_id, state, context, started_at)
                VALUES
                  (:id, :org_id, :template_id, :trigger_id, 'PENDING', cast(:ctx as jsonb), now())
            """),
            {
                "id": str(run_id),
                "org_id": str(row.organization_id),
                "template_id": str(row.template_id) if row.template_id else None,
                "trigger_id": str(row.trigger_id),
                "ctx": json.dumps(ctx),
            },
        )
        created += 1
        logger.info(
            "event_trigger_fired",
            extra={"event_type": row.event_type, "trigger_id": str(row.trigger_id), "run_id": str(run_id)},
        )

    if created:
        session.commit()

    return created
