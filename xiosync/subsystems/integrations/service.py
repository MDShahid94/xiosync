"""Integrations Service — universal external system connector registry.

Registers, tests, and manages connections to any external data system.
provider_type is free-form — the platform enforces no closed list of
connector types. New provider types (Redis, ClickHouse, Kafka, REST, gRPC,
Airtable, Notion, custom…) require no schema migration.

Connection testing is implemented per provider_type in _test_*() methods.
Adding support for a new provider type requires only adding a new _test_*
method — no DB schema changes.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext

__all__ = ["IntegrationsService", "IntegrationRecord", "IntegrationNotFoundError"]


class IntegrationNotFoundError(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class IntegrationRecord:
    id: uuid.UUID
    organization_id: uuid.UUID | None
    name: str
    provider_type: str
    connection_config: dict[str, Any]
    vault_key: str | None
    last_synced_at: datetime | None
    last_sync_status: str | None
    sync_stats: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class IntegrationsService:

    def __init__(self, session: Session) -> None:
        self._db = session

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def register(
        self,
        ctx: OrgContext,
        name: str,
        provider_type: str,
        connection_config: dict[str, Any],
        *,
        vault_key: str | None = None,
    ) -> IntegrationRecord:
        """Register or update an integration provider.

        provider_type is free-form — any string is accepted (cloudflare_d1,
        supabase, postgres, redis, kafka, rest_api, custom, …).
        """
        row = self._db.execute(
            text("""
                INSERT INTO integration_providers
                  (id, organization_id, name, provider_type,
                   connection_config, vault_key, created_at, updated_at)
                VALUES
                  (gen_random_uuid(), :org, :name, :ptype,
                   cast(:cfg as jsonb), :vkey, now(), now())
                ON CONFLICT (organization_id, name) DO UPDATE SET
                    provider_type     = EXCLUDED.provider_type,
                    connection_config = EXCLUDED.connection_config,
                    vault_key         = EXCLUDED.vault_key,
                    updated_at        = now()
                RETURNING id, organization_id, name, provider_type, connection_config,
                          vault_key, last_synced_at, last_sync_status, sync_stats,
                          created_at, updated_at
            """),
            {"org": str(ctx.organization_id), "name": name, "ptype": provider_type,
             "cfg": json.dumps(connection_config), "vkey": vault_key},
        ).fetchone()
        self._db.commit()
        return _row_to_rec(row)

    def list_providers(self, ctx: OrgContext) -> list[IntegrationRecord]:
        rows = self._db.execute(
            text("""
                SELECT id, organization_id, name, provider_type, connection_config,
                       vault_key, last_synced_at, last_sync_status, sync_stats,
                       created_at, updated_at
                FROM integration_providers
                WHERE organization_id = :org
                ORDER BY name
            """),
            {"org": str(ctx.organization_id)},
        ).fetchall()
        return [_row_to_rec(r) for r in rows]

    def get_provider(self, ctx: OrgContext, provider_id: uuid.UUID) -> IntegrationRecord:
        row = self._db.execute(
            text("""
                SELECT id, organization_id, name, provider_type, connection_config,
                       vault_key, last_synced_at, last_sync_status, sync_stats,
                       created_at, updated_at
                FROM integration_providers
                WHERE id = :id AND organization_id = :org
            """),
            {"id": str(provider_id), "org": str(ctx.organization_id)},
        ).fetchone()
        if not row:
            raise IntegrationNotFoundError(f"Integration {provider_id} not found")
        return _row_to_rec(row)

    def delete_provider(self, ctx: OrgContext, provider_id: uuid.UUID) -> None:
        result = self._db.execute(
            text("DELETE FROM integration_providers WHERE id=:id AND organization_id=:org RETURNING id"),
            {"id": str(provider_id), "org": str(ctx.organization_id)},
        ).fetchone()
        if not result:
            raise IntegrationNotFoundError(f"Integration {provider_id} not found")
        self._db.commit()

    # ── Test Connection ────────────────────────────────────────────────────────

    def test_connection(self, ctx: OrgContext, provider_id: uuid.UUID) -> dict[str, Any]:
        """Attempt a lightweight connection test. Returns {ok, message, latency_ms}."""
        provider = self.get_provider(ctx, provider_id)
        credential = self._resolve_credential(ctx, provider)

        if provider.provider_type == "cloudflare_d1":
            return self._test_d1(provider.connection_config, credential)
        elif provider.provider_type in ("postgres", "supabase"):
            return self._test_postgres(provider.connection_config, credential)
        elif provider.provider_type in ("redis", "redis_cluster"):
            return self._test_redis(provider.connection_config, credential)
        elif provider.provider_type in ("mysql", "mariadb"):
            return self._test_mysql(provider.connection_config, credential)
        elif provider.provider_type == "mongodb":
            return self._test_mongodb(provider.connection_config, credential)
        elif provider.provider_type == "github":
            return self._test_github(provider.connection_config, credential)
        elif provider.provider_type == "slack":
            return self._test_slack(provider.connection_config, credential)
        elif provider.provider_type == "google_sheets":
            return self._test_google_sheets(provider.connection_config, credential)
        elif provider.provider_type in ("http_webhook", "generic_http"):
            return self._test_http(provider.connection_config, credential)
        else:
            return {"ok": False, "message": f"Connection test not implemented for {provider.provider_type}"}

    def _test_d1(self, config: dict, credential: str | None) -> dict[str, Any]:
        import time
        import urllib.request
        account_id = config.get("account_id", "")
        database_id = config.get("database_id", "")
        if not account_id or not database_id:
            return {"ok": False, "message": "Missing account_id or database_id in config"}
        url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {credential or ''}"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                ms = int((time.time() - t0) * 1000)
                if data.get("success"):
                    return {"ok": True, "message": "D1 connection successful", "latency_ms": ms}
                return {"ok": False, "message": str(data.get("errors", "unknown"))}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def _test_postgres(self, config: dict, credential: str | None) -> dict[str, Any]:
        import time
        try:
            from sqlalchemy import create_engine as ce
            host = config.get("host", "localhost")
            port = config.get("port", 5432)
            db   = config.get("database", "postgres")
            user = config.get("user", "postgres")
            url  = f"postgresql+psycopg://{user}:{credential or ''}@{host}:{port}/{db}"
            eng  = ce(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
            t0   = time.time()
            with eng.connect() as c:
                c.execute(text("SELECT 1"))
            ms = int((time.time() - t0) * 1000)
            return {"ok": True, "message": "Postgres connection successful", "latency_ms": ms}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
    def _test_redis(self, config: dict, credential: str | None) -> dict[str, Any]:
        import time
        import socket
        host = config.get("host", "localhost")
        port = config.get("port", 6379)
        t0 = time.time()
        try:
            with socket.create_connection((host, port), timeout=10) as s:
                s.sendall(b"\r\nPING\r\n")
                resp = s.recv(1024)
                ms = int((time.time() - t0) * 1000)
                if b"+PONG" in resp:
                    return {"ok": True, "message": "Redis connection successful", "latency_ms": ms}
                return {"ok": False, "message": f"Unexpected response: {resp!r}"}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def _test_mysql(self, config: dict, credential: str | None) -> dict[str, Any]:
        import time
        try:
            from sqlalchemy import create_engine, text
            host = config.get("host")
            port = config.get("port", 3306)
            db = config.get("database")
            user = config.get("user")
            if not host or not db or not user:
                return {"ok": False, "message": "Missing required config keys (host, database, user)"}
            url = f"mysql+pymysql://{user}:{credential or ''}@{host}:{port}/{db}"
            eng = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
            t0 = time.time()
            with eng.connect() as c:
                c.execute(text("SELECT 1"))
            ms = int((time.time() - t0) * 1000)
            return {"ok": True, "message": "MySQL connection successful", "latency_ms": ms}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def _test_mongodb(self, config: dict, credential: str | None) -> dict[str, Any]:
        import time
        try:
            import pymongo
            uri = config.get("uri")
            if not uri:
                host = config.get("host")
                port = config.get("port", 27017)
                if not host:
                    return {"ok": False, "message": "Missing uri or host in config"}
                auth = f"{config.get('user', '')}:{credential}@" if credential else ""
                uri = f"mongodb://{auth}{host}:{port}"
            t0 = time.time()
            client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=3000)
            client.server_info()
            ms = int((time.time() - t0) * 1000)
            return {"ok": True, "message": "MongoDB connection successful", "latency_ms": ms}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def _test_github(self, config: dict, credential: str | None) -> dict[str, Any]:
        import time
        import urllib.request
        import urllib.error
        t0 = time.time()
        try:
            req = urllib.request.Request("https://api.github.com/user", headers={"Authorization": f"Bearer {credential or ''}"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                ms = int((time.time() - t0) * 1000)
                if resp.status == 200:
                    return {"ok": True, "message": "GitHub connection successful", "latency_ms": ms}
                return {"ok": False, "message": f"Unexpected status code {resp.status}"}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "message": f"HTTPError: {exc.code} {exc.reason}"}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def _test_slack(self, config: dict, credential: str | None) -> dict[str, Any]:
        import time
        import urllib.request
        import json
        t0 = time.time()
        try:
            req = urllib.request.Request("https://slack.com/api/auth.test", method="POST", headers={"Authorization": f"Bearer {credential or ''}"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                ms = int((time.time() - t0) * 1000)
                if data.get("ok"):
                    return {"ok": True, "message": "Slack connection successful", "latency_ms": ms}
                return {"ok": False, "message": str(data.get("error", "unknown error"))}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def _test_google_sheets(self, config: dict, credential: str | None) -> dict[str, Any]:
        import time
        import urllib.request
        import urllib.error
        spreadsheet_id = config.get("spreadsheet_id")
        if not spreadsheet_id:
            return {"ok": False, "message": "Missing spreadsheet_id in config"}
        t0 = time.time()
        try:
            url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}?fields=spreadsheetId"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {credential or ''}"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                ms = int((time.time() - t0) * 1000)
                if resp.status == 200:
                    return {"ok": True, "message": "Google Sheets connection successful", "latency_ms": ms}
                return {"ok": False, "message": f"Unexpected status code {resp.status}"}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "message": f"HTTPError: {exc.code} {exc.reason}"}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def _test_http(self, config: dict, credential: str | None) -> dict[str, Any]:
        import time
        import urllib.request
        import urllib.error
        endpoint_url = config.get("endpoint_url")
        if not endpoint_url:
            return {"ok": False, "message": "Missing endpoint_url in config"}
        t0 = time.time()
        try:
            req = urllib.request.Request(endpoint_url, method="HEAD")
            if credential:
                req.add_header("Authorization", f"Bearer {credential}")
            with urllib.request.urlopen(req, timeout=5) as resp:
                ms = int((time.time() - t0) * 1000)
                if resp.status < 400:
                    return {"ok": True, "message": "HTTP connection successful", "latency_ms": ms}
                return {"ok": False, "message": f"HTTP status {resp.status}"}
        except urllib.error.HTTPError as exc:
            if exc.code < 400:
                ms = int((time.time() - t0) * 1000)
                return {"ok": True, "message": "HTTP connection successful", "latency_ms": ms}
            return {"ok": False, "message": f"HTTPError: {exc.code} {exc.reason}"}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}


    # ── Private ───────────────────────────────────────────────────────────────

    def _resolve_credential(self, ctx: OrgContext, provider: IntegrationRecord) -> str | None:
        if not provider.vault_key:
            return None
        try:
            from xiosync.subsystems.vault.service import VaultService
            return VaultService(self._db).get_secret(ctx, provider.vault_key)
        except Exception:
            return None

def _row_to_rec(row: Any) -> IntegrationRecord:
    cfg  = row.connection_config if isinstance(row.connection_config, dict) else json.loads(row.connection_config or "{}")
    stats = row.sync_stats if isinstance(row.sync_stats, dict) else json.loads(row.sync_stats or "{}")
    return IntegrationRecord(
        id=uuid.UUID(str(row.id)),
        organization_id=uuid.UUID(str(row.organization_id)) if row.organization_id else None,
        name=row.name,
        provider_type=row.provider_type,
        connection_config=cfg,
        vault_key=row.vault_key,
        last_synced_at=row.last_synced_at,
        last_sync_status=row.last_sync_status,
        sync_stats=stats,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
