#!/usr/bin/env python3
"""Comprehensive E2E Test Suite for XIOSYNC.

Tests all major subsystems and recent architectural enhancements:
  - Server Health & Worker Infrastructure
  - RFC 7807 Error Responses
  - Tarball & Monolith Worker Distribution
  - Edge Types, Delegation, Sharing Chains, Versioning, Properties
  - Federated Credentials & Password Hash Nullability
  - Async Task Execution
  - Wildcard Grants & Capabilities
  - DAG Relational Tables & Trigger Filters
  - Score & Weight Bounds Constraints
  - Subsystem Protocol Interfaces
  - Migration Chain & Alembic Head
  - Graph Cycle Detection
  - BranchVars Snapshot Isolation
  - Trigger Filter Rule Evaluation
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

# Ensure project root is in sys.path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import httpx

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    import psycopg as psycopg2  # type: ignore

# ── Configuration Constants ──────────────────────────────────────────────────

BASE_URL = os.environ.get("XIOSYNC_BASE_URL", "http://localhost:8000")
WORKER_BASE = os.environ.get("XIOSYNC_WORKER_BASE", "http://100.111.130.118:9300")
WORKER_NOVNC = os.environ.get("XIOSYNC_WORKER_NOVNC", "http://100.111.130.118:6080/vnc.html")
DB_URL = os.environ.get("DATABASE_URL", "postgresql://xiosync:xiosync@localhost:5432/xiosync")

INTERNAL_SECRET = "xiosync-internal-2026-karma"
AUTH_SECRET = "xiogrid-mac-mini-1-auth-secret-2026-karma"
BOOTSTRAP_TOKEN = "-9brrstjHCFBT47MrqxRC2yCjU9-bXTg9oObrSN3--Y"

TEST_ORG_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")
TEST_IDENTITY_ID = uuid.UUID("ede96456-f2ac-4c0f-af78-7ec4a8821438")
TEST_ACTOR_ID = uuid.UUID("00000000-0000-7000-8000-000000000002")

# ── Test Runner Infrastructure ────────────────────────────────────────────────

PASSED = 0
FAILED = 0
ERRORS: list[tuple[str, str]] = []


def run_test(name: str, fn: Any) -> None:
    global PASSED, FAILED
    try:
        fn()
        print(f"  ✅ {name}")
        PASSED += 1
    except AssertionError as e:
        print(f"  ❌ {name}: {e}")
        FAILED += 1
        ERRORS.append((name, str(e)))
    except Exception as e:
        print(f"  ⚠️  {name}: {type(e).__name__}: {e}")
        FAILED += 1
        ERRORS.append((name, f"{type(e).__name__}: {e}"))


def _db_connection():
    # If using SQLAlchemy URL style postgresql+psycopg://, normalize to postgresql://
    clean_url = DB_URL.replace("postgresql+psycopg://", "postgresql://")
    return psycopg2.connect(clean_url)


def _mint_test_jwt(actor_id: uuid.UUID = TEST_ACTOR_ID, org_id: uuid.UUID = TEST_ORG_ID) -> str:
    from xiosync.platform.tokens import issue_access_token  # noqa: PLC0415

    now = datetime.now(UTC)
    session_id = uuid.uuid4()

    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions (
                    id, organization_id, auth_identity_id,
                    refresh_token_hash, access_token_jti, state,
                    expires_at, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s)
                """,
                (
                    str(session_id),
                    str(org_id),
                    "01a0a920-cfa9-75e1-aa76-ccdd96b32287",
                    "e2e_test_hash",
                    f"jti-{session_id}",
                    now + timedelta(hours=1),
                    now,
                    now,
                ),
            )
        conn.commit()

    token, _ = issue_access_token(
        AUTH_SECRET,
        session_id=session_id,
        organization_id=org_id,
        actor_id=actor_id,
        now=now,
    )
    return token


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Server Health
# ══════════════════════════════════════════════════════════════════════════════

def test_server_liveness() -> None:
    r = httpx.get(f"{BASE_URL}/live", timeout=5.0)
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    data = r.json()
    assert data.get("status") == "alive", f"Unexpected status: {data}"


