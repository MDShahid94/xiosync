"""Integration tests for worker enrollment and credential invariants.

Covers:
- INV-WORKER-CRED-1: credentials issued only to APPROVED workers.
- INV-WORKER-CRED-2: revoked credentials are rejected on subsequent checks.
- INV-TRUST-1: TIER_1 (newcomer) workers cannot execute grants requiring a
  higher trust tier without org-level approval / promotion.
- INV-TRUST-2: credential TTL is enforced; expired credentials are invalid.

Each test uses a real migrated PostgreSQL scratch database (doc 06 §10;
INV-TEST-SCHEMA-1).  The ``migrated_database_url`` and
``app_role_database_url`` fixtures come from ``tests/integration/conftest.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from xiosync.domain.workers import (
    credential_is_expired,
    credential_is_valid,
    trust_tier_satisfies,
)
from xiosync.persistence.tenancy import org_scoped_session
from xiosync.platform.ids import new_id
from xiosync.services.workers import (
    CredentialNotFoundError,
    EnrollmentNotFoundError,
    InactiveWorkerError,
    WorkerAlreadyEnrolledError,
    WorkerService,
)

pytestmark = [pytest.mark.integration]

_NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
_WORKER_CRED_KEY = "test-worker-credential-key-distinct-from-jwt-secret"

# Capabilities used in tests — arbitrary UUIDs representing granted capabilities.
_CAP_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_CAP_B = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_org_and_actor(
    admin_url: str,
    *,
    trust_tier: str = "newcomer",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert an organization, an org-owner actor, and a compute actor.

    Returns ``(organization_id, owner_actor_id, worker_actor_id)``.
    """
    org_id = new_id()
    owner_actor_id = new_id()
    worker_actor_id = new_id()

    engine = create_engine(admin_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO organizations (id, slug, name, state) "
                    "VALUES (:id, :slug, 'Worker Test Org', 'active')"
                ),
                {"id": org_id, "slug": f"worker-test-{org_id}"},
            )
            # org-owner (human) — used as approved_by
            conn.execute(
                text(
                    "INSERT INTO actors "
                    "(id, organization_id, actor_type, state, lifecycle_phase, "
                    "trust_tier, health_status) "
                    "VALUES (:id, :org, 'human', 'active', 'operational', 'admin', 'healthy')"
                ),
                {"id": owner_actor_id, "org": org_id},
            )
            # compute worker actor
            conn.execute(
                text(
                    "INSERT INTO actors "
                    "(id, organization_id, actor_type, state, lifecycle_phase, "
                    "trust_tier, health_status) "
                    "VALUES (:id, :org, 'compute', 'active', 'operational', :tier, 'healthy')"
                ),
                {"id": worker_actor_id, "org": org_id, "tier": trust_tier},
            )
    finally:
        engine.dispose()
    return org_id, owner_actor_id, worker_actor_id


