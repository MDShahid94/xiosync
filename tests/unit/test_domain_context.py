"""OrgContext construction invariants (doc 05 §3.1; C1, INV-TENANT-1)."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest
from xiosync.domain.context import (
    ContextResolutionError,
    MembershipRole,
    OrgContext,
    PlatformRole,
)
from xiosync.platform.ids import new_id


def _valid_kwargs() -> dict[str, Any]:
    return {
        "auth_identity_id": new_id(),
        "actor_id": new_id(),
        "organization_id": new_id(),
        "session_id": new_id(),
        "platform_role": PlatformRole.NONE,
        "membership_role": MembershipRole.ORG_MEMBER,
    }


def test_valid_context_constructs_and_is_frozen() -> None:
    context = OrgContext(**_valid_kwargs())
    assert context.platform_role is PlatformRole.NONE
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.organization_id = new_id()  # type: ignore[misc]


@pytest.mark.parametrize("field", ["auth_identity_id", "actor_id", "organization_id", "session_id"])
def test_nil_uuid_is_rejected_as_placeholder(field: str) -> None:
    """C1: no placeholder org (or any placeholder id) can ever exist."""
    kwargs = _valid_kwargs()
    kwargs[field] = uuid.UUID(int=0)
    with pytest.raises(ContextResolutionError, match=field):
        OrgContext(**kwargs)


@pytest.mark.parametrize("field", ["auth_identity_id", "actor_id", "organization_id", "session_id"])
def test_non_uuid_is_rejected(field: str) -> None:
    """Strings — including 'pending'/'default' sentinels — cannot sneak in."""
    kwargs = _valid_kwargs()
    kwargs[field] = "pending"
    with pytest.raises(ContextResolutionError, match=field):
        OrgContext(**kwargs)


def test_role_fields_must_be_enums() -> None:
    kwargs = _valid_kwargs()
    kwargs["platform_role"] = "platform_admin"
    with pytest.raises(ContextResolutionError, match="platform_role"):
        OrgContext(**kwargs)

    kwargs = _valid_kwargs()
    kwargs["membership_role"] = "org_owner"
    with pytest.raises(ContextResolutionError, match="membership_role"):
        OrgContext(**kwargs)


def test_role_enums_cover_doc_03_values_exactly() -> None:
    """The vocabulary is closed (GLOSSARY): no extra or missing roles."""
    assert {role.value for role in PlatformRole} == {"none", "platform_admin"}
    assert {role.value for role in MembershipRole} == {
        "org_owner",
        "org_admin",
        "org_member",
        "org_viewer",
    }
