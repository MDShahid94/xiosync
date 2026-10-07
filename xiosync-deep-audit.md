# XIOSYNC — Deep Architectural Audit

> **Scope:** Neutral, mathematically-grounded analysis of architectural soundness
> **Date:** 2026-10-07 | **Methodology:** Three independent expert auditors + manual verification
> **Standard:** Platform meant for any organization, any project, any future tech choices

---

## Severity Legend

| Level | Meaning |
|-------|---------|
| 🔴 **CRITICAL** | Breaks mathematical soundness, data integrity risk, or blocks platform universality |
| 🟠 **HIGH** | Significant architectural limitation that constrains future extensibility |
| 🟡 **MEDIUM** | Sub-optimal pattern that accumulates technical debt |
| ⚪ **LOW** | Cosmetic or minor improvement opportunity |

---

## Summary: 28 Findings Across 7 Categories

| Category | 🔴 | 🟠 | 🟡 | ⚪ | Total |
|----------|-----|-----|-----|-----|-------|
| [Ontology System](#1-ontology-system) | 2 | 2 | 0 | 0 | 4 |
| [Capability & RBAC](#2-capability--rbac) | 1 | 2 | 0 | 0 | 3 |
| [Sharing System](#3-sharing-system) | 1 | 1 | 0 | 0 | 2 |
| [Schema Design](#4-schema-design) | 1 | 2 | 2 | 0 | 5 |
| [Subsystem Coupling & Worker](#5-subsystem-coupling--worker) | 1 | 2 | 2 | 0 | 5 |
| [API Design](#6-api-design) | 0 | 2 | 1 | 0 | 3 |
| [Event & Trigger System](#7-event--trigger-system) | 0 | 1 | 1 | 0 | 2 |
| **TOTAL** | **6** | **12** | **6** | **0** | **24** |

---

## 1. Ontology System

> **Files:** [`models/ontology.py`](file:///Users/karmareturns/projects/XIOSYNC/xiosync/persistence/models/ontology.py), [`domain/ontology.py`](file:///Users/karmareturns/projects/XIOSYNC/xiosync/domain/ontology.py), [`services/ontology.py`](file:///Users/karmareturns/projects/XIOSYNC/xiosync/services/ontology.py), [`api/routers/ontology.py`](file:///Users/karmareturns/projects/XIOSYNC/xiosync/api/routers/ontology.py)

### O-1 🔴 Broken Edge Typing Integrity

**What exists:** `Edge.edge_type` is a `Text` column ([L192](file:///Users/karmareturns/projects/XIOSYNC/xiosync/persistence/models/ontology.py#L192)) with a comment saying "registry-validated." However, there is **no Foreign Key** connecting it to `type_registry`. The `EdgeService.create_edge` method also does not programmatically validate against `type_registry`.

**Why it matters:** The type registry exists precisely to provide a single authority for valid type values (doc 03 §8). Without referential integrity, arbitrary strings can be committed as edge types, violating the ontology's own design invariant. Any tool or organization can silently introduce invalid types.

**Fix:** Add a composite FK or application-level validation: `ForeignKeyConstraint(['edge_type'], ['type_registry.value'], ...)` scoped to `category='edge_type'`, or add a trigger/check.

---

### O-2 🔴 O(N+E) Cycle Detection Scalability Bomb

**What exists:** `EdgeService.create_edge` ([services/ontology.py](file:///Users/karmareturns/projects/XIOSYNC/xiosync/services/ontology.py)) calls `self._repository.load_adjacency(context, graph_class)` which loads the **entire graph's adjacency map** into application memory to run a Python BFS/DFS cycle check on every single edge insertion.

**Why it matters:** For a platform serving any organization, graph sizes can grow to millions of nodes. Loading the full adjacency list on every write is O(N+E) per insertion, which is catastrophic at scale. This is a mathematical impossibility for production use.

**Fix:** Replace with a PostgreSQL recursive CTE:
```sql
WITH RECURSIVE cycle_check AS (
    SELECT target_id, ARRAY[source_id, target_id] AS path
    FROM edges WHERE source_id = :new_source AND graph_class = :gc
    UNION ALL
    SELECT e.target_id, cc.path || e.target_id
    FROM edges e JOIN cycle_check cc ON e.source_id = cc.target_id
    WHERE e.target_id != ALL(cc.path)
)
SELECT EXISTS (SELECT 1 FROM cycle_check WHERE target_id = :new_source);
```

---

### O-3 🟠 No Edge Version Control

**What exists:** `Memory` implements rigorous append-only versioning with `superseded_by` ([L243](file:///Users/karmareturns/projects/XIOSYNC/xiosync/persistence/models/ontology.py#L243)). However, `Edge` is mutated in-place via `state IN ('active', 'inactive')`. There is no historical provenance for relationships.

**Why it matters:** Knowledge graphs require temporal queries ("what was the org chart on date X?"). Mutating edges in place destroys provenance. This is architecturally inconsistent — Memory gets versioning but Edge doesn't.

**Fix:** Add `superseded_by UUID NULL` and `version INTEGER DEFAULT 1` to `edges`, matching the `Memory` pattern. New edges append rather than mutate.

---

### O-4 🟠 No Edge Properties (Not a Property Graph)

**What exists:** `Edge` stores only `weight: Float | None` and `state: str`. There is no `properties JSONB` or `content JSONB` column.

**Why it matters:** Property graphs (the industry standard — Neo4j, Apache TinkerPop, AWS Neptune) require arbitrary key-value properties on edges. Without this, the ontology cannot represent "Alice manages Bob with seniority_level=3" or "Service A depends on Service B with latency_sla_ms=50". This blocks representation of arbitrary domains.

**Fix:** Add `properties: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))` to `Edge`.

---

## 2. Capability & RBAC

> **Files:** [`models/authorization.py`](file:///Users/karmareturns/projects/XIOSYNC/xiosync/persistence/models/authorization.py), [`domain/authorization.py`](file:///Users/karmareturns/projects/XIOSYNC/xiosync/domain/authorization.py), [`middleware/rbac.py`](file:///Users/karmareturns/projects/XIOSYNC/xiosync/api/middleware/rbac.py)

### C-1 🔴 No Capability Delegation, Inheritance, or Composition

**What exists:** The `Grant` table ([L90-102](file:///Users/karmareturns/projects/XIOSYNC/xiosync/persistence/models/authorization.py#L90-L102)) is a flat mapping: `actor_id → capability_id`. Columns: `id`, `organization_id`, `actor_id`, `capability_id`, `state`, `constraints`, `expires_at`, `created_at`, `revoked_at`.

**What is MISSING:**

| Feature | Status | Impact |
|---------|--------|--------|
| `granted_by` / `delegated_from` | ❌ Missing | Cannot trace who granted what. No audit trail for authorization decisions. |
| Capability inheritance | ❌ Missing | Cannot model "admin inherits all member capabilities." Every capability must be individually granted. |
| Capability composition | ❌ Missing | Cannot model "capability A requires capability B." No dependency graph between capabilities. |
| Cascaded revocation | ❌ Missing | Revoking a delegator's grant does not cascade to delegatees. |

**Why it matters:** For a multi-org platform, capability delegation is foundational. An org admin should be able to delegate specific capabilities to team leads, who delegate subsets to members, with revocation cascading up the chain. Without this, the RBAC system is mathematically a flat ACL, not a proper authorization graph.

**Fix:** Add to `grants`:
```python
granted_by: Mapped[uuid.UUID | None]  # FK → actors.id (who granted this)
parent_grant_id: Mapped[uuid.UUID | None]  # FK → grants.id (delegation chain)
```
Add to `capabilities`:
```python
requires: Mapped[list[uuid.UUID]] = mapped_column(JSONB, default=[])  # prerequisite capabilities
```

---

### C-2 🟠 Superuser Policy Bypass Anti-Pattern

**What exists:** In [`rbac.py`](file:///Users/karmareturns/projects/XIOSYNC/xiosync/api/middleware/rbac.py), `_check_role` returns `True` for Platform Admins and Org Owners, completely short-circuiting the grant-based authorization engine.

**Why it matters:** This breaks the principle of least privilege. Superusers should have explicit wildcard grants that flow through the same policy engine, creating a unified audit trail. The current approach means admin actions are invisible to the authorization system.

**Fix:** On org bootstrap, grant a `*` wildcard capability to the owner. Evaluate all requests through the same grant-resolution path.

---

### C-3 🟠 Grant Expiration Not Enforced at Query Time

**What exists:** `Grant.expires_at` is stored but there is no evidence of a background reaper that transitions expired grants to `revoked` state. If the authorization query doesn't filter `WHERE expires_at > now()`, expired grants remain active.

**Fix:** Either add `AND (expires_at IS NULL OR expires_at > now())` to all grant lookups, or add a periodic reaper.

---

## 3. Sharing System

> **Files:** [`models/sharing.py`](file:///Users/karmareturns/projects/XIOSYNC/xiosync/persistence/models/sharing.py), [`domain/sharing.py`](file:///Users/karmareturns/projects/XIOSYNC/xiosync/domain/sharing.py), [`domain/authorization.py`](file:///Users/karmareturns/projects/XIOSYNC/xiosync/domain/authorization.py)

### S-1 🔴 Granular Permissions Are Dead Code

**What exists:** `ResourceShare.permissions` stores granular permissions like `["read", "execute"]`. However, the `authorize` engine in [`domain/authorization.py`](file:///Users/karmareturns/projects/XIOSYNC/xiosync/domain/authorization.py) only accepts a `ShareChecker` with signature `Callable[[str, uuid.UUID, uuid.UUID], bool]` — it checks if **any** active share exists, completely ignoring the permissions array.

**Why it matters:** Cross-org sharing appears to support granular access (read vs write vs execute), but it functionally doesn't. A "read-only" share grants the same access as a "full" share. This is a data integrity illusion.

**Fix:** Extend `ShareChecker` signature to `Callable[[str, uuid.UUID, uuid.UUID, str], bool]` where the 4th arg is the required permission. Query must filter `permissions @> '["read"]'::jsonb`.

---

### S-2 🟠 No Sharing Chains or Cascaded Revocation

**What exists:** `ResourceShare` tracks `source_org_id → target_org_id` but lacks `parent_share_id`.

**Why it matters:** If Org A shares with Org B, and Org B reshares with Org C, revoking Org A's share does not cascade to Org C. This creates orphaned permissions that violate the security model.

**Fix:** Add `parent_share_id: UUID NULL FK → resource_shares.id`. On revocation, cascade: `UPDATE resource_shares SET state='revoked' WHERE parent_share_id = :id`.

---

## 4. Schema Design

> **Files:** All `xiosync/persistence/models/*.py`

### D-1 🔴 No Federated Authentication Model

**What exists:** [`MemberAuth`](file:///Users/karmareturns/projects/XIOSYNC/xiosync/persistence/models/identity.py#L143) supports only `email` + `password_hash` (Argon2id). There are no structures for OAuth/OIDC providers, external subject IDs, or federated identity linking.

**Why it matters:** A universal platform must support Google, GitHub, Okta, SAML, and arbitrary OIDC providers. The current model requires every user to have a XIOSYNC-local password, which is unacceptable for enterprise SSO.

**Fix:** Create `federated_credentials` table:
```python
class FederatedCredential(Base):
    __tablename__ = "federated_credentials"
    id: UUID PK
    member_auth_id: UUID FK → member_auth.id
    provider: str  # "google", "github", "okta", "saml"
    provider_subject_id: str  # external user ID
    provider_email: str | None
    metadata: JSONB  # claims, tokens, etc.
    created_at: TIMESTAMPTZ
    UNIQUE(provider, provider_subject_id)
```

---

### D-2 🟠 DAG Edges Modeled as Arrays, Not Relational

**What exists:** `XioflowMemoryNode.next_nodes` uses `ARRAY(UUID)` and `previous_intent: Text` for graph edges ([memory_nodes.py](file:///Users/karmareturns/projects/XIOSYNC/xiosync/subsystems/xioflow/models/memory_nodes.py)).

**Why it matters:** Array-based edges cannot be indexed, JOINed, or traversed efficiently in SQL. Cycle detection and deep resolution require loading entire graphs into application memory. A proper associative edge table (`xioflow_edges(source_node_id, target_node_id, edge_type, weight)`) would enable recursive CTEs.

---

### D-3 🟠 Unconstrained JSONB Columns

**What exists:** Critical fields like `external_providers`, `action_params`, `runtime_state`, `artifacts` are unstructured JSONB with no schema validation at any layer.

**Why it matters:** Without JSON Schema validation (DB-level or app-level), any malformed data can be committed, causing downstream failures that are extremely difficult to debug.

**Fix:** Add `CHECK (jsonb_typeof(action_params) = 'object')` at minimum. Ideally, validate with JSON Schema in the service layer.

---

### D-4 🟡 Missing Bound Constraints on Scores

**What exists:** `bayesian_score` and `ema_score` in [`memory_nodes.py:155-158`](file:///Users/karmareturns/projects/XIOSYNC/xiosync/subsystems/xioflow/models/memory_nodes.py#L155) lack `CHECK (bayesian_score BETWEEN 0.0 AND 1.0)`.

**Fix:** Add check constraints to enforce mathematical bounds.

---

### D-5 🟡 Hardcoded String Constraints vs PostgreSQL ENUMs

**What exists:** Extensive use of `CheckConstraint("state IN ('active', 'inactive')")` pattern across all models.

**Why it matters:** While easier to migrate, string checks deny the DB engine native enum optimization and type-safety. For a ground architecture, consider a hybrid: use ENUMs for truly fixed sets (state machines) and registry-validated strings for extensible sets.

---

## 5. Subsystem Coupling & Worker

### W-1 🔴 Parallel DAG Execution Race Condition

**What exists:** In [`dag_executor.py`](file:///Users/karmareturns/projects/XIOSYNC/xiosync/subsystems/xioflow/engine/dag_executor.py), `self.workflow_vars` is a **mutable dictionary shared across the entire DAG execution**. In parallel execution mode (`asyncio.gather` for concurrent branches), multiple branches can mutate `workflow_vars` simultaneously without any locking.

**Why it matters:** This is a classic race condition. If branch A writes `workflow_vars["token"] = "abc"` and branch B writes `workflow_vars["token"] = "xyz"` concurrently, the final value is non-deterministic. This can cause silent data corruption in workflow outputs.

**Fix:** Either:
- Deep-copy `workflow_vars` per branch and merge results with conflict resolution
- Use `asyncio.Lock` around `workflow_vars` mutations
- Make `workflow_vars` immutable (each node receives a frozen snapshot and returns its additions)

---

### W-2 🟠 xiorun_agent.py Is a 7,773-Line Monolith

**What exists:** A single file containing:
- 94 functions, 24 classes, 26 module-level globals
- FastAPI server + lifecycle management
- SOCKS5 proxy bridge + SSH tunnel
- VNC/websockify orchestration
- 22-section stealth JavaScript engine
- Google-specific challenge handlers
- CDP session management
- AI/AGY integration

**Why it matters:** This is unmaintainable, untestable, and violates separation of concerns. A bug in the SOCKS5 bridge code can crash the entire agent including browser session management.

**Recommended decomposition:**
```
colab/
  xiorun_agent.py          → server.py (FastAPI app + endpoint routing)
  network/
    socks5_bridge.py        → WS SOCKS5 relay
    ssh_tunnel.py           → SSH fallback tunnel
    proxy_guard.py          → Proxy health monitoring
  display/
    novnc_manager.py        → x11vnc + websockify lifecycle
  browser/
    stealth_js.py           → 22-section fingerprint engine
    session_manager.py      → Launch, terminate, profile push/pull
    uc_login.py             → Undetected-chromedriver login flow
  dag/
    executor.py             → DAG execution engine
    poll_loop.py            → Adaptive polling
  handlers/
    google_challenge.py     → Google 2FA/reCAPTCHA handler
    google_post_login.py    → Interstitial dismissal
  ai/
    agy_bridge.py           → Antigravity CLI integration
```

---

### W-3 🟠 Tight Cross-Subsystem Coupling

**What exists:** Subsystems directly import each other:
- `xioflow/engine/dag_executor.py` imports from `xioview.registry` and `xiogrid.services.script_runner`
- `worker/run_dispatcher.py` imports from `xiorun.runtime_pool` and `xioflow` engine

**Why it matters:** Subsystems cannot be deployed, tested, or evolved independently. A change in `xioview` can break `xioflow`. This creates a hidden monolith.

**Fix:** Introduce interface protocols (Python `Protocol` classes) at the subsystem boundary. Subsystems depend on protocols, not concrete implementations. Wire them via dependency injection in `app.py`.

---

### W-4 🟡 Circuit Breaker Not Configurable at Runtime

**What exists:** `CircuitBreaker` accepts `failure_threshold` and `recovery_timeout` in `__init__`, but `run_dispatcher.py` instantiates it with defaults (`CircuitBreaker()`).

**Fix:** Load circuit breaker config from database or environment per domain.

---

### W-5 🟡 Worker Background Thread Supervision Is Basic

**What exists:** `main.py` runs daemon threads with simple `while not _shutdown.is_set()` loops. Exceptions are caught and logged, but there's no crash-loop backoff or thread resurrection.

**Fix:** Add exponential backoff on repeated failures and a supervisor that detects dead threads.

---

## 6. API Design

### A-1 🟠 Inconsistent RFC 7807 Error Responses

**What exists:** Error handling mixes generic FastAPI `HTTPException` (in `batch.py`, `execution.py`) with the `_problem` helper pattern. Not all endpoints return RFC 7807 Problem Details.

**Fix:** Create a global exception handler middleware that wraps all errors in RFC 7807 format: `{"type": "...", "title": "...", "status": N, "detail": "..."}`.

---

### A-2 🟠 Synchronous Long-Running Execution

**What exists:** [`execution.py`](file:///Users/karmareturns/projects/XIOSYNC/xiosync/api/routers/execution.py) performs blocking operations up to 300 seconds directly in the HTTP request cycle.

**Why it matters:** This ties up server worker threads, risks load-balancer timeouts, and provides no progress feedback.

**Fix:** Return `202 Accepted` with a polling URI (`Location: /api/v1/operations/{id}`). Client polls for completion.

---

### A-3 🟡 No Content-Negotiation Versioning Strategy

**What exists:** Versioning is purely path-based (`/api/v1`). No framework for accepting `Accept: application/vnd.xiosync.v2+json` or for strict sunsetting.

**Fix:** Implement Accept-header version negotiation alongside path versioning for backward compatibility.

---

## 7. Event & Trigger System

### E-1 🟠 Events Are Audit Logs, Not Event-Sourced

**What exists:** The `events` table records append-only audit entries for state changes. However, the system does **not** derive state by replaying events — state is stored directly in entity tables.

**Why it matters:** True event sourcing enables temporal queries, replay, and complete auditability. The current approach is audit logging, which is less powerful. For a ground architecture, this limits future capabilities like "rebuild the state of the system at timestamp X."

**Impact:** Not necessarily wrong, but should be explicitly documented as an architectural decision. If event sourcing is desired in the future, a dedicated event store with snapshotting would be needed.

---

### E-2 🟡 Primitive Trigger Matching

**What exists:** `event_router.py` matches triggers via simple equality: `trigger.event_name == event.event_type`. There is no rule engine for payload filtering, boolean composition, or complex matching.

**Fix:** Add an optional `filter_expression JSONB` to `xioflow_triggers` evaluated as a JSONPath predicate against the event payload.

---

## Cross-Cutting Concerns

### X-1: Multi-Tenancy Isolation vs Cross-Org Ontology

The ontology uses composite FKs `(organization_id, source_id)` → `actors(organization_id, id)` which **structurally prevents** cross-org edges. Even if resources are shared via the Sharing System, the knowledge graph is strictly siloed. This is architecturally correct for data isolation but means federated knowledge graphs are impossible without schema changes.

**Recommendation:** If cross-org ontology is desired, add an optional `target_organization_id` with a check: `target_organization_id IS NULL OR EXISTS (SELECT 1 FROM resource_shares WHERE ...)`.

### X-2: Direct SQL in API Routers

Multiple routers execute raw SQL via `text()` directly, bypassing domain services and repositories. This spreads business logic across layers and makes it untestable.

**Recommendation:** Route all data access through domain-specific repositories.

---

## Priority Roadmap

### Immediate (Data Integrity Risk)
1. **O-1:** Add edge_type FK or validation against type_registry
2. **C-1:** Add `granted_by`, `parent_grant_id` to grants
3. **S-1:** Wire `permissions` array into ShareChecker evaluation
4. **W-1:** Fix parallel DAG race condition on workflow_vars

### Short-Term (Architecture Soundness)
5. **O-2:** Replace in-memory cycle detection with recursive CTE
6. **D-1:** Create `federated_credentials` table for SSO
7. **O-4:** Add `properties JSONB` to Edge table
8. **A-2:** Make long-running execution async (202 Accepted)

### Medium-Term (Platform Maturity)
9. **O-3:** Add edge versioning (superseded_by pattern)
10. **C-2:** Remove superuser bypass, use wildcard grants
11. **S-2:** Add sharing chains with cascaded revocation
12. **W-2:** Decompose xiorun_agent.py into modules
13. **W-3:** Introduce interface protocols for subsystem decoupling

### Long-Term (Polish)
14. **A-1:** Global RFC 7807 error middleware
15. **D-2:** Replace array-based DAG edges with relational table
16. **E-2:** Add filter expressions to triggers
17. **D-4/D-5:** Score bound constraints, consider PG ENUMs