def test_worker_health() -> None:
    r = httpx.get(f"{WORKER_BASE}/health", timeout=5.0)
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    data = r.json()
    assert data.get("ok") is True, f"Unexpected response: {data}"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: RFC 7807 Error Responses (A-1)
# ══════════════════════════════════════════════════════════════════════════════

def test_404_returns_problem_json() -> None:
    token = _mint_test_jwt()
    headers = {"Authorization": f"Bearer {token}"}
    r = httpx.get(f"{BASE_URL}/api/v1/nonexistent", headers=headers, timeout=5.0)
    assert r.status_code == 404, f"Expected 404, got {r.status_code}"
    ct = r.headers.get("content-type", "")
    assert "application/problem+json" in ct, f"Expected problem+json, got {ct}"
    data = r.json()
    for field in ("type", "title", "status"):
        assert field in data, f"Missing field '{field}' in RFC 7807 problem response: {data}"
    assert data["status"] == 404


def test_validation_error_problem_json() -> None:
    headers = {"X-XIOSYNC-Internal": INTERNAL_SECRET}
    r = httpx.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={"invalid_field": "bad_data"},
        headers=headers,
        timeout=5.0,
    )
    assert r.status_code == 422, f"Expected 422, got {r.status_code}"
    ct = r.headers.get("content-type", "")
    assert "application/problem+json" in ct, f"Expected problem+json, got {ct}"
    data = r.json()
    assert "errors" in data, f"Expected 'errors' array in validation error response: {data}"
    assert isinstance(data["errors"], list) and len(data["errors"]) > 0


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Tarball Distribution (W-2)
# ══════════════════════════════════════════════════════════════════════════════

def test_tarball_endpoint_public() -> None:
    r = httpx.get(f"{BASE_URL}/api/v1/workers/xiorun-agent.tar.gz", timeout=5.0)
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    ct = r.headers.get("content-type", "")
    assert "application/gzip" in ct, f"Expected application/gzip, got {ct}"


def test_tarball_contains_agent_package() -> None:
    r = httpx.get(f"{BASE_URL}/api/v1/workers/xiorun-agent.tar.gz", timeout=5.0)
    assert r.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        names = tar.getnames()
    assert any("agent/config.py" in n for n in names), "Tarball missing agent/config.py"
    assert any("agent/server/app.py" in n for n in names), "Tarball missing agent/server/app.py"
    assert any("agent/browser/uc_login_core.py" in n for n in names), "Tarball missing agent/browser/uc_login_core.py"


def test_monolith_endpoint_still_works() -> None:
    r = httpx.get(f"{BASE_URL}/api/v1/workers/xiorun-agent.py", timeout=5.0)
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    assert len(r.content) > 1000, "Monolith script is unexpectedly empty or short"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4: Edge Type Integrity (O-1)
# ══════════════════════════════════════════════════════════════════════════════

def test_edge_types_seeded() -> None:
    expected_types = {
        "authenticates_as",
        "materializes_to",
        "routes_through",
        "runs_on",
        "depends_on_auth",
        "binds_network",
    }
    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM type_registry WHERE category = 'edge_type'")
            rows = cur.fetchall()
    actual_types = {r[0] for r in rows}
    assert len(actual_types) >= 6, f"Expected at least 6 edge types, got {len(actual_types)}"
    missing = expected_types - actual_types
    assert not missing, f"Missing edge types in type_registry: {missing}"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5: Grant Delegation (C-1)
# ══════════════════════════════════════════════════════════════════════════════

def test_grants_have_delegation_columns() -> None:
    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'grants' AND column_name IN ('granted_by', 'parent_grant_id')
                """
            )
            cols = {r[0] for r in cur.fetchall()}
    assert "granted_by" in cols, "grants table missing 'granted_by' column"
    assert "parent_grant_id" in cols, "grants table missing 'parent_grant_id' column"


def test_capabilities_have_requires() -> None:
    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'capabilities' AND column_name = 'requires'
                """
            )
            row = cur.fetchone()
    assert row is not None, "capabilities table missing 'requires' column"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6: Sharing Chains (S-2)
