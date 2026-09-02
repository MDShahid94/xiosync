"""RBAC enforcement tests — verify capability group checks across all roles.

Tests the ``require_capability()`` FastAPI dependency and its integration
with router-level RBAC applied in ``app.py``.  Each test constructs a
minimal ``Request`` with a known ``OrgContext`` and verifies the expected
allow/deny outcome.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from xiosync.api.middleware.rbac import (
    CapabilityDeniedError,
    _check_role,
    require_capability,
)
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")
_ACTOR_ID = uuid.UUID("00000000-0000-7000-8000-000000000002")
_IDENTITY_ID = uuid.UUID("00000000-0000-7000-8000-aaaaaaaaaaaa")
_SESSION_ID = uuid.UUID("00000000-0000-7000-8000-bbbbbbbbbbbb")


def _make_context(
    membership_role: MembershipRole = MembershipRole.ORG_MEMBER,
    platform_role: PlatformRole = PlatformRole.NONE,
) -> OrgContext:
    return OrgContext(
        auth_identity_id=_IDENTITY_ID,
        actor_id=_ACTOR_ID,
        organization_id=_ORG_ID,
        session_id=_SESSION_ID,
        platform_role=platform_role,
        membership_role=membership_role,
    )


def _make_request(context: OrgContext | None) -> MagicMock:
    req = MagicMock()
    req.state.org_context = context
    req.state.request_id = "test-req-001"
    return req


# ---------------------------------------------------------------------------
# _check_role unit tests
# ---------------------------------------------------------------------------

class TestCheckRole:
    """Unit tests for the fast-path role hierarchy check."""

    def test_platform_admin_always_passes(self) -> None:
        ctx = _make_context(
            membership_role=MembershipRole.ORG_VIEWER,
            platform_role=PlatformRole.PLATFORM_ADMIN,
        )
        assert _check_role(ctx, "platform.admin") is True
        assert _check_role(ctx, "plugin.admin") is True
        assert _check_role(ctx, "workflow.manage") is True
        assert _check_role(ctx, "readonly") is True

    def test_owner_passes_all_groups(self) -> None:
        ctx = _make_context(membership_role=MembershipRole.ORG_OWNER)
        assert _check_role(ctx, "platform.admin") is True
        assert _check_role(ctx, "org.manage") is True
        assert _check_role(ctx, "plugin.admin") is True
        assert _check_role(ctx, "workflow.manage") is True
        assert _check_role(ctx, "readonly") is True

    def test_admin_passes_admin_and_below(self) -> None:
        ctx = _make_context(membership_role=MembershipRole.ORG_ADMIN)
        assert _check_role(ctx, "org.manage") is True
        assert _check_role(ctx, "plugin.admin") is True
        assert _check_role(ctx, "actor.manage") is True
        assert _check_role(ctx, "workflow.manage") is True
        assert _check_role(ctx, "readonly") is True

    def test_admin_denied_owner_only(self) -> None:
        ctx = _make_context(membership_role=MembershipRole.ORG_ADMIN)
        assert _check_role(ctx, "platform.admin") is False

    def test_member_passes_member_groups(self) -> None:
        ctx = _make_context(membership_role=MembershipRole.ORG_MEMBER)
        assert _check_role(ctx, "workflow.manage") is True
        assert _check_role(ctx, "task.execute") is True
        assert _check_role(ctx, "event.manage") is True
        assert _check_role(ctx, "artifact.manage") is True
        assert _check_role(ctx, "ontology.manage") is True
        assert _check_role(ctx, "dlq.manage") is True
        assert _check_role(ctx, "trigger.manage") is True
        assert _check_role(ctx, "readonly") is True

    def test_member_denied_admin_groups(self) -> None:
        ctx = _make_context(membership_role=MembershipRole.ORG_MEMBER)
        assert _check_role(ctx, "platform.admin") is False
        assert _check_role(ctx, "org.manage") is False
        assert _check_role(ctx, "plugin.admin") is False
        assert _check_role(ctx, "actor.manage") is False
        assert _check_role(ctx, "secret.manage") is False
        assert _check_role(ctx, "worker.manage") is False
        assert _check_role(ctx, "share.manage") is False
        assert _check_role(ctx, "webhook.manage") is False
        assert _check_role(ctx, "capability.manage") is False

    def test_viewer_passes_readonly_only(self) -> None:
        ctx = _make_context(membership_role=MembershipRole.ORG_VIEWER)
        assert _check_role(ctx, "readonly") is True
        assert _check_role(ctx, "metering.read") is True

    def test_viewer_denied_everything_else(self) -> None:
        ctx = _make_context(membership_role=MembershipRole.ORG_VIEWER)
        assert _check_role(ctx, "workflow.manage") is False
        assert _check_role(ctx, "task.execute") is False
        assert _check_role(ctx, "plugin.admin") is False
        assert _check_role(ctx, "org.manage") is False
        assert _check_role(ctx, "actor.manage") is False
        assert _check_role(ctx, "platform.admin") is False

    def test_unknown_group_denied(self) -> None:
        ctx = _make_context(membership_role=MembershipRole.ORG_OWNER)
        # Unknown groups are denied (fail-closed) — even for owners
        assert _check_role(ctx, "nonexistent.group") is False

    def test_platform_admin_passes_unknown_group(self) -> None:
        ctx = _make_context(
            membership_role=MembershipRole.ORG_VIEWER,
            platform_role=PlatformRole.PLATFORM_ADMIN,
        )
        assert _check_role(ctx, "nonexistent.group") is True


# ---------------------------------------------------------------------------
# require_capability() async dependency tests
# ---------------------------------------------------------------------------

class TestRequireCapability:
    """Tests for the FastAPI dependency returned by require_capability()."""

    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self) -> None:
        dep = require_capability("readonly")
        # Extract the inner dependency function
        dep_fn = dep.dependency
        req = _make_request(None)
        with pytest.raises(HTTPException) as exc_info:
            await dep_fn(req)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_viewer_allowed_readonly(self) -> None:
        dep = require_capability("readonly")
        dep_fn = dep.dependency
        ctx = _make_context(membership_role=MembershipRole.ORG_VIEWER)
        req = _make_request(ctx)
        # Should not raise
        await dep_fn(req)

    @pytest.mark.asyncio
    async def test_viewer_denied_workflow_manage(self) -> None:
        dep = require_capability("workflow.manage")
        dep_fn = dep.dependency
        ctx = _make_context(membership_role=MembershipRole.ORG_VIEWER)
        req = _make_request(ctx)
        with pytest.raises(CapabilityDeniedError) as exc_info:
            await dep_fn(req)
        assert exc_info.value.status_code == 403
        assert "workflow.manage" in exc_info.value.detail["detail"]

    @pytest.mark.asyncio
    async def test_member_allowed_task_execute(self) -> None:
        dep = require_capability("task.execute")
        dep_fn = dep.dependency
        ctx = _make_context(membership_role=MembershipRole.ORG_MEMBER)
        req = _make_request(ctx)
        await dep_fn(req)

    @pytest.mark.asyncio
    async def test_member_denied_plugin_admin(self) -> None:
        dep = require_capability("plugin.admin")
        dep_fn = dep.dependency
        ctx = _make_context(membership_role=MembershipRole.ORG_MEMBER)
        req = _make_request(ctx)
        with pytest.raises(CapabilityDeniedError):
            await dep_fn(req)

    @pytest.mark.asyncio
    async def test_admin_allowed_plugin_admin(self) -> None:
        dep = require_capability("plugin.admin")
        dep_fn = dep.dependency
        ctx = _make_context(membership_role=MembershipRole.ORG_ADMIN)
        req = _make_request(ctx)
        await dep_fn(req)

    @pytest.mark.asyncio
    async def test_platform_admin_bypasses_all(self) -> None:
        dep = require_capability("platform.admin")
        dep_fn = dep.dependency
        ctx = _make_context(
            membership_role=MembershipRole.ORG_VIEWER,
            platform_role=PlatformRole.PLATFORM_ADMIN,
        )
        req = _make_request(ctx)
        await dep_fn(req)

    @pytest.mark.asyncio
    async def test_denied_error_contains_problem_detail(self) -> None:
        dep = require_capability("secret.manage")
        dep_fn = dep.dependency
        ctx = _make_context(membership_role=MembershipRole.ORG_MEMBER)
        req = _make_request(ctx)
        with pytest.raises(CapabilityDeniedError) as exc_info:
            await dep_fn(req)
        detail = exc_info.value.detail
        assert detail["type"] == "https://xiosync.dev/problems/capability_denied"
        assert detail["status"] == 403
        assert detail["capability_group"] == "secret.manage"
        assert detail["actor_id"] == str(_ACTOR_ID)
        assert detail["membership_role"] == "org_member"


# ---------------------------------------------------------------------------
# Full RBAC matrix — parametrized
# ---------------------------------------------------------------------------

_RBAC_MATRIX = [
    # (role, group, expected_allowed)
    # Viewer
    (MembershipRole.ORG_VIEWER, "readonly", True),
    (MembershipRole.ORG_VIEWER, "metering.read", True),
    (MembershipRole.ORG_VIEWER, "workflow.manage", False),
    (MembershipRole.ORG_VIEWER, "task.execute", False),
    (MembershipRole.ORG_VIEWER, "dlq.manage", False),
    (MembershipRole.ORG_VIEWER, "event.manage", False),
    (MembershipRole.ORG_VIEWER, "artifact.manage", False),
    (MembershipRole.ORG_VIEWER, "trigger.manage", False),
    (MembershipRole.ORG_VIEWER, "ontology.manage", False),
    (MembershipRole.ORG_VIEWER, "plugin.admin", False),
    (MembershipRole.ORG_VIEWER, "org.manage", False),
    (MembershipRole.ORG_VIEWER, "actor.manage", False),
    (MembershipRole.ORG_VIEWER, "secret.manage", False),
    (MembershipRole.ORG_VIEWER, "worker.manage", False),
    (MembershipRole.ORG_VIEWER, "share.manage", False),
    (MembershipRole.ORG_VIEWER, "webhook.manage", False),
    (MembershipRole.ORG_VIEWER, "capability.manage", False),
    (MembershipRole.ORG_VIEWER, "platform.admin", False),
    # Member
    (MembershipRole.ORG_MEMBER, "readonly", True),
    (MembershipRole.ORG_MEMBER, "metering.read", True),
    (MembershipRole.ORG_MEMBER, "workflow.manage", True),
    (MembershipRole.ORG_MEMBER, "task.execute", True),
    (MembershipRole.ORG_MEMBER, "dlq.manage", True),
    (MembershipRole.ORG_MEMBER, "event.manage", True),
    (MembershipRole.ORG_MEMBER, "artifact.manage", True),
    (MembershipRole.ORG_MEMBER, "trigger.manage", True),
    (MembershipRole.ORG_MEMBER, "ontology.manage", True),
    (MembershipRole.ORG_MEMBER, "plugin.admin", False),
    (MembershipRole.ORG_MEMBER, "org.manage", False),
    (MembershipRole.ORG_MEMBER, "actor.manage", False),
    (MembershipRole.ORG_MEMBER, "secret.manage", False),
    (MembershipRole.ORG_MEMBER, "worker.manage", False),
    (MembershipRole.ORG_MEMBER, "share.manage", False),
    (MembershipRole.ORG_MEMBER, "webhook.manage", False),
    (MembershipRole.ORG_MEMBER, "capability.manage", False),
    (MembershipRole.ORG_MEMBER, "platform.admin", False),
    # Admin
    (MembershipRole.ORG_ADMIN, "readonly", True),
    (MembershipRole.ORG_ADMIN, "workflow.manage", True),
    (MembershipRole.ORG_ADMIN, "plugin.admin", True),
    (MembershipRole.ORG_ADMIN, "org.manage", True),
    (MembershipRole.ORG_ADMIN, "actor.manage", True),
    (MembershipRole.ORG_ADMIN, "secret.manage", True),
    (MembershipRole.ORG_ADMIN, "worker.manage", True),
    (MembershipRole.ORG_ADMIN, "share.manage", True),
    (MembershipRole.ORG_ADMIN, "webhook.manage", True),
    (MembershipRole.ORG_ADMIN, "capability.manage", True),
    (MembershipRole.ORG_ADMIN, "platform.admin", False),
    # Owner
    (MembershipRole.ORG_OWNER, "readonly", True),
    (MembershipRole.ORG_OWNER, "plugin.admin", True),
    (MembershipRole.ORG_OWNER, "platform.admin", True),
    (MembershipRole.ORG_OWNER, "capability.manage", True),
]


class TestRBACMatrix:
    """Parametrized test covering the full role × capability group matrix."""

    @pytest.mark.parametrize("role,group,expected", _RBAC_MATRIX,
                             ids=[f"{r.value}-{g}-{'ALLOW' if e else 'DENY'}"
                                  for r, g, e in _RBAC_MATRIX])
    def test_rbac_matrix(self, role: MembershipRole, group: str, expected: bool) -> None:
        ctx = _make_context(membership_role=role)
        assert _check_role(ctx, group) is expected
