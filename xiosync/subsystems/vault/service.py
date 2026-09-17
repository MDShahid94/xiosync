"""Vault Service — CRUD for vaulted_secrets.

Sharing model:
  - org_id = <uuid>  → private to that org (only org members can read)
  - org_id = None    → platform-global (shared across all orgs; admin-only write)

Any secret can reference another org's platform-global secret by key — this
is how shared infra credentials (e.g. a shared CF API token) work without
duplicating the ciphertext.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.subsystems.vault.crypto import VaultCrypto, VaultCryptoError

__all__ = ["VaultService", "VaultRecord", "VaultNotFoundError", "VaultCryptoError"]

_ALLOWED_TYPES = frozenset({
    "generic", "credential", "token", "totp", "api_key", "ssh_key", "oauth"
})


@dataclass(frozen=True, slots=True)
class VaultRecord:
    """Public (non-sensitive) metadata about a vaulted secret."""
    id: uuid.UUID
    organization_id: uuid.UUID | None   # None = platform-global
    key: str
    secret_type: str
    description: str | None
    expires_at: datetime | None
    rotated_at: datetime | None
    created_at: datetime
    updated_at: datetime


class VaultNotFoundError(KeyError):
    """Raised when the requested secret key does not exist."""


def _crypto() -> VaultCrypto:
    import os
    return VaultCrypto(os.environ["XIOSYNC_AUTH_SECRET"])


class VaultService:
    """Use cases for the Vault subsystem.

    Consumers (scripts, workers, API routers) never see raw ciphertext — only
    the plaintext string returned by get_secret().
    """

    def __init__(self, session: Session) -> None:
        self._db = session

    # ── Write ─────────────────────────────────────────────────────────────────

    def put_secret(
        self,
        ctx: OrgContext,
        key: str,
        value: str,
        *,
        secret_type: str = "generic",
        description: str | None = None,
        expires_at: datetime | None = None,
        platform_global: bool = False,
    ) -> VaultRecord:
        """Store or update a secret.

        Args:
            ctx:             Calling org context.
            key:             Namespaced key (e.g. 'cf_api_token', 'totp/user@g.com').
            value:           Plaintext secret value.
            secret_type:     One of the allowed types.
            description:     Human-readable purpose.
            expires_at:      Optional expiry.
            platform_global: If True, stores as a platform-level shared secret
                             (org_id=None). Requires admin capability.
        """
        if secret_type not in _ALLOWED_TYPES:
            raise ValueError(f"secret_type must be one of {sorted(_ALLOWED_TYPES)}")

        org_id = None if platform_global else ctx.organization_id
        ciphertext, iv, auth_tag = _crypto().encrypt(value, org_id)

        # Upsert
        self._db.execute(
            text("""
                INSERT INTO vaulted_secrets
                  (id, organization_id, key, ciphertext, iv, auth_tag,
                   secret_type, description, expires_at, created_at, updated_at)
                VALUES
                  (gen_random_uuid(), :org, :key, :ct, :iv, :tag,
                   :stype, :desc, :exp, now(), now())
                ON CONFLICT (organization_id, key)
                  DO UPDATE SET
                    ciphertext  = EXCLUDED.ciphertext,
                    iv          = EXCLUDED.iv,
                    auth_tag    = EXCLUDED.auth_tag,
                    secret_type = EXCLUDED.secret_type,
                    description = COALESCE(EXCLUDED.description, vaulted_secrets.description),
                    expires_at  = EXCLUDED.expires_at,
                    rotated_at  = now(),
                    updated_at  = now()
            """),
            {
                "org": str(org_id) if org_id else None,
                "key": key,
                "ct":  ciphertext,
                "iv":  iv,
                "tag": auth_tag,
                "stype": secret_type,
                "desc": description,
                "exp": expires_at,
            },
        )
        rec = self._meta(ctx, key, platform_global=platform_global)
        self._db.commit()
        return rec

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_secret(
        self,
        ctx: OrgContext,
        key: str,
        *,
        allow_platform: bool = True,
    ) -> str:
        """Decrypt and return a secret value.

        Lookup order (when allow_platform=True):
          1. Org-scoped secret matching (org_id, key)
          2. Platform-global secret matching (NULL, key)

        Raises:
            VaultNotFoundError: if not found.
            VaultCryptoError:   if decryption fails (tampered data).
        """
        row = self._db.execute(
            text("""
                SELECT ciphertext, iv, auth_tag, organization_id
                FROM vaulted_secrets
                WHERE (
                    (organization_id = :org AND key = :key)
                    OR (organization_id IS NULL AND key = :key AND :allow_platform)
                )
                ORDER BY
                    CASE WHEN organization_id IS NOT NULL THEN 0 ELSE 1 END
                LIMIT 1
            """),
            {"org": str(ctx.organization_id), "key": key,
             "allow_platform": allow_platform},
        ).fetchone()

        if not row:
            raise VaultNotFoundError(f"Secret '{key}' not found")

        org_id = uuid.UUID(str(row.organization_id)) if row.organization_id else None
        return _crypto().decrypt(bytes(row.ciphertext), bytes(row.iv),
                                 bytes(row.auth_tag), org_id)

    def get_meta(self, ctx: OrgContext, key: str) -> VaultRecord:
        """Return metadata (no plaintext) for a secret."""
        return self._meta(ctx, key)

    def list_secrets(
        self,
        ctx: OrgContext,
        *,
        secret_type: str | None = None,
        include_platform: bool = True,
    ) -> list[VaultRecord]:
        """List secret metadata (keys only, no plaintext)."""
        where = [
            "(organization_id = :org OR (:incl_platform AND organization_id IS NULL))"
        ]
        params: dict[str, Any] = {
            "org": str(ctx.organization_id),
            "incl_platform": include_platform,
        }
        if secret_type:
            where.append("secret_type = :stype")
            params["stype"] = secret_type

        rows = self._db.execute(
            text(f"""
                SELECT id, organization_id, key, secret_type,
                       description, expires_at, rotated_at, created_at, updated_at
                FROM vaulted_secrets
                WHERE {" AND ".join(where)}
                ORDER BY key
            """),
            params,
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    # ── Delete ────────────────────────────────────────────────────────────────

    def delete_secret(self, ctx: OrgContext, key: str) -> None:
        result = self._db.execute(
            text("""
                DELETE FROM vaulted_secrets
                WHERE key = :key AND organization_id = :org
                RETURNING id
            """),
            {"key": key, "org": str(ctx.organization_id)},
        ).fetchone()
        if not result:
            raise VaultNotFoundError(f"Secret '{key}' not found")
        self._db.commit()

    # ── Private helpers ───────────────────────────────────────────────────────

    def _meta(
        self, ctx: OrgContext, key: str, *, platform_global: bool = False
    ) -> VaultRecord:
        org_clause = "organization_id IS NULL" if platform_global \
                     else "organization_id = :org"
        row = self._db.execute(
            text(f"""
                SELECT id, organization_id, key, secret_type,
                       description, expires_at, rotated_at, created_at, updated_at
                FROM vaulted_secrets
                WHERE {org_clause} AND key = :key
            """),
            {"org": str(ctx.organization_id), "key": key},
        ).fetchone()
        if not row:
            raise VaultNotFoundError(f"Secret '{key}' not found")
        return _row_to_record(row)


def _row_to_record(row: Any) -> VaultRecord:
    return VaultRecord(
        id=uuid.UUID(str(row.id)),
        organization_id=uuid.UUID(str(row.organization_id)) if row.organization_id else None,
        key=row.key,
        secret_type=row.secret_type,
        description=row.description,
        expires_at=row.expires_at,
        rotated_at=row.rotated_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
