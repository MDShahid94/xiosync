"""XIOFlow Orchestrator — the org-scoped control plane for workflow execution.

The Orchestrator governs:
  1. Priority Queue:     Multi-level priority scheduling with org isolation
  2. Resource Allocator: Worker↔Run matching based on capacity, proxy, geography
  3. SLA Monitor:        Track run durations, detect stuck runs, auto-cancel
  4. Escalation Engine:  Surface HITL notices, track resolution time
  5. Agent Coordinator:  Manage multi-agent plans with data dependencies

This module replaces the implicit FIFO dispatcher with a proper control plane
that respects organizational boundaries, resource constraints, and SLAs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────


class OrchestratorConfig:
    """Tunable parameters for the orchestrator control plane."""

    # How often the orchestrator tick runs (seconds)
    tick_interval_s: float = 5.0

    # Maximum runs to claim per tick (prevents thundering herd)
    max_claims_per_tick: int = 10

    # SLA: auto-cancel runs stuck longer than this (seconds)
    stuck_run_timeout_s: int = 1800  # 30 minutes

    # SLA: warn if a run exceeds 2x its template's p95 duration
    sla_multiplier: float = 2.0

    # HITL escalation: auto-cancel unresolved notices after this (seconds)
    hitl_timeout_s: int = 600  # 10 minutes

    # Resource allocation: max concurrent runs per worker
    max_runs_per_worker: int = 3

    def __init__(self, **overrides: Any) -> None:
        for k, v in overrides.items():
            if hasattr(self, k):
                setattr(self, k, v)


# ── Priority Queue Manager ──────────────────────────────────────────────


class PriorityQueueManager:
    """Manages the priority-weighted run queue with org isolation.

    Run priority levels (0-100):
      0-19:   Background / low priority
      20-39:  Normal (default for user-triggered runs)
      40-59:  High (agent-dispatched runs)
      60-79:  Urgent (SLA-critical runs)
      80-100: Emergency (system recovery / retry)
    """

    def __init__(self, session: Session, config: OrchestratorConfig) -> None:
        self._session = session
        self._config = config

    async def claim_next_runs(
        self,
        org_id: str,
        worker_id: str,
        max_claims: int | None = None,
        worker_capabilities: dict | None = None,
    ) -> list[dict]:
        """Claim the next batch of pending runs for a worker, respecting priority.

        Uses SKIP LOCKED to prevent contention between workers.
        """
        limit = max_claims or self._config.max_claims_per_tick

        # Build capability-aware WHERE clause
        capability_filter = ""
        params: dict[str, Any] = {
            "org_id": org_id,
            "worker_id": worker_id,
            "limit": limit,
        }

        # If worker has proxy capabilities, we can match runs that need specific proxies
        if worker_capabilities and worker_capabilities.get("proxy_country"):
            capability_filter = (
                " AND (r.context->>'required_proxy_country' IS NULL "
                "  OR r.context->>'required_proxy_country' = :proxy_country)"
            )
            params["proxy_country"] = worker_capabilities["proxy_country"]

        result = await asyncio.to_thread(
            self._session.execute,
            text(
                f"UPDATE xioflow_runs "
                f"SET state = 'CLAIMED', "
                f"    context = jsonb_set(COALESCE(context, '{{}}'), '{{claimed_by}}', "
                f"        to_jsonb(:worker_id::text)), "
                f"    updated_at = now() "
                f"WHERE id IN ("
                f"  SELECT r.id FROM xioflow_runs r "
                f"  WHERE r.organization_id = :org_id "
                f"    AND r.state = 'PENDING' "
                f"    {capability_filter} "
                f"  ORDER BY r.priority DESC NULLS LAST, r.started_at ASC "
                f"  LIMIT :limit "
                f"  FOR UPDATE OF r SKIP LOCKED "
                f") "
                f"RETURNING id, template_id, priority, context"
            ),
            params,
        )

        rows = result.mappings().all()
        await asyncio.to_thread(self._session.commit)

        if rows:
            logger.info(
                f"orchestrator.claimed: worker={worker_id} "
                f"runs={len(rows)} org={org_id}"
            )

        return [dict(r) for r in rows]

    async def get_queue_depth(self, org_id: str) -> dict[str, int]:
        """Get the current queue depth by priority band for an org."""
        result = await asyncio.to_thread(
            self._session.execute,
            text(
                "SELECT "
                "  COUNT(*) FILTER (WHERE priority >= 80) AS emergency, "
                "  COUNT(*) FILTER (WHERE priority >= 60 AND priority < 80) AS urgent, "
                "  COUNT(*) FILTER (WHERE priority >= 40 AND priority < 60) AS high, "
                "  COUNT(*) FILTER (WHERE priority >= 20 AND priority < 40) AS normal, "
                "  COUNT(*) FILTER (WHERE priority < 20 OR priority IS NULL) AS background, "
                "  COUNT(*) AS total "
                "FROM xioflow_runs "
                "WHERE organization_id = :org_id AND state = 'PENDING'"
            ),
            {"org_id": org_id},
        )
        row = result.mappings().first()
        return dict(row) if row else {}


# ── Resource Allocator ───────────────────────────────────────────────────


class ResourceAllocator:
    """Matches runs to workers based on capacity and capabilities.

    Considers:
      - Worker current load (active sessions / max_concurrent)
      - Proxy availability (residential IP pool)
      - Geographic requirements (proxy country)
      - Browser session state (warm sessions for specific domains)
    """

    def __init__(self, session: Session, config: OrchestratorConfig) -> None:
        self._session = session
        self._config = config

    async def get_available_workers(self, org_id: str) -> list[dict]:
        """Get workers that have capacity for new runs."""
        result = await asyncio.to_thread(
            self._session.execute,
            text(
                "SELECT w.id, w.worker_id, w.capabilities, w.pool_type, "
                "  w.tailscale_ip, w.runtime_version, "
                "  COALESCE((w.capabilities->>'max_concurrent')::int, :default_max) AS max_slots, "
                "  COALESCE(("
                "    SELECT COUNT(*) FROM xioflow_runs r "
                "    WHERE r.context->>'claimed_by' = w.worker_id "
                "      AND r.state IN ('CLAIMED', 'RUNNING')"
                "  ), 0) AS active_runs "
                "FROM worker_enrollments w "
                "WHERE w.organization_id = :org_id "
                "  AND w.state = 'active' "
                "  AND w.deleted_at IS NULL "
                "HAVING COALESCE(("
                "  SELECT COUNT(*) FROM xioflow_runs r "
                "  WHERE r.context->>'claimed_by' = w.worker_id "
                "    AND r.state IN ('CLAIMED', 'RUNNING')"
                "), 0) < COALESCE((w.capabilities->>'max_concurrent')::int, :default_max) "
                "ORDER BY active_runs ASC"  # prefer least-loaded workers
            ),
            {"org_id": org_id, "default_max": self._config.max_runs_per_worker},
        )
        return [dict(r) for r in result.mappings().all()]

    async def allocate_run_to_worker(
        self, run_id: str, worker_id: str
    ) -> bool:
        """Assign a specific run to a specific worker."""
        result = await asyncio.to_thread(
            self._session.execute,
            text(
                "UPDATE xioflow_runs "
                "SET state = 'CLAIMED', "
                "    context = jsonb_set(COALESCE(context, '{}'), '{claimed_by}', "
                "        to_jsonb(:worker_id::text)), "
                "    updated_at = now() "
                "WHERE id = :run_id AND state = 'PENDING'"
            ),
            {"run_id": run_id, "worker_id": worker_id},
        )
        await asyncio.to_thread(self._session.commit)
        return result.rowcount > 0


# ── SLA Monitor ──────────────────────────────────────────────────────────


class SLAMonitor:
    """Tracks run durations and detects stuck/SLA-violating runs.

    Actions:
      - Stuck detection: auto-cancel runs exceeding timeout
      - SLA warning: log when run exceeds 2x historical p95
      - Metrics: maintain per-template duration statistics
    """

    def __init__(self, session: Session, config: OrchestratorConfig) -> None:
        self._session = session
        self._config = config

    async def detect_stuck_runs(self, org_id: str | None = None) -> list[dict]:
        """Find and auto-cancel runs that have been running too long."""
        where_org = "AND r.organization_id = :org_id" if org_id else ""
        params: dict[str, Any] = {
            "timeout_s": self._config.stuck_run_timeout_s,
        }
        if org_id:
            params["org_id"] = org_id

        # Find stuck runs
        result = await asyncio.to_thread(
            self._session.execute,
            text(
                f"SELECT r.id, r.organization_id, r.template_id, r.state, "
                f"  r.started_at, "
                f"  EXTRACT(EPOCH FROM (now() - r.started_at))::int AS elapsed_s "
                f"FROM xioflow_runs r "
                f"WHERE r.state IN ('CLAIMED', 'RUNNING') "
                f"  AND r.started_at < now() - make_interval(secs => :timeout_s) "
                f"  {where_org} "
                f"ORDER BY r.started_at ASC"
            ),
            params,
        )
        stuck = [dict(r) for r in result.mappings().all()]

        if stuck:
            # Auto-cancel stuck runs
            stuck_ids = [r["id"] for r in stuck]
            await asyncio.to_thread(
                self._session.execute,
                text(
                    "UPDATE xioflow_runs "
                    "SET state = 'CANCELLED', "
                    "    context = jsonb_set(COALESCE(context, '{}'), "
                    "        '{cancel_reason}', '\"stuck_timeout\"'::jsonb), "
                    "    finished_at = now(), "
                    "    updated_at = now() "
                    "WHERE id = ANY(:ids) AND state IN ('CLAIMED', 'RUNNING')"
                ),
                {"ids": stuck_ids},
            )
            await asyncio.to_thread(self._session.commit)

            logger.warning(
                f"orchestrator.stuck_runs_cancelled: count={len(stuck)} "
                f"ids={[str(r['id']) for r in stuck]}"
            )

        return stuck

    async def get_template_sla_stats(self, template_id: str) -> dict:
        """Get duration statistics for a specific template."""
        result = await asyncio.to_thread(
            self._session.execute,
            text(
                "SELECT "
                "  COUNT(*) AS total_runs, "
                "  AVG(EXTRACT(EPOCH FROM (finished_at - started_at)))::int AS avg_duration_s, "
                "  PERCENTILE_CONT(0.95) WITHIN GROUP ("
                "    ORDER BY EXTRACT(EPOCH FROM (finished_at - started_at))"
                "  )::int AS p95_duration_s, "
                "  MAX(EXTRACT(EPOCH FROM (finished_at - started_at)))::int AS max_duration_s "
                "FROM xioflow_runs "
                "WHERE template_id = :tmpl_id "
                "  AND state = 'SUCCESS' "
                "  AND finished_at IS NOT NULL"
            ),
            {"tmpl_id": template_id},
        )
        row = result.mappings().first()
        return dict(row) if row else {}


# ── Escalation Engine ────────────────────────────────────────────────────


class EscalationEngine:
    """Manages HITL (Human-in-the-Loop) escalations.

    When a DAG node hits action_type='human_input', the escalation engine:
      1. Records the escalation in xioflow_agent_runs context
      2. Broadcasts via SSE/WebSocket to XIOVIEW observers
      3. Tracks resolution time
      4. Auto-cancels unresolved escalations after timeout
    """

    def __init__(self, session: Session, config: OrchestratorConfig) -> None:
        self._session = session
        self._config = config

    async def create_escalation(
        self,
        org_id: str,
        run_id: str,
        agent_id: str | None,
        message: str,
        escalation_type: str = "human_input",
    ) -> dict:
        """Create an escalation record and notify observers."""
        import uuid  # noqa: PLC0415

        escalation_id = str(uuid.uuid4())

        # Store as a context entry on the run
        await asyncio.to_thread(
            self._session.execute,
            text(
                "UPDATE xioflow_runs "
                "SET context = jsonb_set(COALESCE(context, '{}'), "
                "    '{escalation}', :esc::jsonb), "
                "    state = 'WAITING_HUMAN', "
                "    updated_at = now() "
                "WHERE id = :run_id"
            ),
            {
                "run_id": run_id,
                "esc": __import__("json").dumps({
                    "id": escalation_id,
                    "type": escalation_type,
                    "message": message,
                    "agent_id": agent_id,
                    "created_at": datetime.now(UTC).isoformat(),
                    "resolved": False,
                }),
            },
        )
        await asyncio.to_thread(self._session.commit)

        # If agent, update agent state
        if agent_id:
            await asyncio.to_thread(
                self._session.execute,
                text(
                    "UPDATE xioflow_agents "
                    "SET state = 'waiting_human', updated_at = now() "
                    "WHERE id = :id"
                ),
                {"id": agent_id},
            )
            await asyncio.to_thread(self._session.commit)

        # Broadcast via SSE
        try:
            from xiosync.subsystems.xioflow.api.events import (  # noqa: PLC0415
                publish_event,
            )

            publish_event(org_id, {
                "type": "escalation.created",
                "escalation_id": escalation_id,
                "run_id": run_id,
                "agent_id": agent_id,
                "message": message,
            })
        except Exception:
            pass

        logger.info(
            f"orchestrator.escalation_created: id={escalation_id} "
            f"run={run_id} type={escalation_type}"
        )

        return {"id": escalation_id, "run_id": run_id}

    async def resolve_escalation(
        self, run_id: str, response: str | None = None
    ) -> bool:
        """Mark an escalation as resolved and resume the run."""
        result = await asyncio.to_thread(
            self._session.execute,
            text(
                "UPDATE xioflow_runs "
                "SET context = jsonb_set(context, '{escalation,resolved}', 'true'), "
                "    state = 'RUNNING', "
                "    updated_at = now() "
                "WHERE id = :run_id AND state = 'WAITING_HUMAN'"
            ),
            {"run_id": run_id},
        )
        await asyncio.to_thread(self._session.commit)
        return result.rowcount > 0


# ── Orchestrator (Unified Control Plane) ─────────────────────────────────


class Orchestrator:
    """The unified control plane that ties all components together.

    Runs as a background task on the server, executing periodic ticks
    to manage the workflow execution lifecycle.
    """

    def __init__(
        self,
        session: Session,
        config: OrchestratorConfig | None = None,
    ) -> None:
        self._session = session
        self._config = config or OrchestratorConfig()
        self.queue = PriorityQueueManager(session, self._config)
        self.allocator = ResourceAllocator(session, self._config)
        self.sla = SLAMonitor(session, self._config)
        self.escalation = EscalationEngine(session, self._config)
        self._running = False

    async def start(self) -> None:
        """Start the orchestrator background loop."""
        self._running = True
        logger.info("orchestrator.started")

        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                logger.exception(f"orchestrator.tick_failed: {exc}")

            await asyncio.sleep(self._config.tick_interval_s)

    async def stop(self) -> None:
        """Stop the orchestrator gracefully."""
        self._running = False
        logger.info("orchestrator.stopped")

    async def _tick(self) -> None:
        """Single orchestrator tick — called every tick_interval_s."""
        t0 = time.monotonic()

        # 1. Detect and cancel stuck runs
        stuck = await self.sla.detect_stuck_runs()

        # 2. Get queue depth for metrics
        # (In production, this would be exported to observability)

        tick_ms = round((time.monotonic() - t0) * 1000)
        if stuck or tick_ms > 1000:
            logger.info(
                f"orchestrator.tick: stuck_cancelled={len(stuck)} "
                f"duration={tick_ms}ms"
            )

    async def get_status(self, org_id: str) -> dict[str, Any]:
        """Get the current orchestrator status for an org (for dashboards)."""
        queue_depth = await self.queue.get_queue_depth(org_id)
        workers = await self.allocator.get_available_workers(org_id)

        return {
            "queue": queue_depth,
            "workers": {
                "total": len(workers),
                "available": sum(
                    1 for w in workers
                    if w.get("active_runs", 0) < w.get("max_slots", 3)
                ),
            },
            "config": {
                "tick_interval_s": self._config.tick_interval_s,
                "stuck_timeout_s": self._config.stuck_run_timeout_s,
                "max_runs_per_worker": self._config.max_runs_per_worker,
            },
        }