# ══════════════════════════════════════════════════════════════════════════════

def test_shares_have_parent_share_id() -> None:
    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'resource_shares' AND column_name = 'parent_share_id'
                """
            )
            row = cur.fetchone()
    assert row is not None, "resource_shares table missing 'parent_share_id' column"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7: Edge Versioning (O-3)
# ══════════════════════════════════════════════════════════════════════════════

def test_edges_have_versioning_columns() -> None:
    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'edges' AND column_name IN ('version', 'superseded_by')
                """
            )
            cols = {r[0] for r in cur.fetchall()}
    assert "version" in cols, "edges table missing 'version' column"
    assert "superseded_by" in cols, "edges table missing 'superseded_by' column"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8: Edge Properties (O-4)
# ══════════════════════════════════════════════════════════════════════════════

def test_edges_have_properties() -> None:
    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_default
                FROM information_schema.columns
                WHERE table_name = 'edges' AND column_name = 'properties'
                """
            )
            row = cur.fetchone()
    assert row is not None, "edges table missing 'properties' column"
    default_val = row[0] or ""
    assert "'{}'::jsonb" in default_val or "{}" in default_val, f"Unexpected default: {default_val}"


def test_edges_properties_check_constraint() -> None:
    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, organization_id FROM actors LIMIT 2")
            actors = cur.fetchall()
            source_id, org_id = actors[0]
            target_id = actors[1][0] if len(actors) > 1 else source_id

    failed_as_expected = False
    conn_test = _db_connection()
    try:
        with conn_test:
            with conn_test.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO edges (
                        id, organization_id, source_id, target_id,
                        edge_type, graph_class, state, properties
                    )
                    VALUES (%s, %s, %s, %s, 'authenticates_as', 'hierarchy', 'active', '["invalid", "array"]'::jsonb)
                    """,
                    (str(uuid.uuid4()), org_id, source_id, target_id),
                )
    except psycopg2.IntegrityError:
        failed_as_expected = True
    finally:
        conn_test.close()

    assert failed_as_expected, "Inserting array JSON into edges.properties should have raised CHECK violation"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9: Federated Credentials (D-1)
# ══════════════════════════════════════════════════════════════════════════════

def test_federated_credentials_table_exists() -> None:
    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM federated_credentials LIMIT 0")
            assert cur.description is not None


def test_password_hash_nullable() -> None:
    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT is_nullable
                FROM information_schema.columns
                WHERE table_name = 'member_auth' AND column_name = 'password_hash'
                """
            )
            row = cur.fetchone()
    assert row is not None, "member_auth.password_hash column not found"
    assert row[0] == "YES", f"Expected is_nullable = 'YES', got {row[0]}"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 10: Async Execution (A-2)
# ══════════════════════════════════════════════════════════════════════════════

def test_async_execution_endpoint() -> None:
    token = _mint_test_jwt()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-XIOSYNC-Internal": INTERNAL_SECRET,
    }
    payload = {
        "script": "print('e2e async test')",
        "language": "python",
    }
    r = httpx.post(
        f"{BASE_URL}/api/v1/execution/script?async=true",
        json=payload,
        headers=headers,
        timeout=5.0,
    )
    assert r.status_code == 202, f"Expected 202 Accepted, got {r.status_code}: {r.text}"
    loc = r.headers.get("location")
    assert loc is not None and "/api/v1/operations/" in loc, f"Expected Location header with operation ID, got {loc}"
    data = r.json()
    assert "operation_id" in data, f"Response missing operation_id: {data}"
    assert data.get("status") == "pending", f"Expected pending status, got {data}"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 11: Wildcard Grants (C-2)
# ══════════════════════════════════════════════════════════════════════════════

def test_wildcard_capability_exists() -> None:
    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM capabilities WHERE name = '*'")
            count = cur.fetchone()[0]
    assert count >= 1, f"Expected at least 1 wildcard capability with name = '*', found {count}"


def test_wildcard_grant_exists() -> None:
    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM grants g
                JOIN capabilities c ON g.capability_id = c.id
                WHERE c.name = '*'
                """
            )
            count = cur.fetchone()[0]
    assert count >= 1, f"Expected at least 1 wildcard grant linked to capability '*', found {count}"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 12: DAG Relational Tables (D-2)
