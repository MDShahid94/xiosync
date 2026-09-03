"""0024 — Drop hardcoded state CHECK constraints (Gap 1.2).

Revision ID: 0024
Revises: 0023

Hardcoded CHECK constraints on ``state`` columns are a maintenance hazard:
every new lifecycle state (e.g. ``failed``, ``ready``, ``busy``) requires a
schema migration even when no structural change is needed.  The lifecycle state
vocabulary is already governed by the ``type_registry`` table seeded during
genesis — the CHECK constraints duplicate that source of truth and cause false
violations when the service layer evolves valid state machines.

Constraints dropped
-------------------
Table                  Constraint name (actual, from live DB)
---------------------  -------------------------------------------------
browser_sessions       ck_browser_sessions_ck_browser_sessions_state
mesh_networks          ck_mesh_networks_ck_mesh_networks_state
browser_pools          ck_browser_pools_ck_browser_pools_state
capabilities           ck_capabilities_state_allowed
plugin_installations   ck_plugin_installations_state_allowed

Write validation is now enforced at the service layer against the
``type_registry`` lifecycle_state vocabulary.  The ``_CORE_LIFECYCLE_STATES``
list in ``xiosync/services/bootstrap.py`` is updated in concert with this
migration to register all values the service layer uses.

Note: ``ck_resource_shares_ck_resource_shares_state_allowed`` is intentionally
kept — ``resource_shares.state`` has a tiny, stable vocabulary
(``active`` / ``revoked``) that is unlikely to expand, and the constraint
provides a useful fast-fail for invalid share states.
"""

from __future__ import annotations

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

# Mapping of (table, constraint_name) for state CHECK constraints to drop.
# Constraint names are the *actual* names as they exist in the live database
# (verified via information_schema.table_constraints).
_STATE_CONSTRAINTS: list[tuple[str, str]] = [
    ("browser_sessions", "ck_browser_sessions_ck_browser_sessions_state"),
    ("mesh_networks", "ck_mesh_networks_ck_mesh_networks_state"),
    ("browser_pools", "ck_browser_pools_ck_browser_pools_state"),
    ("capabilities", "ck_capabilities_state_allowed"),
    ("plugin_installations", "ck_plugin_installations_state_allowed"),
]

# Restore values for downgrade — must exactly match what the original
# migrations created (0011 for capabilities, 0009 for plugin_installations,
# 0019 for browser_* tables).
_RESTORE: list[tuple[str, str, str]] = [
    (
        "browser_sessions",
        "ck_browser_sessions_ck_browser_sessions_state",
        "state IN ('initializing', 'active', 'suspended', 'terminated', 'failed')",
    ),
    (
        "mesh_networks",
        "ck_mesh_networks_ck_mesh_networks_state",
        "state IN ('active', 'configuring', 'error', 'disabled')",
    ),
    (
        "browser_pools",
        "ck_browser_pools_ck_browser_pools_state",
        "state IN ('active', 'suspended', 'archived')",
    ),
    (
        "capabilities",
        "ck_capabilities_state_allowed",
        "state IN ('draft', 'active', 'deprecated')",
    ),
    (
        "plugin_installations",
        "ck_plugin_installations_state_allowed",
        "state IN ('pending_approval', 'approved', 'active', 'suspended', 'revoked')",
    ),
]


def upgrade() -> None:
    for table, constraint in _STATE_CONSTRAINTS:
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}"
        )


def downgrade() -> None:
    for table, constraint, clause in _RESTORE:
        # Re-add constraint only if it doesn't already exist (idempotent).
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {constraint} CHECK ({clause})"
        )
