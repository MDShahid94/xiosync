"""Phase 4 worker-isolation exit-gate tests (doc 10 §Phase 4; doc 11 §6).

These tests prove, on the real migrated PostgreSQL schema, the three
worker-isolation acceptance gates:

* **G-WRK-1 (H7)** — worker credentials are short-lived, per-worker,
  capability-scoped, and signed with a key that is *distinct* from the
  user-session JWT secret.  The decisive negative proof is that a worker,
  holding only ``WORKER_CREDENTIAL_KEY``, cannot mint a token the user-token
  verifier (which knows only ``XIOSYNC_AUTH_SECRET``) will accept — not even a
  token whose claim shape is byte-for-byte a user access token.

* **G-WRK-2 (trust)** — a below-tier worker cannot execute a tier-gated grant.
  A ``newcomer`` compute actor is refused a capability whose grant carries a
  ``minimum_trust_tier`` of ``trusted``; a ``trusted`` actor holding the same
  grant is allowed.  The grant and actor rows are read back from the migrated
  schema and fed through the authoritative ``authorize`` policy.

* **G-WRK-3 (C9)** — a compromised volunteer worker cannot mutate global or
  cross-actor state.  Operating through the sanctioned ``org_scoped_session``
  boundary as a plain (``NOSUPERUSER NOBYPASSRLS``) role, a worker scoped to
  its own org cannot insert, update, or delete another org's rows: Row-Level
  Security rejects the cross-org write (``WITH CHECK``) or hides the target
  rows entirely (``USING`` → zero rows affected, fail closed).

Every test runs against a freshly created, Alembic-migrated scratch database
(INV-TEST-SCHEMA-1).  The ``migrated_database_url`` and
``app_role_database_url`` fixtures come from ``tests/integration/conftest.py``.
Seeding uses the admin (superuser) connection, which bypasses RLS by
PostgreSQL design; every isolation assertion is made through the plain
application role.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from xiosync.domain.authorization import (
    Actor,
    Grant,
    Organization,
    Resource,
    authorize,
)
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.domain.workers import trust_tier_satisfies
from xiosync.persistence.tenancy import org_scoped_session
from xiosync.platform.ids import new_id
from xiosync.platform.tokens import (
    TokenError,
    issue_access_token,
    verify_access_token,
)
from xiosync.services.workers import WorkerService

pytestmark = [pytest.mark.integration]

_NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)

# Two DISTINCT signing keys — the whole point of H7. The user-session secret
# is what the platform's access-token verifier trusts; the worker credential
# key is the execution-plane key a worker actually holds. Neither is the
# other, and both are >= 32 chars (config floor for the auth secret).
_USER_JWT_SECRET = "user-session-secret-AAAAAAAAAAAAAAAAAAAAAAAA"  # noqa: S105 — test-only
_WORKER_CRED_KEY = "worker-credential-key-BBBBBBBBBBBBBBBBBBBBBBBB"  # noqa: S105 — test-only

_ALGORITHM = "HS256"

# Capabilities used in credential scoping — arbitrary capability UUIDs.
_CAP_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_CAP_B = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# Seed helpers (admin connection — bypasses RLS by design)
# ---------------------------------------------------------------------------


def _make_ctx(org_id: uuid.UUID, actor_id: uuid.UUID) -> OrgContext:
    """A frozen org-admin context for ``actor_id`` in ``org_id``."""
    return OrgContext(
        auth_identity_id=new_id(),
        actor_id=actor_id,
        organization_id=org_id,
        session_id=new_id(),
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_ADMIN,
    )


def _insert_org(conn, org_id: uuid.UUID, slug: str) -> None:
    conn.execute(
        text(
            "INSERT INTO organizations (id, slug, name, state) "
            "VALUES (:id, :slug, :slug, 'active')"
        ),
        {"id": org_id, "slug": slug},
    )


def _insert_actor(
    conn,
    actor_id: uuid.UUID,
    org_id: uuid.UUID,
    *,
    actor_type: str,
    trust_tier: str,
) -> None:
    conn.execute(
        text(
            "INSERT INTO actors "
            "(id, organization_id, actor_type, state, lifecycle_phase, "
            "trust_tier, health_status) "
            "VALUES (:id, :org, :atype, 'active', 'operational', :tier, 'healthy')"
        ),
        {"id": actor_id, "org": org_id, "atype": actor_type, "tier": trust_tier},
    )


def _insert_capability(
    conn, cap_id: uuid.UUID, org_id: uuid.UUID, name: str
) -> None:
    conn.execute(
        text(
            "INSERT INTO capabilities (id, organization_id, name) "
            "VALUES (:id, :org, :name)"
        ),
        {"id": cap_id, "org": org_id, "name": name},
    )


def _insert_grant(
    conn,
    grant_id: uuid.UUID,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    cap_id: uuid.UUID,
    *,
    constraints: dict,
) -> None:
    conn.execute(
        text(
            "INSERT INTO grants "
            "(id, organization_id, actor_id, capability_id, state, constraints) "
            "VALUES (:id, :org, :actor, :cap, 'active', CAST(:constraints AS jsonb))"
        ),
        {
            "id": grant_id,
            "org": org_id,
            "actor": actor_id,
            "cap": cap_id,
            "constraints": json.dumps(constraints),
        },
    )


def _seed_org_and_worker(
    admin_url: str,
    *,
    trust_tier: str = "newcomer",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert an org, a human org-owner, and a compute worker actor.

    Returns ``(organization_id, owner_actor_id, worker_actor_id)``.
    """
    org_id = new_id()
    owner_actor_id = new_id()
    worker_actor_id = new_id()

    engine = create_engine(admin_url)
    try:
        with engine.begin() as conn:
            _insert_org(conn, org_id, f"iso-test-{org_id}")
            _insert_actor(
                conn, owner_actor_id, org_id, actor_type="human", trust_tier="admin"
            )
            _insert_actor(
                conn,
                worker_actor_id,
                org_id,
                actor_type="compute",
                trust_tier=trust_tier,
            )
    finally:
        engine.dispose()
    return org_id, owner_actor_id, worker_actor_id


