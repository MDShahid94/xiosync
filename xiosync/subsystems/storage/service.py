"""Storage Service — CRUD for storage_providers + storage_objects.

Sharing model:
  - provider org_id = None   → platform-global shared provider
  - provider org_id = <uuid> → org-private provider

Lookup precedence (when resolving 'default' provider for an operation):
  1. Org-private default provider (is_default=true, lowest priority number)
  2. Platform-global default provider
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.subsystems.storage.adapters.base import AccessInfo, make_adapter

__all__ = [
    "StorageService", "ProviderRecord", "ObjectRecord",
    "StorageNotFoundError", "StorageProviderError",
]

_ALLOWED_PROVIDER_TYPES = frozenset({
    "google_drive", "cloudflare_r2", "s3", "supabase_storage", "local"
})
_ALLOWED_OBJECT_TYPES = frozenset({
    "chrome_profile", "ts_state", "session_export", "workflow_artifact", "generic"
})


class StorageNotFoundError(KeyError):
    pass

class StorageProviderError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    id: uuid.UUID
    organization_id: uuid.UUID | None
    name: str
    provider_type: str
    config: dict[str, Any]
    vault_key: str | None
    is_default: bool
    is_writable: bool
    priority: int
    is_platform_global: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ObjectRecord:
    id: uuid.UUID
    organization_id: uuid.UUID | None
    provider_id: uuid.UUID
    object_key: str
    object_type: str
    size_bytes: int | None
    checksum_sha256: str | None
    content_type: str | None
    metadata: dict[str, Any]
    last_accessed_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class StorageService:

    def __init__(self, session: Session) -> None:
        self._db = session

    # ── Providers ─────────────────────────────────────────────────────────────

    def register_provider(
        self,
        ctx: OrgContext,
        name: str,
        provider_type: str,
        config: dict[str, Any],
        *,
        vault_key: str | None = None,
        is_default: bool = False,
        is_writable: bool = True,
        priority: int = 100,
        platform_global: bool = False,
    ) -> ProviderRecord:
        """Register or update a storage provider.

        Args:
            vault_key: Key in vaulted_secrets holding provider credentials.
            platform_global: If True, registered as org_id=NULL (shared across all orgs).
        """
        if provider_type not in _ALLOWED_PROVIDER_TYPES:
            raise StorageProviderError(
                f"provider_type must be one of {sorted(_ALLOWED_PROVIDER_TYPES)}"
            )

        org_id = None if platform_global else ctx.organization_id

        # Validate via adapter (dry run — no I/O)
        adapter = make_adapter(provider_type, config, credential=None)
        adapter.validate_config()

        # If setting as default, unset current default for this org
        if is_default:
            self._db.execute(
                text("""
                    UPDATE storage_providers
                    SET is_default = false, updated_at = now()
                    WHERE organization_id IS NOT DISTINCT FROM :org
                      AND is_default = true
                """),
                {"org": str(org_id) if org_id else None},
            )

        self._db.execute(
            text("""
                INSERT INTO storage_providers
                  (id, organization_id, name, provider_type, config, vault_key,
                   is_default, is_writable, priority, created_at, updated_at)
                VALUES
                  (gen_random_uuid(), :org, :name, :ptype, cast(:cfg as jsonb),
                   :vkey, :isd, :isw, :pri, now(), now())
                ON CONFLICT (organization_id, name) DO UPDATE SET
                    provider_type = EXCLUDED.provider_type,
                    config        = EXCLUDED.config,
                    vault_key     = EXCLUDED.vault_key,
                    is_default    = EXCLUDED.is_default,
                    is_writable   = EXCLUDED.is_writable,
                    priority      = EXCLUDED.priority,
                    updated_at    = now()
            """),
            {
                "org": str(org_id) if org_id else None,
                "name": name,
                "ptype": provider_type,
                "cfg": __import__("json").dumps(config),
                "vkey": vault_key,
                "isd": is_default,
                "isw": is_writable,
                "pri": priority,
            },
        )
        rec = self._get_provider_by_name(ctx, name, platform_global=platform_global)
        self._db.commit()
        return rec

    def list_providers(
        self,
        ctx: OrgContext,
        *,
        include_platform: bool = True,
    ) -> list[ProviderRecord]:
        rows = self._db.execute(
            text("""
                SELECT id, organization_id, name, provider_type, config, vault_key,
                       is_default, is_writable, priority, created_at, updated_at
                FROM storage_providers
                WHERE (organization_id = :org
                       OR (:incl AND organization_id IS NULL))
                ORDER BY priority ASC, name ASC
            """),
            {"org": str(ctx.organization_id), "incl": include_platform},
        ).fetchall()
        return [_row_to_provider(r) for r in rows]

    def get_provider(self, ctx: OrgContext, provider_id: uuid.UUID) -> ProviderRecord:
        row = self._db.execute(
            text("""
                SELECT id, organization_id, name, provider_type, config, vault_key,
                       is_default, is_writable, priority, created_at, updated_at
                FROM storage_providers
                WHERE id = :id
                  AND (organization_id = :org OR organization_id IS NULL)
            """),
            {"id": str(provider_id), "org": str(ctx.organization_id)},
        ).fetchone()
        if not row:
            raise StorageNotFoundError(f"Provider {provider_id} not found")
        return _row_to_provider(row)

    def delete_provider(self, ctx: OrgContext, provider_id: uuid.UUID) -> None:
        result = self._db.execute(
            text("""
                DELETE FROM storage_providers
                WHERE id = :id AND organization_id = :org
                RETURNING id
            """),
            {"id": str(provider_id), "org": str(ctx.organization_id)},
        ).fetchone()
        if not result:
            raise StorageNotFoundError(f"Provider {provider_id} not found")
        self._db.commit()

    # ── Access info (adapter dispatch) ────────────────────────────────────────

    def get_access_info(
        self,
        ctx: OrgContext,
        provider_id: uuid.UUID,
        object_key: str,
        operation: str,
    ) -> AccessInfo:
        """Return provider access instructions for a worker to access a blob."""
        provider = self.get_provider(ctx, provider_id)
        credential = self._resolve_credential(ctx, provider)
        adapter = make_adapter(provider.provider_type, provider.config, credential)
        return adapter.get_access_info(object_key, operation)

    def _resolve_credential(
        self, ctx: OrgContext, provider: ProviderRecord
    ) -> str | None:
        if not provider.vault_key:
            return None
        try:
            from xiosync.subsystems.vault.service import VaultService
            vault = VaultService(self._db)
            return vault.get_secret(ctx, provider.vault_key)
        except Exception:
            return None

    # ── Objects ───────────────────────────────────────────────────────────────

    def register_object(
        self,
        ctx: OrgContext,
        provider_id: uuid.UUID,
        object_key: str,
        *,
        object_type: str = "generic",
        size_bytes: int | None = None,
        checksum_sha256: str | None = None,
        content_type: str | None = None,
        metadata: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> ObjectRecord:
        """Register a blob that a worker has uploaded to a provider.

        Workers call this AFTER uploading to Drive/R2/S3 to index the object.
        """
        if object_type not in _ALLOWED_OBJECT_TYPES:
            raise StorageProviderError(
                f"object_type must be one of {sorted(_ALLOWED_OBJECT_TYPES)}"
            )
        import json
        self._db.execute(
            text("""
                INSERT INTO storage_objects
                  (id, organization_id, provider_id, object_key, object_type,
                   size_bytes, checksum_sha256, content_type, metadata, expires_at,
                   created_at, updated_at)
                VALUES
                  (gen_random_uuid(), :org, :pid, :key, :otype,
                   :size, :chk, :ctype, cast(:meta as jsonb), :exp,
                   now(), now())
                ON CONFLICT (provider_id, object_key) DO UPDATE SET
                    object_type    = EXCLUDED.object_type,
                    size_bytes     = COALESCE(EXCLUDED.size_bytes, storage_objects.size_bytes),
                    checksum_sha256 = COALESCE(EXCLUDED.checksum_sha256, storage_objects.checksum_sha256),
                    content_type   = COALESCE(EXCLUDED.content_type, storage_objects.content_type),
                    metadata       = storage_objects.metadata || EXCLUDED.metadata,
                    expires_at     = EXCLUDED.expires_at,
                    updated_at     = now()
            """),
            {
                "org": str(ctx.organization_id),
                "pid": str(provider_id),
                "key": object_key,
                "otype": object_type,
                "size": size_bytes,
                "chk": checksum_sha256,
                "ctype": content_type,
                "meta": json.dumps(metadata or {}),
                "exp": expires_at,
            },
        )
        rec = self._get_object(ctx, provider_id, object_key)
        self._db.commit()
        return rec

    def list_objects(
        self,
        ctx: OrgContext,
        *,
        provider_id: uuid.UUID | None = None,
        object_type: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
        limit: int = 50,
    ) -> list[ObjectRecord]:
        where = ["o.organization_id = :org"]
        params: dict[str, Any] = {"org": str(ctx.organization_id), "limit": limit}
        if provider_id:
            where.append("o.provider_id = :pid")
            params["pid"] = str(provider_id)
        if object_type:
            where.append("o.object_type = :otype")
            params["otype"] = object_type
        if metadata_filter:
            import json
            where.append("o.metadata @> cast(:mf as jsonb)")
            params["mf"] = json.dumps(metadata_filter)

        rows = self._db.execute(
            text(f"""
                SELECT o.id, o.organization_id, o.provider_id, o.object_key,
                       o.object_type, o.size_bytes, o.checksum_sha256, o.content_type,
                       o.metadata, o.last_accessed_at, o.expires_at, o.created_at, o.updated_at
                FROM storage_objects o
                WHERE {" AND ".join(where)}
                ORDER BY o.created_at DESC
                LIMIT :limit
            """),
            params,
        ).fetchall()
        return [_row_to_object(r) for r in rows]

    def deindex_object(
        self,
        ctx: OrgContext,
        provider_id: uuid.UUID,
        object_key: str,
    ) -> None:
        """Remove object from the index (does NOT delete from provider)."""
        result = self._db.execute(
            text("""
                DELETE FROM storage_objects
                WHERE provider_id = :pid AND object_key = :key
                  AND organization_id = :org
                RETURNING id
            """),
            {"pid": str(provider_id), "key": object_key, "org": str(ctx.organization_id)},
        ).fetchone()
        if not result:
            raise StorageNotFoundError(f"Object {object_key!r} not found")
        self._db.commit()

    # ── Private helpers ───────────────────────────────────────────────────────

    def _get_provider_by_name(
        self, ctx: OrgContext, name: str, *, platform_global: bool
    ) -> ProviderRecord:
        org_clause = "organization_id IS NULL" if platform_global \
                     else "organization_id = :org"
        row = self._db.execute(
            text(f"""
                SELECT id, organization_id, name, provider_type, config, vault_key,
                       is_default, is_writable, priority, created_at, updated_at
                FROM storage_providers WHERE {org_clause} AND name = :name
            """),
            {"org": str(ctx.organization_id), "name": name},
        ).fetchone()
        if not row:
            raise StorageNotFoundError(f"Provider {name!r} not found")
        return _row_to_provider(row)

    def _get_object(
        self, ctx: OrgContext, provider_id: uuid.UUID, object_key: str
    ) -> ObjectRecord:
        row = self._db.execute(
            text("""
                SELECT id, organization_id, provider_id, object_key, object_type,
                       size_bytes, checksum_sha256, content_type, metadata,
                       last_accessed_at, expires_at, created_at, updated_at
                FROM storage_objects
                WHERE provider_id = :pid AND object_key = :key
            """),
            {"pid": str(provider_id), "key": object_key},
        ).fetchone()
        if not row:
            raise StorageNotFoundError(f"Object {object_key!r} not found")
        return _row_to_object(row)


# ── Row → dataclass helpers ───────────────────────────────────────────────────

def _row_to_provider(row: Any) -> ProviderRecord:
    import json
    config = row.config if isinstance(row.config, dict) else json.loads(row.config or "{}")
    org_id = uuid.UUID(str(row.organization_id)) if row.organization_id else None
    return ProviderRecord(
        id=uuid.UUID(str(row.id)),
        organization_id=org_id,
        name=row.name,
        provider_type=row.provider_type,
        config=config,
        vault_key=row.vault_key,
        is_default=row.is_default,
        is_writable=row.is_writable,
        priority=row.priority,
        is_platform_global=(org_id is None),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_object(row: Any) -> ObjectRecord:
    import json
    meta = row.metadata if isinstance(row.metadata, dict) else json.loads(row.metadata or "{}")
    return ObjectRecord(
        id=uuid.UUID(str(row.id)),
        organization_id=uuid.UUID(str(row.organization_id)) if row.organization_id else None,
        provider_id=uuid.UUID(str(row.provider_id)),
        object_key=row.object_key,
        object_type=row.object_type,
        size_bytes=row.size_bytes,
        checksum_sha256=row.checksum_sha256,
        content_type=row.content_type,
        metadata=meta,
        last_accessed_at=row.last_accessed_at,
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
