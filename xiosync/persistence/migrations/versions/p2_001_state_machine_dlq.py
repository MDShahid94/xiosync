"""Phase 2 audit: enforce state machine transitions + DLQ insertion trigger

Revision ID: a1b2c3d4e5f6
Revises: f1a2b3c4d5e6
Create Date: 2026-10-06 13:00:00.000000+00:00

Addresses audit findings:
  - HIGH: xioflow_runs state transitions not enforced at DB layer
  - HIGH: xioflow_dead_letters never receives INSERT (DLQ is dead code)

Creates:
  1. A trigger on xioflow_runs that prevents moving OUT of terminal states
     (SUCCESS, FAILED, CANCELLED) — once terminal, state is immutable.
  2. A trigger on xioflow_tasks that auto-inserts into xioflow_dead_letters
     when a task reaches FAILED state and has exhausted retries.
"""
from alembic import op
from sqlalchemy import text

revision = "p2001_statemachine"
down_revision = "f1a2b3c4d5e6"


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. State machine enforcement trigger for xioflow_runs ─────────────
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION xioflow_runs_state_guard()
        RETURNS TRIGGER AS $$
        BEGIN
            -- Terminal states: SUCCESS, FAILED, CANCELLED — no transition out
            IF OLD.state IN ('SUCCESS', 'FAILED', 'CANCELLED')
               AND NEW.state != OLD.state THEN
                RAISE EXCEPTION
                    'xioflow_runs state transition denied: % → % (run_id=%). '
                    'Terminal states are immutable.',
                    OLD.state, NEW.state, OLD.id;
            END IF;
            -- Valid forward transitions only:
            --   PENDING  → RUNNING, CANCELLED
            --   RUNNING  → SUCCESS, FAILED, CANCELLED
            IF OLD.state = 'PENDING' AND NEW.state NOT IN ('RUNNING', 'CANCELLED') THEN
                RAISE EXCEPTION
                    'xioflow_runs invalid transition: PENDING → % (run_id=%)',
                    NEW.state, OLD.id;
            END IF;
            IF OLD.state = 'RUNNING' AND NEW.state NOT IN ('SUCCESS', 'FAILED', 'CANCELLED') THEN
                RAISE EXCEPTION
                    'xioflow_runs invalid transition: RUNNING → % (run_id=%)',
                    NEW.state, OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """))

    conn.execute(text("""
        DROP TRIGGER IF EXISTS trg_xioflow_runs_state_guard ON xioflow_runs;
    """))

    conn.execute(text("""
        CREATE TRIGGER trg_xioflow_runs_state_guard
            BEFORE UPDATE OF state ON xioflow_runs
            FOR EACH ROW
            EXECUTE FUNCTION xioflow_runs_state_guard();
    """))

    # ── 2. DLQ auto-insertion trigger for xioflow_tasks ───────────────────
    # When a task transitions to FAILED and retry_count >= max_retries,
    # automatically insert into xioflow_dead_letters.
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION xioflow_tasks_dlq_insert()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.state = 'FAILED'
               AND (OLD.state IS NULL OR OLD.state != 'FAILED')
               AND NEW.retry_count >= COALESCE(NEW.max_retries, 3) THEN
                INSERT INTO xioflow_dead_letters
                    (id, run_id, task_id, organization_id,
                     payload, retry_count, last_error, resolved, created_at)
                VALUES
                    (gen_random_uuid(), NEW.run_id, NEW.id, NEW.organization_id,
                     COALESCE(NEW.result, '{}'::jsonb),
                     NEW.retry_count,
                     COALESCE(NEW.error, 'unknown'),
                     FALSE,
                     NOW());
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """))

    conn.execute(text("""
        DROP TRIGGER IF EXISTS trg_xioflow_tasks_dlq ON xioflow_tasks;
    """))

    conn.execute(text("""
        CREATE TRIGGER trg_xioflow_tasks_dlq
            AFTER UPDATE OF state ON xioflow_tasks
            FOR EACH ROW
            EXECUTE FUNCTION xioflow_tasks_dlq_insert();
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP TRIGGER IF EXISTS trg_xioflow_runs_state_guard ON xioflow_runs"))
    conn.execute(text("DROP FUNCTION IF EXISTS xioflow_runs_state_guard()"))
    conn.execute(text("DROP TRIGGER IF EXISTS trg_xioflow_tasks_dlq ON xioflow_tasks"))
    conn.execute(text("DROP FUNCTION IF EXISTS xioflow_tasks_dlq_insert()"))