# ══════════════════════════════════════════════════════════════════════════════

def test_dag_nodes_table_exists() -> None:
    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM xioflow_dag_nodes LIMIT 0")
            assert cur.description is not None


def test_dag_edges_table_exists() -> None:
    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM xioflow_dag_edges LIMIT 0")
            assert cur.description is not None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 13: Trigger Filters (E-2)
# ══════════════════════════════════════════════════════════════════════════════

def test_triggers_have_filter_expression() -> None:
    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'xioflow_triggers' AND column_name = 'filter_expression'
                """
            )
            row = cur.fetchone()
    assert row is not None, "xioflow_triggers table missing 'filter_expression' column"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 14: Score Bounds (D-4)
# ══════════════════════════════════════════════════════════════════════════════

def test_edge_weight_bounds() -> None:
    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, organization_id FROM actors LIMIT 2")
            actors = cur.fetchall()
            source_id, org_id = actors[0]
            target_id = actors[1][0] if len(actors) > 1 else source_id

    failed_as_expected = False
    conn_test = _db_connection()
    try:
        with conn_test:
            with conn_test.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO edges (
                        id, organization_id, source_id, target_id,
                        edge_type, graph_class, state, weight
                    )
                    VALUES (%s, %s, %s, %s, 'authenticates_as', 'hierarchy', 'active', 999.0)
                    """,
                    (str(uuid.uuid4()), org_id, source_id, target_id),
                )
    except psycopg2.IntegrityError as exc:
        failed_as_expected = "ck_edges_weight_bounds" in str(exc) or "check constraint" in str(exc).lower()
    finally:
        conn_test.close()

    assert failed_as_expected, "Inserting edge with weight=999.0 must fail CHECK constraint"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 15: Protocol Classes (W-3)
# ══════════════════════════════════════════════════════════════════════════════

def test_protocols_importable() -> None:
    from xiosync.protocols.subsystem_interfaces import (  # noqa: PLC0415
        DAGExecutorProtocol,
        RuntimePoolProtocol,
        ScriptRunnerProtocol,
        ViewRegistryProtocol,
    )

    assert ScriptRunnerProtocol is not None
    assert ViewRegistryProtocol is not None
    assert RuntimePoolProtocol is not None
    assert DAGExecutorProtocol is not None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 16: XIOGRID Infrastructure
# ══════════════════════════════════════════════════════════════════════════════

def test_worker_sessions_endpoint() -> None:
    r = httpx.get(f"{WORKER_BASE}/sessions", timeout=5.0)
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    data = r.json()
    assert "sessions" in data, f"Expected 'sessions' key in worker response: {data}"


def test_worker_novnc_reachable() -> None:
    r = httpx.get(WORKER_NOVNC, timeout=5.0)
    assert r.status_code in (200, 301, 302), f"Expected reachable HTTP response, got {r.status_code}"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 17: Migration Chain Integrity
# ══════════════════════════════════════════════════════════════════════════════

def test_alembic_at_head() -> None:
    with _db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            row = cur.fetchone()
    assert row is not None, "alembic_version table is empty"
    expected = "0064_score_bounds_and_enums"
    assert row[0] == expected, f"Expected alembic version {expected}, got {row[0]}"


def test_migration_count() -> None:
    versions_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "xiosync",
        "persistence",
        "migrations",
        "versions",
    )
    assert os.path.isdir(versions_dir), f"Directory not found: {versions_dir}"
    all_files = os.listdir(versions_dir)
    target_migrations = [
        f for f in all_files if f.endswith(".py") and any(f.startswith(f"{i:04d}") for i in range(55, 65))
    ]
    assert len(target_migrations) == 10, f"Expected 10 migrations from 0055 to 0064, found {len(target_migrations)}: {target_migrations}"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 18: Cycle Detection (O-2) - Functional
# ══════════════════════════════════════════════════════════════════════════════

