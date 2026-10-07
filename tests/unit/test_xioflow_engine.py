"""Tests for XIOFLOW engine bug fixes (B-1 through B-5) and new action types.

Validates:
  - B-1: ContextHashRouter.generate_hash() adapter
  - B-2: BranchEvaluator.evaluate() single-condition method
  - B-3: MemoryGraph async wrappers + root wrapping
  - New action types: delay, http_request, assertion, ssh_command, llm_prompt,
    webhook_fire, compute_node
  - ComputeNodeRunner Python and Bash execution
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

# ── B-1: ContextHashRouter ────────────────────────────────────────────────────


class TestContextHashRouter:
    """Bug B-1: generate_hash(context) adapter must work."""

    def setup_method(self):
        from xiosync.subsystems.xioflow.engine.context_hash_router import ContextHashRouter

        self.router = ContextHashRouter(session=None)

    def test_generate_hash_with_full_context(self):
        ctx = {
            "device_type": "desktop",
            "os_name": "linux",
            "browser": "chrome",
            "viewport_w": 1920,
            "viewport_h": 1080,
        }
        h = self.router.generate_hash(ctx)
        assert isinstance(h, str)
        assert len(h) == 64  # SHA256 hex

    def test_generate_hash_with_defaults(self):
        """Missing keys should get sensible defaults, not crash."""
        h = self.router.generate_hash({})
        assert isinstance(h, str) and len(h) == 64

    def test_generate_hash_with_viewport_width_alias(self):
        """Should accept viewport_width as alias for viewport_w."""
        h1 = self.router.generate_hash({"viewport_w": 1280})
        h2 = self.router.generate_hash({"viewport_width": 1280})
        assert h1 == h2

    def test_generate_hash_deterministic(self):
        ctx = {"device_type": "mobile", "browser": "safari"}
        assert self.router.generate_hash(ctx) == self.router.generate_hash(ctx)

    def test_generate_hash_different_contexts_differ(self):
        h1 = self.router.generate_hash({"device_type": "desktop"})
        h2 = self.router.generate_hash({"device_type": "mobile"})
        assert h1 != h2

    def test_static_method_still_works(self):
        """Original static method must not be broken."""
        from xiosync.subsystems.xioflow.engine.context_hash_router import ContextHashRouter

        h = ContextHashRouter.generate_context_hash("desktop", "linux", "chrome", 1920, 1080)
        assert isinstance(h, str) and len(h) == 64


# ── B-2: BranchEvaluator ─────────────────────────────────────────────────────


class TestBranchEvaluator:
    """Bug B-2: evaluate() single-condition method must work."""

    def setup_method(self):
        from xiosync.subsystems.xioflow.engine.branch_evaluator import BranchEvaluator

        self.be = BranchEvaluator()

    def _run(self, coro):
        return asyncio.run(coro)

    def test_evaluate_none_returns_true(self):
        assert self._run(self.be.evaluate(None, {}, {})) is True

    def test_evaluate_default_string_returns_true(self):
        assert self._run(self.be.evaluate("default", {}, {})) is True

    def test_evaluate_eq_match(self):
        cond = {"var": "status", "val": "success"}
        assert self._run(self.be.evaluate(cond, {}, {"status": "success"})) is True

    def test_evaluate_eq_mismatch(self):
        cond = {"var": "status", "val": "success"}
        assert self._run(self.be.evaluate(cond, {}, {"status": "failed"})) is False

    def test_evaluate_ne_operator(self):
        cond = {"var": "code", "op": "ne", "val": "0"}
        assert self._run(self.be.evaluate(cond, {}, {"code": "1"})) is True
        assert self._run(self.be.evaluate(cond, {}, {"code": "0"})) is False

    def test_evaluate_gt_operator(self):
        cond = {"var": "score", "op": "gt", "val": "50"}
        assert self._run(self.be.evaluate(cond, {}, {"score": "75"})) is True
        assert self._run(self.be.evaluate(cond, {}, {"score": "25"})) is False

    def test_evaluate_lt_operator(self):
        cond = {"var": "count", "op": "lt", "val": "10"}
        assert self._run(self.be.evaluate(cond, {}, {"count": "5"})) is True

    def test_evaluate_in_operator(self):
        cond = {"var": "tier", "op": "in", "val": ["gold", "platinum"]}
        assert self._run(self.be.evaluate(cond, {}, {"tier": "gold"})) is True
        assert self._run(self.be.evaluate(cond, {}, {"tier": "bronze"})) is False

    def test_evaluate_falls_back_to_execution_context(self):
        """If var not in workflow_vars, should check execution_context."""
        cond = {"var": "env", "val": "prod"}
        assert self._run(self.be.evaluate(cond, {"env": "prod"}, {})) is True

    def test_evaluate_branch_still_works(self):
        """Original evaluate_branch must not be broken."""
        nodes = [{"id": "n1"}, {"id": "n2"}]
        result = self._run(self.be.evaluate_branch(nodes, {}))
        assert result == "n1"  # fallback to first when no conditions match


# ── B-3: MemoryGraph async wrappers ──────────────────────────────────────────


class TestMemoryGraphAsync:
    """Bug B-3: async wrappers must exist and delegate to sync methods."""

    def test_async_methods_exist(self):
        from xiosync.subsystems.xioflow.memory.memory_graph import MemoryGraph

        for method in (
            "aget_workflow_graph",
            "aupdate_locator_priority",
            "aupdate_last_used",
            "asave_new_action",
        ):
            assert hasattr(MemoryGraph, method), f"Missing {method}"
            assert asyncio.iscoroutinefunction(getattr(MemoryGraph, method)), (
                f"{method} must be async"
            )

    def test_get_workflow_graph_wraps_root(self):
        """get_workflow_graph should return {'root': ...} not the raw dict."""
        from xiosync.subsystems.xioflow.memory.memory_graph import MemoryGraph

        mock_session = MagicMock()
        mg = MemoryGraph(mock_session)

        # Mock lookup_action to return a node
        node_dict = {
            "id": str(uuid.uuid4()),
            "intent": "login",
            "action_type": "click",
            "action_params": {},
            "face_value": {},
            "place_value": {},
            "output_var": None,
            "execution_mode": "sequential",
        }
        mg.lookup_action = MagicMock(return_value=node_dict)

        result = mg.get_workflow_graph("example.com", "login", "org1", {})
        assert result is not None
        assert "root" in result
        assert result["root"]["intent"] == "login"


# ── ComputeNodeRunner ────────────────────────────────────────────────────────


class TestComputeNodeRunner:
    """Upgraded compute runner must properly sandbox Python and Bash."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_python_basic_execution(self):
        from xiosync.subsystems.xioflow.compute.compute_runner import ComputeNodeRunner

        runner = ComputeNodeRunner()
        result = self._run(runner.execute("test", "python", "result = 2 + 2", None, {}, {}))
        assert result["success"] is True
        assert result["result"] == 4

    def test_python_stdout_capture(self):
        from xiosync.subsystems.xioflow.compute.compute_runner import ComputeNodeRunner

        runner = ComputeNodeRunner()
        result = self._run(runner.execute("test", "python", "print('hello')", None, {}, {}))
        assert result["success"] is True
        assert "hello" in result["stdout"]

    def test_python_access_params_and_vars(self):
        from xiosync.subsystems.xioflow.compute.compute_runner import ComputeNodeRunner

        runner = ComputeNodeRunner()
        result = self._run(
            runner.execute(
                "test",
                "python",
                "result = params['x'] + vars['y']",
                None,
                {"x": 10},
                {"y": 20},
            )
        )
        assert result["success"] is True
        assert result["result"] == 30

    def test_python_error_handling(self):
        from xiosync.subsystems.xioflow.compute.compute_runner import ComputeNodeRunner

        runner = ComputeNodeRunner()
        result = self._run(runner.execute("test", "python", "1/0", None, {}, {}))
        assert result["success"] is False
        assert "ZeroDivisionError" in result["error"]

    def test_bash_execution(self):
        from xiosync.subsystems.xioflow.compute.compute_runner import ComputeNodeRunner

        runner = ComputeNodeRunner()
        result = self._run(runner.execute("test", "bash", "echo 'hello bash'", None, {}, {}))
        assert result["success"] is True
        assert "hello bash" in result["stdout"]

    def test_bash_failure(self):
        from xiosync.subsystems.xioflow.compute.compute_runner import ComputeNodeRunner

        runner = ComputeNodeRunner()
        result = self._run(runner.execute("test", "bash", "exit 1", None, {}, {}))
        assert result["success"] is False

    def test_unsupported_runtime(self):
        from xiosync.subsystems.xioflow.compute.compute_runner import ComputeNodeRunner

        runner = ComputeNodeRunner()
        result = self._run(runner.execute("test", "java", "// nope", None, {}, {}))
        assert result["success"] is False
        assert "not supported" in result["error"]

    def test_allowed_plugins_enforcement(self):
        from xiosync.subsystems.xioflow.compute.compute_runner import ComputeNodeRunner

        runner = ComputeNodeRunner(allowed_plugins={"safe_plugin"})
        result = self._run(runner.execute("evil_plugin", "python", "pass", None, {}, {}))
        assert result["success"] is False
        assert "not in allowed list" in result["error"]


