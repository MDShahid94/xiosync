"""XIOFLOW memory tables: xioflow_memory_nodes + xioflow_consensus_votes.

Revision ID: 0027
Revises: 5c2c0ef8c0d1
Create Date: 2026-09-04

Creates the two genuinely new tables for the XIOFLOW subsystem:

1. ``xioflow_memory_nodes`` — the core action memory graph with 10-tier locator
   payload, Bayesian consensus scores, device context hash, and DAG graph edges.

2. ``xioflow_consensus_votes`` — Bayesian vote ledger for tracking EdgeWorker
   success/failure telemetry per memory node.

All other XIOFLOW concerns (workflow definitions, execution tracking, DLQ,
secrets, plugins, scheduling) reuse existing XIOSYNC tables.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID, ARRAY, TIMESTAMP

revision: str = "0027"
down_revision: str | None = "5c2c0ef8c0d1"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_ts = TIMESTAMP(timezone=True)


def upgrade() -> None:
    # ── xioflow_memory_nodes ────────────────────────────────────────────
    op.create_table(
        "xioflow_memory_nodes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id")),

        # Memory Tier
        sa.Column("tier", sa.Text(), nullable=False, server_default=sa.text("'project_experimental'")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'ACTIVE'")),

        # Semantic Identity
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=False),

        # Device Context Hash
        sa.Column("context_hash", sa.Text(), nullable=False, server_default=sa.text("'default'")),
        sa.Column("device_type", sa.Text()),
        sa.Column("os_name", sa.Text()),
        sa.Column("browser", sa.Text()),
        sa.Column("viewport_width", sa.Integer()),
        sa.Column("viewport_height", sa.Integer()),

        # 10-Tier Locator Payload
        sa.Column("face_value", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("place_value", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),

        # Execution Definition
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("action_params", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("output_var", sa.Text()),
        sa.Column("execution_mode", sa.Text(), nullable=False, server_default=sa.text("'sequential'")),

        # Graph Edges
        sa.Column("previous_intent", sa.Text()),
        sa.Column("next_nodes", ARRAY(UUID(as_uuid=True)), server_default=sa.text("'{}'")),
        sa.Column("condition", sa.Text(), server_default=sa.text("'default'")),

        # Volatility & Plugin
        sa.Column("volatility_type", sa.Text(), nullable=False, server_default=sa.text("'static'")),
        sa.Column("fallback_plugin", sa.Text()),

        # Bayesian Consensus
        sa.Column("bayesian_score", sa.Float(), nullable=False, server_default=sa.text("0.5")),
        sa.Column("ema_score", sa.Float(), nullable=False, server_default=sa.text("0.5")),
        sa.Column("total_vote_weight", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("promotions", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("ref_count", sa.Integer(), nullable=False, server_default=sa.text("0")),

        # Locator Priority Cache
        sa.Column("locator_priority", ARRAY(sa.Integer()), server_default=sa.text("'{1,2,3,4,5,6,7,8,9,10}'")),

        # Provenance
        sa.Column("recorded_by", UUID(as_uuid=True), sa.ForeignKey("actors.id")),
        sa.Column("recording_method", sa.Text(), nullable=False, server_default=sa.text("'auto_learn'")),
        sa.Column("client_id", sa.Text()),

        # Timestamps
        sa.Column("created_at", _ts, nullable=False, server_default=sa.text("now()")),
        sa.Column("last_used", _ts, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", _ts, nullable=False, server_default=sa.text("now()")),

        # Lookup Key — computed column
        sa.Column("lookup_key", sa.Text()),
    )

    # Create the computed column via raw SQL (SQLAlchemy/Alembic doesn't
    # natively support GENERATED ALWAYS AS ... STORED).
    op.execute("""
        ALTER TABLE xioflow_memory_nodes
        DROP COLUMN lookup_key;
    """)
    op.execute("""
        ALTER TABLE xioflow_memory_nodes
        ADD COLUMN lookup_key TEXT GENERATED ALWAYS AS
            (domain || '::' || intent || '::' || context_hash) STORED;
    """)

    # Unique constraint
    op.create_unique_constraint("uq_xfmn_org_id", "xioflow_memory_nodes", ["organization_id", "id"])

    # Check constraints
    op.execute("""
        ALTER TABLE xioflow_memory_nodes ADD CONSTRAINT ck_xfmn_tier_allowed
        CHECK (tier IN ('project_experimental', 'project_ground_truth',
                        'organization_shared', 'platform_global'));
    """)
    op.execute("""
        ALTER TABLE xioflow_memory_nodes ADD CONSTRAINT ck_xfmn_status_allowed
        CHECK (status IN ('ACTIVE', 'ARCHIVED', 'DEPRECATED'));
    """)
    op.execute("""
        ALTER TABLE xioflow_memory_nodes ADD CONSTRAINT ck_xfmn_action_type_allowed
        CHECK (action_type IN ('click', 'type', 'extract_data', 'scroll_down',
                               'navigate', 'wait', 'done', 'trigger_sub_workflow',
                               'compute_node', 'conditional'));
    """)
    op.execute("""
        ALTER TABLE xioflow_memory_nodes ADD CONSTRAINT ck_xfmn_execution_mode_allowed
        CHECK (execution_mode IN ('sequential', 'parallel'));
    """)
    op.execute("""
        ALTER TABLE xioflow_memory_nodes ADD CONSTRAINT ck_xfmn_volatility_allowed
        CHECK (volatility_type IN ('static', 'dynamic', 'bubble'));
    """)
    op.execute("""
        ALTER TABLE xioflow_memory_nodes ADD CONSTRAINT ck_xfmn_recording_method_allowed
        CHECK (recording_method IN ('auto_learn', 'teacher_extension',
                                    'declarative_dag', 'mcp_chat'));
    """)

    # Performance indexes
    op.create_index("idx_xfmn_lookup", "xioflow_memory_nodes", ["lookup_key", "tier", "status"])
    op.create_index("idx_xfmn_domain_intent", "xioflow_memory_nodes", ["domain", "intent"])
    op.create_index("idx_xfmn_org", "xioflow_memory_nodes", ["organization_id"])
    op.create_index("idx_xfmn_project", "xioflow_memory_nodes", ["project_id"])
    op.create_index("idx_xfmn_context", "xioflow_memory_nodes", ["context_hash"])

    # ── xioflow_consensus_votes ─────────────────────────────────────────
    op.create_table(
        "xioflow_consensus_votes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("node_id", UUID(as_uuid=True), sa.ForeignKey("xioflow_memory_nodes.id"), nullable=False),
        sa.Column("voter_id", sa.Text(), nullable=False),
        sa.Column("raw_vote", sa.Float(), nullable=False),
        sa.Column("tier_confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("winning_tier", sa.Integer()),
        sa.Column("context_hash", sa.Text()),
        sa.Column("created_at", _ts, nullable=False, server_default=sa.text("now()")),
    )

    op.create_unique_constraint(
        "uq_xfcv_node_voter_context", "xioflow_consensus_votes",
        ["node_id", "voter_id", "context_hash"],
    )
    op.create_index("idx_xfcv_org", "xioflow_consensus_votes", ["organization_id"])
    op.create_index("idx_xfcv_node", "xioflow_consensus_votes", ["node_id"])


def downgrade() -> None:
    op.drop_table("xioflow_consensus_votes")
    op.drop_table("xioflow_memory_nodes")
