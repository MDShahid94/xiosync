"""Run Dispatcher — claims PENDING xioflow_runs and executes them.

Lifecycle per run:
  1. SELECT ... FOR UPDATE SKIP LOCKED — claim one PENDING run
  2. Mark run RUNNING, create xioflow_task row (CLAIMED)
  3. Resolve template_type via ExecutorRegistry:
       'script'      → ScriptRunner (Node.js .mjs subprocess)
       'xioflow_dag' → DAGExecutor  (async Python engine, bridged via asyncio.run)
  4. Write task result (SUCCESS|FAILED) + update run state
  5. Any FAILED → run → FAILED + DLQ entry

Design decisions:
  - One run per dispatcher tick (serial per worker process, but multiple
    worker processes can run in parallel — SKIP LOCKED prevents double-claim)
  - script-type templates call ScriptRunner synchronously in-thread
  - xioflow_dag templates use asyncio.run() to bridge the async DAGExecutor
    into this synchronous worker loop
  - All DB work is flushed inside one Session; no partial commits
  - New executor types can be added by registering in _EXECUTOR_DISPATCH
    without changing the main loop
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from xiosync.platform.ids import new_id
from xiosync.subsystems.xiogrid.services.script_runner import ScriptRunner, ScriptRunnerError

logger = logging.getLogger(__name__)

_runner = ScriptRunner()  # singleton — reused across ticks, stateless

# ── XIOVIEW page registry ─────────────────────────────────────────────────────
# All page lookups delegate to XIORunRuntimePool — the single source of truth.
# _register_page / _unregister_page are preserved as public API because
# launcher.py calls them; they forward into the pool so there is exactly one
# dict tracking live pages instead of the previous two that could desync.
#
# In a multi-process deployment, swap XIORunRuntimePool for a Redis-backed
# implementation without touching this module's interface.


async def get_active_page(session_id: str) -> Any | None:
    """Return the live Playwright page for `session_id`, or None if not found."""
    try:
        from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

        return get_runtime_pool().get_page(session_id)
    except Exception:  # noqa: BLE001
        return None


def _register_page(session_id: str | None, page: Any) -> None:
    """Register a live page into the runtime pool (called by BrowserLauncher)."""
    if not session_id:
        return
    try:
        from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

        pool = get_runtime_pool()
        # _register is the internal registration path — page is already launched,
        # we just need to make sure the pool knows about it.
        if session_id not in pool._pages:
            pool._pages[session_id] = page
    except Exception:  # noqa: BLE001
        pass


def _unregister_page(session_id: str | None) -> None:
    """Remove a page from the runtime pool (called by BrowserLauncher teardown)."""
    if not session_id:
        return
    try:
        from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

        get_runtime_pool()._pages.pop(session_id, None)
    except Exception:  # noqa: BLE001
        pass


def _publish(org_id: str, event: dict) -> None:
    """Fire-and-forget SSE publish — never raises, never blocks the worker."""
    try:
        from xiosync.subsystems.xioflow.api.events import publish_event

        publish_event(org_id, event)
    except Exception:  # noqa: BLE001
        pass  # SSE broker may not be loaded in worker-only mode


def _register_active_run(session_id: str, run_id: str, template_type: str) -> None:
    """Register an active run so XIOVIEW can detect it (both script + DAG)."""
    try:
        from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

        get_runtime_pool().register_active_run(session_id, run_id, template_type)
    except Exception:  # noqa: BLE001
        pass


def _unregister_active_run(session_id: str) -> None:
    """Remove the active run marker when execution completes."""
    try:
        from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

        get_runtime_pool().unregister_active_run(session_id)
    except Exception:  # noqa: BLE001
        pass


def dispatch_pending_runs(session: Session, *, max_per_tick: int = 3) -> int:
    """Claim up to `max_per_tick` PENDING xioflow_runs and execute them.

    Returns the number of runs processed (success or fail — not skipped).
    """
    processed = 0

    for _ in range(max_per_tick):
        run = _claim_next_pending(session)
        if not run:
            break

        run_id = run["id"]
        template_type = run.get("template_type", "script")
        script_ref = run.get("script_ref", "")
        dag_domain = run.get("dag_domain") or ""
        dag_root_intent = run.get("dag_root_intent") or ""
        organization_id = run.get("organization_id") or ""
        context = run.get("context") or {}
        proxy_url = run.get("proxy_url")
        session_id = run.get("session_id")
        worker_ts_ip = run.get("worker_ts_ip")
        pool_id = run.get("pool_id")

        logger.info(
            "run_dispatcher.claimed",
            extra={
                "run_id": run_id,
                "template_type": template_type,
                "script_ref": script_ref,
                "dag_domain": dag_domain,
                "dag_root_intent": dag_root_intent,
            },
        )
        _publish(
            organization_id,
            {
                "type": "run.started",
                "run_id": run_id,
                "template_type": template_type,
            },
        )

        task_id = str(new_id())
        _create_task(
            session,
            task_id=task_id,
            run_id=run_id,
            node_intent=script_ref or dag_root_intent or "dag_root",
        )

        # Register active run in runtime_pool so XIOVIEW can detect it
        # and block manual interactions for BOTH script and DAG paradigms.
        _view_session_id = session_id or run_id
        _register_active_run(_view_session_id, run_id, template_type)

        try:
            if template_type == "script" and script_ref:
                result = _execute_script(
                    script_ref=script_ref,
                    context=context,
                    proxy_url=proxy_url,
                    session_id=uuid.UUID(session_id) if session_id else None,
                )
                # ── Auto-trace: if trace_mode was requested and script succeeded,
                # schedule DAG augmentation from the script's execution trace.
                if context.get("trace_mode") and result.get("success"):
                    _schedule_trace_deploy(
                        session=session,
                        run_id=run_id,
                        org_id=organization_id,
                        script_ref=script_ref,
                        context=context,
                        result=result,
                    )
            elif template_type == "xioflow_dag":
                if not dag_domain or not dag_root_intent:
                    result = {
                        "success": False,
                        "status": "error",
                        "error": "xioflow_dag template missing dag_domain or dag_root_intent",
                    }
                else:
                    result = _execute_dag_workflow(
                        dag_domain=dag_domain,
                        dag_root_intent=dag_root_intent,
                        context=context,
                        org_id=organization_id,
                        run_id=run_id,
                        proxy_url=proxy_url,
                        worker_ts_ip=worker_ts_ip,
                        pool_id=pool_id,
                        session=session,
                    )
            else:
                result = {
                    "success": False,
                    "status": "error",
                    "error": f"unknown template_type={template_type!r} or missing script_ref",
                }
        finally:
            _unregister_active_run(_view_session_id)

        _finalise(session, run_id=run_id, task_id=task_id, result=result)
        session.commit()
        _publish(
            organization_id,
            {
                "type": "run.completed" if result.get("success") else "run.failed",
                "run_id": run_id,
                "state": "SUCCESS" if result.get("success") else "FAILED",
            },
        )
        processed += 1

    return processed


# ── Internal helpers ──────────────────────────────────────────────────────────


def _claim_next_pending(session: Session) -> dict[str, Any] | None:
    """Atomically claim one PENDING run. Returns None if queue is empty."""
    row = session.execute(
        text("""
            WITH claimed AS (
                SELECT r.id, r.organization_id, r.context,
                       t.template_type, t.script_ref,
                       t.dag_domain, t.dag_root_intent,
                       bs.proxy_url,
                       bs.id::text         AS session_id,
                       bs.worker_ts_ip,
                       bs.pool_id::text    AS pool_id
                FROM   xioflow_runs r
                LEFT JOIN workflow_templates t ON t.id = r.template_id
                LEFT JOIN browser_sessions bs
                       ON bs.organization_id = r.organization_id
                      AND bs.state IN ('active','initializing')
                      AND bs.proxy_url IS NOT NULL
                WHERE  r.state = 'PENDING'
                  AND  (t.template_type IS NULL OR t.template_type != 'xioflow_dag')
                ORDER  BY r.started_at
                LIMIT  1
                FOR UPDATE OF r SKIP LOCKED
            )
            UPDATE xioflow_runs
            SET    state = 'RUNNING',
                   context = CASE 
                     WHEN claimed.session_id IS NOT NULL 
                     THEN xioflow_runs.context || jsonb_build_object('session_id', claimed.session_id)
                     ELSE xioflow_runs.context
                   END
            FROM   claimed
            WHERE  xioflow_runs.id = claimed.id
            RETURNING
                xioflow_runs.id,
                claimed.organization_id,
                claimed.template_type,
                claimed.script_ref,
                claimed.dag_domain,
                claimed.dag_root_intent,
                xioflow_runs.context,
                claimed.proxy_url,
                claimed.session_id,
                claimed.worker_ts_ip,
                claimed.pool_id
        """)
    ).fetchone()

    if not row:
        return None

    return {
        "id": str(row.id),
        "organization_id": str(row.organization_id) if row.organization_id else None,
        "template_type": row.template_type,
        "script_ref": row.script_ref,
        "dag_domain": row.dag_domain,
        "dag_root_intent": row.dag_root_intent,
        "context": row.context or {},
        "proxy_url": row.proxy_url,
        "session_id": row.session_id,
        "worker_ts_ip": row.worker_ts_ip,
        "pool_id": row.pool_id,
    }


def _schedule_trace_deploy(
    *,
    session: Session,
    run_id: str,
    org_id: str,
    script_ref: str,
    context: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Build and deploy a DAG from a successful script run's trace data.

    For script-mode workflows, the trace is reconstructed from the
    script's structured result output (steps, actions).  This is a
    best-effort operation — failure to deploy the trace does not
    affect the script run's success status.
    """
    try:
        from urllib.parse import urlparse

        from xiosync.subsystems.xioflow.ingestion.dag_graph_builder import DAGGraphBuilder
        from xiosync.subsystems.xioflow.ingestion.trace_collector import (
            TraceAction,
            TraceCollector,
        )
        from xiosync.subsystems.xioflow.memory.memory_graph import MemoryGraph

        # Derive domain from script context
        domain = context.get("dag_domain", "")
        if not domain:
            xiorun_url = context.get("xiorun_url", "")
            domain = urlparse(xiorun_url).netloc if xiorun_url else script_ref.replace(".mjs", "")

        trace = TraceCollector(org_id=org_id, domain=domain, run_id=run_id)

        # Reconstruct trace from the script result's step data
        # Scripts output structured step info when ctx.step() is used
        steps = result.get("result", {}).get("steps", [])
        if steps:
            for step_data in steps:
                step_name = step_data.get("name", "unknown")
                trace.enter_step(step_name)
                for act in step_data.get("actions", []):
                    trace.record(
                        TraceAction(
                            category=act.get("category", "browser"),
                            action_type=act.get("action_type", "unknown"),
                            action_params=act.get("action_params", {}),
                            url=act.get("url", ""),
                            domain=urlparse(act.get("url", "")).netloc or domain,
                            place_value=act.get("place_value"),
                            face_value=act.get("face_value"),
                        )
                    )
                trace.exit_step(step_name)
        else:
            # Minimal trace: record the script execution itself as a single node
            trace.enter_step("script_execution")
            trace.record(
                TraceAction(
                    category="system",
                    action_type="script",
                    action_params={
                        "script_ref": script_ref,
                        "result_status": result.get("status", "success"),
                    },
                )
            )
            trace.exit_step("script_execution")

        if trace.actions:
            dag_spec = DAGGraphBuilder().build(
                trace,
                context,
                template_name=script_ref,
            )
            nodes = dag_spec.get("nodes", [])
            if nodes:
                import asyncio

                mg = MemoryGraph(session)
                node_ids = []
                dag_domain = dag_spec.get("dag_domain", domain)
                ctx_hash = dag_spec.get("metadata", {}).get("context_hash", "default")
                for node in nodes:
                    node_id = asyncio.run(
                        mg.asave_new_action(
                            org_id=org_id,
                            domain=dag_domain,
                            intent=node.get("intent"),
                            face_value=node.get("face_value") or {},
                            place_value=node.get("place_value") or {},
                            action_type=node.get("action_type"),
                            action_params=node.get("action_params", {}),
                            previous_intent=node.get("previous_node_id"),
                            recorded_by="auto_trace",
                            recording_method="auto_trace",
                            context_hash=ctx_hash,
                            output_var=node.get("output_var"),
                            execution_mode=node.get("execution_mode", "sequential"),
                        )
                    )
                    node_ids.append(str(node_id))

                # ── Consensus voting: +1.0 for each successfully traced node ──
                # This drives Bayesian EMA tier promotion:
                #   project_experimental → project_ground_truth → organization_shared
                try:
                    from xiosync.subsystems.xioflow.memory.consensus_engine import ConsensusEngine

                    consensus = ConsensusEngine(session)
                    voter_id = f"auto_trace:{run_id}"
                    for nid in node_ids:
                        consensus.submit_vote(
                            org_id=org_id,
                            node_id=nid,
                            voter_id=voter_id,
                            raw_vote=1.0,
                            tier_confidence=1.0,  # auto-trace = real execution = full confidence
                            context_hash=ctx_hash,
                        )
                    logger.info(
                        "run_dispatcher.consensus_votes_submitted",
                        extra={"run_id": run_id, "nodes": len(node_ids), "vote": 1.0},
                    )
                except Exception as vote_exc:
                    logger.warning(
                        "run_dispatcher.consensus_vote_failed",
                        extra={"run_id": run_id, "error": str(vote_exc)},
                    )

                logger.info(
                    "run_dispatcher.trace_deployed",
                    extra={
                        "run_id": run_id,
                        "script_ref": script_ref,
                        "nodes": len(node_ids),
                        "domain": dag_domain,
                    },
                )

    except Exception as exc:
        # Best-effort — don't fail the run because trace deploy broke
        logger.warning(
            "run_dispatcher.trace_deploy_failed",
            extra={"run_id": run_id, "error": str(exc)},
        )


