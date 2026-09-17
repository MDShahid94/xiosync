"""0043 — Identity lease system with TTL.

Problem: allocate() marks last_used_at but has no explicit lease record.
If a worker crashes mid-task, the identity stays "recently used" and won't
be re-allocated by LRU until other identities are exhausted — up to hours.

Solution: identity_leases table
  - A worker acquires a lease when allocating an identity (set via PUT /identities/{id}/lease)
  - The lease has a TTL (default: 30 min) after which it auto-expires
  - Expired leases are detected by the allocate() query and ignored
  - Workers explicitly release leases on success/failure

This enables:
  1. Accurate real-time pool pressure visibility (which identities are in use)
  2. Fast recovery after worker crashes (lease expires in TTL, not hours)
  3. Audit trail of which worker used which identity when

Schema:
  identity_leases
    id              UUID PK
    identity_id     UUID FK → identities(id) ON DELETE CASCADE
    worker_id       UUID FK → worker_enrollments(id) ON DELETE CASCADE  (nullable)
    organization_id UUID FK → organizations(id)
    acquired_at     TIMESTAMPTZ DEFAULT now()
    expires_at      TIMESTAMPTZ NOT NULL  -- acquired_at + lease_duration
    released_at     TIMESTAMPTZ           -- set on explicit release
    run_context     JSONB                 -- optional: {run_id, task_ref, purpose}
    state           TEXT DEFAULT 'active' -- active | released | expired

Indexes:
  - (identity_id, state) WHERE state='active'  — fast "is this identity leased?" check
  - (expires_at) WHERE state='active'          — expired lease sweep by background task
  - (worker_id) WHERE state='active'           — "what does this worker hold?"
"""
from alembic import op
import sqlalchemy as sa

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS identity_leases (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            identity_id     UUID NOT NULL
                            REFERENCES identities(id) ON DELETE CASCADE,
            worker_id       UUID
                            REFERENCES worker_enrollments(id) ON DELETE SET NULL,
            organization_id UUID NOT NULL
                            REFERENCES organizations(id) ON DELETE CASCADE,
            acquired_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at      TIMESTAMPTZ NOT NULL,
            released_at     TIMESTAMPTZ,
            run_context     JSONB NOT NULL DEFAULT '{}',
            state           TEXT NOT NULL DEFAULT 'active'
        )
    """))

    # Fast "is this identity currently leased?" lookup
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_identity_leases_identity_active
        ON identity_leases (identity_id)
        WHERE state = 'active'
    """))

    # Background sweep: find leases past their TTL
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_identity_leases_expires_active
        ON identity_leases (expires_at)
        WHERE state = 'active'
    """))

    # Worker's current holdings
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_identity_leases_worker_active
        ON identity_leases (worker_id)
        WHERE state = 'active' AND worker_id IS NOT NULL
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS identity_leases CASCADE"))
