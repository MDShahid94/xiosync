"""Unit tests for xiosync.subsystems.xiorun.pg_advisory_lock and worker_locks router."""
from __future__ import annotations

import hashlib
import struct

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from xiosync.api.routers.worker_locks import router as worker_locks_router
from xiosync.platform.engine_ref import set_engine
from xiosync.subsystems.xiorun.pg_advisory_lock import (
    _ACTIVE_LOCK_METADATA,
    _ACTIVE_LOCK_SESSIONS,
    _LOCKS_MUTEX,
    _resource_key_to_bigint,
    advisory_lock_scope,
    get_lock_holder,
    get_lock_info,
    is_locked,
    release_lock,
    try_acquire_lock,
)


@pytest.fixture(autouse=True)
def cleanup_active_locks():
    """Ensure in-memory lock tracking is clean before and after each test."""
    with _LOCKS_MUTEX:
        for sess in list(_ACTIVE_LOCK_SESSIONS.values()):
            try:
                sess.close()
            except Exception as exc:
                pytest.fail(f"Failed to close session during test setup: {exc}")
        _ACTIVE_LOCK_SESSIONS.clear()
        _ACTIVE_LOCK_METADATA.clear()
    yield
    with _LOCKS_MUTEX:
        for sess in list(_ACTIVE_LOCK_SESSIONS.values()):
            try:
                sess.close()
            except Exception as exc:
                pytest.fail(f"Failed to close session during test teardown: {exc}")
        _ACTIVE_LOCK_SESSIONS.clear()
        _ACTIVE_LOCK_METADATA.clear()


@pytest.fixture
def real_pg_engine():
    """Connect to local Postgres if available; otherwise skip test."""
    try:
        engine = create_engine("postgresql+psycopg://postgres:@localhost/postgres")
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return engine
    except Exception as exc:
        pytest.skip(f"Local PostgreSQL not available: {exc}")


def test_resource_key_to_bigint_deterministic():
    """_resource_key_to_bigint produces signed 64-bit int matching hashlib sha256."""
    key = "ts_states/TS_colab-master.state"
    val = _resource_key_to_bigint(key)
    assert isinstance(val, int)
    assert -9223372036854775808 <= val <= 9223372036854775807

    # Compare with explicit hashlib + unpack
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    expected = struct.unpack(">q", digest[:8])[0]
    assert val == expected


def test_resource_key_to_bigint_different_keys():
    """Different keys produce different bigint hashes."""
    val1 = _resource_key_to_bigint("key-alpha")
    val2 = _resource_key_to_bigint("key-beta")
    assert val1 != val2


def test_pg_advisory_lock_lifecycle_real_engine(real_pg_engine: Engine):
    """Full lifecycle test against live PostgreSQL engine."""
    res_key = "test/lifecycle/resource_1"
    node_1 = "node-alpha"
    node_2 = "node-beta"

    # Initially unlocked
    assert not is_locked(real_pg_engine, res_key)
    assert get_lock_holder(real_pg_engine, res_key) is None

    # Acquire lock for node_1
    assert try_acquire_lock(real_pg_engine, res_key, node_1) is True
    assert is_locked(real_pg_engine, res_key) is True
    assert get_lock_holder(real_pg_engine, res_key) == node_1

    info = get_lock_info(real_pg_engine, res_key)
    assert info is not None
    assert info["locked"] is True
    assert info["holder"] == node_1
    assert info["acquired_at"] is not None

    # Attempt second acquire on same key fails
    assert try_acquire_lock(real_pg_engine, res_key, node_2) is False

    # Release lock
    assert release_lock(real_pg_engine, res_key) is True
    assert not is_locked(real_pg_engine, res_key)
    assert get_lock_holder(real_pg_engine, res_key) is None


def test_advisory_lock_scope_context_manager(real_pg_engine: Engine):
    """advisory_lock_scope acquires and automatically releases lock."""
    res_key = "test/scope/resource_2"

    assert not is_locked(real_pg_engine, res_key)
    with advisory_lock_scope(real_pg_engine, res_key, "worker-scoped"):
        assert is_locked(real_pg_engine, res_key)

    assert not is_locked(real_pg_engine, res_key)