def test_cycle_detection_cte_exists() -> None:
    from xiosync.persistence.ontology import EdgeRepository  # noqa: PLC0415

    assert hasattr(EdgeRepository, "would_create_cycle_cte"), "EdgeRepository missing 'would_create_cycle_cte' method"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 19: BranchVars (W-1) - Unit
# ══════════════════════════════════════════════════════════════════════════════

def test_branch_vars_isolation() -> None:
    from xiosync.subsystems.xioflow.engine.branch_vars import BranchVars  # noqa: PLC0415

    parent = {"key1": "original", "shared": 100}
    branch = BranchVars(parent)
    branch["key1"] = "mutated"
    branch["new_key"] = "added"

    assert parent["key1"] == "original", "Parent dict was mutated by branch write!"
    assert "new_key" not in parent, "Parent dict received key added to branch!"
    assert branch["key1"] == "mutated"
    assert branch["new_key"] == "added"


def test_branch_vars_merge() -> None:
    from xiosync.subsystems.xioflow.engine.branch_vars import BranchVars, merge_branch_results  # noqa: PLC0415

    parent = {"status": "init", "count": 0}
    b1 = BranchVars(parent)
    b2 = BranchVars(parent)

    b1["status"] = "branch_1_update"
    b1["count"] = 10

    b2["status"] = "branch_2_winner"
    b2["extra"] = "from_b2"

    merge_branch_results(parent, [b1, b2])

    assert parent["status"] == "branch_2_winner", f"Last-writer-wins failed for 'status': {parent['status']}"
    assert parent["count"] == 10, f"Expected count=10 from branch 1, got {parent['count']}"
    assert parent["extra"] == "from_b2", f"Expected extra='from_b2', got {parent.get('extra')}"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 20: Trigger Filter Evaluation (E-2) - Unit
# ══════════════════════════════════════════════════════════════════════════════

def test_filter_eq() -> None:
    from xiosync.subsystems.xioflow.domain.trigger_filter import _evaluate_filter  # noqa: PLC0415

    expr = {"field": "amount", "op": "eq", "value": 100}
    assert _evaluate_filter(expr, {"amount": 100}) is True
    assert _evaluate_filter(expr, {"amount": 99}) is False


def test_filter_gt() -> None:
    from xiosync.subsystems.xioflow.domain.trigger_filter import _evaluate_filter  # noqa: PLC0415

    expr = {"field": "amount", "op": "gt", "value": 50}
    assert _evaluate_filter(expr, {"amount": 100}) is True
    assert _evaluate_filter(expr, {"amount": 50}) is False
    assert _evaluate_filter(expr, {"amount": 25}) is False


def test_filter_nested_dot() -> None:
    from xiosync.subsystems.xioflow.domain.trigger_filter import _evaluate_filter  # noqa: PLC0415

    expr = {"field": "user.role", "op": "eq", "value": "admin"}
    assert _evaluate_filter(expr, {"user": {"role": "admin"}}) is True
    assert _evaluate_filter(expr, {"user": {"role": "member"}}) is False


def test_filter_all_combinator() -> None:
    from xiosync.subsystems.xioflow.domain.trigger_filter import _evaluate_filter  # noqa: PLC0415

    expr = {
        "all": [
            {"field": "a", "op": "eq", "value": 1},
            {"field": "b", "op": "eq", "value": 2},
        ]
    }
    assert _evaluate_filter(expr, {"a": 1, "b": 2}) is True
    assert _evaluate_filter(expr, {"a": 1, "b": 3}) is False
    assert _evaluate_filter(expr, {"a": 0, "b": 2}) is False


# ── Main Entry Point ──────────────────────────────────────────────────────────

