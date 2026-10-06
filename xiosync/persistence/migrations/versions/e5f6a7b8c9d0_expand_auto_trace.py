"""expand action_type + add auto_trace recording_method

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-29 12:55:00.000000+00:00

Creates (or replaces) the CHECK constraints for action_type and
recording_method on xioflow_memory_nodes.  Uses IF EXISTS for
idempotent drops.
"""
from alembic import op
from sqlalchemy import text

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── Drop existing constraints (idempotent) ────────────────
    conn.execute(text(
        "ALTER TABLE xioflow_memory_nodes "
        "DROP CONSTRAINT IF EXISTS ck_xfmn_action_type_allowed"
    ))
    conn.execute(text(
        "ALTER TABLE xioflow_memory_nodes "
        "DROP CONSTRAINT IF EXISTS ck_xfmn_recording_method_allowed"
    ))

    # ── Create with expanded values ───────────────────────────
    conn.execute(text(
        "ALTER TABLE xioflow_memory_nodes ADD CONSTRAINT ck_xfmn_action_type_allowed "
        "CHECK (action_type IN ("
        "'click', 'type', 'fill', 'extract_data', 'scroll_down', "
        "'navigate', 'wait', 'done', 'trigger_sub_workflow', "
        "'compute_node', 'conditional', "
        "'script', 'delay', 'http_request', "
        "'assertion', 'human_input', 'ssh_command', "
        "'llm_prompt', 'webhook_fire', "
        "'press', 'hover', 'select_option', 'check', 'uncheck'))"
    ))
    conn.execute(text(
        "ALTER TABLE xioflow_memory_nodes ADD CONSTRAINT ck_xfmn_recording_method_allowed "
        "CHECK (recording_method IN ("
        "'auto_learn', 'teacher_extension', "
        "'declarative_dag', 'mcp_chat', 'auto_trace'))"
    ))


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(text(
        "ALTER TABLE xioflow_memory_nodes "
        "DROP CONSTRAINT IF EXISTS ck_xfmn_recording_method_allowed"
    ))
    conn.execute(text(
        "ALTER TABLE xioflow_memory_nodes "
        "DROP CONSTRAINT IF EXISTS ck_xfmn_action_type_allowed"
    ))

    conn.execute(text(
        "ALTER TABLE xioflow_memory_nodes ADD CONSTRAINT ck_xfmn_action_type_allowed "
        "CHECK (action_type IN ("
        "'click', 'type', 'extract_data', 'scroll_down', "
        "'navigate', 'wait', 'done', 'trigger_sub_workflow', "
        "'compute_node', 'conditional', "
        "'script', 'delay', 'http_request', "
        "'assertion', 'human_input', 'ssh_command', "
        "'llm_prompt', 'webhook_fire'))"
    ))
    conn.execute(text(
        "ALTER TABLE xioflow_memory_nodes ADD CONSTRAINT ck_xfmn_recording_method_allowed "
        "CHECK (recording_method IN ("
        "'auto_learn', 'teacher_extension', 'declarative_dag', 'mcp_chat'))"
    ))
