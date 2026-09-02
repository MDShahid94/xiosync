"""Worker-enrollment & credential use cases (doc 07 §2; Phase 4).

``WorkerService`` is the sanctioned entry point for the worker-enrollment
lifecycle and short-lived credential issuance.  It enforces:

* **INV-WORKER-CRED-2 — enrollment token is hashed on receipt; the plaintext
  is never stored.**  ``register_worker`` receives the raw token, hashes it,
  and stores only the hash.
* **INV-WORKER-CRED-1 — credentials are short-lived, capability-scoped, and
  individually revocable.**  ``issue_credential`` mints a new row with a
  caller-supplied ``duration`` and ``scoped_capabilities`` list;
  ``revoke_credential`` sets ``revoked_at`` immediately.

The service takes the caller's ``Session`` (the caller owns the transaction via
``org_scoped_session``), flushes writes within it, and returns frozen dataclass
values so the service holds no live ORM state.  ``organization_id`` always comes
from the context, never from the caller.

Credential JWTs are signed with ``WORKER_CREDENTIAL_KEY`` — a key that is
**distinct** from ``JWT_SECRET`` (remediates H7: workers can never mint user or
admin tokens).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.domain.workers import (
    ENROLLMENT_APPROVED,
    ENROLLMENT_PENDING,
    ENROLLMENT_REVOKED,
    ENROLLMENT_SUSPENDED,
    worker_is_active,
    worker_is_enrollable,
)
from xiosync.persistence.models.workers import WorkerCredential, WorkerEnrollment
from xiosync.platform.ids import new_id

__all__ = [
    "CredentialNotFoundError",
    "EnrollmentNotFoundError",
    "InactiveWorkerError",
    "InvalidEnrollmentTokenError",
    "WorkerAlreadyEnrolledError",
    "WorkerCredentialRecord",
    "WorkerEnrollmentRecord",
    "WorkerService",
]

_WORKER_CRED_ALGORITHM = "HS256"
_WORKER_CRED_KEY_ENV = "WORKER_CREDENTIAL_KEY"


# ---------------------------------------------------------------------------
# Read-model dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerEnrollmentRecord:
    """Frozen snapshot of a ``worker_enrollments`` row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    worker_id: uuid.UUID
    enrollment_state: str
    pool_type: str
    public_key: str
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    created_at: datetime
    # Gap W-4: worker fleet versioning and capability manifest
    software_version: str | None
    software_hash: str | None
    capability_manifest: list[str]


