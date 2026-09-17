"""0039 — Drop all remaining closed-set CHECK constraints.

Every CHECK constraint that restricts a type/kind/provider column to a
hardcoded set is a universality blocker: it prevents the platform from
supporting new providers, object types, or execution modes without a DB
migration. These constraints belong at the application layer (validation,
docs) — NOT at the DB layer.

Tables cleaned:
  storage_objects         — ck_storage_object_type     (XIOBR object types)
  xioflow_memory_nodes    — ck_xfmn_tier_allowed        (XIOBR experiment tiers)
                          — ck_xfmn_recording_method_allowed (browser extension specific)
                          — ck_xfmn_action_type_allowed  (browser automation specific)
  artifacts               — ck_artifacts_*_provider_type_allowed (storage backends)
  secret_refs             — ck_secret_refs_*_provider_allowed    (secret backends)
  resource_shares         — ck_resource_shares_*_type_allowed    (resource types)
  worker_enrollments      — ck_worker_enrollments_pool_type_allowed ('colab' is a tag)

Constraints NOT touched (semantically valid closed sets at platform layer):
  sessions.state          (active/revoked/expired — platform auth lifecycle)
  events.severity         (debug/info/warn/error/critical — logging standard)
  xioflow_runs.state      (PENDING/RUNNING/SUCCESS/FAILED — engine states)
  xioflow_tasks.state     (same)
  xioflow_triggers.type   (cron/event — two real trigger types)
  capabilities.execution_mode (sync/async/streaming — protocol contract)
  grants.state, memberships, projects.state — platform governance
"""
from alembic import op
import sqlalchemy as sa

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # storage_objects — XIOBR-specific object type whitelist
    conn.execute(sa.text(
        "ALTER TABLE storage_objects DROP CONSTRAINT IF EXISTS ck_storage_object_type"
    ))

    # xioflow_memory_nodes — XIOBR experiment/browser automation specifics
    for ck in (
        "ck_xfmn_tier_allowed",
        "ck_xfmn_recording_method_allowed",
        "ck_xfmn_action_type_allowed",
    ):
        conn.execute(sa.text(f"ALTER TABLE xioflow_memory_nodes DROP CONSTRAINT IF EXISTS {ck}"))

    # artifacts — hardcoded storage backend types (r2/s3/gcs/azure_blob only)
    conn.execute(sa.text("""
        ALTER TABLE artifacts DROP CONSTRAINT IF EXISTS ck_artifacts_ck_artifacts_provider_type_allowed
    """))

    # secret_refs — hardcoded secret backend types
    conn.execute(sa.text("""
        ALTER TABLE secret_refs DROP CONSTRAINT IF EXISTS ck_secret_refs_ck_secret_refs_provider_allowed
    """))

    # resource_shares — hardcoded shareable resource types
    conn.execute(sa.text("""
        ALTER TABLE resource_shares DROP CONSTRAINT IF EXISTS ck_resource_shares_ck_resource_shares_type_allowed
    """))

    # worker_enrollments — 'colab' as first-class pool_type is a deployment detail, not a platform concept
    conn.execute(sa.text("""
        ALTER TABLE worker_enrollments DROP CONSTRAINT IF EXISTS ck_worker_enrollments_pool_type_allowed
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("""
        ALTER TABLE storage_objects ADD CONSTRAINT ck_storage_object_type
        CHECK (object_type = ANY (ARRAY['chrome_profile','ts_state','session_export','workflow_artifact','generic']))
    """))
    conn.execute(sa.text("""
        ALTER TABLE xioflow_memory_nodes ADD CONSTRAINT ck_xfmn_tier_allowed
        CHECK (tier = ANY (ARRAY['project_experimental','project_ground_truth','platform_canonical']))
    """))
    conn.execute(sa.text("""
        ALTER TABLE xioflow_memory_nodes ADD CONSTRAINT ck_xfmn_recording_method_allowed
        CHECK (recording_method = ANY (ARRAY['auto_learn','teacher_extension','manual']))
    """))
    conn.execute(sa.text("""
        ALTER TABLE xioflow_memory_nodes ADD CONSTRAINT ck_xfmn_action_type_allowed
        CHECK (action_type = ANY (ARRAY['click','type','extract_data','navigate','wait','screenshot']))
    """))
    conn.execute(sa.text("""
        ALTER TABLE artifacts ADD CONSTRAINT ck_artifacts_ck_artifacts_provider_type_allowed
        CHECK (provider_type = ANY (ARRAY['r2','s3','gcs','azure_blob']))
    """))
    conn.execute(sa.text("""
        ALTER TABLE secret_refs ADD CONSTRAINT ck_secret_refs_ck_secret_refs_provider_allowed
        CHECK (provider = ANY (ARRAY['env','vault','aws-sm','gcp-sm','azure-kv']))
    """))
    conn.execute(sa.text("""
        ALTER TABLE resource_shares ADD CONSTRAINT ck_resource_shares_ck_resource_shares_type_allowed
        CHECK (resource_type = ANY (ARRAY['capability','artifact','workflow']))
    """))
    conn.execute(sa.text("""
        ALTER TABLE worker_enrollments ADD CONSTRAINT ck_worker_enrollments_pool_type_allowed
        CHECK (pool_type = ANY (ARRAY['managed','volunteer','colab','transient']))
    """))