def _create_task(
    session: Session,
    *,
    task_id: str,
    run_id: str,
    node_intent: str,
) -> None:
    session.execute(
        text("""
            INSERT INTO xioflow_tasks
              (id, run_id, node_intent, state, attempt_count, claimed_at)
            VALUES
              (:id, :run_id, :intent, 'CLAIMED', 1, now())
        """),
        {"id": task_id, "run_id": run_id, "intent": node_intent},
    )


def _execute_script(
    *,
    script_ref: str,
    context: dict[str, Any],
    proxy_url: str | None,
    session_id: uuid.UUID | None,
) -> dict[str, Any]:
    try:
        run_result = _runner.run(
            script_ref=script_ref,
            context=context,
            session_id=session_id,
            proxy_url=proxy_url,
        )
        return {
            "success": run_result.success,
            "status": run_result.status,
            "result": run_result.result,
            "error": run_result.error,
            "stdout_tail": run_result.stdout[-500:] if run_result.stdout else "",
        }
    except ScriptRunnerError as exc:
        return {"success": False, "status": "launch_error", "error": str(exc)}


class _MockPage:
    """Minimal page stub for non-browser DAGs (delay, http_request, script nodes).

    DAGExecutor.execute_workflow reads page.url to derive the domain.
    For headless DAGs, we supply a synthetic URL so the executor can resolve
    domain-scoped memory nodes correctly. All page action methods are no-ops
    that log a warning — they should never be reached for correctly authored
    headless DAGs, but if they are the run still completes instead of crashing.
    """

    def __init__(self, dag_domain: str) -> None:
        self.url = f"https://{dag_domain}/"
        self.mouse = _MockMouse()

    async def goto(self, url: str, **_kw) -> None:
        logger.warning(
            "_MockPage.goto called on headless DAG — node type mismatch", extra={"url": url}
        )

    async def wait_for_load_state(self, *_a, **_kw) -> None:
        pass

    async def wait_for_selector(self, selector: str, **_kw) -> None:
        logger.warning("_MockPage.wait_for_selector — headless DAG", extra={"selector": selector})

    async def wait_for_timeout(self, timeout: float, **_kw) -> None:
        import asyncio as _asyncio

        await _asyncio.sleep(timeout / 1000)

    async def evaluate(self, expression: str, *_a, **_kw):
        logger.warning("_MockPage.evaluate — headless DAG", extra={"expr": expression[:80]})
        return None

    def locator(self, selector: str):
        return _MockLocator()


