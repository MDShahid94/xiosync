"""Score and priority bound constraints + PG ENUM consideration documentation.

Revision ID: 0064_score_bounds_and_enums
Revises: 0063_trigger_filters

D-4: Score Bound Constraints
Several tables have score, priority, or weight columns with no DB-level bounds.
A runaway score could skew ordering and priority scheduling. This migration adds
CHECK constraints to enforce bounds:
- priority: 0 to 1000 (integer priority ordering, e.g., storage_providers, database_providers,
  domain_proxy_rules, xioflow_agent_runs, xioflow_dag_edges)
- priority (task queue): -1000 to 1000 (xioflow_tasks supports negative priority backoff
  from worker/reaper retry decay)
- weight: 0.0 to 1.0 (normalized graph edge weights)
- score / health: 0.0 to 1.0 (credentials.health_score)
- consensus scores: -1.0 to 1.0 (xioflow_memory_nodes bayesian_score/ema_score,
  xioflow_consensus_votes raw_vote), 0.0 to 1.0 (tier_confidence), >= 0.0 (total_vote_weight)

D-5: Future PG ENUM Considerations
Many tables currently enforce enumerated columns using CHECK constraints
(e.g., CHECK (state IN ('active', 'inactive', ...))). While flexible for schema evolution,
PostgreSQL native ENUM types provide superior storage compactness (4 bytes vs text)
and type safety at the storage layer. Future work should consider converting
the following high-traffic columns to native PostgreSQL ENUMs:
1. `state IN ('active', 'inactive', ...)` — ubiquitous across actors, edges, identities,
   projects, organizations, as well as execution states
   ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'CANCELLED', etc.).
2. `trigger IN ('user_command', 'schedule', 'auto', 'error', 'system')` — workflow triggers.
3. `outcome IN ('success', 'partial', 'failed', 'pending')` — run and audit outcomes.
4. `membership_role IN ('org_owner', 'org_admin', 'org_member', 'org_viewer')` — organization memberships.

Migration to PG ENUMs should use `CREATE TYPE ... AS ENUM (...)` and `ALTER TABLE ... ALTER COLUMN ... TYPE ... USING ...::text::..._enum`.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0064_score_bounds_and_enums"
down_revision = "0063_trigger_filters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Edges weight bounds (0.0 to 1.0) ────────────────────────────────────
    op.create_check_constraint(
        "ck_edges_weight_bounds",
        "edges",
        sa.text("weight IS NULL OR (weight >= 0.0 AND weight <= 1.0)"),
    )

    # ── Storage & DB providers priority bounds (0 to 1000) ─────────────────
    op.create_check_constraint(
        "ck_storage_providers_priority_bounds",
        "storage_providers",
        sa.text("priority >= 0 AND priority <= 1000"),
    )
    op.create_check_constraint(
        "ck_database_providers_priority_bounds",
        "database_providers",
        sa.text("priority >= 0 AND priority <= 1000"),
    )

    # ── Domain proxy rules priority bounds (0 to 1000) ──────────────────────
    op.create_check_constraint(
        "ck_domain_proxy_rules_priority_bounds",
        "domain_proxy_rules",
        sa.text("priority >= 0 AND priority <= 1000"),
    )

    # ── Workflow agent runs priority bounds (0 to 1000) ────────────────────
    op.create_check_constraint(
        "ck_xioflow_agent_runs_priority_bounds",
        "xioflow_agent_runs",
        sa.text("priority >= 0 AND priority <= 1000"),
    )

    # ── Task queue priority bounds (-1000 to 1000, supports retry decay) ────
    op.create_check_constraint(
        "ck_xioflow_tasks_priority_bounds",
        "xioflow_tasks",
        sa.text("priority >= -1000 AND priority <= 1000"),
    )

    # ── DAG edges priority bounds (0 to 1000) ──────────────────────────────
    op.create_check_constraint(
        "ck_xioflow_dag_edges_priority_bounds",
        "xioflow_dag_edges",
        sa.text("priority >= 0 AND priority <= 1000"),
    )

    # ── Credentials health score bounds (0.0 to 1.0) ────────────────────────
    op.create_check_constraint(
        "ck_credentials_health_score_bounds",
        "credentials",
        sa.text("health_score >= 0.0 AND health_score <= 1.0"),
    )

    # ── Memory nodes score & weight bounds ─────────────────────────────────
    op.create_check_constraint(
        "ck_xioflow_memory_nodes_bayesian_score_bounds",
        "xioflow_memory_nodes",
        sa.text("bayesian_score >= -1.0 AND bayesian_score <= 1.0"),
    )
    op.create_check_constraint(
        "ck_xioflow_memory_nodes_ema_score_bounds",
        "xioflow_memory_nodes",
        sa.text("ema_score >= -1.0 AND ema_score <= 1.0"),
    )
    op.create_check_constraint(
        "ck_xioflow_memory_nodes_total_vote_weight_bounds",
        "xioflow_memory_nodes",
        sa.text("total_vote_weight >= 0.0"),
    )

    # ── Consensus votes score bounds ────────────────────────────────────────
    op.create_check_constraint(
        "ck_xioflow_consensus_votes_raw_vote_bounds",
        "xioflow_consensus_votes",
        sa.text("raw_vote >= -1.0 AND raw_vote <= 1.0"),
    )
    op.create_check_constraint(
        "ck_xioflow_consensus_votes_tier_confidence_bounds",
        "xioflow_consensus_votes",
        sa.text("tier_confidence >= 0.0 AND tier_confidence <= 1.0"),
    )


def downgrade() -> None:
    # Drop consensus votes constraints
    op.drop_constraint("ck_xioflow_consensus_votes_tier_confidence_bounds", "xioflow_consensus_votes")
    op.drop_constraint("ck_xioflow_consensus_votes_raw_vote_bounds", "xioflow_consensus_votes")

    # Drop memory nodes constraints
    op.drop_constraint("ck_xioflow_memory_nodes_total_vote_weight_bounds", "xioflow_memory_nodes")
    op.drop_constraint("ck_xioflow_memory_nodes_ema_score_bounds", "xioflow_memory_nodes")
    op.drop_constraint("ck_xioflow_memory_nodes_bayesian_score_bounds", "xioflow_memory_nodes")

    # Drop credentials health score constraint
    op.drop_constraint("ck_credentials_health_score_bounds", "credentials")

    # Drop DAG edges priority constraint
    op.drop_constraint("ck_xioflow_dag_edges_priority_bounds", "xioflow_dag_edges")

    # Drop task queue priority constraint
    op.drop_constraint("ck_xioflow_tasks_priority_bounds", "xioflow_tasks")

    # Drop agent runs priority constraint
    op.drop_constraint("ck_xioflow_agent_runs_priority_bounds", "xioflow_agent_runs")

    # Drop domain proxy rules priority constraint
    op.drop_constraint("ck_domain_proxy_rules_priority_bounds", "domain_proxy_rules")

    # Drop storage & DB providers priority constraints
    op.drop_constraint("ck_database_providers_priority_bounds", "database_providers")
    op.drop_constraint("ck_storage_providers_priority_bounds", "storage_providers")

    # Drop edges weight constraint
    op.drop_constraint("ck_edges_weight_bounds", "edges")
