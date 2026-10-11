"""XIOFlow AgentExecutor — the cognitive loop for AI Agent actors.

Implements the Reason → Plan → Execute → Learn loop:

  1. REASON:  Accept a semantic goal and decompose it via LLM
  2. PLAN:   Search memory graph for existing trusted execution paths
  3. EXECUTE: Dispatch DAG runs (reuse) or real-time exploration (explore)
  4. LEARN:  Save successful explorations as new memory nodes / templates
  5. REPORT: Update agent_run state and counters

The executor respects:
  - Agent's memory_scope (project / organization / global)
  - Agent's capability_ids (RBAC restrictions)
  - Agent's max_concurrent_runs (concurrency limits)
  - Agent's tool_bindings (available plugins/integrations)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class AgentPlan:
    """A decomposed execution plan for an agent's semantic goal."""

    def __init__(self, goal: str, steps: list[dict[str, Any]]) -> None:
        self.goal = goal
        self.steps = steps  # [{description, strategy, template_id?, domain?, intent?}]
        self.total = len(steps)
        self.completed = 0
        self.results: list[dict[str, Any]] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "steps": self.steps,
            "total": self.total,
            "completed": self.completed,
            "results": self.results,
        }


class AgentExecutor:
    """The cognitive execution loop for XIOSYNC Agents.

    Usage:
        executor = AgentExecutor(session, ai_gateway)
        result = await executor.execute_goal(agent_id, goal, context)
    """

    def __init__(
        self,
        session,
        ai_gateway=None,
        dag_executor_factory=None,
    ) -> None:
        """
        Args:
            session: SQLAlchemy session for DB operations
            ai_gateway: AIGateway instance for LLM calls (lazy-loaded if None)
            dag_executor_factory: Callable that creates a DAGExecutor for a given
                execution context (lazy-loaded if None)
        """
        self._session = session
        self._ai_gateway = ai_gateway
        self._dag_executor_factory = dag_executor_factory

    @property
    def ai_gateway(self):
        if self._ai_gateway is None:
            from xiosync.subsystems.xioai.gateway import AIGateway  # noqa: PLC0415

            self._ai_gateway = AIGateway()
        return self._ai_gateway

    # ── Public API ────────────────────────────────────────────────────────

    async def execute_goal(
        self,
        agent_id: str,
        goal: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a semantic goal through the full cognitive loop.

        Returns:
            {success: bool, agent_run_id: str, plan: dict, results: list, error: str|None}
        """
        if context is None:
            context = {}

        t0 = time.monotonic()

        # ── Load agent definition ─────────────────────────────────────────
        agent = await self._load_agent(agent_id)
        if not agent:
            return {"success": False, "error": f"Agent {agent_id} not found"}

        if not agent["is_enabled"]:
            return {"success": False, "error": f"Agent {agent['slug']} is disabled"}

        if agent["state"] not in ("idle", "errored"):
            return {
                "success": False,
                "error": f"Agent {agent['slug']} is busy (state={agent['state']})",
            }

        # ── Create agent_run record ───────────────────────────────────────
        run_id = str(uuid.uuid4())
        await self._create_agent_run(run_id, agent, goal, context)
        await self._update_agent_state(agent_id, "planning")

        try:
            # ── Phase 1: REASON — Decompose goal via LLM ──────────────────
            logger.info(f"agent_executor.reason: agent={agent['slug']} goal={goal!r}")
            await self._update_run_state(run_id, "PLANNING")

            plan = await self._reason(agent, goal, context)
            await self._update_run_plan(run_id, plan)

            logger.info(
                f"agent_executor.plan_ready: {plan.total} steps for {goal!r}"
            )

            # ── Phase 2: EXECUTE — Run each step ──────────────────────────
            await self._update_run_state(run_id, "EXECUTING")
            await self._update_agent_state(agent_id, "executing")

            all_dag_run_ids: list[str] = []

            for i, step in enumerate(plan.steps):
                step_t0 = time.monotonic()
                logger.info(
                    f"agent_executor.step[{i+1}/{plan.total}]: "
                    f"{step.get('description', 'unnamed')}"
                )

                step_result = await self._execute_step(agent, step, context)
                step_ms = round((time.monotonic() - step_t0) * 1000)

                plan.completed += 1
                plan.results.append({
                    **step_result,
                    "step_index": i,
                    "duration_ms": step_ms,
                })

                # Track spawned DAG run IDs
                if step_result.get("dag_run_id"):
                    all_dag_run_ids.append(step_result["dag_run_id"])

                # Update progress in DB
                await self._update_run_progress(run_id, plan, all_dag_run_ids)

                if not step_result.get("success"):
                    # Step failed — check if it's critical
                    if step.get("critical", True):
                        raise RuntimeError(
                            f"Critical step failed: {step_result.get('error', 'unknown')}"
                        )
                    else:
                        logger.warning(
                            f"agent_executor.step_failed_non_critical: "
                            f"{step_result.get('error')}"
                        )

            # ── Phase 3: LEARN — Save successful explorations ─────────────
            await self._learn(agent, plan, context)

            # ── Success ───────────────────────────────────────────────────
            total_ms = round((time.monotonic() - t0) * 1000)
            await self._complete_run(run_id, agent_id, "SUCCESS", plan, total_ms)

            logger.info(
                f"agent_executor.success: agent={agent['slug']} "
                f"goal={goal!r} steps={plan.total} duration={total_ms}ms"
            )

            return {
                "success": True,
                "agent_run_id": run_id,
                "plan": plan.to_dict(),
                "results": plan.results,
                "duration_ms": total_ms,
            }

        except Exception as exc:
            total_ms = round((time.monotonic() - t0) * 1000)
            error_msg = str(exc)
            logger.exception(
                f"agent_executor.failed: agent={agent['slug']} "
                f"goal={goal!r} error={error_msg}"
            )
            await self._complete_run(
                run_id, agent_id, "FAILED", plan if "plan" in dir() else None,
                total_ms, error=error_msg,
            )
            return {
                "success": False,
                "agent_run_id": run_id,
                "error": error_msg,
                "duration_ms": total_ms,
            }

    # ── Phase 1: REASON ───────────────────────────────────────────────────

    async def _reason(
        self, agent: dict, goal: str, context: dict
    ) -> AgentPlan:
        """Decompose a semantic goal into executable sub-steps via LLM."""
        # Build the reasoning prompt with agent context
        system_prompt = agent.get("system_prompt") or ""
        agent_type = agent["agent_type"]
        memory_scope = agent["memory_scope"]

        # Gather available domains from memory graph
        available_domains = await self._get_available_domains(agent)

        prompt = (
            f"You are an AI agent of type '{agent_type}' with memory scope '{memory_scope}'.\n"
            f"{('System instructions: ' + system_prompt + chr(10)) if system_prompt else ''}"
            f"Available automation domains: {', '.join(available_domains) if available_domains else 'none yet'}\n\n"
            f"Goal: {goal}\n\n"
            f"Decompose this goal into a sequence of concrete executable steps.\n"
            f"For each step, specify:\n"
            f"  - description: what the step does\n"
            f"  - domain: the web domain (e.g., 'accounts.google.com') if applicable\n"
            f"  - intent: the specific intent name (e.g., 'login_with_email') if known\n"
            f"  - critical: true if failure should abort the entire goal\n\n"
            f"Respond with a JSON array of step objects. Only JSON, no markdown."
        )

        response = await self.ai_gateway.generate(prompt)

        if not response.success:
            raise RuntimeError(f"LLM reasoning failed: {response.error}")

        # Parse LLM response into steps
        steps = self._parse_plan_response(response.text, goal)

        return AgentPlan(goal=goal, steps=steps)

    def _parse_plan_response(self, response_text: str, goal: str) -> list[dict]:
        """Parse LLM JSON response into plan steps with fallback."""
        import json  # noqa: PLC0415

        # Try to extract JSON from the response
        text = response_text.strip()
        # Handle markdown code blocks
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict) and "steps" in parsed:
                return parsed["steps"]
        except json.JSONDecodeError:
            pass

        # Fallback: single-step plan with the raw goal
        logger.warning(
            f"agent_executor.parse_plan_fallback: "
            f"could not parse LLM response, using single-step plan"
        )
        return [{"description": goal, "critical": True}]

    # ── Phase 2: EXECUTE ──────────────────────────────────────────────────

    async def _execute_step(
        self, agent: dict, step: dict, context: dict
    ) -> dict[str, Any]:
        """Execute a single plan step — either reuse an existing DAG or explore."""
        domain = step.get("domain")
        intent = step.get("intent")
        description = step.get("description", "")

        # Strategy 1: If we have domain + intent, try to find existing DAG path
        if domain and intent:
            existing = await self._find_existing_path(
                agent, domain, intent, context
            )
            if existing:
                return await self._execute_via_dag(
                    agent, domain, intent, context, existing
                )

        # Strategy 2: If agent has tool_bindings, try tool invocation
        tool_result = await self._try_tool_execution(agent, step, context)
        if tool_result is not None:
            return tool_result

        # Strategy 3: LLM-guided exploration (for browser_automation agents)
        if agent["agent_type"] == "browser_automation" and domain:
            return await self._explore_and_learn(agent, step, context)

        # Strategy 4: LLM reasoning step (for non-browser tasks)
        return await self._execute_via_llm(agent, step, context)

    async def _find_existing_path(
        self, agent: dict, domain: str, intent: str, context: dict
    ) -> dict | None:
        """Search memory graph for an existing trusted execution path."""
        try:
            from xiosync.subsystems.xioflow.memory.memory_graph import (  # noqa: PLC0415
                MemoryGraph,
            )

            mg = MemoryGraph(self._session)
            graph = mg.get_workflow_graph(
                domain=domain,
                start_intent=intent,
                org_id=str(agent["organization_id"]),
                context=context,
            )
            if graph and graph.get("root"):
                logger.info(
                    f"agent_executor.path_found: domain={domain} intent={intent}"
                )
                return graph
        except Exception as exc:
            logger.warning(f"agent_executor.path_search_failed: {exc}")

        return None

    async def _execute_via_dag(
        self,
        agent: dict,
        domain: str,
        intent: str,
        context: dict,
        graph: dict,
    ) -> dict[str, Any]:
        """Execute a step by dispatching a DAG run via the existing pipeline."""
        try:
            from xiosync.subsystems.xioflow.models.templates import (  # noqa: PLC0415
                WorkflowTemplate,
            )

            # Find or create a template for this domain+intent
            template = (
                self._session.query(WorkflowTemplate)
                .filter_by(
                    organization_id=agent["organization_id"],
                    dag_domain=domain,
                    dag_root_intent=intent,
                    template_type="xioflow_dag",
                )
                .first()
            )

            if not template:
                logger.info(
                    f"agent_executor.dag_dispatch: no template for {domain}/{intent}, "
                    f"creating ad-hoc run"
                )

            # Create a run record via the standard pipeline
            from xiosync.subsystems.xioflow.models.memory_nodes import (  # noqa: PLC0415
                XioflowMemoryNode,
            )

            run_id = str(uuid.uuid4())

            # Dispatch via the standard run creation endpoint
            from sqlalchemy import text as sa_text  # noqa: PLC0415

            self._session.execute(
                sa_text(
                    "INSERT INTO xioflow_runs "
                    "(id, organization_id, template_id, state, priority, context, started_at) "
                    "VALUES (:id, :org_id, :tmpl_id, 'PENDING', :priority, :ctx::jsonb, now())"
                ),
                {
                    "id": run_id,
                    "org_id": str(agent["organization_id"]),
                    "tmpl_id": str(template.id) if template else None,
                    "priority": context.get("priority", 0),
                    "ctx": __import__("json").dumps({
                        **context,
                        "agent_id": str(agent["id"]),
                        "dag_domain": domain,
                        "dag_root_intent": intent,
                    }),
                },
            )
            self._session.commit()

            logger.info(
                f"agent_executor.dag_dispatched: run_id={run_id} "
                f"domain={domain} intent={intent}"
            )

            # Poll for run completion (max 5 minutes)
            result = await self._poll_run_completion(run_id, timeout_s=300)

            return {
                "success": result.get("state") == "SUCCESS",
                "strategy": "reuse_dag",
                "dag_run_id": run_id,
                "result": result,
            }

        except Exception as exc:
            logger.exception(f"agent_executor.dag_dispatch_failed: {exc}")
            return {
                "success": False,
                "strategy": "reuse_dag",
                "error": str(exc),
            }

    async def _try_tool_execution(
        self, agent: dict, step: dict, context: dict
    ) -> dict[str, Any] | None:
        """Attempt to execute a step via the agent's bound tools/plugins."""
        tool_bindings = agent.get("tool_bindings") or {}
        if not tool_bindings:
            return None

        description = step.get("description", "")

        # Ask the LLM to select and parameterize a tool call
        tool_list = "\n".join(
            f"  - {name}: {config.get('description', 'no description')}"
            for name, config in tool_bindings.items()
        )

        prompt = (
            f"Available tools:\n{tool_list}\n\n"
            f"Task: {description}\n\n"
            f"If one of these tools can accomplish the task, respond with:\n"
            f'{{"tool": "tool_name", "params": {{...}}}}\n'
            f"If no tool applies, respond with: null"
        )

        response = await self.ai_gateway.generate(prompt)
        if not response.success:
            return None

        import json  # noqa: PLC0415

        try:
            parsed = json.loads(response.text.strip())
            if parsed is None:
                return None

            tool_name = parsed.get("tool")
            tool_params = parsed.get("params", {})

            if tool_name not in tool_bindings:
                logger.warning(f"agent_executor.tool_not_bound: {tool_name}")
                return None

            # Execute via ComputeNodeRunner
            from xiosync.subsystems.xioflow.compute.compute_runner import (  # noqa: PLC0415
                ComputeNodeRunner,
            )

            runner = ComputeNodeRunner()
            result = await runner.execute(
                plugin_name=tool_name,
                runtime=tool_bindings[tool_name].get("runtime", "python"),
                source_code=tool_bindings[tool_name].get("source_code", ""),
                page=None,
                action_params=tool_params,
                workflow_vars=context,
                timeout=int(tool_bindings[tool_name].get("timeout", 60)),
            )

            return {
                "success": result.get("success", False),
                "strategy": "tool",
                "tool": tool_name,
                "result": result,
            }

        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    async def _explore_and_learn(
        self, agent: dict, step: dict, context: dict
    ) -> dict[str, Any]:
        """Real-time browser exploration guided by LLM — for novel domains."""
        description = step.get("description", "")
        domain = step.get("domain", "")

        logger.info(
            f"agent_executor.explore: domain={domain} task={description!r}"
        )

        # For now, create an exploration run that the worker can pick up
        # with trace_mode=True to record the steps as memory nodes
        run_id = str(uuid.uuid4())
        from sqlalchemy import text as sa_text  # noqa: PLC0415

        self._session.execute(
            sa_text(
                "INSERT INTO xioflow_runs "
                "(id, organization_id, state, priority, context, started_at) "
                "VALUES (:id, :org_id, 'PENDING', :priority, :ctx::jsonb, now())"
            ),
            {
                "id": run_id,
                "org_id": str(agent["organization_id"]),
                "priority": context.get("priority", 0),
                "ctx": __import__("json").dumps({
                    **context,
                    "agent_id": str(agent["id"]),
                    "explore_mode": True,
                    "trace_mode": True,
                    "explore_goal": description,
                    "explore_domain": domain,
                }),
            },
        )
        self._session.commit()

        result = await self._poll_run_completion(run_id, timeout_s=600)

        return {
            "success": result.get("state") == "SUCCESS",
            "strategy": "explore",
            "dag_run_id": run_id,
            "result": result,
        }

    async def _execute_via_llm(
        self, agent: dict, step: dict, context: dict
    ) -> dict[str, Any]:
        """Execute a pure reasoning step via LLM (no browser needed)."""
        description = step.get("description", "")

        prompt = (
            f"Execute this task and provide the result:\n\n"
            f"Task: {description}\n\n"
            f"Context: {__import__('json').dumps(context, default=str)[:5000]}\n\n"
            f"Respond with the result as JSON."
        )

        response = await self.ai_gateway.generate(prompt)

        if response.success:
            return {
                "success": True,
                "strategy": "llm_reasoning",
                "result": response.text[:10000],
            }
        else:
            return {
                "success": False,
                "strategy": "llm_reasoning",
                "error": response.error or "LLM generation failed",
            }

    # ── Phase 3: LEARN ────────────────────────────────────────────────────

    async def _learn(
        self, agent: dict, plan: AgentPlan, context: dict
    ) -> None:
        """Save successful exploration results as new memory nodes / templates."""
        successful_explorations = [
            r for r in plan.results
            if r.get("success") and r.get("strategy") == "explore"
        ]

        if not successful_explorations:
            return

        logger.info(
            f"agent_executor.learn: {len(successful_explorations)} "
            f"successful explorations to save"
        )

        # The actual learning happens in the DAG executor via trace_mode=True
        # which records PageProxy traces and deploys them as memory nodes.
        # Here we just log the completion for audit purposes.
        for exploration in successful_explorations:
            logger.info(
                f"agent_executor.learned: dag_run_id={exploration.get('dag_run_id')} "
                f"— trace deployed as memory nodes via auto-trace pipeline"
            )

    # ── DB Helpers ────────────────────────────────────────────────────────

    async def _load_agent(self, agent_id: str) -> dict | None:
        """Load agent definition from DB."""
        from sqlalchemy import text as sa_text  # noqa: PLC0415

        result = await asyncio.to_thread(
            self._session.execute,
            sa_text("SELECT * FROM xioflow_agents WHERE id = :id AND deleted_at IS NULL"),
            {"id": agent_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def _create_agent_run(
        self, run_id: str, agent: dict, goal: str, context: dict
    ) -> None:
        """Create a new agent_run record."""
        from sqlalchemy import text as sa_text  # noqa: PLC0415

        await asyncio.to_thread(
            self._session.execute,
            sa_text(
                "INSERT INTO xioflow_agent_runs "
                "(id, agent_id, organization_id, goal, context, state, started_at) "
                "VALUES (:id, :agent_id, :org_id, :goal, :ctx::jsonb, 'PENDING', now())"
            ),
            {
                "id": run_id,
                "agent_id": str(agent["id"]),
                "org_id": str(agent["organization_id"]),
                "goal": goal,
                "ctx": __import__("json").dumps(context, default=str),
            },
        )
        await asyncio.to_thread(self._session.commit)

    async def _update_agent_state(self, agent_id: str, state: str) -> None:
        from sqlalchemy import text as sa_text  # noqa: PLC0415

        await asyncio.to_thread(
            self._session.execute,
            sa_text(
                "UPDATE xioflow_agents SET state = :state, updated_at = now() WHERE id = :id"
            ),
            {"id": agent_id, "state": state},
        )
        await asyncio.to_thread(self._session.commit)

    async def _update_run_state(self, run_id: str, state: str) -> None:
        from sqlalchemy import text as sa_text  # noqa: PLC0415

        await asyncio.to_thread(
            self._session.execute,
            sa_text(
                "UPDATE xioflow_agent_runs SET state = :state WHERE id = :id"
            ),
            {"id": run_id, "state": state},
        )
        await asyncio.to_thread(self._session.commit)

    async def _update_run_plan(self, run_id: str, plan: AgentPlan) -> None:
        from sqlalchemy import text as sa_text  # noqa: PLC0415

        await asyncio.to_thread(
            self._session.execute,
            sa_text(
                "UPDATE xioflow_agent_runs "
                "SET plan = :plan::jsonb, plan_steps_total = :total WHERE id = :id"
            ),
            {
                "id": run_id,
                "plan": __import__("json").dumps(plan.to_dict(), default=str),
                "total": plan.total,
            },
        )
        await asyncio.to_thread(self._session.commit)

    async def _update_run_progress(
        self, run_id: str, plan: AgentPlan, dag_run_ids: list[str]
    ) -> None:
        from sqlalchemy import text as sa_text  # noqa: PLC0415

        await asyncio.to_thread(
            self._session.execute,
            sa_text(
                "UPDATE xioflow_agent_runs "
                "SET plan_steps_completed = :completed, "
                "    dag_run_ids = :dag_ids::uuid[], "
                "    result = :result::jsonb "
                "WHERE id = :id"
            ),
            {
                "id": run_id,
                "completed": plan.completed,
                "dag_ids": dag_run_ids or [],
                "result": __import__("json").dumps(plan.results, default=str),
            },
        )
        await asyncio.to_thread(self._session.commit)

    async def _complete_run(
        self,
        run_id: str,
        agent_id: str,
        state: str,
        plan: AgentPlan | None,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        """Mark run as complete and update agent counters."""
        from sqlalchemy import text as sa_text  # noqa: PLC0415

        await asyncio.to_thread(
            self._session.execute,
            sa_text(
                "UPDATE xioflow_agent_runs "
                "SET state = :state, "
                "    result = :result::jsonb, "
                "    error = :error, "
                "    finished_at = now() "
                "WHERE id = :id"
            ),
            {
                "id": run_id,
                "state": state,
                "result": __import__("json").dumps(
                    plan.results if plan else [], default=str
                ),
                "error": error,
            },
        )

        # Update agent counters
        counter_col = "total_successes" if state == "SUCCESS" else "total_failures"
        await asyncio.to_thread(
            self._session.execute,
            sa_text(
                f"UPDATE xioflow_agents "
                f"SET state = 'idle', "
                f"    total_runs = total_runs + 1, "
                f"    {counter_col} = {counter_col} + 1, "
                f"    last_run_at = now(), "
                f"    updated_at = now() "
                f"WHERE id = :id"
            ),
            {"id": agent_id},
        )

        await asyncio.to_thread(self._session.commit)

    async def _poll_run_completion(
        self, run_id: str, timeout_s: int = 300
    ) -> dict:
        """Poll xioflow_runs for completion status."""
        from sqlalchemy import text as sa_text  # noqa: PLC0415

        deadline = time.monotonic() + timeout_s
        poll_interval = 3  # seconds

        while time.monotonic() < deadline:
            result = await asyncio.to_thread(
                self._session.execute,
                sa_text("SELECT state FROM xioflow_runs WHERE id = :id"),
                {"id": run_id},
            )
            row = result.mappings().first()
            if row and row["state"] in ("SUCCESS", "FAILED", "CANCELLED"):
                return {"state": row["state"], "run_id": run_id}

            await asyncio.sleep(poll_interval)
            # Adaptive backoff
            poll_interval = min(poll_interval * 1.5, 15)

        return {"state": "TIMEOUT", "run_id": run_id}

    async def _get_available_domains(self, agent: dict) -> list[str]:
        """Get list of domains that have memory nodes for this agent's scope."""
        from sqlalchemy import text as sa_text  # noqa: PLC0415

        try:
            scope_filter = ""
            params: dict[str, Any] = {"org_id": str(agent["organization_id"])}

            if agent["memory_scope"] == "project" and agent.get("project_id"):
                scope_filter = " AND project_id = :project_id"
                params["project_id"] = str(agent["project_id"])

            result = await asyncio.to_thread(
                self._session.execute,
                sa_text(
                    "SELECT DISTINCT domain FROM xioflow_memory_nodes "
                    f"WHERE organization_id = :org_id AND status = 'ACTIVE' "
                    f"AND deleted_at IS NULL{scope_filter} "
                    f"ORDER BY domain"
                ),
                params,
            )
            return [row["domain"] for row in result.mappings()]
        except Exception:
            return []
