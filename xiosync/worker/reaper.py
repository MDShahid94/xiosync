"""Lease expiry reaper — retries stale xioflow_tasks or dead-letters them.

Retry policy (per task):
  retry_count < max_retries  → set PENDING, increment retry_count,
                               apply exponential backoff via priority decay
  retry_count >= max_retries → set FAILED, write xioflow_dead_letters row,
                               mark parent xioflow_run as FAILED

Expiry detection uses lease_expires_at when set (preferred — set by worker on
claim) or falls back to claimed_at + _STALE_MINUTES for tasks claimed before
migration 0044.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Fallback stale threshold for tasks that pre-date the lease_expires_at column
_STALE_MINUTES = 10


def reap_expired_leases(session: Session) -> int:
    """Find CLAIMED tasks whose lease has expired and requeue or dead-letter them.

    Returns the count of tasks reaped.
    """
    from xiosync.platform.ids import new_id

    reaped = 0

    # Fetch stale tasks — prefer lease_expires_at, fall back to claimed_at threshold
    stale = session.execute(
        text("""
            SELECT
                t.id,
                t.run_id,
                t.attempt_count,
                t.retry_count,
                t.max_retries,
                t.node_intent
            FROM xioflow_tasks t
            WHERE t.state = 'CLAIMED'
              AND (
                  -- Task has an explicit lease expiry (post-0044)
                  (t.lease_expires_at IS NOT NULL AND t.lease_expires_at < now())
                  OR
                  -- Fallback for tasks without lease_expires_at
                  (t.lease_expires_at IS NULL
                   AND t.claimed_at < now() - interval '10 minutes')
              )
            LIMIT 100
        """)
    ).fetchall()

    for row in stale:
        task_id = str(row.id)
        run_id = str(row.run_id)
        retry_count = row.retry_count or 0
        max_retries = row.max_retries if row.max_retries is not None else 3
        intent = row.node_intent

        if retry_count < max_retries:
            # ── Retry path ───────────────────────────────────────────────────
            new_retry = retry_count + 1
            # Exponential backoff via priority decay (higher retry → lower priority)
            # Normal tasks have priority=0; retried tasks get negative priority
            backoff_priority = -(new_retry * 10)

            session.execute(
                text("""
                    UPDATE xioflow_tasks
                    SET    state         = 'PENDING',
                           claimed_at    = NULL,
                           leased_by     = NULL,
                           lease_expires_at = NULL,
                           worker_id     = NULL,
                           retry_count   = :retry,
                           attempt_count = attempt_count + 1,
                           priority      = :priority,
                           error         = 'lease_expired_retry'
                    WHERE  id = :id
                """),
                {"id": task_id, "retry": new_retry, "priority": backoff_priority},
            )
            logger.warning(
                "task_lease_expired_retry",
                extra={
                    "task_id": task_id,
                    "retry_count": new_retry,
                    "max_retries": max_retries,
                    "backoff_priority": backoff_priority,
                },
            )
        else:
            # ── Dead-letter path ─────────────────────────────────────────────
            session.execute(
                text("""
                    UPDATE xioflow_tasks
                    SET    state = 'FAILED',
                           error = 'max_retries_exceeded'
                    WHERE  id = :id
                """),
                {"id": task_id},
            )

            dlq_id = str(new_id())
            session.execute(
                text("""
                    INSERT INTO xioflow_dead_letters
                      (id, run_id, task_id, payload, retry_count, last_error, created_at)
                    VALUES
                      (:id, :run_id, :task_id,
                       cast(:payload as jsonb),
                       :retry_count, :error, now())
                    ON CONFLICT DO NOTHING
                """),
                {
                    "id": dlq_id,
                    "run_id": run_id,
                    "task_id": task_id,
                    "payload": json.dumps({"intent": intent, "retry_count": retry_count}),
                    "retry_count": retry_count,
                    "error": "max_retries_exceeded",
                },
            )

            # Mark the parent run as FAILED so it stops being picked up
            session.execute(
                text("""
                    UPDATE xioflow_runs
                    SET    state = 'FAILED',
                           finished_at = now(),
                           error = 'task_max_retries_exceeded'
                    WHERE  id = :run_id
                      AND  state NOT IN ('SUCCESS', 'FAILED', 'CANCELLED')
                """),
                {"run_id": run_id},
            )

            logger.error(
                "task_dead_lettered",
                extra={
                    "task_id": task_id,
                    "run_id": run_id,
                    "dlq_id": dlq_id,
                    "retry_count": retry_count,
                },
            )

        reaped += 1

    if reaped:
        session.commit()

    return reaped
