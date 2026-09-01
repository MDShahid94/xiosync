"""Worker-enrollment & trust-tier domain predicates (doc 07 §2; INV-TRUST-1/2).

Pure functions — no I/O, no database, no xiosync imports — so every caller
(service layer, integration tests, property-based checks) can import these
without touching the ORM or event loop.

Trust tiers are totally ordered (weakest → strongest):

    newcomer < contributor < trusted < core < admin

INV-TRUST-1: a worker *satisfies* a required tier when its own tier is at
least that tier (index comparison).  The predicate is the single source of
truth; the service layer never re-implements it.

Enrollment states are a closed lifecycle:

    pending → approved → suspended → revoked

Pool types classify how a worker joined:

    managed   — provisioned by the platform operator
    volunteer — self-enrolled, requires explicit approval
"""

from __future__ import annotations

from datetime import datetime

# ---------------------------------------------------------------------------
# Trust tiers — total ordering, weakest first.
# ---------------------------------------------------------------------------

TRUST_TIERS: tuple[str, ...] = (
    "newcomer",
    "contributor",
    "trusted",
    "core",
    "admin",
)

# ---------------------------------------------------------------------------
# Enrollment states
# ---------------------------------------------------------------------------

ENROLLMENT_PENDING = "pending"
ENROLLMENT_APPROVED = "approved"
ENROLLMENT_SUSPENDED = "suspended"
ENROLLMENT_REVOKED = "revoked"

# ---------------------------------------------------------------------------
# Pool types
# ---------------------------------------------------------------------------

POOL_MANAGED = "managed"
POOL_VOLUNTEER = "volunteer"


# ---------------------------------------------------------------------------
# Trust-tier predicates
# ---------------------------------------------------------------------------


def trust_tier_index(tier: str) -> int:
    """Return the 0-based position of *tier* in the ordered trust tier set.

    Raises ``ValueError`` for any string that is not a recognised tier.
    """
    try:
        return TRUST_TIERS.index(tier)
    except ValueError as exc:
        raise ValueError(
            f"Unknown trust tier {tier!r}. Must be one of {TRUST_TIERS}."
        ) from exc


def trust_tier_satisfies(actor_tier: str, required_tier: str) -> bool:
    """Return True when *actor_tier* is at least *required_tier*.

    INV-TRUST-1: capability grants are gated on this predicate; it is the
    single authoritative comparison.  Both arguments must be recognised tiers
    (``ValueError`` otherwise).
    """
    return trust_tier_index(actor_tier) >= trust_tier_index(required_tier)


# ---------------------------------------------------------------------------
# Enrollment-state predicates
# ---------------------------------------------------------------------------


def worker_is_enrollable(enrollment_state: str) -> bool:
    """Return True only when the enrollment row is still ``pending``.

    A ``pending`` enrollment has not yet been approved, suspended, or revoked,
    so the approval transition is still available.
    """
    return enrollment_state == ENROLLMENT_PENDING


def worker_is_active(enrollment_state: str) -> bool:
    """Return True only when the enrollment is ``approved``.

    An approved worker may receive credentials and lease tasks.  Any other
    state — pending, suspended, or revoked — is not active.
    """
    return enrollment_state == ENROLLMENT_APPROVED


# ---------------------------------------------------------------------------
# Trust-tier promotion / demotion predicates
# ---------------------------------------------------------------------------


def worker_can_promote(
    current_tier: str,
    successful_executions: int,
    required_executions: int,
) -> bool:
    """Return True when the worker qualifies for a one-step trust promotion.

    Conditions (both must hold):
    * *current_tier* is **not** ``admin`` — the ceiling tier cannot promote.
    * *successful_executions* >= *required_executions*.

    INV-TRUST-2 (proof-based promotion): promotion is gated on demonstrated
    successful work, never on time-in-role alone.
    """
    if current_tier == "admin":
        return False
    # Validate that current_tier is known (raises ValueError if not).
    trust_tier_index(current_tier)
    return successful_executions >= required_executions


def worker_can_demote(
    current_tier: str,
    failed_executions: int,
    threshold: int,
) -> bool:
    """Return True when the worker qualifies for a one-step trust demotion.

    Conditions (both must hold):
    * *current_tier* is **not** ``newcomer`` — the floor tier cannot demote.
    * *failed_executions* >= *threshold*.

    INV-TRUST-2 (proof-based demotion): demotion is triggered by accumulated
    evidence of failure, not a manual override.
    """
    if current_tier == "newcomer":
        return False
    # Validate that current_tier is known (raises ValueError if not).
    trust_tier_index(current_tier)
    return failed_executions >= threshold


# ---------------------------------------------------------------------------
# Credential predicates
# ---------------------------------------------------------------------------


def credential_is_valid(
    expires_at: datetime,
    revoked_at: datetime | None,
    now: datetime,
) -> bool:
    """Return True when the credential is both un-revoked and un-expired.

    INV-WORKER-CRED-1: a credential is valid if and only if it has not been
    explicitly revoked (``revoked_at is None``) *and* its expiry wall-clock
    has not yet passed (``expires_at > now``).
    """
    return revoked_at is None and expires_at > now


def credential_is_expired(expires_at: datetime, now: datetime) -> bool:
    """Return True when the credential's wall-clock expiry has passed.

    This predicate is independent of revocation — a credential can be both
    expired *and* revoked; expiry alone is checked here.
    """
    return expires_at <= now