class _MockMouse:
    async def click(self, x: float, y: float, **_kw) -> None:
        logger.warning("_MockMouse.click — headless DAG", extra={"x": x, "y": y})


class _MockLocator:
    @property
    def first(self):
        return self

    async def click(self, **_kw) -> None:
        logger.warning("_MockLocator.click — headless DAG")

    async def fill(self, text: str, **_kw) -> None:
        logger.warning("_MockLocator.fill — headless DAG", extra={"text": text[:40]})

    async def text_content(self, **_kw) -> str:
        return ""


def _execute_dag_workflow(
    *,
    dag_domain: str,
    dag_root_intent: str,
    context: dict[str, Any],
    org_id: str,
    run_id: str,
    proxy_url: str | None,
    worker_ts_ip: str | None = None,
    pool_id: str | None = None,
    session: Session,
) -> dict[str, Any]:
    """Bridge the sync worker loop to the async DAGExecutor.

    Uses asyncio.run() to call the async engine from the synchronous
    worker thread.  All 5 engine components are wired inside the async
    coroutine so they share the same event loop.  Browser-capable actions
    need proxy_url + worker_ts_ip from an active browser_sessions row;
    non-browser DAGs (http_request, delay, assertion, sub_workflow, script
    nodes) work without one.
    """
    try:
        return asyncio.run(
            _execute_dag_async(
                dag_domain=dag_domain,
                dag_root_intent=dag_root_intent,
                context=context,
                org_id=org_id,
                run_id=run_id,
                proxy_url=proxy_url,
                worker_ts_ip=worker_ts_ip,
                pool_id=pool_id,
                session=session,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "run_dispatcher.dag_error",
            extra={
                "run_id": run_id,
                "dag_domain": dag_domain,
                "dag_root_intent": dag_root_intent,
            },
        )
        return {"success": False, "status": "dag_error", "error": str(exc)}


async def _execute_dag_async(
    *,
    dag_domain: str,
    dag_root_intent: str,
    context: dict[str, Any],
    org_id: str,
    run_id: str,
    proxy_url: str | None,
    worker_ts_ip: str | None = None,
    pool_id: str | None = None,
    session: Session,
) -> dict[str, Any]:
    """Async core: wire all engine components and execute the DAG.

    If proxy_url + worker_ts_ip are present (browser session available),
    launches a real patchright browser on the Colab worker via XIORUN.
    Otherwise falls back to _MockPage for headless DAGs (delay/http/script).

    Constructor signatures (verified against source):
      MemoryGraph(session: Session)
      ContextHashRouter(session: AsyncSession | Any)
      LocatorCascade(page: Any)
      CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
      BranchEvaluator()   — no args
      DAGExecutor(memory_graph, locator_cascade, context_hash_router,
                  circuit_breaker, branch_evaluator, page, ...)
      execute_workflow(intent: str, org_id: str, context: dict,
                       execution_context: dict | None = None) -> bool
    """
    from xiosync.subsystems.xioflow.engine.branch_evaluator import BranchEvaluator
    from xiosync.subsystems.xioflow.engine.circuit_breaker import CircuitBreaker
    from xiosync.subsystems.xioflow.engine.context_hash_router import ContextHashRouter
    from xiosync.subsystems.xioflow.engine.dag_executor import DAGExecutor
    from xiosync.subsystems.xioflow.engine.locator_cascade import LocatorCascade
    from xiosync.subsystems.xioflow.memory.memory_graph import MemoryGraph

    # DB-backed memory graph — shares the worker's sync session
    memory_graph = MemoryGraph(session)

    # 5-tier viewport/device matching — also uses the session for DB queries
    context_hash_router = ContextHashRouter(session)

    # Per-domain circuit breaker (3 failures → OPEN)
    circuit_breaker = CircuitBreaker()

    # Conditional edge evaluation
    branch_evaluator = BranchEvaluator()

    # ── Page acquisition ────────────────────────────────────────────────────
    # Extract XIORUN-relevant fields from context (caller may inject identity_id)
    session_id = context.get("session_id")
    identity_id = context.get("identity_id")  # caller-supplied; None = blank session

    # XIOVIEW key: prefer session_id (stable), fallback to run_id
    _view_session_id = session_id or run_id

    pool = None
    page: Any

    if proxy_url and worker_ts_ip and session_id:
        # Real browser session — launch patchright on Colab via XIORUN
        from xiosync.platform.engine_ref import get_engine  # noqa: PLC0415
        from xiosync.subsystems.xiorun.runtime_pool import get_runtime_pool  # noqa: PLC0415

        pool = get_runtime_pool()
        page = await pool.get_or_launch(
            session_id=session_id,
            identity_id=identity_id,
            org_id=org_id,
            proxy_url=proxy_url,
            worker_ts_ip=worker_ts_ip,
            pool_id=pool_id,
            dag_domain=dag_domain,
            engine=get_engine(),
        )
        # Let the launcher know which asyncio task to cancel on proxy loss
        launcher = pool.get_launcher(session_id)
        if launcher:
            launcher.set_run_task(asyncio.current_task())
    else:
        # Headless DAG — mock page provides dag_domain as page.url
        page = _MockPage(dag_domain)

    # Register the page with XIOVIEW so live observers can access it.
    # Key by session_id from context; fall back to run_id for headless DAGs.
    _register_page(_view_session_id, page)

    # Locator cascade resolves DOM selectors — page injected per-node
    locator_cascade = LocatorCascade(page)

    executor = DAGExecutor(
        memory_graph,
        locator_cascade,
        context_hash_router,
        circuit_breaker,
        branch_evaluator,
        page,
        script_runner=_runner,  # reuse module-level singleton — no per-node instantiation
    )

    try:
        success = await executor.execute_workflow(
            dag_root_intent,
            org_id,
            {**context, "run_id": run_id, "org_id": org_id, "proxy_url": proxy_url or ""},
        )
    finally:
        _unregister_page(_view_session_id)
        # Graceful teardown: save profile + suspend session
        if pool is not None and session_id:
            await pool.release(
                session_id,
                org_id,
                page_url=getattr(page, "url", None),
            )

    return {
        "success": success,
        "status": "dag_completed" if success else "dag_failed",
        "result": {
            "domain": dag_domain,
            "root_intent": dag_root_intent,
            "workflow_vars": getattr(executor, "workflow_vars", {}),
        },
        "error": (
            None if success else "DAG execution failed — see xioflow_tasks for per-node detail"
        ),
    }


def _finalise(
    session: Session,
    *,
    run_id: str,
    task_id: str,
    result: dict[str, Any],
) -> None:
    """Write task result and transition run to terminal state."""
    now = datetime.now(UTC)
    success = result.get("success", False)
    task_state = "SUCCESS" if success else "FAILED"
    run_state = "SUCCESS" if success else "FAILED"

    session.execute(
        text("""
            UPDATE xioflow_tasks
            SET    state = :state,
                   result = cast(:result as jsonb),
                   error = :error,
                   completed_at = :now
            WHERE  id = :id
        """),
        {
            "state": task_state,
            "result": json.dumps(result.get("result", {})),
            "error": result.get("error"),
            "now": now,
            "id": task_id,
        },
    )

    session.execute(
        text("""
            UPDATE xioflow_runs
            SET    state = :state,
                   finished_at = :now
            WHERE  id = :id
        """),
        {"state": run_state, "now": now, "id": run_id},
    )

    if not success and result.get("status") not in ("deferred",):
        # Write to DLQ so operators can inspect
        dlq_id = str(new_id())
        session.execute(
            text("""
                INSERT INTO xioflow_dead_letters
                  (id, run_id, task_id, payload, retry_count, last_error, created_at)
                VALUES
                  (:id, :run_id, :task_id, cast(:payload as jsonb), 1, :error, now())
                ON CONFLICT DO NOTHING
            """),
            {
                "id": dlq_id,
                "run_id": run_id,
                "task_id": task_id,
                "payload": json.dumps(
                    {
                        "script_ref": result.get("status"),
                        "stdout_tail": result.get("stdout_tail", ""),
                    }
                ),
                "error": str(result.get("error", ""))[:500],
            },
        )

    logger.info(
        "run_dispatcher.finalised",
        extra={"run_id": run_id, "task_id": task_id, "state": run_state},
    )
