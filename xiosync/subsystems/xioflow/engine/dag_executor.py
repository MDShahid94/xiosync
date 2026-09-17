from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _emit_action(
    org_id: str,
    run_id: str,
    event_type: str,
    node_id: str | None,
    intent: str,
    action_type: str | None,
    **extra,
) -> None:
    """Fire-and-forget: publish a workflow action event to SSE broker + XIOVIEW.

    Never raises — action logging must not break workflow execution.
    """
    try:
        from xiosync.subsystems.xioflow.api.events import publish_event
        from xiosync.subsystems.xioview.registry import get_registry
        event = {
            "type": event_type,
            "event_type": event_type,
            "org_id": org_id,
            "run_id": run_id,
            "node_id": node_id,
            "intent": intent,
            "action": action_type,
            **extra,
        }
        # SSE subscribers (dashboards)
        publish_event(org_id, event)
        # XIOVIEW WebSocket subscribers — broadcast to all sessions in this org
        # Uses the public push_event_to_org() API instead of accessing _sessions directly.
        get_registry().push_event_to_org(org_id, {"type": "action_log", **event})
    except Exception:  # noqa: BLE001
        pass  # never break execution


class DAGExecutor:
    """
    The recursive DAG walker - the main execution engine of XIOFLOW.
    """

    def __init__(
        self,
        memory_graph,
        locator_cascade,
        context_hash_router,
        circuit_breaker,
        branch_evaluator,
        page,
        max_concurrent: int = 5,
        max_execution_depth: int = 50,
        max_viewport_tier: int = 5,
        script_runner=None,  # injected ScriptRunner singleton; created lazily if None
    ):
        """
        Initialize the DAGExecutor.
        """
        self.memory_graph = memory_graph
        self.locator_cascade = locator_cascade
        self.context_hash_router = context_hash_router
        self.circuit_breaker = circuit_breaker
        self.branch_evaluator = branch_evaluator
        self.page = page
        self.max_concurrent = max_concurrent
        self.max_execution_depth = max_execution_depth
        self.max_viewport_tier = max_viewport_tier
        self.workflow_vars: dict = {}
        self._org_id: str = ""
        self._run_id: str = ""
        # ScriptRunner: prefer the injected singleton (run_dispatcher._runner),
        # fall back to a new instance only when running standalone (tests, etc.)
        self._script_runner = script_runner

    async def execute_workflow(self, intent: str, org_id: str, context: dict, execution_context: dict | None = None) -> bool:
        """
        Execute a workflow DAG starting from the intent.
        """
        if execution_context is None:
            execution_context = {}

        # Carry org_id and run_id through for action event emission
        self._org_id = org_id
        self._run_id = context.get("run_id", "")

        # Get current URL's domain
        url = self.page.url
        parsed_url = urlparse(url)
        domain = parsed_url.netloc

        # Generate context hash
        context_hash = self.context_hash_router.generate_hash(context)

        # Fetch DAG
        graph = await self.memory_graph.get_workflow_graph(intent, org_id, domain, context_hash)
        if not graph:
            logger.warning(f"No workflow graph found for intent: {intent}")
            return False

        root_node = graph.get('root')
        if not root_node:
            logger.warning("Graph missing root node")
            return False

        return await self._execute_node(root_node, execution_context)

    async def _execute_node(self, node: dict, execution_context: dict, _visited: set | None = None, _depth: int = 0) -> bool:
        """
        Core recursive method to execute a DAG node.
        """
        if _visited is None:
            _visited = set()

        node_id = node.get('id')

        # Safety guards
        if node_id in _visited:
            logger.error(f"Cycle detected at node {node_id}")
            return False

        if _depth >= self.max_execution_depth:
            logger.error(f"Max execution depth reached at node {node_id}")
            return False

        if not self.circuit_breaker.allow_request():
            logger.error("Circuit breaker triggered, halting execution")
            return False

        _visited.add(node_id)
        intent = node.get('intent', 'unknown')
        logger.info(f"Executing node: {intent} (depth={_depth})")

        action_type = node.get('action_type')
        action_params = node.get('action_params', {})

        # ── Action event: started ─────────────────────────────────────────────
        _t0 = time.monotonic()
        _emit_action(
            self._org_id, self._run_id,
            "workflow.action.started",
            node_id, intent, action_type,
            depth=_depth,
        )
        _node_success: bool | None = None
        try:
            _node_success = await self._execute_node_inner(
                node, action_type, action_params, execution_context, _visited, _depth
            )
            return _node_success
        except Exception:
            _node_success = False
            raise
        finally:
            _ms = round((time.monotonic() - _t0) * 1000)
            _etype = "workflow.action.completed" if _node_success else "workflow.action.failed"
            _emit_action(
                self._org_id, self._run_id, _etype,
                node_id, intent, action_type,
                duration_ms=_ms,
            )

    async def _execute_node_inner(
        self,
        node: dict,
        action_type: str | None,
        action_params: dict,
        execution_context: dict,
        _visited: set,
        _depth: int,
    ) -> bool:
        """Core execution logic — split from _execute_node to keep instrumentation clean."""
        intent = node.get('intent', 'unknown')

        # Deep-copy action_params before variable injection — parallel branches share the
        # same node dict, so mutating in-place would create a race condition.
        action_params = dict(action_params)

        # Handle variable injection — {{variable}} substitution ONLY.
        # vault:// references must NOT be touched here; they are resolved
        # by the dedicated second pass below.
        for k, v in list(action_params.items()):
            if isinstance(v, str):
                # Fix late-binding closure: capture var via default arg
                def replace_var(match, _ctx=execution_context, _wvars=self.workflow_vars):
                    var_name = match.group(1)
                    return str(_ctx.get(var_name, _wvars.get(var_name, match.group(0))))

                new_v = re.sub(r'\{\{(.*?)\}\}', replace_var, v)
                action_params[k] = new_v

        # Resolve vault:// references — vault://identities/xyz/totp_secret
        # These are declared in action_params and must be resolved before node execution.
        for k, v in list(action_params.items()):
            if isinstance(v, str) and v.startswith('vault://'):
                vault_key = v.removeprefix('vault://')
                try:
                    from xiosync.subsystems.vault.service import VaultService  # noqa: PLC0415
                    from xiosync.platform.engine_ref import get_engine  # noqa: PLC0415
                    from sqlalchemy.orm import Session as _OrmSession  # noqa: PLC0415
                    from xiosync.domain.context import OrgContext as _OrgCtx  # noqa: PLC0415
                    import uuid as _uuid  # noqa: PLC0415

                    # Use run_id as the actor so vault audit logs attribute the
                    # access to this specific workflow run rather than nil UUID.
                    _actor = _uuid.UUID(self._run_id) if self._run_id else _uuid.UUID(int=0)
                    with _OrmSession(get_engine()) as _vs:
                        _ctx = _OrgCtx(
                            organization_id=_uuid.UUID(self._org_id),
                            actor_id=_actor,
                        )
                        resolved = VaultService(_vs).get_secret(_ctx, vault_key, allow_platform=True)
                        action_params[k] = resolved
                        logger.debug("dag_executor.vault_resolved", extra={"key": vault_key})
                except Exception as _ve:
                    logger.warning("dag_executor.vault_resolve_failed",
                                   extra={"key": vault_key, "error": str(_ve)})
                    # Keep the vault:// string — downstream node will fail descriptively

        # NOTE: we deliberately do NOT write action_params back to node['action_params'].
        # node is a reference into the shared memory_graph dict.  In parallel execution
        # mode multiple coroutines share the same node reference; mutating it here would
        # contaminate sibling branches with resolved/injected values from this branch.
        # The local `action_params` copy is sufficient for the rest of this method.

        success = True
        if action_type == 'trigger_sub_workflow':
            # Execute a nested sub-workflow by fetching its graph from memory and
            # recursing into _execute_node.  The sub-graph shares this executor's
            # circuit breaker, workflow_vars, and visited set to prevent cycles.
            target_intent = action_params.get('target_intent')
            if not target_intent:
                logger.error(f"trigger_sub_workflow node {intent!r} missing target_intent")
                self.circuit_breaker.record_failure()
                return False
            try:
                url = self.page.url
                from urllib.parse import urlparse as _up  # noqa: PLC0415
                domain = _up(url).netloc
                context_hash = self.context_hash_router.generate_hash(execution_context)
                sub_graph = await self.memory_graph.get_workflow_graph(
                    target_intent, self._org_id, domain, context_hash
                )
                if not sub_graph:
                    logger.warning(
                        f"trigger_sub_workflow: no graph found for intent={target_intent!r}",
                        extra={"parent_intent": intent},
                    )
                    self.circuit_breaker.record_failure()
                    return False
                sub_root = sub_graph.get('root')
                if not sub_root:
                    logger.error(f"trigger_sub_workflow: sub-graph for {target_intent!r} has no root node")
                    self.circuit_breaker.record_failure()
                    return False
                logger.info(f"trigger_sub_workflow: executing sub-graph for {target_intent!r}")
                sub_ok = await self._execute_node(
                    sub_root, execution_context, _depth=_depth + 1
                )
                if not sub_ok:
                    self.circuit_breaker.record_failure()
                    return False
                self.circuit_breaker.record_success()
            except Exception as exc:  # noqa: BLE001
                self.circuit_breaker.record_failure()
                logger.exception(f"trigger_sub_workflow node {intent!r} exception: {exc}")
                return False

        elif action_type == 'compute_node':
            # Compute nodes are not yet implemented (require a sandboxed runtime).
            # Fail explicitly so the caller knows rather than silently claiming success.
            logger.error(
                "dag_executor.compute_node_not_implemented",
                extra={"intent": intent, "action_params": action_params},
            )
            self.circuit_breaker.record_failure()
            return False

        elif action_type == 'conditional':
            # branch_evaluator picks next node, filtering next_nodes below
            pass

        elif action_type == 'done':
            return True

        elif action_type == 'script':
            # LegacyScriptNode — runs a .mjs workflow file as a DAG node.
            # Bridges the two execution tracks: a DAG can delegate one step
            # to an imperative .mjs script and receive its JSON output.
            #
            # action_params expected:
            #   script_ref: str    — e.g. "google-signin.mjs"
            #   context:    dict   — extra key/value context to pass
            #
            # On success the script's result dict is merged into workflow_vars.
            script_ref = action_params.get('script_ref', '')
            if not script_ref:
                logger.error(f"script node {intent!r} missing script_ref")
                self.circuit_breaker.record_failure()
                return False
            try:
                from xiosync.subsystems.xiogrid.services.script_runner import (  # noqa: PLC0415
                    ScriptRunner, ScriptRunnerError,
                )
                # Reuse the injected singleton; create a new instance only as last resort.
                runner = self._script_runner
                if runner is None:
                    runner = ScriptRunner()
                    self._script_runner = runner  # cache for subsequent nodes in this run
                proxy_url = execution_context.get('proxy_url') or None
                run_result = runner.run(
                    script_ref=script_ref,
                    context={**execution_context, **self.workflow_vars,
                             **action_params.get('context', {})},
                    session_id=None,
                    proxy_url=proxy_url,
                )
                if run_result.success:
                    self.circuit_breaker.record_success()
                    # Merge script outputs into workflow_vars for downstream nodes
                    if isinstance(run_result.result, dict):
                        self.workflow_vars.update(run_result.result)
                    output_var = node.get('output_var')
                    if output_var:
                        self.workflow_vars[output_var] = run_result.result
                    logger.info(f"script node {intent!r} succeeded: {script_ref}")
                else:
                    self.circuit_breaker.record_failure()
                    logger.warning(f"script node {intent!r} failed: {run_result.error}")
                    return False
            except Exception as exc:  # noqa: BLE001
                self.circuit_breaker.record_failure()
                logger.exception(f"script node {intent!r} exception: {exc}")
                return False

        elif action_type == 'delay':
            # Non-browser pause — waits ms milliseconds before continuing
            ms = int(action_params.get('ms', action_params.get('timeout', 1000)))
            await asyncio.sleep(ms / 1000)

        elif action_type == 'http_request':
            # Inline HTTP call — useful for webhooks, API assertions, data fetch
            # action_params: method, url, headers, body, output_var
            try:
                import httpx
                method = action_params.get('method', 'GET').upper()
                url_target = action_params.get('url', '')
                headers = action_params.get('headers', {})
                body = action_params.get('body') or action_params.get('json')
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.request(method, url_target,
                                                headers=headers, json=body)
                    resp.raise_for_status()
                output_var = node.get('output_var')
                if output_var:
                    try:
                        self.workflow_vars[output_var] = resp.json()
                    except Exception:
                        self.workflow_vars[output_var] = resp.text
                self.circuit_breaker.record_success()
                logger.info(f"http_request node {intent!r}: {method} {url_target} → {resp.status_code}")
            except Exception as exc:  # noqa: BLE001
                self.circuit_breaker.record_failure()
                logger.warning(f"http_request node {intent!r} failed: {exc}")
                return False

        elif action_type == 'navigate':
            await self.page.goto(action_params['url'])
            await self.page.wait_for_load_state('networkidle')

        elif action_type == 'scroll_down':
            await self.page.evaluate('window.scrollBy(0, window.innerHeight)')

        elif action_type == 'wait':
            if 'selector' in action_params:
                await self.page.wait_for_selector(action_params['selector'])
            else:
                await self.page.wait_for_timeout(action_params.get('timeout', 2000))

        elif action_type in ('click', 'type', 'fill', 'extract_data'):
            action_success = False
            url = self.page.url
            from urllib.parse import urlparse
            domain = urlparse(url).netloc
            intent = node.get('intent', 'unknown')

            # Normalise action name: 'type' is an alias for 'fill'
            _act = 'fill' if action_type == 'type' else action_type

            for tier in range(1, self.max_viewport_tier + 1):
                mem_nodes = await self.context_hash_router.query_by_viewport_tier(
                    domain, intent, execution_context, tier,
                    execution_context.get('org_id', ''),
                )
                for mem_node in mem_nodes:
                    place_value       = mem_node.get('place_value')
                    face_value        = mem_node.get('face_value')
                    locator_priority  = mem_node.get('locator_priority')

                    # resolve() → (success: bool, winning_tier: int|None, locator: str|None)
                    resolve_ok, winning_tier, locator = await self.locator_cascade.resolve(
                        place_value, face_value, _act, action_params, locator_priority
                    )

                    if resolve_ok:
                        self.circuit_breaker.record_success()
                        await self.memory_graph.update_locator_priority(mem_node['id'], winning_tier)
                        await self.memory_graph.update_last_used(mem_node['id'])

                        # Capture extracted text into output_var
                        output_var = node.get('output_var')
                        if output_var and _act == 'extract_data':
                            try:
                                extracted = await self.page.locator(locator).first.text_content()
                                self.workflow_vars[output_var] = extracted
                            except Exception:
                                pass

                        action_success = True
                        break
                if action_success:
                    break

            if not action_success:
                logger.warning("All locators failed, invoking AI healer")
                logger.info("Tier 10 AI healer would be invoked here")
                self.circuit_breaker.record_failure()
                return False

        next_nodes = node.get('next_nodes', [])

        if not next_nodes:
            return success

        execution_mode = node.get('execution_mode', 'sequential')

        # Filter next nodes if condition present
        valid_next_nodes = []
        for next_node in next_nodes:
            if 'condition' in next_node:
                if await self.branch_evaluator.evaluate(next_node['condition'], execution_context, self.workflow_vars):
                    valid_next_nodes.append(next_node)
            else:
                valid_next_nodes.append(next_node)

        if execution_mode == 'sequential':
            for next_node in valid_next_nodes:
                if not await self._execute_node(next_node, execution_context, set(_visited), _depth + 1):
                    return False
        elif execution_mode == 'parallel':
            semaphore = asyncio.Semaphore(self.max_concurrent)

            async def exec_with_sem(n):
                async with semaphore:
                    return await self._execute_node(n, execution_context, set(_visited), _depth + 1)

            results = await asyncio.gather(*(exec_with_sem(n) for n in valid_next_nodes))
            if not all(results):
                return False

        return True
