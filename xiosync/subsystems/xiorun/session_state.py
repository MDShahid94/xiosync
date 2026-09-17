"""session_state.py — Cookie/storageState JSON vault I/O (FALLBACK restore path).

Used ONLY when no Chrome profile tarball exists in R2 (first-ever session
for a given identity, or profile was intentionally purged).

Anti-degradation guarantees (ported from XIOBR session-manager.mjs):
  1. TTL-aware merge: for every cookie, the one with the longer remaining
     TTL survives. Session cookies (expires=-1/0) are always valid.
  2. Mid-auth URL guard: if the page URL is on a Google OAuth flow,
     skip updating Google-domain cookies entirely (keep existing valid tokens).
  3. SID-TTL safety: if the incoming Google SID cookie's TTL is more than
     SID_TTL_THRESHOLD_SEC shorter than the existing one, reject the update.
     This catches the "invalidated redirect token" pattern where Google
     issues a fresh SID with the same name but a shorter TTL after a failed auth.

Emergency saves (on browser.disconnected):
  Always activate mid-auth guard. Never overwrite Google cookies.
  Non-Google cookies are still saved.

Vault key convention (established by D1 migration):
  identities/{identity_id}/cookie_state
  organization_id = 00000000-0000-7000-8000-000000000000 (Org Zero)
  conflict target = (organization_id, key)
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Org Zero — all cookie_state secrets are scoped here
_ORG_ZERO = uuid.UUID("00000000-0000-7000-8000-000000000000")

# URLs that indicate an in-progress Google OAuth/login flow
MID_AUTH_URL_PATTERNS: tuple[str, ...] = (
    "accounts.google.com/o/oauth2",
    "accounts.google.com/signin",
    "accounts.google.com/v3/signin",
    "accounts.google.com/ServiceLogin",
    "accounts.google.com/checkpoint",
    "accounts.google.com/b/0/accountchooser",
)

# Domains treated as Google-auth-sensitive
GOOGLE_DOMAINS: tuple[str, ...] = (
    "google.com",
    "accounts.google.com",
    "myaccount.google.com",
    "mail.google.com",
)

# SID cookie names where TTL shortening signals invalidation
GOOGLE_SID_NAMES: frozenset[str] = frozenset({"SID", "SSID", "HSID", "APISID", "SAPISID", "SIDCC"})

# If incoming Google SID TTL is this many seconds shorter → reject update
SID_TTL_THRESHOLD_SEC: int = 3_600  # 1 hour


def _is_mid_auth(page_url: str | None) -> bool:
    if not page_url:
        return False
    return any(pat in page_url for pat in MID_AUTH_URL_PATTERNS)


def _cookie_remaining_ttl(cookie: dict[str, Any], now_sec: float) -> float:
    """Returns remaining TTL in seconds. Infinity for session cookies."""
    expires = cookie.get("expires", -1) or -1
    if expires <= 0:
        return float("inf")
    return max(0.0, expires - now_sec)


def _apex_domain(domain: str) -> str:
    return domain.lstrip(".")


def _is_google_domain(cookie: dict[str, Any]) -> bool:
    apex = _apex_domain(cookie.get("domain", ""))
    return any(apex == gd or apex.endswith("." + gd) for gd in GOOGLE_DOMAINS)


def _sanitise_partition_keys(cookies: list[dict[str, Any]]) -> None:
    """Fix partitionKey: CDP Chrome 119+ returns an object, Playwright needs a string."""
    for c in cookies:
        pk = c.get("partitionKey")
        if pk is not None and not isinstance(pk, str):
            if isinstance(pk, dict):
                c["partitionKey"] = pk.get("topLevelSite", "")
            else:
                c["partitionKey"] = str(pk)


def _merge_cookies(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    guard_google: bool,
    now_sec: float,
) -> list[dict[str, Any]]:
    """Merge two cookie lists. Longer TTL always wins."""
    idx: dict[tuple[str, str, str], dict[str, Any]] = {}
    for c in existing:
        key = (_apex_domain(c.get("domain", "")), c.get("name", ""), c.get("path", "/"))
        idx[key] = c

    for c in incoming:
        domain = _apex_domain(c.get("domain", ""))
        name   = c.get("name", "")
        path   = c.get("path", "/")
        key    = (domain, name, path)

        if guard_google and _is_google_domain(c):
            continue

        prev = idx.get(key)
        if prev is None:
            idx[key] = c
            continue

        if _is_google_domain(c) and name in GOOGLE_SID_NAMES:
            prev_ttl = _cookie_remaining_ttl(prev, now_sec)
            new_ttl  = _cookie_remaining_ttl(c,    now_sec)
            if prev_ttl - new_ttl > SID_TTL_THRESHOLD_SEC:
                logger.warning(
                    "xiorun.session_state.sid_ttl_guard",
                    extra={
                        "cookie_name": name, "domain": domain,
                        "prev_ttl_h": round(prev_ttl / 3600, 1),
                        "new_ttl_h":  round(new_ttl  / 3600, 1),
                    },
                )
                continue

        prev_ttl = _cookie_remaining_ttl(prev, now_sec)
        new_ttl  = _cookie_remaining_ttl(c,    now_sec)
        if new_ttl >= prev_ttl:
            idx[key] = c

    return list(idx.values())


def _get_crypto():
    """Return a VaultCrypto instance from XIOSYNC_AUTH_SECRET."""
    from xiosync.subsystems.vault.crypto import VaultCrypto  # noqa: PLC0415
    return VaultCrypto(os.environ["XIOSYNC_AUTH_SECRET"])


class SessionStateIO:
    """Cookie/storageState JSON vault fallback store.

    Vault key: identities/{identity_id}/cookie_state
    Org scope:  Org Zero (00000000-0000-7000-8000-000000000000)
    """

    def __init__(self, engine: object) -> None:
        self._engine = engine

    def load(self, identity_id: str, org_id: str) -> dict[str, Any] | None:
        """Read storageState dict from vaulted_secrets.

        Returns None if no cookie_state vaulted for this identity.
        """
        vault_key = f"identities/{identity_id}/cookie_state"
        try:
            from sqlalchemy import text  # noqa: PLC0415
            from sqlalchemy.orm import Session as OrmSession  # noqa: PLC0415

            with OrmSession(self._engine) as sess:
                row = sess.execute(
                    text("""
                        SELECT ciphertext, iv, auth_tag, organization_id
                        FROM vaulted_secrets
                        WHERE organization_id = :org AND key = :k
                        LIMIT 1
                    """),
                    {"org": str(_ORG_ZERO), "k": vault_key},
                ).mappings().first()

                if not row:
                    return None

                plaintext = _get_crypto().decrypt(
                    bytes(row["ciphertext"]),
                    bytes(row["iv"]),
                    bytes(row["auth_tag"]),
                    _ORG_ZERO,
                )
                state = json.loads(plaintext)

                if isinstance(state, dict) and "cookies" in state:
                    _sanitise_partition_keys(state["cookies"])

                return state

        except Exception as exc:
            logger.warning("xiorun.session_state.load_error", extra={
                "identity_id": identity_id, "error": str(exc)
            })
            return None

    def save(
        self,
        identity_id: str,
        org_id: str,
        state: dict[str, Any],
        page_url: str | None = None,
        emergency: bool = False,
    ) -> None:
        """Merge-save storageState to vaulted_secrets.

        Anti-degradation:
          - Mid-auth guard: skip Google cookies if page_url is OAuth URL
          - Emergency mode: always activates mid-auth guard
          - SID-TTL check: rejects incoming Google SID if TTL is much shorter
          - TTL-aware merge: longer remaining TTL always wins
        """
        vault_key    = f"identities/{identity_id}/cookie_state"
        now_sec      = time.time()
        guard_google = emergency or _is_mid_auth(page_url)

        try:
            from sqlalchemy import text  # noqa: PLC0415
            from sqlalchemy.orm import Session as OrmSession  # noqa: PLC0415

            with OrmSession(self._engine) as sess:
                # Load existing state for merge
                existing_cookies: list[dict] = []
                row = sess.execute(
                    text("""
                        SELECT ciphertext, iv, auth_tag
                        FROM vaulted_secrets
                        WHERE organization_id = :org AND key = :k
                        LIMIT 1
                    """),
                    {"org": str(_ORG_ZERO), "k": vault_key},
                ).mappings().first()

                if row:
                    try:
                        old_state = json.loads(
                            _get_crypto().decrypt(
                                bytes(row["ciphertext"]),
                                bytes(row["iv"]),
                                bytes(row["auth_tag"]),
                                _ORG_ZERO,
                            )
                        )
                        existing_cookies = old_state.get("cookies", [])
                    except Exception as _dec_exc:
                        logger.warning(
                            "session_state.cookie_decrypt_failed",
                            extra={
                                "identity_id": str(identity_id),
                                "error":       str(_dec_exc),
                                "effect":      "merging with empty existing_cookies",
                            },
                        )

                incoming_cookies = state.get("cookies", [])
                merged_cookies   = _merge_cookies(
                    existing_cookies, incoming_cookies, guard_google, now_sec
                )

                merged_state = {
                    **state,
                    "cookies":      merged_cookies,
                    "_version":     2,
                    "_captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }

                ciphertext, iv, auth_tag = _get_crypto().encrypt(
                    json.dumps(merged_state), _ORG_ZERO
                )

                # conflict target: (organization_id, key) — unique constraint
                sess.execute(
                    text("""
                        INSERT INTO vaulted_secrets
                            (id, organization_id, key, ciphertext, iv, auth_tag,
                             secret_type, created_at, updated_at)
                        VALUES
                            (gen_random_uuid(), :org, :k, :ct, :iv, :at,
                             'generic', now(), now())
                        ON CONFLICT (organization_id, key)
                        DO UPDATE SET
                            ciphertext = EXCLUDED.ciphertext,
                            iv         = EXCLUDED.iv,
                            auth_tag   = EXCLUDED.auth_tag,
                            rotated_at = now(),
                            updated_at = now()
                    """),
                    {
                        "org": str(_ORG_ZERO),
                        "k":   vault_key,
                        "ct":  ciphertext,
                        "iv":  iv,
                        "at":  auth_tag,
                    },
                )
                sess.commit()

                logger.info("xiorun.session_state.saved", extra={
                    "identity_id":  identity_id,
                    "cookie_count": len(merged_cookies),
                    "guard_google": guard_google,
                    "emergency":    emergency,
                })

        except Exception as exc:
            logger.warning("xiorun.session_state.save_error", extra={
                "identity_id": identity_id, "error": str(exc)
            })