def test_worker_locks_pg_api_endpoints(real_pg_engine: Engine, monkeypatch: pytest.MonkeyPatch):
    """Test worker_locks API endpoints for PostgreSQL advisory locks."""
    worker_secret = "test-secret-999"
    monkeypatch.setenv("XIOSYNC_WORKER_ORG_SECRET", worker_secret)

    app = FastAPI()
    app.include_router(worker_locks_router, prefix="/api/v1")
    set_engine(real_pg_engine)

    client = TestClient(app)
    res_key = "drive/mount/test_obj.bin"
    node = "colab-worker-42"

    # 1. Acquire unauthorized
    resp = client.post(
        "/api/v1/workers/lock/pg/acquire",
        json={"resource_key": res_key, "node_name": node},
    )
    assert resp.status_code == 401

    # 2. Acquire authorized
    resp = client.post(
        "/api/v1/workers/lock/pg/acquire",
        json={"resource_key": res_key, "node_name": node, "ttl_seconds": 60},
        headers={"X-Worker-Secret": worker_secret},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["acquired"] is True
    assert data["holder"] == node

    # 3. Status endpoint shows locked
    resp = client.get(
        f"/api/v1/workers/lock/pg/status/{res_key}",
        headers={"X-Worker-Secret": worker_secret},
    )
    assert resp.status_code == 200
    status_data = resp.json()
    assert status_data["locked"] is True
    assert status_data["holder"] == node

    # 4. Competing node tries to acquire
    resp = client.post(
        "/api/v1/workers/lock/pg/acquire",
        json={"resource_key": res_key, "node_name": "other-node"},
        headers={"X-Worker-Secret": worker_secret},
    )
    assert resp.status_code == 200
    competing_data = resp.json()
    assert competing_data["acquired"] is False
    assert competing_data["holder"] == node

    # 5. Competing node tries to release -> rejected (not holder)
    resp = client.post(
        "/api/v1/workers/lock/pg/release",
        json={"resource_key": res_key, "node_name": "other-node"},
        headers={"X-Worker-Secret": worker_secret},
    )
    assert resp.status_code == 200
    assert resp.json()["released"] is False
    assert "not_holder" in resp.json()["reason"]

    # 6. Holder releases -> released
    resp = client.post(
        "/api/v1/workers/lock/pg/release",
        json={"resource_key": res_key, "node_name": node},
        headers={"X-Worker-Secret": worker_secret},
    )
    assert resp.status_code == 200
    assert resp.json()["released"] is True

    # 7. Release again -> lock_not_found
    resp = client.post(
        "/api/v1/workers/lock/pg/release",
        json={"resource_key": res_key, "node_name": node},
        headers={"X-Worker-Secret": worker_secret},
    )
    assert resp.status_code == 200
    assert resp.json()["released"] is False
    assert resp.json()["reason"] == "lock_not_found"

    # 8. Status endpoint shows unlocked
    resp = client.get(
        f"/api/v1/workers/lock/pg/status/{res_key}",
        headers={"X-Worker-Secret": worker_secret},
    )
    assert resp.status_code == 200
    assert resp.json()["locked"] is False


def test_worker_locks_pg_endpoints_engine_none(monkeypatch: pytest.MonkeyPatch):
    """Test worker_locks API endpoints when database engine is None (503 response)."""
    worker_secret = "test-secret-engine-none"
    monkeypatch.setenv("XIOSYNC_WORKER_ORG_SECRET", worker_secret)

    app = FastAPI()
    app.include_router(worker_locks_router, prefix="/api/v1")
    set_engine(None)

    client = TestClient(app)

    resp = client.post(
        "/api/v1/workers/lock/pg/acquire",
        json={"resource_key": "any", "node_name": "any"},
        headers={"X-Worker-Secret": worker_secret},
    )
    assert resp.status_code == 503
    assert "Database engine not available" in resp.json()["detail"]