def _make_ctx(
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> OrgContext:  # type: ignore[name-defined]  # noqa: F821
    from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole

    return OrgContext(
        auth_identity_id=new_id(),
        actor_id=actor_id,
        organization_id=org_id,
        session_id=new_id(),
        platform_role=PlatformRole.NONE,
        membership_role=MembershipRole.ORG_ADMIN,
    )


# ---------------------------------------------------------------------------
# INV-WORKER-CRED-1: credentials issued only to APPROVED workers
# ---------------------------------------------------------------------------


class TestCredentialIssuedOnlyToApprovedWorkers:
    """INV-WORKER-CRED-1: issue_credential must refuse non-approved enrollments."""

    def test_pending_worker_cannot_get_credential(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pending (unapproved) worker is refused a credential."""
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, worker_actor_id = _seed_org_and_actor(migrated_database_url)
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                enrollment = svc.register_worker(
                    ctx,
                    enrollment_token="tok-pending-cred-test",
                    public_key="ssh-ed25519 AAAA pending",
                    worker_id=worker_actor_id,
                )
                assert enrollment.enrollment_state == "pending"

                with pytest.raises(InactiveWorkerError):
                    svc.issue_credential(
                        ctx,
                        enrollment.id,
                        scoped_capabilities=[str(_CAP_A)],
                        duration=timedelta(hours=1),
                        now=_NOW,
                    )
        finally:
            engine.dispose()

    def test_suspended_worker_cannot_get_credential(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A suspended worker is refused a credential (InactiveWorkerError)."""
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, worker_actor_id = _seed_org_and_actor(migrated_database_url)
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                enrollment = svc.register_worker(
                    ctx,
                    enrollment_token="tok-suspend-cred-test",
                    public_key="ssh-ed25519 AAAA suspend",
                    worker_id=worker_actor_id,
                )
                approved = svc.approve_worker(ctx, enrollment.id, approved_by=owner_id)
                assert approved.enrollment_state == "approved"
                suspended = svc.suspend_worker(ctx, approved.id)
                assert suspended.enrollment_state == "suspended"

                with pytest.raises(InactiveWorkerError):
                    svc.issue_credential(
                        ctx,
                        enrollment.id,
                        scoped_capabilities=[str(_CAP_A)],
                        duration=timedelta(hours=1),
                        now=_NOW,
                    )
        finally:
            engine.dispose()

    def test_approved_worker_receives_credential(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An approved worker successfully receives a credential."""
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, worker_actor_id = _seed_org_and_actor(migrated_database_url)
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                enrollment = svc.register_worker(
                    ctx,
                    enrollment_token="tok-approved-cred-test",
                    public_key="ssh-ed25519 AAAA approved",
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

            assert cred.revoked_at is None
            assert cred.expires_at == _NOW + timedelta(hours=4)
            assert cred.enrollment_id == enrollment.id
            assert cred.organization_id == org_id
        finally:
            engine.dispose()

    def test_revoked_worker_cannot_get_credential(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A revoked worker is refused a credential."""
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, worker_actor_id = _seed_org_and_actor(migrated_database_url)
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                enrollment = svc.register_worker(
                    ctx,
                    enrollment_token="tok-revoke-cred-test",
                    public_key="ssh-ed25519 AAAA revoked",
                    worker_id=worker_actor_id,
                )
                svc.approve_worker(ctx, enrollment.id, approved_by=owner_id)
                svc.revoke_worker(ctx, enrollment.id)

                with pytest.raises(InactiveWorkerError):
                    svc.issue_credential(
                        ctx,
                        enrollment.id,
                        scoped_capabilities=[str(_CAP_A)],
                        duration=timedelta(hours=1),
                        now=_NOW,
                    )
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# INV-WORKER-CRED-2: revoked credentials are rejected on subsequent checks
# ---------------------------------------------------------------------------


class TestRevokedCredentialRejected:
    """INV-WORKER-CRED-2: a revoked credential is invalid after revocation."""

    def test_active_credential_is_valid(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Before revocation the credential passes ``credential_is_valid``."""
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, worker_actor_id = _seed_org_and_actor(migrated_database_url)
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                enrollment = svc.register_worker(
                    ctx,
                    enrollment_token="tok-active-valid",
                    public_key="ssh-ed25519 AAAA active-valid",
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

            assert credential_is_valid(
                expires_at=cred.expires_at, revoked_at=cred.revoked_at, now=_NOW
            )
        finally:
            engine.dispose()

    def test_revoked_credential_is_invalid(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After ``revoke_credential`` the stored record fails ``credential_is_valid``."""
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, worker_actor_id = _seed_org_and_actor(migrated_database_url)
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            cred_id: uuid.UUID
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                enrollment = svc.register_worker(
                    ctx,
                    enrollment_token="tok-revoke-valid",
                    public_key="ssh-ed25519 AAAA revoke-valid",
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
                cred_id = cred.id

            # Revoke in a fresh transaction.
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                svc.revoke_credential(ctx, cred_id)
                # Read back the revoked record within the same session.
                revoked = svc.get_credential(ctx, cred_id)

            assert revoked is not None
            assert revoked.revoked_at is not None
            # Domain predicate must reject a revoked credential.
            assert not credential_is_valid(
                expires_at=revoked.expires_at,
                revoked_at=revoked.revoked_at,
                now=_NOW,
            )
        finally:
            engine.dispose()

    def test_revoke_nonexistent_credential_raises(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Revoking a credential that does not exist raises CredentialNotFoundError."""
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, _ = _seed_org_and_actor(migrated_database_url)
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                with pytest.raises(CredentialNotFoundError):
                    svc.revoke_credential(ctx, new_id())
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# INV-TRUST-1: newcomer cannot execute grants requiring a higher tier
# ---------------------------------------------------------------------------


class TestTrustTierEnforcement:
    """INV-TRUST-1: trust-tier satisfaction is gated by domain predicates."""

    def test_newcomer_does_not_satisfy_contributor_required_tier(self) -> None:
        """newcomer fails a grant that requires contributor (pure domain predicate)."""
        assert trust_tier_satisfies("newcomer", "newcomer") is True
        assert trust_tier_satisfies("newcomer", "contributor") is False
        assert trust_tier_satisfies("newcomer", "trusted") is False

    def test_contributor_does_not_satisfy_trusted_required_tier(self) -> None:
        """contributor fails a grant that requires trusted."""
        assert trust_tier_satisfies("contributor", "contributor") is True
        assert trust_tier_satisfies("contributor", "trusted") is False

    def test_trusted_satisfies_newcomer_and_contributor(self) -> None:
        """A higher tier satisfies all lower required tiers."""
        assert trust_tier_satisfies("trusted", "newcomer") is True
        assert trust_tier_satisfies("trusted", "contributor") is True
        assert trust_tier_satisfies("trusted", "trusted") is True

    def test_newcomer_worker_enrollment_starts_at_pending(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A newcomer-tier worker registers as pending; no automatic promotion."""
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, worker_actor_id = _seed_org_and_actor(
            migrated_database_url, trust_tier="newcomer"
        )
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                enrollment = svc.register_worker(
                    ctx,
                    enrollment_token="tok-trust-tier-test",
                    public_key="ssh-ed25519 AAAA newcomer",
                    worker_id=worker_actor_id,
                )

            # The enrollment starts pending — no automatic org-level approval.
            assert enrollment.enrollment_state == "pending"
            # newcomer tier cannot satisfy contributor or higher.
            assert not trust_tier_satisfies("newcomer", "contributor")
        finally:
            engine.dispose()

    def test_explicit_approval_required_for_volunteer_pool(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A volunteer-pool newcomer worker requires explicit org-admin approval.

        INV-TRUST-1: the ``approved_by`` field is set only on explicit approval;
        it is null on a pending enrollment. Approval records the approver's UUID
        so the action is auditable.
        """
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, worker_actor_id = _seed_org_and_actor(
            migrated_database_url, trust_tier="newcomer"
        )
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                enrollment = svc.register_worker(
                    ctx,
                    enrollment_token="tok-volunteer-approval",
                    public_key="ssh-ed25519 AAAA volunteer",
                    pool_type="volunteer",
                    worker_id=worker_actor_id,
                )
                # pending: no approver yet
                assert enrollment.approved_by is None
                assert enrollment.approved_at is None

                # Only after explicit approval does the state change.
                approved = svc.approve_worker(
                    ctx, enrollment.id, approved_by=owner_id
                )

            assert approved.enrollment_state == "approved"
            assert approved.approved_by == owner_id
            assert approved.approved_at is not None
        finally:
            engine.dispose()

    def test_approve_already_approved_raises(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attempting to approve an already-approved enrollment raises ValueError."""
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, worker_actor_id = _seed_org_and_actor(migrated_database_url)
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                enrollment = svc.register_worker(
                    ctx,
                    enrollment_token="tok-double-approve",
                    public_key="ssh-ed25519 AAAA double-approve",
                    worker_id=worker_actor_id,
                )
                svc.approve_worker(ctx, enrollment.id, approved_by=owner_id)
                with pytest.raises(ValueError, match="only 'pending' enrollments"):
                    svc.approve_worker(ctx, enrollment.id, approved_by=owner_id)
        finally:
            engine.dispose()

    def test_enrollment_not_found_raises(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """approve_worker on a missing enrollment raises EnrollmentNotFoundError."""
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, _ = _seed_org_and_actor(migrated_database_url)
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                with pytest.raises(EnrollmentNotFoundError):
                    svc.approve_worker(ctx, new_id(), approved_by=owner_id)
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# INV-TRUST-2: credential TTL is enforced
# ---------------------------------------------------------------------------


class TestCredentialTTLEnforcement:
    """INV-TRUST-2: an expired credential is invalid per domain predicates."""

    def test_credential_valid_before_expiry(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A credential is valid at issue time (well before expiry)."""
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, worker_actor_id = _seed_org_and_actor(migrated_database_url)
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                enrollment = svc.register_worker(
                    ctx,
                    enrollment_token="tok-ttl-valid",
                    public_key="ssh-ed25519 AAAA ttl-valid",
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

            check_at = _NOW + timedelta(minutes=30)
            assert credential_is_valid(
                expires_at=cred.expires_at, revoked_at=cred.revoked_at, now=check_at
            )
        finally:
            engine.dispose()

    def test_credential_invalid_after_expiry(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A credential is invalid once its TTL has passed."""
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, worker_actor_id = _seed_org_and_actor(migrated_database_url)
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                enrollment = svc.register_worker(
                    ctx,
                    enrollment_token="tok-ttl-expired",
                    public_key="ssh-ed25519 AAAA ttl-expired",
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

            # Simulate the credential's TTL elapsing.
            after_expiry = cred.expires_at + timedelta(seconds=1)
            assert credential_is_expired(expires_at=cred.expires_at, now=after_expiry)
            assert not credential_is_valid(
                expires_at=cred.expires_at, revoked_at=cred.revoked_at, now=after_expiry
            )
        finally:
            engine.dispose()

    def test_credential_invalid_exactly_at_expiry(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A credential whose expiry equals 'now' is invalid (boundary: >)."""
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, worker_actor_id = _seed_org_and_actor(migrated_database_url)
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                enrollment = svc.register_worker(
                    ctx,
                    enrollment_token="tok-ttl-boundary",
                    public_key="ssh-ed25519 AAAA ttl-boundary",
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

            # exactly at the expiry boundary
            assert not credential_is_valid(
                expires_at=cred.expires_at, revoked_at=cred.revoked_at, now=cred.expires_at
            )
        finally:
            engine.dispose()

    def test_short_lived_credential_different_from_long_lived(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Short-duration credentials expire sooner; TTL is caller-controlled."""
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, worker_actor_id = _seed_org_and_actor(migrated_database_url)
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                enrollment = svc.register_worker(
                    ctx,
                    enrollment_token="tok-ttl-compare",
                    public_key="ssh-ed25519 AAAA ttl-compare",
                    worker_id=worker_actor_id,
                )
                svc.approve_worker(ctx, enrollment.id, approved_by=owner_id)

                short_cred = svc.issue_credential(
                    ctx,
                    enrollment.id,
                    scoped_capabilities=[str(_CAP_A)],
                    duration=timedelta(minutes=5),
                    now=_NOW,
                )
            assert short_cred.expires_at == _NOW + timedelta(minutes=5)
            # A check 10 minutes after issue sees the short cred as expired.
            ten_min_later = _NOW + timedelta(minutes=10)
            assert credential_is_expired(expires_at=short_cred.expires_at, now=ten_min_later)
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Guard: duplicate enrollment is rejected
# ---------------------------------------------------------------------------


class TestEnrollmentDuplicateGuard:
    """WorkerAlreadyEnrolledError is raised on duplicate (org, worker) pairs."""

    def test_duplicate_enrollment_raises(
        self,
        migrated_database_url: str,
        app_role_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WORKER_CREDENTIAL_KEY", _WORKER_CRED_KEY)

        org_id, owner_id, worker_actor_id = _seed_org_and_actor(migrated_database_url)
        ctx = _make_ctx(org_id, owner_id)
        engine = create_engine(app_role_database_url)
        try:
            with org_scoped_session(engine, ctx) as session:
                svc = WorkerService(session)
                svc.register_worker(
                    ctx,
                    enrollment_token="tok-dup-first",
                    public_key="ssh-ed25519 AAAA dup-first",
                    worker_id=worker_actor_id,
                )
                with pytest.raises(WorkerAlreadyEnrolledError):
                    svc.register_worker(
                        ctx,
                        enrollment_token="tok-dup-second",
                        public_key="ssh-ed25519 AAAA dup-second",
                        worker_id=worker_actor_id,
                    )
        finally:
            engine.dispose()
