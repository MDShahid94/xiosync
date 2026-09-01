"""org_scoped_session — the sanctioned RLS gate (docs 05 §3.2, 06; C1/C2).

Proves on the real migrated schema, as a plain application role, that:

- inside ``org_scoped_session(engine, context)`` reads are scoped to the
  context's organization (the boundary sets the same GUC the rev-0002
  policies key on);
- the binding is transaction-local: after the context manager exits, the
  same engine's next transaction sees zero rows (fail closed — no residual
  org on pooled connections);
- an exception inside the scope rolls the transaction back.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.persistence.tenancy import RLS_ORG_SETTING, org_scoped_session
from xiosync.platform.ids import new_id

from .test_identity_security import _ORG_1, _ORG_2, _seed_two_orgs

pytestmark = pytest.mark.integration


def _context_for(org_id: uuid.UUID) -> OrgContext:
    return OrgContext(
        auth_identity_id=new_id(),
        actor_id=new_id(),
        organization_id=org_id,
        session_id=new_id(),
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_MEMBER,
    )


def test_scoped_session_sees_only_context_org(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    _seed_two_orgs(migrated_database_url)
    engine = create_engine(app_role_database_url)
    try:
        with org_scoped_session(engine, _context_for(_ORG_1)) as session:
            rows = session.execute(text("SELECT id FROM organizations")).fetchall()
            assert [row[0] for row in rows] == [_ORG_1]

        with org_scoped_session(engine, _context_for(_ORG_2)) as session:
            rows = session.execute(text("SELECT id FROM organizations")).fetchall()
            assert [row[0] for row in rows] == [_ORG_2]
    finally:
        engine.dispose()


def test_scope_dies_with_the_transaction(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    """SET LOCAL leaves no residual org on the pooled connection."""
    _seed_two_orgs(migrated_database_url)
    # pool_size=1 forces reuse of the very connection the scope ran on.
    engine = create_engine(app_role_database_url, pool_size=1, max_overflow=0)
    try:
        with org_scoped_session(engine, _context_for(_ORG_1)) as session:
            assert session.execute(text("SELECT count(*) FROM organizations")).scalar_one() == 1

        with engine.connect() as connection:
            setting = connection.execute(
                text("SELECT current_setting(:name, true)"),
                {"name": RLS_ORG_SETTING},
            ).scalar_one()
            assert setting in (None, ""), f"residual org leaked to pooled connection: {setting!r}"
            assert connection.execute(text("SELECT count(*) FROM organizations")).scalar_one() == 0
    finally:
        engine.dispose()


def test_exception_rolls_back_the_scoped_transaction(
    migrated_database_url: str, app_role_database_url: str
) -> None:
    _seed_two_orgs(migrated_database_url)
    engine = create_engine(app_role_database_url)
    marker = new_id()
    try:
        with pytest.raises(RuntimeError, match="boom"):
            with org_scoped_session(engine, _context_for(_ORG_1)) as session:
                session.execute(
                    text(
                        "INSERT INTO actors (id, organization_id, actor_type, state, "
                        "lifecycle_phase, trust_tier, health_status) "
                        "VALUES (:id, :org, 'human', 'active', 'operational', "
                        "'newcomer', 'healthy')"
                    ),
                    {"id": marker, "org": _ORG_1},
                )
                raise RuntimeError("boom")

        with org_scoped_session(engine, _context_for(_ORG_1)) as session:
            count = session.execute(
                text("SELECT count(*) FROM actors WHERE id = :id"), {"id": marker}
            ).scalar_one()
            assert count == 0, "rolled-back insert must not persist"
    finally:
        engine.dispose()
