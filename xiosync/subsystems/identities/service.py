"""Identity Service — universal external-identity + credential registry.

An Identity is any externally-managed account that XIOSYNC members use on
third-party platforms (Google, GitHub, Stripe, Cloudflare, custom, …).
XIOSYNC does not define which platforms are valid — the `platform` field
is free-form text. Identities are org-scoped by default; platform_global=True
makes them shared across all orgs (organization_id=NULL).

Credential registry:
  All sensitive values are encrypted at rest in vaulted_secrets and referenced
  by vault_key. The `credentials` table stores only metadata + vault references.
  Large blobs (Chrome profiles, certs) are referenced via storage_object_key.

Allocation:
  allocate(platform, tags) returns the least-recently-used active identity for
  a given platform, enabling workers to acquire identities without manual
  assignment (round-robin, LRU scheduling). Tags provide fine-grained filtering.

Sharing model:
  identities.organization_id = NULL  → platform-global shared identity
  identities.organization_id = <id>  → org-private identity
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext

__all__ = [
    "IdentityService", "IdentityRecord", "CredentialRecord",
    "IdentityNotFoundError", "IdentityConflictError",
]

# Open state set — validated at service layer only, never in DB schema.
# Platforms may define their own states via tags or metadata.
_DEFAULT_STATES = frozenset({
    "active", "suspended", "banned", "unverified", "expired", "archived"
})


class IdentityNotFoundError(KeyError):
    pass


class IdentityConflictError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IdentityRecord:
    id: uuid.UUID
    organization_id: uuid.UUID | None
    identifier: str               # email, username, account ID, phone, handle, …
    platform: str                 # google, github, stripe, cloudflare, custom, …
    display_name: str | None
    state: str                    # active | suspended | banned | unverified | expired | archived | …
    metadata: dict[str, Any]      # free-form: xiobr_tier (legacy), credits, plan, quota, …
    tags: list[str]               # free-form labels: ['pro', 'bot', 'verified', …]
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime
    is_platform_global: bool


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    id: uuid.UUID
    organization_id: uuid.UUID | None
    identity_id: uuid.UUID
    credential_type: str          # free-form: password, api_key, oauth_token, cookie, totp_secret, …
    label: str                    # differentiates multiple creds of same type; default='default'
    vault_key: str | None         # reference into vaulted_secrets
    storage_object_key: str | None # reference into storage_objects (large blobs)
    health_score: float
    last_refreshed_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class IdentityService:

    def __init__(self, session: Session) -> None:
        self._db = session

    # ── Identities ────────────────────────────────────────────────────────────

    def create_identity(
        self,
        ctx: OrgContext,
        identifier: str,
        platform: str,
        *,
        display_name: str | None = None,
        state: str = "active",
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        platform_global: bool = False,
    ) -> IdentityRecord:
        """Register a new identity. Raises IdentityConflictError if already exists."""
        org_id = None if platform_global else ctx.organization_id
        try:
            row = self._db.execute(
                text("""
                    INSERT INTO identities
                      (id, organization_id, identifier, platform, display_name,
                       state, metadata, tags, created_at, updated_at)
                    VALUES
                      (gen_random_uuid(), :org, :ident, :plat, :dname,
                       :state, cast(:meta as jsonb), :tags, now(), now())
                    RETURNING id, organization_id, identifier, platform, display_name,
                              state, metadata, tags, last_used_at, created_at, updated_at
                """),
                {
                    "org": str(org_id) if org_id else None,
                    "ident": identifier, "plat": platform,
                    "dname": display_name, "state": state,
                    "meta": json.dumps(metadata or {}),
                    "tags": list(tags or []),
                },
            ).fetchone()
        except Exception as exc:
            if "unique" in str(exc).lower():
                raise IdentityConflictError(
                    f"Identity {identifier!r} on {platform!r} already exists"
                ) from exc
            raise
        rec = _row_to_identity(row)
        self._db.commit()
        return rec

    def list_identities(
        self,
        ctx: OrgContext,
        *,
        platform: str | None = None,
        state: str | None = None,
        tags: list[str] | None = None,
        include_platform: bool = True,
        limit: int = 100,
    ) -> list[IdentityRecord]:
        where = ["(i.organization_id = :org OR (:incl AND i.organization_id IS NULL))"]
        params: dict[str, Any] = {
            "org": str(ctx.organization_id),
            "incl": include_platform,
            "limit": limit,
        }
        if platform:
            where.append("i.platform = :plat")
            params["plat"] = platform
        if state:
            where.append("i.state = :state")
            params["state"] = state
        if tags:
            where.append("i.tags @> :tags")
            params["tags"] = tags

        rows = self._db.execute(
            text(f"""
                SELECT id, organization_id, identifier, platform, display_name,
                       state, metadata, tags, last_used_at, created_at, updated_at
                FROM identities i
                WHERE {" AND ".join(where)}
                ORDER BY i.platform, i.identifier
                LIMIT :limit
            """),
            params,
        ).fetchall()
        return [_row_to_identity(r) for r in rows]

    def get_identity(
        self,
        ctx: OrgContext,
        identity_id: uuid.UUID,
        *,
        include_platform: bool = True,
    ) -> IdentityRecord:
        """Fetch a single identity by ID.

        ``include_platform`` controls whether platform-global identities
        (``organization_id IS NULL``) are accessible.  Mirrors the same gate
        in ``list_identities()`` so the two are consistent.
        """
        row = self._db.execute(
            text("""
                SELECT id, organization_id, identifier, platform, display_name,
                       state, metadata, tags, last_used_at, created_at, updated_at
                FROM identities
                WHERE id = :id
                  AND (
                      organization_id = :org
                      OR (:incl AND organization_id IS NULL)
                  )
            """),
            {
                "id": str(identity_id),
                "org": str(ctx.organization_id),
                "incl": include_platform,
            },
        ).fetchone()
        if not row:
            raise IdentityNotFoundError(f"Identity {identity_id} not found")
        return _row_to_identity(row)

    def allocate(
        self,
        ctx: OrgContext,
        platform: str,
        *,
        tags: list[str] | None = None,
    ) -> IdentityRecord:
        """Return the best available active identity for a platform.

        Scoring (ascending = better):
          1. Exclude identities with an active non-expired lease (crash-safe)
          2. Staleness penalty: identities whose default credential hasn't been
             refreshed in >24h are ranked after fresh ones.
          3. LRU tiebreaker: within same freshness bucket, pick least-recently-used.
          4. Marks last_used_at on acquisition.

        Filter by tags for fine-grained selection (e.g. ['pro'], ['verified']).
        """
        params: dict[str, Any] = {"org": str(ctx.organization_id), "plat": platform}
        tag_clause = ""
        if tags:
            tag_clause = "AND i.tags @> :tags"
            params["tags"] = tags

        row = self._db.execute(
            text(f"""
                SELECT i.id, i.organization_id, i.identifier, i.platform, i.display_name,
                       i.state, i.metadata, i.tags, i.last_used_at, i.created_at, i.updated_at
                FROM identities i
                WHERE (i.organization_id = :org OR i.organization_id IS NULL)
                  AND i.platform = :plat AND i.state = 'active'
                  {tag_clause}
                  -- Exclude identities with an active lease (worker crash protection)
                  AND NOT EXISTS (
                      SELECT 1 FROM identity_leases lk
                      WHERE lk.identity_id = i.id
                        AND lk.state = 'active'
                        AND lk.expires_at > now()
                  )
                ORDER BY
                  -- Staleness bucket: 0=fresh (<24h), 1=stale (>=24h or never refreshed)
                  CASE WHEN EXISTS (
                      SELECT 1 FROM credentials c
                      WHERE c.identity_id = i.id
                        AND c.label = 'default'
                        AND c.last_refreshed_at > now() - interval '24 hours'
                  ) THEN 0 ELSE 1 END ASC,
                  -- LRU tiebreaker within each freshness bucket
                  i.last_used_at ASC NULLS FIRST
                LIMIT 1
                FOR UPDATE OF i SKIP LOCKED
            """),
            params,
        ).fetchone()
        if not row:
            raise IdentityNotFoundError(
                f"No active {platform!r} identity available"
                + (f" with tags={tags}" if tags else "")
            )
        self._db.execute(
            text("UPDATE identities SET last_used_at = now(), updated_at = now() WHERE id = :id"),
            {"id": str(row.id)},
        )
        rec = _row_to_identity(row)
        self._db.commit()
        return rec

    def update_identity(
        self,
        ctx: OrgContext,
        identity_id: uuid.UUID,
        *,
        state: str | None = None,
        display_name: str | None = None,
        metadata_patch: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> IdentityRecord:
        sets = ["updated_at = now()"]
        params: dict[str, Any] = {
            "id": str(identity_id), "org": str(ctx.organization_id)
        }
        if state is not None:
            sets.append("state = :state"); params["state"] = state
        if display_name is not None:
            sets.append("display_name = :dname"); params["dname"] = display_name
        if metadata_patch:
            sets.append("metadata = metadata || cast(:mp as jsonb)")
            params["mp"] = json.dumps(metadata_patch)
        if tags is not None:
            sets.append("tags = :tags"); params["tags"] = tags

        result = self._db.execute(
            text(f"""
                UPDATE identities SET {", ".join(sets)}
                WHERE id = :id AND organization_id = :org
                RETURNING id, organization_id, identifier, platform, display_name,
                          state, metadata, tags, last_used_at, created_at, updated_at
            """),
            params,
        ).fetchone()
        if not result:
            raise IdentityNotFoundError(f"Identity {identity_id} not found")
        rec = _row_to_identity(result)
        self._db.commit()
        return rec

    def delete_identity(self, ctx: OrgContext, identity_id: uuid.UUID) -> None:
        result = self._db.execute(
            text("DELETE FROM identities WHERE id = :id AND organization_id = :org RETURNING id"),
            {"id": str(identity_id), "org": str(ctx.organization_id)},
        ).fetchone()
        if not result:
            raise IdentityNotFoundError(f"Identity {identity_id} not found")
        self._db.commit()

    # ── Credentials ───────────────────────────────────────────────────────────

    def put_credential(
        self,
        ctx: OrgContext,
        identity_id: uuid.UUID,
        credential_type: str,
        *,
        label: str = "default",
        secret_value: str | None = None,
        vault_key: str | None = None,
        storage_object_key: str | None = None,
        health_score: float = 1.0,
        expires_at: datetime | None = None,
    ) -> CredentialRecord:
        """Store or rotate a credential for an identity.

        credential_type is free-form — any string is accepted:
          'password', 'api_key', 'oauth_token', 'cookie', 'totp_secret',
          'ssh_key', 'client_cert', 'session_token', 'refresh_token', …

        If secret_value is provided, it is encrypted into vaulted_secrets
        automatically. Pass vault_key if already stored via the Vault API.
        """
        # Auto-vault the secret via VaultService (single source of truth for crypto +
        # ON CONFLICT rotation logic).  Avoids duplicating VaultCrypto instantiation
        # and raw vaulted_secrets SQL outside the vault subsystem.
        if secret_value is not None and vault_key is None:
            from xiosync.subsystems.vault.service import VaultService  # noqa: PLC0415
            _lbl_suffix = f"/{label}" if label != "default" else ""
            vault_key = f"identities/{identity_id}/{credential_type}{_lbl_suffix}"
            secret_type = _CRED_TO_SECRET_TYPE.get(credential_type, "generic")
            VaultService(self._db).put_secret(
                ctx,
                vault_key,
                secret_value,
                secret_type=secret_type,
                description=f"Auto-vaulted {credential_type} for identity {identity_id}",
            )

        row = self._db.execute(
            text("""
                INSERT INTO credentials
                  (id, organization_id, identity_id, credential_type, label,
                   vault_key, storage_object_key, health_score,
                   last_refreshed_at, expires_at, created_at, updated_at)
                VALUES
                  (gen_random_uuid(), :org, :iid, :ctype, :label,
                   :vkey, :sokey, :health, now(), :exp, now(), now())
                ON CONFLICT (identity_id, credential_type, label) DO UPDATE SET
                    vault_key          = COALESCE(EXCLUDED.vault_key, credentials.vault_key),
                    storage_object_key = COALESCE(EXCLUDED.storage_object_key, credentials.storage_object_key),
                    health_score       = EXCLUDED.health_score,
                    last_refreshed_at  = now(),
                    expires_at         = EXCLUDED.expires_at,
                    updated_at         = now()
                RETURNING id, organization_id, identity_id, credential_type, label,
                          vault_key, storage_object_key, health_score,
                          last_refreshed_at, expires_at, created_at, updated_at
            """),
            {
                "org": str(ctx.organization_id),
                "iid": str(identity_id), "ctype": credential_type, "label": label,
                "vkey": vault_key, "sokey": storage_object_key,
                "health": health_score, "exp": expires_at,
            },
        ).fetchone()
        self._db.commit()
        return _row_to_credential(row)

    def get_credential(
        self,
        ctx: OrgContext,
        identity_id: uuid.UUID,
        credential_type: str,
        *,
        decrypt: bool = False,
    ) -> CredentialRecord | tuple[CredentialRecord, str]:
        """Get credential metadata. If decrypt=True, also returns the plaintext value."""
        row = self._db.execute(
            text("""
                SELECT c.id, c.organization_id, c.identity_id, c.credential_type, c.label,
                       c.vault_key, c.storage_object_key, c.health_score,
                       c.last_refreshed_at, c.expires_at, c.created_at, c.updated_at
                FROM credentials c
                JOIN identities i ON i.id = c.identity_id
                WHERE c.identity_id = :iid AND c.credential_type = :ctype
                  AND (i.organization_id = :org OR i.organization_id IS NULL)
            """),
            {"iid": str(identity_id), "ctype": credential_type,
             "org": str(ctx.organization_id)},
        ).fetchone()
        if not row:
            raise IdentityNotFoundError(
                f"No {credential_type!r} credential for identity {identity_id}"
            )
        rec = _row_to_credential(row)
        if decrypt and rec.vault_key:
            from xiosync.subsystems.vault.service import VaultService
            value = VaultService(self._db).get_secret(ctx, rec.vault_key)
            return rec, value
        return rec

    def list_credentials(
        self, ctx: OrgContext, identity_id: uuid.UUID
    ) -> list[CredentialRecord]:
        rows = self._db.execute(
            text("""
                SELECT c.id, c.organization_id, c.identity_id, c.credential_type, c.label,
                       c.vault_key, c.storage_object_key, c.health_score,
                       c.last_refreshed_at, c.expires_at, c.created_at, c.updated_at
                FROM credentials c
                JOIN identities i ON i.id = c.identity_id
                WHERE c.identity_id = :iid
                  AND (i.organization_id = :org OR i.organization_id IS NULL)
                ORDER BY c.credential_type, c.label
            """),
            {"iid": str(identity_id), "org": str(ctx.organization_id)},
        ).fetchall()
        return [_row_to_credential(r) for r in rows]

    def update_health(
        self,
        ctx: OrgContext,
        identity_id: uuid.UUID,
        credential_type: str,
        health_score: float,
    ) -> None:
        """Workers call this after validating a credential."""
        self._db.execute(
            text("""
                UPDATE credentials
                SET health_score = :score, last_refreshed_at = now(), updated_at = now()
                WHERE identity_id = :iid AND credential_type = :ctype
            """),
            {"iid": str(identity_id), "ctype": credential_type, "score": health_score},
        )
        self._db.commit()

    # ── Leases ────────────────────────────────────────────────────────────────

    def acquire_lease(
        self,
        ctx: OrgContext,
        identity_id: uuid.UUID,
        *,
        duration_seconds: int = 1800,
        worker_id: uuid.UUID | None = None,
        run_context: dict | None = None,
    ) -> dict:
        """Acquire an exclusive time-bounded lease on an identity.

        Raises IdentityNotFoundError if an active non-expired lease already exists.
        """
        existing = self._db.execute(
            text("""
                SELECT id FROM identity_leases
                WHERE identity_id = :iid
                  AND state = 'active' AND expires_at > now()
                LIMIT 1
            """),
            {"iid": str(identity_id)},
        ).fetchone()
        if existing:
            raise IdentityNotFoundError(
                f"Identity {identity_id} is already leased ({existing.id}). "
                "Release it or wait for the TTL to expire."
            )
        row = self._db.execute(
            text("""
                INSERT INTO identity_leases
                  (identity_id, worker_id, organization_id,
                   acquired_at, expires_at, run_context, state)
                VALUES
                  (:iid, :wid, :org,
                   now(), now() + :dur * interval '1 second',
                   cast(:ctx as jsonb), 'active')
                RETURNING id, acquired_at, expires_at, state
            """),
            {
                "iid": str(identity_id),
                "wid": str(worker_id) if worker_id else None,
                "org": str(ctx.organization_id),
                "dur": duration_seconds,
                "ctx": json.dumps(run_context or {}),
            },
        ).fetchone()
        self._db.commit()
        return {
            "lease_id": str(row.id),
            "identity_id": str(identity_id),
            "acquired_at": row.acquired_at.isoformat(),
            "expires_at": row.expires_at.isoformat(),
            "state": row.state,
        }

    def release_lease(self, ctx: OrgContext, lease_id: uuid.UUID) -> None:
        """Explicitly release a lease. Idempotent."""
        self._db.execute(
            text("""
                UPDATE identity_leases
                SET state='released', released_at=now()
                WHERE id = :lid
                  AND organization_id = :org
                  AND state = 'active'
            """),
            {"lid": str(lease_id), "org": str(ctx.organization_id)},
        )
        self._db.commit()

    def expire_stale_leases(self, ctx: OrgContext) -> int:
        """Mark all past-TTL active leases as expired. Returns row count."""
        result = self._db.execute(
            text("""
                UPDATE identity_leases
                SET state='expired'
                WHERE state = 'active'
                  AND expires_at < now()
                  AND organization_id = :org
            """),
            {"org": str(ctx.organization_id)},
        )
        self._db.commit()
        return result.rowcount

    def list_leases(
        self,
        ctx: OrgContext,
        identity_id: uuid.UUID | None = None,
        worker_id: uuid.UUID | None = None,
        active_only: bool = True,
    ) -> list[dict]:
        """List leases with optional filters."""
        filters = ["organization_id = :org"]
        params: dict = {"org": str(ctx.organization_id)}
        if active_only:
            filters.append("state = 'active' AND expires_at > now()")
        if identity_id:
            filters.append("identity_id = :iid")
            params["iid"] = str(identity_id)
        if worker_id:
            filters.append("worker_id = :wid")
            params["wid"] = str(worker_id)
        rows = self._db.execute(
            text(f"SELECT id, identity_id, worker_id, acquired_at, expires_at, "
                 f"released_at, run_context, state FROM identity_leases "
                 f"WHERE {' AND '.join(filters)} ORDER BY acquired_at DESC"),
            params,
        ).fetchall()
        return [
            {
                "lease_id": str(r.id),
                "identity_id": str(r.identity_id),
                "worker_id": str(r.worker_id) if r.worker_id else None,
                "acquired_at": r.acquired_at.isoformat(),
                "expires_at": r.expires_at.isoformat(),
                "released_at": r.released_at.isoformat() if r.released_at else None,
                "run_context": r.run_context if isinstance(r.run_context, dict) else json.loads(r.run_context or "{}"),
                "state": r.state,
            }
            for r in rows
        ]


# ── Mapping helpers ───────────────────────────────────────────────────────────

# Maps credential_type strings to vault secret_type values.
# This is a soft hint — any credential_type NOT listed here defaults to 'generic'.
_CRED_TO_SECRET_TYPE: dict[str, str] = {
    "password":      "credential",
    "oauth_token":   "oauth",
    "refresh_token": "oauth",
    "api_key":       "api_key",
    "ssh_key":       "ssh_key",
    "totp_secret":   "totp",
    "cookie":        "credential",
    "session_token": "credential",
    "client_cert":   "ssh_key",
}


def _row_to_identity(row: Any) -> IdentityRecord:
    meta = row.metadata if isinstance(row.metadata, dict) else json.loads(row.metadata or "{}")
    tags = list(row.tags) if row.tags else []
    org_id = uuid.UUID(str(row.organization_id)) if row.organization_id else None
    return IdentityRecord(
        id=uuid.UUID(str(row.id)),
        organization_id=org_id,
        identifier=row.identifier,
        platform=row.platform,
        display_name=row.display_name,
        state=row.state,
        metadata=meta,
        tags=tags,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        is_platform_global=(org_id is None),
    )


def _row_to_credential(row: Any) -> CredentialRecord:
    return CredentialRecord(
        id=uuid.UUID(str(row.id)),
        organization_id=uuid.UUID(str(row.organization_id)) if row.organization_id else None,
        identity_id=uuid.UUID(str(row.identity_id)),
        credential_type=row.credential_type,
        label=getattr(row, "label", "default"),
        vault_key=row.vault_key,
        storage_object_key=row.storage_object_key,
        health_score=float(row.health_score),
        last_refreshed_at=row.last_refreshed_at,
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
