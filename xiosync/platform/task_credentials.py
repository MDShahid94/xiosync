"""Single-use, per-lease task credentials (doc 07 §3; INV-TASK-SEC-1/2; D-007).

A *task credential* is the scoped, short-lived bearer material a worker receives
**at lease time** so a capability that needs secrets (e.g. an API key) never has
the raw stored secret placed in the task payload (INV-TASK-SEC-1). It is minted
from the control-plane signing key — the ``WORKER_CREDENTIAL_KEY`` that is
**distinct** from the user-session ``JWT_SECRET`` (H7 remediation) — and is bound
to the exact ``(task_id, worker_id)`` pair for the lease that produced it
(INV-TASK-SEC-2), expiring with that lease so it cannot be replayed on another
task or after the lease is gone.

Design mirrors :mod:`xiosync.platform.tokens`: signing/verification are pure
functions that take the ``secret`` as an argument so time and key are fully
controllable under test. ``verify_task_credential`` proves signature, lifetime,
claim-completeness, **and** the ``(task_id, worker_id)`` binding — a signature
alone never authorizes a task. The single-use nature is expressed by the
per-lease ``lease_id`` (``lse``) claim and the unique ``jti``; a credential
whose lease has expired or been superseded fails the lifetime check.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from xiosync.platform.ids import new_id

# A task credential never outlives its lease; the lease API caps a lease at
# one hour (``LeaseRequest.duration_seconds <= 3600``), so a credential minted
# at lease time is bounded by the same ceiling (INV-TASK-SEC-2 — expires with
# the lease).
TASK_CREDENTIAL_MAX_TTL = timedelta(hours=1)

# The task-credential signing key is the execution-plane worker credential key
# (D-007). It is NEVER the user-session JWT secret (H7 remediation).
WORKER_CREDENTIAL_KEY_ENV = "WORKER_CREDENTIAL_KEY"

_ALGORITHM = "HS256"
_REQUIRED_CLAIMS = ("jti", "tsk", "wkr", "lse", "org", "cap", "iat", "exp")


class TaskCredentialError(ValueError):
    """The task credential is malformed, mis-signed, expired, missing claims,
    or bound to a different ``(task_id, worker_id)`` pair.

    Callers never branch on *why* — a single rejection path, no oracle
    (mirrors :class:`xiosync.platform.tokens.TokenError`).
    """


@dataclass(frozen=True, slots=True)
class TaskCredentialClaims:
    """The decoded, verified claim set of one task credential."""

    jti: str
    task_id: uuid.UUID
    worker_id: uuid.UUID
    lease_id: uuid.UUID
    organization_id: uuid.UUID
    scoped_capabilities: tuple[uuid.UUID, ...]
    issued_at: datetime
    expires_at: datetime


def mint_task_credential(
    secret: str,
    *,
    task_id: uuid.UUID,
    worker_id: uuid.UUID,
    lease_id: uuid.UUID,
    organization_id: uuid.UUID,
    scoped_capabilities: list[uuid.UUID],
    now: datetime,
    expires_at: datetime,
) -> tuple[str, TaskCredentialClaims]:
    """Mint a single-use task credential bound to ``(task_id, worker_id)``.

    ``expires_at`` MUST be the lease's own expiry so the credential dies with
    the lease (INV-TASK-SEC-2). ``scoped_capabilities`` MUST be non-empty — a
    credential with no capability grants nothing and is a defect (INV-TASK-SEC-1
    scoped material). Returns ``(token, claims)``.
    """
    if not secret:
        raise ValueError("secret must not be empty")
    if now.tzinfo is None or expires_at.tzinfo is None:
        raise ValueError("now and expires_at must be timezone-aware (platform/clock rule)")
    if not scoped_capabilities:
        raise ValueError(
            "scoped_capabilities must be non-empty; a task credential is always "
            "scoped to at least one capability (INV-TASK-SEC-1)"
        )
    ttl = expires_at - now
    if ttl <= timedelta(0):
        raise ValueError("expires_at must be strictly after now")
    if ttl > TASK_CREDENTIAL_MAX_TTL:
        raise ValueError(
            f"task credential TTL must be within (0, {TASK_CREDENTIAL_MAX_TTL}]; got {ttl}"
        )

    jti = str(new_id())
    capabilities = tuple(scoped_capabilities)
    payload = {
        "jti": jti,
        "tsk": str(task_id),
        "wkr": str(worker_id),
        "lse": str(lease_id),
        "org": str(organization_id),
        "cap": [str(c) for c in capabilities],
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, secret, algorithm=_ALGORITHM)
    claims = TaskCredentialClaims(
        jti=jti,
        task_id=task_id,
        worker_id=worker_id,
        lease_id=lease_id,
        organization_id=organization_id,
        scoped_capabilities=capabilities,
        issued_at=now,
        expires_at=expires_at,
    )
    return token, claims


def verify_task_credential(
    secret: str,
    token: str,
    *,
    now: datetime,
    expected_task_id: uuid.UUID | None = None,
    expected_worker_id: uuid.UUID | None = None,
) -> TaskCredentialClaims:
    """Verify signature, lifetime, claims, and the ``(task_id, worker_id)`` bind.

    INV-TASK-SEC-2: when ``expected_task_id`` / ``expected_worker_id`` are
    supplied, a credential whose ``tsk`` / ``wkr`` claim does not match is
    rejected — this is the replay guard that stops a credential minted for one
    task being presented on another. Raises :class:`TaskCredentialError` on any
    defect, including an expired lease (checked against the injected ``now``).
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (platform/clock rule)")
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            # Lifetime is checked below against the *injected* clock so tests
            # control time; PyJWT's exp/iat checks read the wall clock.
            options={
                "require": list(_REQUIRED_CLAIMS),
                "verify_exp": False,
                "verify_iat": False,
            },
        )
        if int(payload["exp"]) <= int(now.timestamp()):
            raise TaskCredentialError("task credential rejected")
        claims = TaskCredentialClaims(
            jti=str(payload["jti"]),
            task_id=uuid.UUID(str(payload["tsk"])),
            worker_id=uuid.UUID(str(payload["wkr"])),
            lease_id=uuid.UUID(str(payload["lse"])),
            organization_id=uuid.UUID(str(payload["org"])),
            scoped_capabilities=tuple(uuid.UUID(str(c)) for c in payload["cap"]),
            issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=UTC),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
        )
    except TaskCredentialError:
        raise
    except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
        raise TaskCredentialError("task credential rejected") from exc

    # Binding checks are the INV-TASK-SEC-2 replay guard. They run after the
    # cryptographic decode so a mismatch and a bad signature look identical.
    if expected_task_id is not None and claims.task_id != expected_task_id:
        raise TaskCredentialError("task credential rejected")
    if expected_worker_id is not None and claims.worker_id != expected_worker_id:
        raise TaskCredentialError("task credential rejected")
    return claims


def load_task_credential_signing_key() -> str:
    """Read the task-credential signing key from the environment (H7 / D-007).

    Returns ``WORKER_CREDENTIAL_KEY`` — the execution-plane key that is
    **distinct** from the user-session JWT secret. Raises ``RuntimeError`` when
    it is absent or empty so a misconfigured deployment fails loudly rather than
    silently downgrading security (INV-SEC-1: no embedded default).
    """
    key = os.environ.get(WORKER_CREDENTIAL_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(
            f"Environment variable {WORKER_CREDENTIAL_KEY_ENV!r} is not set or empty. "
            "Task credential minting requires a key that is distinct from the "
            "user-session JWT secret (doc 07 §3; H7 remediation)."
        )
    return key