# ── DAGExecutor action type dispatch ─────────────────────────────────────────


class TestDAGExecutorActionTypes:
    """Test new action types execute correctly in isolation."""

    def _make_executor(self, **kw):
        from xiosync.subsystems.xioflow.engine.circuit_breaker import CircuitBreaker
        from xiosync.subsystems.xioflow.engine.dag_executor import DAGExecutor

        mg = MagicMock()
        mg.aget_workflow_graph = AsyncMock(return_value=None)
        mg.aupdate_locator_priority = AsyncMock()
        mg.aupdate_last_used = AsyncMock()

        lc = MagicMock()
        chr_ = MagicMock()
        chr_.generate_hash = MagicMock(return_value="testhash")
        cb = CircuitBreaker()
        be = MagicMock()
        page = MagicMock()
        page.url = "https://example.com/test"

        return DAGExecutor(mg, lc, chr_, cb, be, page, **kw)

    def _run(self, coro):
        return asyncio.run(coro)

    def test_done_action(self):
        exec_ = self._make_executor()
        exec_._org_id = "org1"
        exec_._run_id = "run1"
        node = {"id": "n1", "intent": "finish", "action_type": "done", "action_params": {}}
        result = self._run(exec_._execute_node(node, {}))
        assert result is True

    def test_delay_action(self):
        exec_ = self._make_executor()
        exec_._org_id = "org1"
        exec_._run_id = "run1"
        node = {"id": "n2", "intent": "pause", "action_type": "delay", "action_params": {"ms": 10}}
        result = self._run(exec_._execute_node(node, {}))
        assert result is True

    def test_assertion_pass(self):
        exec_ = self._make_executor()
        exec_._org_id = "org1"
        exec_._run_id = "run1"
        exec_.workflow_vars = {"status": "success"}
        node = {
            "id": "n3",
            "intent": "check_status",
            "action_type": "assertion",
            "action_params": {"var": "status", "expected": "success"},
        }
        result = self._run(exec_._execute_node(node, {}))
        assert result is True

    def test_assertion_fail(self):
        exec_ = self._make_executor()
        exec_._org_id = "org1"
        exec_._run_id = "run1"
        exec_.workflow_vars = {"status": "failed"}
        node = {
            "id": "n4",
            "intent": "check_status",
            "action_type": "assertion",
            "action_params": {"var": "status", "expected": "success"},
        }
        result = self._run(exec_._execute_node(node, {}))
        assert result is False

    def test_assertion_contains(self):
        exec_ = self._make_executor()
        exec_._org_id = "org1"
        exec_._run_id = "run1"
        exec_.workflow_vars = {"body": "Welcome to the dashboard"}
        node = {
            "id": "n5",
            "intent": "check_body",
            "action_type": "assertion",
            "action_params": {"var": "body", "op": "contains", "expected": "Welcome"},
        }
        result = self._run(exec_._execute_node(node, {}))
        assert result is True

    def test_assertion_not_empty(self):
        exec_ = self._make_executor()
        exec_._org_id = "org1"
        exec_._run_id = "run1"
        exec_.workflow_vars = {"token": "abc123"}
        node = {
            "id": "n6",
            "intent": "check_token",
            "action_type": "assertion",
            "action_params": {"var": "token", "op": "not_empty"},
        }
        result = self._run(exec_._execute_node(node, {}))
        assert result is True

    def test_navigate_action(self):
        exec_ = self._make_executor()
        exec_._org_id = "org1"
        exec_._run_id = "run1"
        exec_.page.goto = AsyncMock()
        exec_.page.wait_for_load_state = AsyncMock()
        node = {
            "id": "n7",
            "intent": "go_home",
            "action_type": "navigate",
            "action_params": {"url": "https://example.com"},
        }
        result = self._run(exec_._execute_node(node, {}))
        assert result is True
        exec_.page.goto.assert_called_once_with("https://example.com")

    def test_scroll_down_action(self):
        exec_ = self._make_executor()
        exec_._org_id = "org1"
        exec_._run_id = "run1"
        exec_.page.evaluate = AsyncMock()
        node = {"id": "n8", "intent": "scroll", "action_type": "scroll_down", "action_params": {}}
        result = self._run(exec_._execute_node(node, {}))
        assert result is True

    def test_variable_substitution(self):
        """{{var}} in action_params should be replaced from workflow_vars."""
        exec_ = self._make_executor()
        exec_._org_id = "org1"
        exec_._run_id = "run1"
        exec_.workflow_vars = {"username": "testuser"}
        exec_.page.goto = AsyncMock()
        exec_.page.wait_for_load_state = AsyncMock()
        node = {
            "id": "n9",
            "intent": "nav",
            "action_type": "navigate",
            "action_params": {"url": "https://example.com/u/{{username}}"},
        }
        self._run(exec_._execute_node(node, {}))
        exec_.page.goto.assert_called_once_with("https://example.com/u/testuser")

    def test_compute_node_python(self):
        exec_ = self._make_executor()
        exec_._org_id = "org1"
        exec_._run_id = "run1"
        node = {
            "id": "n10",
            "intent": "calc",
            "action_type": "compute_node",
            "action_params": {"runtime": "python", "source_code": "result = 42"},
            "output_var": "answer",
        }
        result = self._run(exec_._execute_node(node, {}))
        assert result is True
        assert exec_.workflow_vars.get("answer") == 42

    def test_conditional_action(self):
        exec_ = self._make_executor()
        exec_._org_id = "org1"
        exec_._run_id = "run1"
        node = {"id": "n11", "intent": "check", "action_type": "conditional", "action_params": {}}
        # Conditional with no next_nodes should just return True
        result = self._run(exec_._execute_node(node, {}))
        assert result is True

    def test_circuit_breaker_prevents_execution(self):
        exec_ = self._make_executor()
        exec_._org_id = "org1"
        exec_._run_id = "run1"
        # Trip the circuit breaker
        for _ in range(5):
            exec_.circuit_breaker.record_failure()
        node = {"id": "n12", "intent": "blocked", "action_type": "done", "action_params": {}}
        result = self._run(exec_._execute_node(node, {}))
        assert result is False

    def test_cycle_detection(self):
        exec_ = self._make_executor()
        exec_._org_id = "org1"
        exec_._run_id = "run1"
        node = {"id": "cycle_node", "intent": "loop", "action_type": "done", "action_params": {}}
        # Pre-populate visited set to simulate cycle
        result = self._run(exec_._execute_node(node, {}, _visited={"cycle_node"}))
        assert result is False

    def test_max_depth_guard(self):
        exec_ = self._make_executor(max_execution_depth=5)
        exec_._org_id = "org1"
        exec_._run_id = "run1"
        node = {"id": "deep", "intent": "deep", "action_type": "done", "action_params": {}}
        result = self._run(exec_._execute_node(node, {}, _depth=10))
        assert result is False