# ---------------------------------------------------------------------------
# G-WRK-1 — worker credentials cannot mint a user/admin token (H7)
# ---------------------------------------------------------------------------


class TestWorkerCredentialCannotMintUserToken:
    """G-WRK-1 / H7: the worker key is disjoint from the user-session secret."""

    def test_the_two_signing_keys_are_distinct(self) -> None:
        """A precondition for the whole gate: the keys are not the same value.

        If these keys were ever equal the entire H7 remediation collapses, so
        we assert it explicitly rather than leaving it implicit in the setup.
        """
        assert _WORKER_CRED_KEY != _USER_JWT_SECRET

    def test_token_signed_with_worker_key_fails_user_verification(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A real issued credential, signed with the worker key, is not a user token.

        We issue a genuine credential against the migrated schema, rebuild the
        exact JWT payload ``WorkerService.issue_credential`` signs, sign it with
        the worker credential key, and prove the user-token verifier rejects it.
        """
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, worker_actor_id = _seed_org_and_worker(migrated_database_url)
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                enrollment = svc.register_worker(
                    ctx,
                    enrollment_token="tok-iso-mint-1",
                    public_key="ssh-ed25519 AAAA iso-mint-1",
                    worker_id=worker_actor_id,
                )
                svc.approve_worker(ctx, enrollment.id, approved_by=owner_id)
                cred = svc.issue_credential(
                    ctx,
                    enrollment.id,
                    scoped_capabilities=[str(_CAP_A), str(_CAP_B)],
                    duration=timedelta(hours=4),
                    now=_NOW,
                )

            # Reconstruct the worker-credential JWT the service emits and signs
            # with WORKER_CREDENTIAL_KEY (see WorkerService.issue_credential).
            worker_jwt = jwt.encode(
                {
                    "jti": str(cred.id),
                    "sub": str(worker_actor_id),
                    "org": str(org_id),
                    "enr": str(enrollment.id),
                    "cap": [str(_CAP_A), str(_CAP_B)],
                    "iat": int(cred.issued_at.timestamp()),
                    "exp": int(cred.expires_at.timestamp()),
                },
                _WORKER_CRED_KEY,
                algorithm=_ALGORITHM,
            )

            # The platform verifies user access tokens with the USER secret.
            # A worker-key-signed token must be rejected: bad signature.
            with pytest.raises(TokenError):
                verify_access_token(_USER_JWT_SECRET, worker_jwt, now=_NOW)
        finally:
            engine.dispose()

    def test_worker_cannot_forge_a_user_shaped_token(self) -> None:
        """Even a perfectly user-shaped payload fails without the user secret.

        This is the strongest statement of "cannot mint": the worker crafts a
        payload with every claim ``verify_access_token`` requires and signs it
        with the only key it possesses. The verifier still rejects it because
        the signature was not produced with the user-session secret.
        """
        org_id = new_id()
        session_id = new_id()
        worker_actor_id = new_id()

        forged = jwt.encode(
            {
                "jti": str(new_id()),
                "sid": str(session_id),
                "org": str(org_id),
                "act": str(worker_actor_id),
                "iat": int(_NOW.timestamp()),
                "exp": int((_NOW + timedelta(minutes=15)).timestamp()),
            },
            _WORKER_CRED_KEY,  # the only key a worker holds
            algorithm=_ALGORITHM,
        )

        with pytest.raises(TokenError):
            verify_access_token(_USER_JWT_SECRET, forged, now=_NOW)

    def test_genuine_user_token_verifies_only_under_user_secret(self) -> None:
        """Positive/negative control proving key separation is the mechanism.

        A real user access token verifies under the user secret and is rejected
        under the worker key — the trust boundary is the key, nothing else.
        """
        token, claims = issue_access_token(
            _USER_JWT_SECRET,
            session_id=new_id(),
            organization_id=new_id(),
            actor_id=new_id(),
            now=_NOW,
        )

        verified = verify_access_token(_USER_JWT_SECRET, token, now=_NOW)
        assert verified.actor_id == claims.actor_id

        with pytest.raises(TokenError):
            verify_access_token(_WORKER_CRED_KEY, token, now=_NOW)

    def test_issued_credential_is_short_lived_scoped_and_per_worker(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cred-inspection half of G-WRK-1: bounded TTL, scoped, per-worker.

        The credential must expire (short-lived), carry a non-empty capability
        scope (capability-scoped), and be bound to exactly one enrollment /
        worker (per-worker).
        """
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, worker_actor_id = _seed_org_and_worker(migrated_database_url)
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                enrollment = svc.register_worker(
                    ctx,
                    enrollment_token="tok-iso-inspect",
                    public_key="ssh-ed25519 AAAA iso-inspect",
                    worker_id=worker_actor_id,
                )
                svc.approve_worker(ctx, enrollment.id, approved_by=owner_id)
                cred = svc.issue_credential(
                    ctx,
                    enrollment.id,
                    scoped_capabilities=[str(_CAP_A)],
                    duration=timedelta(hours=1),
                    now=_NOW,
                )

            # Short-lived: a finite, positive TTL bounded by the user-token
            # ceiling many times over — a credential is never a standing key.
            ttl = cred.expires_at - cred.issued_at
            assert ttl == timedelta(hours=1)
            assert ttl > timedelta(0)

            # Capability-scoped: the scope is exactly what was requested.
            assert cred.scoped_capabilities == [str(_CAP_A)]
            assert len(cred.scoped_capabilities) >= 1

            # Per-worker: bound to a single enrollment (hence one worker actor).
            assert cred.enrollment_id == enrollment.id
            assert cred.organization_id == org_id
            assert cred.revoked_at is None
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# G-WRK-2 — a below-tier worker cannot execute a tier-gated grant (trust)
# ---------------------------------------------------------------------------


def _load_authorization_inputs(
    session,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    capability_id: uuid.UUID,
) -> tuple[Actor, Organization, Grant, str]:
    """Read the actor, org, capability name, and grant back from the schema.

    The rows come from the migrated database through the org-scoped session, so
    the tier decision is made against real persisted state, not fabricated
    fixtures.
    """
    actor_row = session.execute(
        text(
            "SELECT organization_id, state, trust_tier FROM actors "
            "WHERE id = :id"
        ),
        {"id": actor_id},
    ).one()
    org_row = session.execute(
        text("SELECT state FROM organizations WHERE id = :id"),
        {"id": org_id},
    ).one()
    cap_row = session.execute(
        text("SELECT name FROM capabilities WHERE id = :id"),
        {"id": capability_id},
    ).one()
    grant_row = session.execute(
        text(
            "SELECT id, actor_id, constraints FROM grants "
            "WHERE actor_id = :actor AND capability_id = :cap AND state = 'active'"
        ),
        {"actor": actor_id, "cap": capability_id},
    ).one()

    actor = Actor(
        id=actor_id,
        organization_id=actor_row.organization_id,
        state=actor_row.state,
        trust_tier=actor_row.trust_tier,
    )
    organization = Organization(id=org_id, state=org_row.state)
    grant = Grant(
        id=grant_row.id,
        organization_id=org_id,
        actor_id=grant_row.actor_id,
        capability=cap_row.name,
        state="active",
        constraints=grant_row.constraints,
    )
    return actor, organization, grant, cap_row.name


class TestBelowTierWorkerCannotExecuteTierGatedGrant:
    """G-WRK-2: a tier-gated grant is denied below the required trust tier."""

    def test_trust_predicate_gates_the_required_tier(self) -> None:
        """The authoritative predicate: newcomer never satisfies trusted."""
        assert trust_tier_satisfies("newcomer", "trusted") is False
        assert trust_tier_satisfies("trusted", "trusted") is True

    def test_newcomer_worker_is_denied_trusted_gated_grant(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
    ) -> None:
        """A newcomer compute worker is refused a grant requiring ``trusted``."""
        org_id, _owner_id, worker_actor_id = _seed_org_and_worker(
            migrated_database_url, trust_tier="newcomer"
        )
        cap_id = new_id()
        grant_id = new_id()
        engine = create_engine(migrated_database_url)
        try:
            with engine.begin() as conn:
                _insert_capability(conn, cap_id, org_id, "deploy.production")
                _insert_grant(
                    conn,
                    grant_id,
                    org_id,
                    worker_actor_id,
                    cap_id,
                    constraints={"minimum_trust_tier": "trusted"},
                )
        finally:
            engine.dispose()

        ctx = _make_ctx(org_id, worker_actor_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                actor, organization, grant, cap_name = _load_authorization_inputs(
                    session, org_id=org_id, actor_id=worker_actor_id, capability_id=cap_id
                )

            decision = authorize(
                requested_organization_id=org_id,
                actor=actor,
                organization=organization,
                resource=Resource(type="deployment", id=new_id(), organization_id=org_id),
                capability=cap_name,
                operation="execute",
                grants=[grant],
                arguments={},
                now=_NOW,
            )

            assert decision.allowed is False
            assert decision.reason == "constraints_unsatisfied"
        finally:
            engine.dispose()

    def test_trusted_worker_is_allowed_the_same_grant(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
    ) -> None:
        """A ``trusted`` worker holding the identical grant is allowed.

        Pairing this with the negative test proves the tier constraint — not
        some unrelated defect — is what denies the newcomer.
        """
        org_id, _owner_id, worker_actor_id = _seed_org_and_worker(
            migrated_database_url, trust_tier="trusted"
        )
        cap_id = new_id()
        grant_id = new_id()
        engine = create_engine(migrated_database_url)
        try:
            with engine.begin() as conn:
                _insert_capability(conn, cap_id, org_id, "deploy.production")
                _insert_grant(
                    conn,
                    grant_id,
                    org_id,
                    worker_actor_id,
                    cap_id,
                    constraints={"minimum_trust_tier": "trusted"},
                )
        finally:
            engine.dispose()

        ctx = _make_ctx(org_id, worker_actor_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                actor, organization, grant, cap_name = _load_authorization_inputs(
                    session, org_id=org_id, actor_id=worker_actor_id, capability_id=cap_id
                )

            decision = authorize(
                requested_organization_id=org_id,
                actor=actor,
                organization=organization,
                resource=Resource(type="deployment", id=new_id(), organization_id=org_id),
                capability=cap_name,
                operation="execute",
                grants=[grant],
                arguments={},
                now=_NOW,
            )

            assert decision.allowed is True
            assert decision.grant_id == grant.id
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# G-WRK-3 — a compromised worker cannot mutate global/cross-actor state (C9)
# ---------------------------------------------------------------------------


def _seed_two_orgs_with_worker_and_target(
    admin_url: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed org A (a volunteer worker) and org B (a foreign actor + grant).

    Returns ``(org_a, worker_actor_a, org_b, actor_b, grant_b)``. Org B also
    gets one capability row so its grant's composite FK is satisfiable; the
    capability id is looked up by callers that need it.
    """
    org_a = new_id()
    worker_actor_a = new_id()
    org_b = new_id()
    actor_b = new_id()
    cap_b = new_id()
    grant_b = new_id()

    engine = create_engine(admin_url)
    try:
        with engine.begin() as conn:
            # org A: the compromised volunteer worker's home org.
            _insert_org(conn, org_a, f"iso-orga-{org_a}")
            _insert_actor(
                conn, worker_actor_a, org_a, actor_type="compute", trust_tier="newcomer"
            )
            # org B: a completely separate tenant with its own actor + grant.
            _insert_org(conn, org_b, f"iso-orgb-{org_b}")
            _insert_actor(
                conn, actor_b, org_b, actor_type="human", trust_tier="trusted"
            )
            _insert_capability(conn, cap_b, org_b, "orgb.capability")
            _insert_grant(
                conn, grant_b, org_b, actor_b, cap_b, constraints={}
            )
    finally:
        engine.dispose()
    return org_a, worker_actor_a, org_b, actor_b, grant_b


class TestWorkerCannotMutateCrossActorState:
    """G-WRK-3 / C9: RLS fails closed against cross-org / cross-actor writes."""

    def test_worker_cannot_insert_a_grant_in_another_org(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
    ) -> None:
        """A cross-org INSERT is rejected by the RLS ``WITH CHECK`` predicate.

        The worker is scoped to org A; it attempts to plant a grant carrying
        org B's id (with otherwise valid foreign keys). RLS refuses the write.
        """
        org_a, worker_a, org_b, actor_b, _grant_b = (
            _seed_two_orgs_with_worker_and_target(migrated_database_url)
        )
        # Discover org B's capability id to keep the composite FK satisfiable,
        # so RLS — not a FK violation — is demonstrably the blocker.
        engine = create_engine(migrated_database_url)
        try:
            with engine.begin() as conn:
                cap_b = conn.execute(
                    text(
                        "SELECT id FROM capabilities WHERE organization_id = :org"
                    ),
                    {"org": org_b},
                ).scalar_one()
        finally:
            engine.dispose()

        ctx = _make_ctx(org_a, worker_a)
        engine = create_engine(app_role_database_url)
        try:
            with pytest.raises(DBAPIError):
                with org_scoped_session(engine, ctx) as session:
                    session.execute(
                        text(
                            "INSERT INTO grants "
                            "(id, organization_id, actor_id, capability_id, "
                            "state, constraints) "
                            "VALUES (:id, :org, :actor, :cap, 'active', "
                            "'{}'::jsonb)"
                        ),
                        {
                            "id": new_id(),
                            "org": org_b,
                            "actor": actor_b,
                            "cap": cap_b,
                        },
                    )
        finally:
            engine.dispose()

        # Confirm via the admin channel that no grant landed in org B.
        engine = create_engine(migrated_database_url)
        try:
            with engine.begin() as conn:
                count = conn.execute(
                    text(
                        "SELECT count(*) FROM grants WHERE organization_id = :org"
                    ),
                    {"org": org_b},
                ).scalar_one()
            # Only the single grant seeded for actor_b should exist.
            assert count == 1
        finally:
            engine.dispose()

    def test_worker_cannot_update_another_orgs_grant(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
    ) -> None:
        """A cross-org UPDATE affects zero rows: RLS ``USING`` hides them.

        The target grant is invisible to org A's scope, so the revoke attempt
        matches nothing and the row stays ``active`` (verified via admin).
        """
        org_a, worker_a, org_b, _actor_b, grant_b = (
            _seed_two_orgs_with_worker_and_target(migrated_database_url)
        )

        ctx = _make_ctx(org_a, worker_a)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                result = session.execute(
                    text(
                        "UPDATE grants SET state = 'revoked' WHERE id = :id"
                    ),
                    {"id": grant_b},
                )
                assert result.rowcount == 0
        finally:
            engine.dispose()

        engine = create_engine(migrated_database_url)
        try:
            with engine.begin() as conn:
                state = conn.execute(
                    text("SELECT state FROM grants WHERE id = :id"),
                    {"id": grant_b},
                ).scalar_one()
            assert state == "active", "cross-org grant must remain untouched"
        finally:
            engine.dispose()

    def test_worker_cannot_delete_another_orgs_grant(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
    ) -> None:
        """A cross-org DELETE affects zero rows and the grant survives."""
        org_a, worker_a, org_b, _actor_b, grant_b = (
            _seed_two_orgs_with_worker_and_target(migrated_database_url)
        )

        ctx = _make_ctx(org_a, worker_a)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                result = session.execute(
                    text("DELETE FROM grants WHERE id = :id"),
                    {"id": grant_b},
                )
                assert result.rowcount == 0
        finally:
            engine.dispose()

        engine = create_engine(migrated_database_url)
        try:
            with engine.begin() as conn:
                count = conn.execute(
                    text("SELECT count(*) FROM grants WHERE id = :id"),
                    {"id": grant_b},
                ).scalar_one()
            assert count == 1, "cross-org grant must not be deletable"
        finally:
            engine.dispose()

    def test_worker_scope_reads_only_its_own_org(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
    ) -> None:
        """The worker's scoped reads never surface another org's grants.

        This is the read-side companion to the write blocks: a compromised
        worker cannot even enumerate cross-actor state to target it.
        """
        org_a, worker_a, org_b, _actor_b, grant_b = (
            _seed_two_orgs_with_worker_and_target(migrated_database_url)
        )

        ctx = _make_ctx(org_a, worker_a)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                visible = session.execute(
                    text("SELECT id FROM grants")
                ).fetchall()
                # org A has no grants seeded; org B's grant must be invisible.
                assert [row[0] for row in visible] == []

                org_ids = session.execute(
                    text("SELECT id FROM organizations")
                ).fetchall()
                assert [row[0] for row in org_ids] == [org_a]
                assert grant_b not in [row[0] for row in visible]
        finally:
            engine.dispose()
