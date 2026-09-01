"""Unit tests for Wave 3 domain and service layer.

Covers:
- T-2: DAG data flow validation + resolve_node_inputs
- S-1: Secret provider validation, state validation
- R-5: Cron expression validation, next_cron_fire, trigger type validation
- S-3: Extensible constraint evaluator registry
- P-4: VersionGovernanceMiddleware
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

# ═══════════════════════════════════════════════════════════════════════════════
# T-2: Data Flow
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.domain.workflows import (
    DataFlowError,
    WorkflowCycleError,
    WorkflowSpecError,
    resolve_node_inputs,
    validate_data_flow,
    validate_workflow_dag,
)


def _linear_spec(
    *, input_from: Any = None, extra_nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a two-node linear DAG: A → B."""
    nodes = [
        {"id": "A", "capability_id": str(uuid.uuid4())},
        {"id": "B", "capability_id": str(uuid.uuid4()), **({"input_from": input_from} if input_from is not None else {})},
    ]
    if extra_nodes:
        nodes.extend(extra_nodes)
    return {
        "nodes": nodes,
        "edges": [{"from": "A", "to": "B"}],
    }


class TestValidateDataFlow:
    """T-2: validate_data_flow."""

    def test_no_input_from_is_valid(self) -> None:
        spec = _linear_spec()
        validate_workflow_dag(spec)  # Should not raise

    def test_whole_result_shorthand_valid(self) -> None:
        spec = _linear_spec(input_from="A")
        validate_workflow_dag(spec)

    def test_key_level_mapping_valid(self) -> None:
        spec = _linear_spec(input_from={"raw_data": "A.result.data"})
        validate_workflow_dag(spec)

    def test_unknown_upstream_raises(self) -> None:
        spec = _linear_spec(input_from="UNKNOWN")
        with pytest.raises(DataFlowError, match="unknown node"):
            validate_workflow_dag(spec)

    def test_forward_reference_raises(self) -> None:
        """Node B references C, but C is downstream of B."""
        spec = {
            "nodes": [
                {"id": "A", "capability_id": str(uuid.uuid4())},
                {"id": "B", "capability_id": str(uuid.uuid4()), "input_from": "C"},
                {"id": "C", "capability_id": str(uuid.uuid4())},
            ],
            "edges": [{"from": "A", "to": "B"}, {"from": "B", "to": "C"}],
        }
        with pytest.raises(DataFlowError, match="not a topological predecessor"):
            validate_workflow_dag(spec)

    def test_key_level_unknown_upstream_raises(self) -> None:
        spec = _linear_spec(input_from={"x": "UNKNOWN.data"})
        with pytest.raises(DataFlowError, match="unknown node"):
            validate_workflow_dag(spec)

    def test_key_level_forward_ref_raises(self) -> None:
        spec = {
            "nodes": [
                {"id": "A", "capability_id": str(uuid.uuid4())},
                {"id": "B", "capability_id": str(uuid.uuid4()), "input_from": {"x": "C.result"}},
                {"id": "C", "capability_id": str(uuid.uuid4())},
            ],
            "edges": [{"from": "A", "to": "B"}, {"from": "B", "to": "C"}],
        }
        with pytest.raises(DataFlowError, match="not a topological predecessor"):
            validate_workflow_dag(spec)

    def test_invalid_input_from_type_raises(self) -> None:
        spec = _linear_spec(input_from=42)
        with pytest.raises(DataFlowError, match="must be a string or mapping"):
            validate_workflow_dag(spec)

    def test_empty_key_raises(self) -> None:
        spec = _linear_spec(input_from={"": "A.result"})
        with pytest.raises(DataFlowError, match="invalid key"):
            validate_workflow_dag(spec)

    def test_empty_source_path_raises(self) -> None:
        spec = _linear_spec(input_from={"x": ""})
        with pytest.raises(DataFlowError, match="invalid source path"):
            validate_workflow_dag(spec)


class TestResolveNodeInputs:
    """T-2: resolve_node_inputs runtime resolution."""

    def test_whole_result_shorthand(self) -> None:
        result = resolve_node_inputs("A", {"A": {"foo": "bar"}})
        assert result == {"foo": "bar"}

    def test_whole_result_non_mapping(self) -> None:
        result = resolve_node_inputs("A", {"A": 42})
        assert result == {"_result": 42}

    def test_key_level_mapping(self) -> None:
        upstream = {"fetch": {"result": {"data": [1, 2, 3], "meta": "x"}}}
        result = resolve_node_inputs({"raw": "fetch.result.data"}, upstream)
        assert result == {"raw": [1, 2, 3]}

    def test_key_level_missing_path(self) -> None:
        upstream = {"fetch": {"result": {}}}
        result = resolve_node_inputs({"raw": "fetch.result.data"}, upstream)
        assert result == {"raw": None}

    def test_key_level_missing_upstream(self) -> None:
        result = resolve_node_inputs({"raw": "missing.result"}, {})
        assert result == {"raw": None}

    def test_multi_key_mapping(self) -> None:
        upstream = {
            "A": {"x": 1},
            "B": {"y": 2},
        }
        result = resolve_node_inputs(
            {"from_a": "A.x", "from_b": "B.y"},
            upstream,
        )
        assert result == {"from_a": 1, "from_b": 2}


