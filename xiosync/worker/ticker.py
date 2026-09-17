"""Cron trigger ticker — fires due xioflow_triggers and creates xioflow_runs.

Uses croniter for proper cron expression evaluation.
Supported trigger_type values:
  'cron'  — fired when cron_schedule expression is due
  'event' — fired by event_router, not by this ticker

Idempotency: last_fired_at is updated atomically in the same transaction
as the run INSERT. Concurrent workers won't double-fire because the UPDATE
uses a WHERE last_fired_at condition that only matches once.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _is_due(cron_schedule: str | None, last_fired_at: datetime | None, now: datetime) -> bool:
    """Return True if this cron trigger should fire right now.

    Uses croniter to compute the previous scheduled time relative to `now`.
    A trigger is due if `last_fired_at` is before the most recent scheduled slot.

    Handles None cron_schedule gracefully (triggers with bad config are skipped).
    """
    if not cron_schedule:
        return False
    try:
        from croniter import croniter
        if not croniter.is_valid(cron_schedule):
            logger.warning("invalid_cron_schedule", extra={"schedule": cron_schedule})
            return False
        # Get the most recently past scheduled time
        c = croniter(cron_schedule, now)
        prev = c.get_prev(datetime)
        # Make prev tz-aware if now is tz-aware
        if prev.tzinfo is None and now.tzinfo is not None:
            prev = prev.replace(tzinfo=UTC)
        if last_fired_at is None:
            return True  # Never fired — fire now
        # Ensure last_fired_at is tz-aware
        if last_fired_at.tzinfo is None:
            last_fired_at = last_fired_at.replace(tzinfo=UTC)
        return last_fired_at < prev
    except Exception as exc:  # noqa: BLE001
        logger.warning("cron_eval_error", extra={"schedule": cron_schedule, "error": str(exc)})
        return False


def tick_cron_triggers(session: Session) -> int:
    """Find enabled cron triggers that are due and enqueue xioflow_runs.

    Returns the number of runs created.
    """
    try:
        from xiosync.platform.ids import new_id
    except ImportError:
        new_id = uuid.uuid4  # type: ignore[assignment]

    now = datetime.now(UTC)
    created = 0

    rows = session.execute(
        text("""
            SELECT id, organization_id, template_id, context_defaults,
                   cron_schedule, last_fired_at
            FROM xioflow_triggers
            WHERE trigger_type = 'cron'
              AND enabled = true
            LIMIT 100
        """)
    ).fetchall()

    for row in rows:
        trigger_id = str(row.id)
        org_id = str(row.organization_id)
        template_id = str(row.template_id) if row.template_id else None
        ctx_defaults = row.context_defaults or {}
        cron_schedule = row.cron_schedule
        last_fired_at = row.last_fired_at

        if not _is_due(cron_schedule, last_fired_at, now):
            continue

        run_id = str(new_id())

        # Atomically mark fired + prevent double-fire under concurrent workers:
        # the UPDATE only succeeds if last_fired_at hasn't changed since we read it.
        updated = session.execute(
            text("""
                UPDATE xioflow_triggers
                SET    last_fired_at = now()
                WHERE  id = :id
                  AND  (last_fired_at IS NULL OR last_fired_at = :prev_fired)
            """),
            {
                "id": trigger_id,
                "prev_fired": last_fired_at,
            },
        ).rowcount

        if updated == 0:
            # Another worker already fired this trigger in this window
            continue

        session.execute(
            text("""
                INSERT INTO xioflow_runs
                  (id, organization_id, template_id, trigger_id, state, context, started_at)
                VALUES
                  (:id, :org_id, :template_id, :trigger_id, 'PENDING',
                   cast(:ctx as jsonb), now())
            """),
            {
                "id": run_id,
                "org_id": org_id,
                "template_id": template_id,
                "trigger_id": trigger_id,
                "ctx": json.dumps(ctx_defaults),
            },
        )
        created += 1
        logger.info(
            "cron_trigger_fired",
            extra={
                "trigger_id": trigger_id,
                "run_id": run_id,
                "schedule": cron_schedule,
            },
        )

    if created:
        session.commit()

    return created
