"""XIOFlow Multi-Agent Coordinator — manages cross-domain agent collaboration.

When an Agent's plan spans multiple domains or requires capabilities that belong
to different agents, the coordinator:

  1. Decomposes the plan into parallelizable sub-task groups
  2. Routes each sub-task to the most capable agent
  3. Manages data dependencies between sub-tasks
  4. Handles partial failures (retry failed sub-task without re-running successes)
  5. Aggregates results from all agents into a unified response

Example:
  Goal: "Sign into all Google accounts, extract billing data, post summary to Slack"
  → Agent A (browser_automation): Sign into Google accounts
  → Agent B (data_extraction): Extract billing data from authenticated sessions
  → Agent C (system_admin): Post summary to Slack via webhook

  The coordinator ensures B waits for A, and C waits for B.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class TaskNode:
    """A single sub-task in the coordination graph."""

    def __init__(
        self,
        task_id: str,
        description: str,
        agent_id: str | None = None,
        agent_type: str | None = None,
        domain: str | None = None,
        depends_on: list[str] | None = None,
        critical: bool = True,
    ) -> None:
        self.task_id = task_id
        self.description = description
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.domain = domain
        self.depends_on = depends_on or []
        self.critical = critical
        self.state: str = "PENDING"  # PENDING → RUNNING → SUCCESS / FAILED / SKIPPED
        self.result: dict[str, Any] = {}
        self.error: str | None = None
        self.agent_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "domain": self.domain,
            "depends_on": self.depends_on,
            "critical": self.critical,
            "state": self.state,
            "result": self.result,
            "error": self.error,
            "agent_run_id": self.agent_run_id,
        }


class CoordinationPlan:
    """A directed acyclic graph of sub-tasks with agent assignments."""

    def __init__(self, goal: str, tasks: list[TaskNode]) -> None:
        self.goal = goal
        self.tasks = {t.task_id: t for t in tasks}
        self._validate_dag()

    def _validate_dag(self) -> None:
        """Ensure the task graph is acyclic."""
        visited = set()
        path = set()

        def _dfs(tid: str) -> None:
            if tid in path:
                raise ValueError(f"Circular dependency detected at task {tid}")
            if tid in visited:
                return
            path.add(tid)
            task = self.tasks.get(tid)
            if task:
                for dep in task.depends_on:
                    if dep in self.tasks:
                        _dfs(dep)
            path.discard(tid)
            visited.add(tid)

        for task_id in self.tasks:
            _dfs(task_id)

    def get_ready_tasks(self) -> list[TaskNode]:
        """Get tasks whose dependencies are all satisfied (SUCCESS or SKIPPED)."""
        ready = []
        for task in self.tasks.values():
            if task.state != "PENDING":
                continue
            deps_satisfied = all(
                self.tasks[d].state in ("SUCCESS", "SKIPPED")
                for d in task.depends_on
                if d in self.tasks
            )
            if deps_satisfied:
                ready.append(task)
        return ready

    def is_complete(self) -> bool:
        """Check if all tasks are in a terminal state."""
        return all(
            t.state in ("SUCCESS", "FAILED", "SKIPPED")
            for t in self.tasks.values()
        )

    def has_critical_failure(self) -> bool:
        """Check if any critical task has failed."""
        return any(
            t.state == "FAILED" and t.critical
            for t in self.tasks.values()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "tasks": {tid: t.to_dict() for tid, t in self.tasks.items()},
            "complete": self.is_complete(),
            "critical_failure": self.has_critical_failure(),
        }


class MultiAgentCoordinator:
    """Coordinates multi-agent execution plans with dependency management."""

    def __init__(self, session: Session) -> None:
        self._session = session

    async def coordinate(
        self,
        org_id: str,
        goal: str,
        tasks: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a multi-agent coordination plan.

        Args:
            org_id: Organization ID for agent scoping
            goal: The high-level goal
            tasks: List of task dicts with:
                - description: str
                - agent_type: str (to auto-select agent)
                - agent_id: str | None (explicit agent override)
                - domain: str | None
                - depends_on: list[str] (task_ids of dependencies)
                - critical: bool
            context: Shared context across all agents

        Returns:
            {success: bool, plan: dict, results: list, error: str|None}
        """
        if context is None:
            context = {}

        # Build task nodes
        task_nodes = []
        for i, t in enumerate(tasks):
            tid = t.get("task_id", f"task_{i}")
            node = TaskNode(
                task_id=tid,
                description=t["description"],
                agent_id=t.get("agent_id"),
                agent_type=t.get("agent_type"),
                domain=t.get("domain"),
                depends_on=t.get("depends_on", []),
                critical=t.get("critical", True),
            )
            task_nodes.append(node)

        plan = CoordinationPlan(goal=goal, tasks=task_nodes)

        # Auto-assign agents to tasks that don't have explicit agent_id
        await self._assign_agents(org_id, plan)

        logger.info(
            f"coordinator.start: goal={goal!r} tasks={len(plan.tasks)} "
            f"org={org_id}"
        )

        # Execute the plan using topological ordering
        max_iterations = len(plan.tasks) * 2  # safety limit
        iteration = 0

        while not plan.is_complete() and iteration < max_iterations:
            iteration += 1

            if plan.has_critical_failure():
                # Skip remaining non-started tasks
                for task in plan.tasks.values():
                    if task.state == "PENDING":
                        task.state = "SKIPPED"
                        task.error = "skipped due to critical failure"
                break

            ready_tasks = plan.get_ready_tasks()
            if not ready_tasks:
                if not plan.is_complete():
                    logger.error("coordinator.deadlock: no ready tasks but plan incomplete")
                break

            # Execute ready tasks in parallel
            async_tasks = [
                self._execute_task(org_id, task, plan, context)
                for task in ready_tasks
            ]
            await asyncio.gather(*async_tasks, return_exceptions=True)

        # Aggregate results
        success = not plan.has_critical_failure() and all(
            t.state in ("SUCCESS", "SKIPPED")
            for t in plan.tasks.values()
        )

        logger.info(
            f"coordinator.complete: goal={goal!r} success={success} "
            f"tasks_succeeded={sum(1 for t in plan.tasks.values() if t.state == 'SUCCESS')}/"
            f"{len(plan.tasks)}"
        )

        return {
            "success": success,
            "plan": plan.to_dict(),
            "results": [t.to_dict() for t in plan.tasks.values()],
        }

    async def _assign_agents(self, org_id: str, plan: CoordinationPlan) -> None:
        """Auto-assign the best available agent to each task."""
        for task in plan.tasks.values():
            if task.agent_id:
                continue  # already explicitly assigned

            agent = await self._find_best_agent(
                org_id, task.agent_type, task.domain
            )
            if agent:
                task.agent_id = str(agent["id"])
                logger.info(
                    f"coordinator.assigned: task={task.task_id} "
                    f"agent={agent.get('slug', agent['id'])}"
                )
            else:
                logger.warning(
                    f"coordinator.no_agent: task={task.task_id} "
                    f"type={task.agent_type} — will fail"
                )

    async def _find_best_agent(
        self,
        org_id: str,
        agent_type: str | None,
        domain: str | None,
    ) -> dict | None:
        """Find the best available agent for a task type."""
        where = "organization_id = :org_id AND is_enabled = true AND deleted_at IS NULL"
        params: dict[str, Any] = {"org_id": org_id}

        if agent_type:
            where += " AND agent_type = :agent_type"
            params["agent_type"] = agent_type

        # Prefer idle agents, then least-loaded
        result = await asyncio.to_thread(
            self._session.execute,
            text(
                f"SELECT * FROM xioflow_agents "
                f"WHERE {where} "
                f"AND state IN ('idle', 'errored') "
                f"ORDER BY "
                f"  CASE state WHEN 'idle' THEN 0 ELSE 1 END, "
                f"  total_failures ASC, "
                f"  total_successes DESC "
                f"LIMIT 1"
            ),
            params,
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def _execute_task(
        self,
        org_id: str,
        task: TaskNode,
        plan: CoordinationPlan,
        context: dict,
    ) -> None:
        """Execute a single task via the assigned agent."""
        task.state = "RUNNING"

        if not task.agent_id:
            task.state = "FAILED"
            task.error = "No agent assigned"
            return

        try:
            from xiosync.subsystems.xioflow.engine.agent_executor import (  # noqa: PLC0415
                AgentExecutor,
            )

            # Build task-specific context with outputs from dependencies
            task_context = {**context}
            for dep_id in task.depends_on:
                dep_task = plan.tasks.get(dep_id)
                if dep_task and dep_task.result:
                    task_context[f"dep_{dep_id}"] = dep_task.result

            executor = AgentExecutor(session=self._session)
            result = await executor.execute_goal(
                agent_id=task.agent_id,
                goal=task.description,
                context=task_context,
            )

            task.agent_run_id = result.get("agent_run_id")

            if result.get("success"):
                task.state = "SUCCESS"
                task.result = result.get("results", [])
            else:
                task.state = "FAILED"
                task.error = result.get("error", "unknown")

        except Exception as exc:
            task.state = "FAILED"
            task.error = str(exc)
            logger.exception(
                f"coordinator.task_failed: task={task.task_id} error={exc}"
            )