# ═══════════════════════════════════════════════════════════════════════════════
# S-1: Secrets Domain
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.domain.secrets import (
    InvalidSecretStateError,
    PROVIDER_TYPES,
    SECRET_STATES,
    validate_provider,
    validate_secret_state,
)


class TestSecretsDomain:
    """S-1: secrets domain validation."""

    def test_valid_providers(self) -> None:
        for p in PROVIDER_TYPES:
            validate_provider(p)  # Should not raise

    def test_invalid_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown secret provider"):
            validate_provider("invalid_provider")

    def test_valid_states(self) -> None:
        for s in SECRET_STATES:
            validate_secret_state(s)

    def test_invalid_state_raises(self) -> None:
        with pytest.raises(InvalidSecretStateError, match="invalid secret state"):
            validate_secret_state("invalid_state")


# ═══════════════════════════════════════════════════════════════════════════════
# R-5: Triggers Domain
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.domain.triggers import (
    TRIGGER_TYPES,
    TRIGGER_STATES,
    validate_cron_expression,
    validate_trigger_state,
    validate_trigger_type,
    next_cron_fire,
)


class TestTriggersDomain:
    """R-5: triggers domain validation."""

    def test_valid_trigger_types(self) -> None:
        for t in TRIGGER_TYPES:
            validate_trigger_type(t)

    def test_invalid_trigger_type_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown trigger type"):
            validate_trigger_type("invalid")

    def test_valid_trigger_states(self) -> None:
        for s in TRIGGER_STATES:
            validate_trigger_state(s)

    def test_invalid_trigger_state_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid trigger state"):
            validate_trigger_state("invalid")

    def test_valid_cron_expressions(self) -> None:
        for expr in ["* * * * *", "0 */2 * * *", "30 9 * * 1-5", "0 0 1 * *"]:
            validate_cron_expression(expr)

    def test_invalid_cron_too_few_fields(self) -> None:
        with pytest.raises(ValueError, match="must have exactly 5"):
            validate_cron_expression("* * *")

    def test_invalid_cron_too_many_fields(self) -> None:
        with pytest.raises(ValueError, match="must have exactly 5"):
            validate_cron_expression("* * * * * *")

    def test_next_cron_fire_advances(self) -> None:
        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        next_fire = next_cron_fire("*/5 * * * *", now)
        assert next_fire is not None
        assert next_fire > now

    def test_next_cron_fire_every_minute(self) -> None:
        now = datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone.utc)
        next_fire = next_cron_fire("* * * * *", now)
        assert next_fire is not None
        assert next_fire == datetime(2026, 1, 1, 12, 31, 0, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════════
# S-3: Extensible Grant Constraints
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.domain.authorization import (
    Actor as AuthActor,
    Decision,
    Grant,
    Organization as AuthOrg,
    Resource,
    authorize,
    register_constraint_evaluator,
    unregister_constraint_evaluator,
    get_supported_constraints,
)


class TestExtensibleConstraints:
    """S-3: extensible constraint evaluator registry."""

    def test_builtin_constraints_registered(self) -> None:
        supported = get_supported_constraints()
        assert "operations" in supported
        assert "resource_types" in supported
        assert "resource_ids" in supported
        assert "minimum_trust_tier" in supported
        assert "not_before" in supported
        assert "not_after" in supported
        assert "arguments" in supported
        assert "rate" in supported

    def test_custom_constraint_register_and_deny(self) -> None:
        """Register a custom constraint, verify it's checked, then unregister."""
        def _always_deny(value: Any, *args: Any) -> bool:
            return False

        register_constraint_evaluator("test_custom", _always_deny)
        try:
            assert "test_custom" in get_supported_constraints()

            now = datetime.now(timezone.utc)
            org_id = uuid.uuid4()
            actor = AuthActor(id=uuid.uuid4(), organization_id=org_id, state="active", trust_tier="admin")
            org = AuthOrg(id=org_id, state="active")
            resource = Resource(type="test", id=uuid.uuid4(), organization_id=org_id)
            grant = Grant(
                id=uuid.uuid4(), organization_id=org_id, actor_id=actor.id,
                capability="test.cap", state="active",
                constraints={"test_custom": "anything"},
            )

            decision = authorize(
                requested_organization_id=org_id, actor=actor, organization=org,
                resource=resource, capability="test.cap", operation="read",
                grants=[grant], arguments={}, now=now,
            )
            assert not decision.allowed
            assert decision.reason == "constraints_unsatisfied"
        finally:
            unregister_constraint_evaluator("test_custom")

    def test_custom_constraint_register_and_allow(self) -> None:
        """Register a custom constraint that always allows."""
        def _always_allow(value: Any, *args: Any) -> bool:
            return True

        register_constraint_evaluator("test_custom_allow", _always_allow)
        try:
            now = datetime.now(timezone.utc)
            org_id = uuid.uuid4()
            actor = AuthActor(id=uuid.uuid4(), organization_id=org_id, state="active", trust_tier="admin")
            org = AuthOrg(id=org_id, state="active")
            resource = Resource(type="test", id=uuid.uuid4(), organization_id=org_id)
            grant = Grant(
                id=uuid.uuid4(), organization_id=org_id, actor_id=actor.id,
                capability="test.cap", state="active",
                constraints={"test_custom_allow": "anything"},
            )

            decision = authorize(
                requested_organization_id=org_id, actor=actor, organization=org,
                resource=resource, capability="test.cap", operation="read",
                grants=[grant], arguments={}, now=now,
            )
            assert decision.allowed
        finally:
            unregister_constraint_evaluator("test_custom_allow")

    def test_unknown_constraint_denies_by_default(self) -> None:
        now = datetime.now(timezone.utc)
        org_id = uuid.uuid4()
        actor = AuthActor(id=uuid.uuid4(), organization_id=org_id, state="active", trust_tier="admin")
        org = AuthOrg(id=org_id, state="active")
        resource = Resource(type="test", id=uuid.uuid4(), organization_id=org_id)
        grant = Grant(
            id=uuid.uuid4(), organization_id=org_id, actor_id=actor.id,
            capability="test.cap", state="active",
            constraints={"completely_unknown_constraint": True},
        )

        decision = authorize(
            requested_organization_id=org_id, actor=actor, organization=org,
            resource=resource, capability="test.cap", operation="read",
            grants=[grant], arguments={}, now=now,
        )
        assert not decision.allowed

    def test_unregister_removes_constraint(self) -> None:
        register_constraint_evaluator("temp", lambda *a: True)
        assert "temp" in get_supported_constraints()
        unregister_constraint_evaluator("temp")
        assert "temp" not in get_supported_constraints()


# ═══════════════════════════════════════════════════════════════════════════════
# P-4: Version Governance Middleware
# ═══════════════════════════════════════════════════════════════════════════════

from starlette.testclient import TestClient
from fastapi import FastAPI


class TestVersionGovernanceMiddleware:
    """P-4: API version governance middleware."""

    def _make_app(self, deprecation_config: dict[str, Any] | None = None) -> FastAPI:
        from xiosync.api.middleware.versioning import VersionGovernanceMiddleware

        app = FastAPI()
        app.add_middleware(VersionGovernanceMiddleware, deprecation_config=deprecation_config or {})

        @app.get("/test")
        def _test() -> dict[str, str]:
            return {"ok": "yes"}

        return app

    def test_version_header_present(self) -> None:
        app = self._make_app()
        client = TestClient(app)
        resp = client.get("/test")
        assert resp.status_code == 200
        assert resp.headers["X-API-Version"] == "1.0"

    def test_accept_version_acknowledged(self) -> None:
        app = self._make_app()
        client = TestClient(app)
        resp = client.get("/test", headers={"Accept-Version": "2.0"})
        assert resp.headers["X-Accepted-Version"] == "2.0"

    def test_sunset_header_on_deprecated_endpoint(self) -> None:
        config = {"GET /test": {"sunset": "2027-06-01", "deprecation": "2027-01-01"}}
        app = self._make_app(deprecation_config=config)
        client = TestClient(app)
        resp = client.get("/test")
        assert resp.headers["Sunset"] == "2027-06-01"
        assert resp.headers["Deprecation"] == "2027-01-01"

    def test_no_sunset_on_unconfigured_endpoint(self) -> None:
        app = self._make_app()
        client = TestClient(app)
        resp = client.get("/test")
        assert "Sunset" not in resp.headers
        assert "Deprecation" not in resp.headers


# ═══════════════════════════════════════════════════════════════════════════════
# Event Types Registration
# ═══════════════════════════════════════════════════════════════════════════════

from xiosync.domain.events import EVENT_TYPES


class TestEventTypes:
    """Verify Wave 3 event types are registered."""

    def test_task_output_registered(self) -> None:
        assert "task.output" in EVENT_TYPES

    def test_webhook_dispatch_registered(self) -> None:
        assert "webhook.dispatch" in EVENT_TYPES

    def test_existing_types_still_registered(self) -> None:
        for t in ["state_change", "action_executed", "error", "heartbeat"]:
            assert t in EVENT_TYPES


# ═══════════════════════════════════════════════════════════════════════════════
# Config Key Registration
# ═══════════════════════════════════════════════════════════════════════════════


class TestConfigKeys:
    """Verify Wave 3 config keys are registered."""

    def test_api_deprecation_config_registered(self) -> None:
        from xiosync.platform.config import _KNOWN_PREFIXED_KEYS
        assert "XIOSYNC_API_DEPRECATION_CONFIG" in _KNOWN_PREFIXED_KEYS

    def test_cross_org_sharing_registered(self) -> None:
        from xiosync.platform.config import _KNOWN_PREFIXED_KEYS
        assert "XIOSYNC_ENABLE_CROSS_ORG_SHARING" in _KNOWN_PREFIXED_KEYS
