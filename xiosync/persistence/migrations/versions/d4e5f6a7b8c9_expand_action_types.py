"""Expand xioflow_memory_nodes action_type CHECK constraint for universal workflows.

Adds: script, delay, http_request, assertion, human_input, ssh_command,
      llm_prompt, webhook_fire — enabling non-browser DAG workflows.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
"""
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop old constraint
    op.execute("""
        ALTER TABLE xioflow_memory_nodes
        DROP CONSTRAINT IF EXISTS ck_xfmn_action_type_allowed;
    """)
    # Create expanded constraint with all action types
    op.execute("""
        ALTER TABLE xioflow_memory_nodes
        ADD CONSTRAINT ck_xfmn_action_type_allowed
        CHECK (action_type IN (
            -- Browser action types:
            'click', 'type', 'extract_data', 'scroll_down',
            'navigate', 'wait', 'done',
            -- Control flow:
            'trigger_sub_workflow', 'compute_node', 'conditional',
            -- Engine-level (non-browser):
            'script', 'delay', 'http_request',
            -- Extended universal action types:
            'assertion', 'human_input', 'ssh_command',
            'llm_prompt', 'webhook_fire'
        ));
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE xioflow_memory_nodes
        DROP CONSTRAINT IF EXISTS ck_xfmn_action_type_allowed;
    """)
    op.execute("""
        ALTER TABLE xioflow_memory_nodes
        ADD CONSTRAINT ck_xfmn_action_type_allowed
        CHECK (action_type IN ('click', 'type', 'extract_data', 'scroll_down',
                               'navigate', 'wait', 'done', 'trigger_sub_workflow',
                               'compute_node', 'conditional'));
    """)
