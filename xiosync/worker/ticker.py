"""Cron trigger ticker — polls for due cron triggers and fires them.

Runs on a configurable interval (default 15s). For each due trigger:
1. Marks it as fired (updates last_fired_at, computes next_fire_at)
2. Creates a new workflow run
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from xiosync.worker.context import system_context
from xiosync.persistence.models.identity import Organization
from xiosync.services.triggers import TriggerService
from xiosync.services.workflows import WorkflowService

logger = logging.getLogger("xiosync.worker.ticker")


def tick_cron_triggers(session: Session) -> int:
    """Poll and fire all due cron triggers. Returns count of triggers fired."""
    from sqlalchemy import select

    svc = TriggerService(session)
    now = datetime.now(timezone.utc)

    due = svc.get_due_cron_triggers(now=now)
    if not due:
        return 0

    fired = 0
    for trigger in due:
        try:
            context = system_context(trigger.organization_id)

            # Mark the trigger as fired (updates next_fire_at).
            svc.mark_fired(context, trigger.id, now=now)

            # Create a workflow run for the trigger's workflow.
            wf_svc = WorkflowService(session)
            wf_svc.start_run(
                context,
                trigger.workflow_id,
                initiated_by=trigger.created_by or trigger.organization_id,
            )

            fired += 1
            logger.info(
                "trigger_fired",
                extra={
                    "trigger_id": str(trigger.id),
                    "workflow_id": str(trigger.workflow_id),
                    "organization_id": str(trigger.organization_id),
                },
            )
        except Exception:
            logger.exception(
                "trigger_fire_failed",
                extra={"trigger_id": str(trigger.id)},
            )

    if fired:
        session.commit()

    return fired
