"""Append-only events privilege boundary & versioned memory (H6).

Revision ID: 0006; Revises: 0005.

Phase 2 Step 4 (doc 10) closes the H6 remediation (doc 03 INV-EVENT-1/INV-MEM-2,
doc 06 §6). Two guarantees are hardened here, at the database:

**Events — append-only as a *privilege boundary*, not only a trigger.**
Revision 0004 already installs the ``trg_events_append_only`` BEFORE UPDATE OR
DELETE trigger, so a destructive statement raises regardless of who issues it.
Doc 06 INV-EVENT-DB-1 additionally requires that the *application role never
even holds* UPDATE/DELETE on ``events`` — "immutability is a privilege boundary,
not a code convention". PostgreSQL grants no table privileges to ``PUBLIC`` by
default, so this migration makes that posture explicit and self-documenting by
revoking UPDATE/DELETE on ``events`` from ``PUBLIC``. A correctly provisioned
application role is therefore granted **only** ``SELECT, INSERT`` on ``events``
(proven in ``tests/integration/test_events_immutability.py``); the 0004 trigger
remains the defense-in-depth backstop for any over-privileged role.

**Memory — versioned, never overwritten (INV-MEM-2 / doc 06 §6).**
A ``memory`` row is immutable once written, with exactly one permitted mutation:
setting ``superseded_by`` from ``NULL`` to the id of its successor version,
exactly once. Any other column change, any repointing of an already-superseded
row, any attempt to clear ``superseded_by``, and every ``DELETE`` are rejected
by the ``trg_memory_versioning`` trigger. New knowledge is a new row
(``version + 1``) that the prior row is pointed at — originals are always
retained. ``DELETE`` on ``memory`` is likewise revoked from ``PUBLIC``.

Reversibility (INV-MIG-3): downgrade drops the memory trigger and its function.
The ``REVOKE ... FROM PUBLIC`` statements only re-assert PostgreSQL's default
no-privilege posture for ``PUBLIC`` (nothing was granted to it before), so they
have no inverse to restore; re-running upgrade re-issues them idempotently and
the empty-DB ``upgrade -> downgrade -> upgrade`` round-trip stays clean.
"""

from __future__ import annotations

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- Events: privilege boundary (doc 06 INV-EVENT-DB-1) -----------------
    # The 0004 trigger already blocks UPDATE/DELETE for any role; this makes the
    # grant posture explicit so a well-provisioned app role holds SELECT/INSERT
    # only and never even carries the destructive privileges.
    op.execute("REVOKE UPDATE, DELETE ON TABLE events FROM PUBLIC")

    # -- Memory: versioned, append-except-for-supersession (INV-MEM-2) ------
    op.execute(
        """
        CREATE FUNCTION enforce_memory_versioning() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'memory is versioned and append-only; rows are never deleted';
            END IF;

            -- The only legal mutation is setting superseded_by exactly once,
            -- from NULL to the successor version's id (doc 06 §6, INV-MEM-2).
            IF OLD.superseded_by IS NOT NULL THEN
                RAISE EXCEPTION
                    'memory row % is already superseded and is immutable', OLD.id;
            END IF;
            IF NEW.superseded_by IS NULL THEN
                RAISE EXCEPTION
                    'memory is append-only; the only permitted update is '
                    'setting superseded_by to a successor version';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
               OR NEW.owner_actor_id IS DISTINCT FROM OLD.owner_actor_id
               OR NEW.kind IS DISTINCT FROM OLD.kind
               OR NEW.content IS DISTINCT FROM OLD.content
               OR NEW.embedding_ref IS DISTINCT FROM OLD.embedding_ref
               OR NEW.visibility IS DISTINCT FROM OLD.visibility
               OR NEW.provenance IS DISTINCT FROM OLD.provenance
               OR NEW.version IS DISTINCT FROM OLD.version
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION
                    'memory rows are immutable except for the superseded_by pointer';
            END IF;

            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_memory_versioning BEFORE UPDATE OR DELETE ON memory "
        "FOR EACH ROW EXECUTE FUNCTION enforce_memory_versioning()"
    )
    # Memory keeps UPDATE (to set superseded_by) but never DELETE.
    op.execute("REVOKE DELETE ON TABLE memory FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_memory_versioning ON memory")
    op.execute("DROP FUNCTION enforce_memory_versioning()")
    # The REVOKE ... FROM PUBLIC statements above only re-asserted PostgreSQL's
    # default (PUBLIC never held these privileges), so there is nothing to
    # restore on downgrade.