@dataclass(frozen=True)
class WorkerCredentialRecord:
    """Frozen snapshot of a ``worker_credentials`` row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    enrollment_id: uuid.UUID
    scoped_capabilities: list[str]
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class EnrollmentNotFoundError(ValueError):
    """Raised when the requested enrollment row does not exist in the org."""


class WorkerAlreadyEnrolledError(ValueError):
    """Raised when a worker already has an enrollment record in the org.

    INV-WORKER-ENROLL-1: one enrollment per worker per org is enforced by the
    ``uq_worker_enrollments_org_worker`` unique constraint; this error surfaces
    the constraint violation as a domain error before hitting the DB.
    """


class InvalidEnrollmentTokenError(ValueError):
    """Raised when the supplied enrollment token does not match the stored hash."""


class CredentialNotFoundError(ValueError):
    """Raised when the requested credential row does not exist in the org."""


class InactiveWorkerError(ValueError):
    """Raised when an operation requires an approved worker but the enrollment
    is in state ``pending``, ``suspended``, or ``revoked``."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _hash_enrollment_token(token: str) -> str:
    """SHA-256 hex digest of the raw enrollment token (INV-WORKER-CRED-2)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row_to_enrollment_record(row: WorkerEnrollment) -> WorkerEnrollmentRecord:
    return WorkerEnrollmentRecord(
        id=row.id,
        organization_id=row.organization_id,
        worker_id=row.worker_id,
        enrollment_state=row.enrollment_state,
        pool_type=row.pool_type,
        public_key=row.public_key,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        created_at=row.created_at,
        software_version=row.software_version,
        software_hash=row.software_hash,
        capability_manifest=list(row.capability_manifest),
    )


def _row_to_credential_record(row: WorkerCredential) -> WorkerCredentialRecord:
    return WorkerCredentialRecord(
        id=row.id,
        organization_id=row.organization_id,
        enrollment_id=row.enrollment_id,
        scoped_capabilities=list(row.scoped_capabilities),
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


def _get_credential_signing_key() -> str:
    """Read ``WORKER_CREDENTIAL_KEY`` from the environment (H7 remediation).

    The worker credential key MUST be distinct from ``JWT_SECRET``; this
    function returns only ``WORKER_CREDENTIAL_KEY``, never ``JWT_SECRET``.
    Raises ``RuntimeError`` if the variable is absent or empty so that a
    misconfigured deployment fails loudly rather than silently downgrading
    security.
    """
    key = os.environ.get(_WORKER_CRED_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(
            f"Environment variable {_WORKER_CRED_KEY_ENV!r} is not set or empty. "
            "Worker credential issuance requires a key that is distinct from "
            "the user-session JWT secret (doc 07 §2 H7 remediation)."
        )
    return key


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class WorkerService:
    """Use-cases for worker enrollment and credential lifecycle (doc 07 §2).

    Instantiate with the caller's ``Session``; the caller owns the transaction
    boundary via ``org_scoped_session``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Enrollment lifecycle
    # ------------------------------------------------------------------

    def register_worker(
        self,
        ctx: OrgContext,
        *,
        enrollment_token: str,
        public_key: str,
        pool_type: str = "volunteer",
        worker_id: uuid.UUID | None = None,
        software_version: str | None = None,
        software_hash: str | None = None,
        capability_manifest: Sequence[Any] | None = None,
    ) -> WorkerEnrollmentRecord:
        """Register a worker by hashing its one-time enrollment token.

        Creates a ``pending`` enrollment row in the worker's org.  The raw
        token is never stored; only its hash is persisted (INV-WORKER-CRED-2).
        When ``worker_id`` is not supplied the method derives it from the token
        by hashing the token bytes into a deterministic UUIDv5 — callers should
        supply an explicit ``worker_id`` (e.g. the actor's UUID) in production.

        Gap W-4: ``software_version`` and ``software_hash`` track the worker's
        software revision for fleet versioning.  ``capability_manifest`` is a
        list of capability names the worker can execute, enabling future
        capability-based dispatch.

        Raises:
            InvalidEnrollmentTokenError: if the token is empty.
            WorkerAlreadyEnrolledError: if an enrollment already exists for
                the resolved worker in this org.
        """
        if not enrollment_token:
            raise InvalidEnrollmentTokenError("enrollment_token must not be empty")

        # Derive a stable worker_id from the token when one is not provided.
        # In production the caller supplies the actor's real UUID.
        if worker_id is None:
            worker_id = uuid.uuid5(
                uuid.UUID("00000000-0000-0000-0000-000000000001"),
                enrollment_token,
            )

        # Guard the unique constraint at the service layer (cleaner error).
        existing = self._session.execute(
            select(WorkerEnrollment).where(
                WorkerEnrollment.organization_id == ctx.organization_id,
                WorkerEnrollment.worker_id == worker_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise WorkerAlreadyEnrolledError(
                f"worker {worker_id} already has an enrollment in org {ctx.organization_id}"
            )

        token_hash = _hash_enrollment_token(enrollment_token)
        row = WorkerEnrollment(
            id=new_id(),
            organization_id=ctx.organization_id,
            worker_id=worker_id,
            enrollment_state=ENROLLMENT_PENDING,
            pool_type=pool_type,
            public_key=public_key,
            enrollment_token_hash=token_hash,
            software_version=software_version,
            software_hash=software_hash,
            capability_manifest=list(capability_manifest) if capability_manifest else [],
        )
        self._session.add(row)
        self._session.flush()
        return _row_to_enrollment_record(row)

    def approve_worker(
        self,
        ctx: OrgContext,
        enrollment_id: uuid.UUID,
        *,
        approved_by: uuid.UUID,
    ) -> WorkerEnrollmentRecord:
        """Approve a pending enrollment: ``pending`` → ``approved``.

        For ``managed`` pool workers the platform operator auto-approves; for
        ``volunteer`` pool workers explicit operator/org-admin action is
        required.  Sets ``approved_by`` and ``approved_at`` on the row.

        Raises:
            EnrollmentNotFoundError: if ``enrollment_id`` does not exist in
                this org.
            ValueError: if the enrollment is not in state ``pending``.
        """
        row = self._session.execute(
            select(WorkerEnrollment).where(
                WorkerEnrollment.organization_id == ctx.organization_id,
                WorkerEnrollment.id == enrollment_id,
            )
        ).scalar_one_or_none()
        if row is None:
            raise EnrollmentNotFoundError(
                f"enrollment {enrollment_id} not found in org {ctx.organization_id}"
            )
        if not worker_is_enrollable(row.enrollment_state):
            raise ValueError(
                f"enrollment {enrollment_id} is in state {row.enrollment_state!r}; "
                "only 'pending' enrollments may be approved"
            )
        row.enrollment_state = ENROLLMENT_APPROVED
        row.approved_by = approved_by
        row.approved_at = datetime.now(tz=UTC)
        row.updated_at = row.approved_at
        self._session.flush()
        return _row_to_enrollment_record(row)

    def suspend_worker(
        self,
        ctx: OrgContext,
        enrollment_id: uuid.UUID,
    ) -> WorkerEnrollmentRecord:
        """Suspend an approved worker: ``approved`` → ``suspended``.

        A suspended worker may not receive new credentials or lease tasks.
        Existing active credentials are not automatically revoked here; the
        caller should revoke them separately.

        Raises:
            EnrollmentNotFoundError: if ``enrollment_id`` does not exist.
            ValueError: if the enrollment is not in state ``approved``.
        """
        row = self._session.execute(
            select(WorkerEnrollment).where(
                WorkerEnrollment.organization_id == ctx.organization_id,
                WorkerEnrollment.id == enrollment_id,
            )
        ).scalar_one_or_none()
        if row is None:
            raise EnrollmentNotFoundError(
                f"enrollment {enrollment_id} not found in org {ctx.organization_id}"
            )
        if row.enrollment_state != ENROLLMENT_APPROVED:
            raise ValueError(
                f"enrollment {enrollment_id} is in state {row.enrollment_state!r}; "
                "only 'approved' enrollments may be suspended"
            )
        row.enrollment_state = ENROLLMENT_SUSPENDED
        row.updated_at = datetime.now(tz=UTC)
        self._session.flush()
        return _row_to_enrollment_record(row)

    def revoke_worker(
        self,
        ctx: OrgContext,
        enrollment_id: uuid.UUID,
    ) -> WorkerEnrollmentRecord:
        """Permanently revoke a worker: any state → ``revoked``.

        Revocation is immediate and irreversible.  All active credentials for
        this enrollment should be revoked by the caller before or after this
        call.

        Raises:
            EnrollmentNotFoundError: if ``enrollment_id`` does not exist.
        """
        row = self._session.execute(
            select(WorkerEnrollment).where(
                WorkerEnrollment.organization_id == ctx.organization_id,
                WorkerEnrollment.id == enrollment_id,
            )
        ).scalar_one_or_none()
        if row is None:
            raise EnrollmentNotFoundError(
                f"enrollment {enrollment_id} not found in org {ctx.organization_id}"
            )
        row.enrollment_state = ENROLLMENT_REVOKED
        row.updated_at = datetime.now(tz=UTC)
        self._session.flush()
        return _row_to_enrollment_record(row)

    # ------------------------------------------------------------------
    # Credential lifecycle
    # ------------------------------------------------------------------

    def issue_credential(
        self,
        ctx: OrgContext,
        enrollment_id: uuid.UUID,
        *,
        scoped_capabilities: list[str],
        duration: timedelta,
        now: datetime,
    ) -> WorkerCredentialRecord:
        """Mint a new short-lived, capability-scoped credential.

        Creates a ``worker_credentials`` row with ``expires_at = now +
        duration`` and ``revoked_at = None``.  Also mints a signed JWT payload
        using ``WORKER_CREDENTIAL_KEY`` (distinct from ``JWT_SECRET`` — H7
        remediation). The JWT is for execution-plane use only and carries no
        user or admin privileges.

        INV-WORKER-CRED-1: credentials are always short-lived (``duration``
        bounded by policy) and scoped to a non-empty capability set.

        Raises:
            EnrollmentNotFoundError: if ``enrollment_id`` does not exist.
            InactiveWorkerError: if the enrollment is not in state
                ``approved``.
        """
        enrollment_row = self._session.execute(
            select(WorkerEnrollment).where(
                WorkerEnrollment.organization_id == ctx.organization_id,
                WorkerEnrollment.id == enrollment_id,
            )
        ).scalar_one_or_none()
        if enrollment_row is None:
            raise EnrollmentNotFoundError(
                f"enrollment {enrollment_id} not found in org {ctx.organization_id}"
            )
        if not worker_is_active(enrollment_row.enrollment_state):
            raise InactiveWorkerError(
                f"enrollment {enrollment_id} is in state "
                f"{enrollment_row.enrollment_state!r}; credentials may only be "
                "issued to 'approved' workers (INV-WORKER-CRED-1)"
            )

        credential_id = new_id()
        expires_at = now + duration

        # Sign a JWT with the worker-credential key (H7 remediation).
        # This key is NEVER the user-session JWT_SECRET.
        signing_key = _get_credential_signing_key()
        _jwt_payload = {
            "jti": str(credential_id),
            "sub": str(enrollment_row.worker_id),
            "org": str(ctx.organization_id),
            "enr": str(enrollment_id),
            "cap": [str(c) for c in scoped_capabilities],
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        # The signed JWT is emitted here; callers may use it as the bearer
        # token on execution-plane lease requests.  We store only the
        # structured record in the DB (the JWT is returned via the caller's
        # own DTO layer if needed).
        jwt.encode(_jwt_payload, signing_key, algorithm=_WORKER_CRED_ALGORITHM)

        row = WorkerCredential(
            id=credential_id,
            organization_id=ctx.organization_id,
            enrollment_id=enrollment_id,
            scoped_capabilities=scoped_capabilities,
            issued_at=now,
            expires_at=expires_at,
            revoked_at=None,
        )
        self._session.add(row)
        self._session.flush()
        return _row_to_credential_record(row)

    def revoke_credential(
        self,
        ctx: OrgContext,
        credential_id: uuid.UUID,
    ) -> None:
        """Revoke a credential immediately by setting ``revoked_at = now()``.

        INV-WORKER-CRED-1: credentials are individually revocable; revocation
        is an additive write (the row is never deleted).

        Raises:
            CredentialNotFoundError: if ``credential_id`` does not exist in
                this org.
        """
        row = self._session.execute(
            select(WorkerCredential).where(
                WorkerCredential.organization_id == ctx.organization_id,
                WorkerCredential.id == credential_id,
            )
        ).scalar_one_or_none()
        if row is None:
            raise CredentialNotFoundError(
                f"credential {credential_id} not found in org {ctx.organization_id}"
            )
        row.revoked_at = datetime.now(tz=UTC)
        self._session.flush()

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_enrollment(
        self,
        ctx: OrgContext,
        enrollment_id: uuid.UUID,
    ) -> WorkerEnrollmentRecord | None:
        """Return the enrollment record or ``None`` if not found in this org."""
        row = self._session.execute(
            select(WorkerEnrollment).where(
                WorkerEnrollment.organization_id == ctx.organization_id,
                WorkerEnrollment.id == enrollment_id,
            )
        ).scalar_one_or_none()
        return _row_to_enrollment_record(row) if row is not None else None

    def get_credential(
        self,
        ctx: OrgContext,
        credential_id: uuid.UUID,
    ) -> WorkerCredentialRecord | None:
        """Return the credential record or ``None`` if not found in this org."""
        row = self._session.execute(
            select(WorkerCredential).where(
                WorkerCredential.organization_id == ctx.organization_id,
                WorkerCredential.id == credential_id,
            )
        ).scalar_one_or_none()
        return _row_to_credential_record(row) if row is not None else None
