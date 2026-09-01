"""Lease expiry reaper — reclaims abandoned task leases.

Runs on a configurable interval (default 30s). Finds tasks in ``leased``
state whose ``lease_expires_at < now`` and resets them to ``queued``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.worker.context import system_context
from xiosync.persistence.models.identity import Organization
from xiosync.services.workflows import WorkflowService

logger = logging.getLogger("xiosync.worker.reaper")


def reap_expired_leases(session: Session) -> int:
    """Reclaim all expired leases across all organizations. Returns count."""
    # Get all active organizations.
    org_ids = list(
        session.scalars(
            select(Organization.id).where(Organization.state == "active")
        ).all()
    )

    total_reclaimed = 0
    for org_id in org_ids:
        try:
            context = system_context(org_id)
            svc = WorkflowService(session)
            reclaimed = svc.expire_leases(context)
            count = len(reclaimed)
            if count > 0:
                total_reclaimed += count
                logger.info(
                    "leases_reclaimed",
                    extra={
                        "organization_id": str(org_id),
                        "count": count,
                        "task_ids": [str(t) for t in reclaimed],
                    },
                )
        except Exception:
            logger.exception(
                "lease_reap_failed",
                extra={"organization_id": str(org_id)},
            )

    if total_reclaimed:
        session.commit()

    return total_reclaimed