def main() -> int:
    print("======================================================================")
    print("                 XIOSYNC E2E COMPREHENSIVE TEST SUITE                 ")
    print("======================================================================")

    print("\n[SECTION 1: Server Health]")
    run_test("test_server_liveness", test_server_liveness)
    run_test("test_worker_health", test_worker_health)

    print("\n[SECTION 2: RFC 7807 Error Responses (A-1)]")
    run_test("test_404_returns_problem_json", test_404_returns_problem_json)
    run_test("test_validation_error_problem_json", test_validation_error_problem_json)

    print("\n[SECTION 3: Tarball Distribution (W-2)]")
    run_test("test_tarball_endpoint_public", test_tarball_endpoint_public)
    run_test("test_tarball_contains_agent_package", test_tarball_contains_agent_package)
    run_test("test_monolith_endpoint_still_works", test_monolith_endpoint_still_works)

    print("\n[SECTION 4: Edge Type Integrity (O-1)]")
    run_test("test_edge_types_seeded", test_edge_types_seeded)

    print("\n[SECTION 5: Grant Delegation (C-1)]")
    run_test("test_grants_have_delegation_columns", test_grants_have_delegation_columns)
    run_test("test_capabilities_have_requires", test_capabilities_have_requires)

    print("\n[SECTION 6: Sharing Chains (S-2)]")
    run_test("test_shares_have_parent_share_id", test_shares_have_parent_share_id)

    print("\n[SECTION 7: Edge Versioning (O-3)]")
    run_test("test_edges_have_versioning_columns", test_edges_have_versioning_columns)

    print("\n[SECTION 8: Edge Properties (O-4)]")
    run_test("test_edges_have_properties", test_edges_have_properties)
    run_test("test_edges_properties_check_constraint", test_edges_properties_check_constraint)

    print("\n[SECTION 9: Federated Credentials (D-1)]")
    run_test("test_federated_credentials_table_exists", test_federated_credentials_table_exists)
    run_test("test_password_hash_nullable", test_password_hash_nullable)

    print("\n[SECTION 10: Async Execution (A-2)]")
    run_test("test_async_execution_endpoint", test_async_execution_endpoint)

    print("\n[SECTION 11: Wildcard Grants (C-2)]")
    run_test("test_wildcard_capability_exists", test_wildcard_capability_exists)
    run_test("test_wildcard_grant_exists", test_wildcard_grant_exists)

    print("\n[SECTION 12: DAG Relational Tables (D-2)]")
    run_test("test_dag_nodes_table_exists", test_dag_nodes_table_exists)
    run_test("test_dag_edges_table_exists", test_dag_edges_table_exists)

    print("\n[SECTION 13: Trigger Filters (E-2)]")
    run_test("test_triggers_have_filter_expression", test_triggers_have_filter_expression)

    print("\n[SECTION 14: Score Bounds (D-4)]")
    run_test("test_edge_weight_bounds", test_edge_weight_bounds)

    print("\n[SECTION 15: Protocol Classes (W-3)]")
    run_test("test_protocols_importable", test_protocols_importable)

    print("\n[SECTION 16: XIOGRID Infrastructure]")
    run_test("test_worker_sessions_endpoint", test_worker_sessions_endpoint)
    run_test("test_worker_novnc_reachable", test_worker_novnc_reachable)

    print("\n[SECTION 17: Migration Chain Integrity]")
    run_test("test_alembic_at_head", test_alembic_at_head)
    run_test("test_migration_count", test_migration_count)

    print("\n[SECTION 18: Cycle Detection (O-2) - Functional]")
    run_test("test_cycle_detection_cte_exists", test_cycle_detection_cte_exists)

    print("\n[SECTION 19: BranchVars (W-1) - Unit]")
    run_test("test_branch_vars_isolation", test_branch_vars_isolation)
    run_test("test_branch_vars_merge", test_branch_vars_merge)

    print("\n[SECTION 20: Trigger Filter Evaluation (E-2) - Unit]")
    run_test("test_filter_eq", test_filter_eq)
    run_test("test_filter_gt", test_filter_gt)
    run_test("test_filter_nested_dot", test_filter_nested_dot)
    run_test("test_filter_all_combinator", test_filter_all_combinator)

    print("\n======================================================================")
    print(f"Results: {PASSED} passed, {FAILED} failed")
    if ERRORS:
        print("\nFailures:")
        for name, err in ERRORS:
            print(f"  ❌ {name}: {err}")
    print("======================================================================")

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
