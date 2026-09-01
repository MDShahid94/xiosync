"""Short-lived HS256 access tokens (doc 05 §2.2; C8; D-023).

An access token is *not* the session: it is a signed pointer at one. Every
claim doc 05 §2.2 names is required — ``jti``, ``session_id``, ``org_id``,
``actor_id`` — plus standard ``iat``/``exp``. Verification here proves only
signature and lifetime; INV-SESSION-1 (the revocation kill switch) is the
service layer's job, which must still look the session row up.

TTL is capped at 15 minutes (doc 05 §2.2). Signing uses PyJWT (D-023);
tokens never carry secrets or roles — authorization is decided server-side.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from xiosync.platform.ids import new_id

ACCESS_TOKEN_MAX_TTL = timedelta(minutes=15)
DEFAULT_ACCESS_TOKEN_TTL = ACCESS_TOKEN_MAX_TTL

_ALGORITHM = "HS256"
_REQUIRED_CLAIMS = ("jti", "sid", "org", "act", "iat", "exp")


class TokenError(ValueError):
    """The token is malformed, mis-signed, expired, or missing claims.

    Callers never branch on *why* — a single rejection path, no oracle.
    """


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    """The decoded, verified claim set of one access token."""

    jti: str
    session_id: uuid.UUID
    organization_id: uuid.UUID
    actor_id: uuid.UUID
    issued_at: datetime
    expires_at: datetime


def issue_access_token(
    secret: str,
    *,
    session_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID,
    now: datetime,
    ttl: timedelta = DEFAULT_ACCESS_TOKEN_TTL,
) -> tuple[str, AccessTokenClaims]:
    """Sign and return ``(token, claims)``. ``ttl`` may not exceed 15 min."""
    if not secret:
        raise ValueError("secret must not be empty")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (platform/clock rule)")
    if ttl <= timedelta(0) or ttl > ACCESS_TOKEN_MAX_TTL:
        raise ValueError(f"ttl must be within (0, {ACCESS_TOKEN_MAX_TTL}]; got {ttl}")

    jti = str(new_id())
    expires_at = now + ttl
    payload = {
        "jti": jti,
        "sid": str(session_id),
        "org": str(organization_id),
        "act": str(actor_id),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, secret, algorithm=_ALGORITHM)
    claims = AccessTokenClaims(
        jti=jti,
        session_id=session_id,
        organization_id=organization_id,
        actor_id=actor_id,
        issued_at=now,
        expires_at=expires_at,
    )
    return token, claims


def verify_access_token(secret: str, token: str, *, now: datetime) -> AccessTokenClaims:
    """Verify signature, lifetime, and claim completeness; decode claims.

    Raises ``TokenError`` on any defect. The caller MUST additionally check
    the referenced session row is ``active`` (INV-SESSION-1) — a valid
    signature alone never grants access.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (platform/clock rule)")
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            # Lifetime is checked below against the *injected* clock so that
            # tests control time; PyJWT's exp/iat checks read the wall clock.
            options={
                "require": list(_REQUIRED_CLAIMS),
                "verify_exp": False,
                "verify_iat": False,
            },
        )
        if int(payload["exp"]) <= int(now.timestamp()):
            raise TokenError("token rejected")
        return AccessTokenClaims(
            jti=str(payload["jti"]),
            session_id=uuid.UUID(str(payload["sid"])),
            organization_id=uuid.UUID(str(payload["org"])),
            actor_id=uuid.UUID(str(payload["act"])),
            issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=UTC),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
        )
    except TokenError:
        raise
    except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
        raise TokenError("token rejected") from exc
