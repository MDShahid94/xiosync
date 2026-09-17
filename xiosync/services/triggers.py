"""XIOFlow Trigger Service — CRUD for xioflow_triggers.

Replaces the legacy WorkflowTrigger ORM (which pointed at a non-existent
``workflow_triggers`` table) with direct SQL against ``xioflow_triggers`` —
the table the ticker actually reads.

Schema (from migration 0029):
    id               UUID PK
    organization_id  UUID FK
    template_id      UUID FK → workflow_templates
    trigger_type     TEXT  ('cron' | 'event')
    cron_schedule    TEXT  — cron expression e.g. '0 */6 * * *'
    event_name       TEXT  — event topic e.g. 'session.expired'
    enabled          BOOL  DEFAULT true
    context_defaults JSONB — merged into run.context at fire time
    last_fired_at    TIMESTAMPTZ
    created_at       TIMESTAMPTZ DEFAULT now()
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.platform.ids import new_id

__all__ = [
    "TriggerNotFoundError",
    "TriggerRecord",
    "XioflowTriggerService",
    # Legacy alias kept so any surviving import doesn't crash
    "TriggerService",
]

_ALLOWED_TYPES = frozenset({"cron", "event"})


@dataclass(frozen=True, slots=True)
class TriggerRecord:
    """Frozen snapshot of an xioflow_triggers row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    template_id: uuid.UUID
    trigger_type: str            # 'cron' | 'event'
    cron_schedule: str | None    # e.g. '*/5 * * * *'
    event_name: str | None       # e.g. 'session.expired'
    enabled: bool
    context_defaults: dict[str, Any]
    last_fired_at: datetime | None
    created_at: datetime


class TriggerNotFoundError(ValueError):
    """Raised when the requested trigger does not exist in the org."""


def _row_to_record(row: Any) -> TriggerRecord:
    return TriggerRecord(
        id=uuid.UUID(str(row.id)),
        organization_id=uuid.UUID(str(row.organization_id)),
        template_id=uuid.UUID(str(row.template_id)),
        trigger_type=row.trigger_type,
        cron_schedule=row.cron_schedule,
        event_name=row.event_name,
        enabled=bool(row.enabled),
        context_defaults=dict(row.context_defaults or {}),
        last_fired_at=row.last_fired_at,
        created_at=row.created_at,
    )


class XioflowTriggerService:
    """Use cases for xioflow_triggers (XIOFLOW-native schema)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Create ────────────────────────────────────────────────────────────────

    def create_trigger(
        self,
        ctx: OrgContext,
        *,
        template_id: uuid.UUID,
        trigger_type: str,
        cron_schedule: str | None = None,
        event_name: str | None = None,
        context_defaults: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> TriggerRecord:
        """Create a new cron or event trigger linked to a template.

        Raises:
            ValueError: if trigger_type is invalid or required fields are missing.
        """
        if trigger_type not in _ALLOWED_TYPES:
            raise ValueError(f"trigger_type must be one of {sorted(_ALLOWED_TYPES)}")
        if trigger_type == "cron" and not cron_schedule:
            raise ValueError("cron_schedule is required for trigger_type='cron'")
        if trigger_type == "event" and not event_name:
            raise ValueError("event_name is required for trigger_type='event'")

        # Verify template belongs to this org
        tmpl = self._session.execute(
            text("SELECT id FROM workflow_templates WHERE id=:id AND organization_id=:org"),
            {"id": str(template_id), "org": str(ctx.organization_id)},
        ).fetchone()
        if not tmpl:
            raise ValueError(f"Template {template_id} not found in org {ctx.organization_id}")

        trigger_id = new_id()
        self._session.execute(
            text("""
                INSERT INTO xioflow_triggers
                  (id, organization_id, template_id, trigger_type,
                   cron_schedule, event_name, enabled, context_defaults, created_at)
                VALUES
                  (:id, :org, :tmpl, :ttype,
                   :cron, :event, :enabled, cast(:ctx as jsonb), now())
            """),
            {
                "id": str(trigger_id),
                "org": str(ctx.organization_id),
                "tmpl": str(template_id),
                "ttype": trigger_type,
                "cron": cron_schedule,
                "event": event_name,
                "enabled": enabled,
                "ctx": json.dumps(context_defaults or {}),
            },
        )
        self._session.commit()
        return self.get_trigger(ctx, trigger_id)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_trigger(self, ctx: OrgContext, trigger_id: uuid.UUID) -> TriggerRecord:
        row = self._session.execute(
            text("""
                SELECT id, organization_id, template_id, trigger_type,
                       cron_schedule, event_name, enabled, context_defaults,
                       last_fired_at, created_at
                FROM xioflow_triggers
                WHERE id = :id AND organization_id = :org
            """),
            {"id": str(trigger_id), "org": str(ctx.organization_id)},
        ).fetchone()
        if not row:
            raise TriggerNotFoundError(f"Trigger {trigger_id} not found")
        return _row_to_record(row)

    def list_triggers(
        self,
        ctx: OrgContext,
        *,
        trigger_type: str | None = None,
        enabled: bool | None = None,
        template_id: uuid.UUID | None = None,
    ) -> list[TriggerRecord]:
        where = ["organization_id = :org"]
        params: dict[str, Any] = {"org": str(ctx.organization_id)}

        if trigger_type:
            where.append("trigger_type = :ttype")
            params["ttype"] = trigger_type
        if enabled is not None:
            where.append("enabled = :enabled")
            params["enabled"] = enabled
        if template_id:
            where.append("template_id = :tmpl")
            params["tmpl"] = str(template_id)

        rows = self._session.execute(
            text(f"""
                SELECT id, organization_id, template_id, trigger_type,
                       cron_schedule, event_name, enabled, context_defaults,
                       last_fired_at, created_at
                FROM xioflow_triggers
                WHERE {" AND ".join(where)}
                ORDER BY created_at DESC
            """),
            params,
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    # ── Enable / Disable ──────────────────────────────────────────────────────

    def enable_trigger(self, ctx: OrgContext, trigger_id: uuid.UUID) -> TriggerRecord:
        return self._set_enabled(ctx, trigger_id, True)

    def disable_trigger(self, ctx: OrgContext, trigger_id: uuid.UUID) -> TriggerRecord:
        return self._set_enabled(ctx, trigger_id, False)

    def _set_enabled(
        self, ctx: OrgContext, trigger_id: uuid.UUID, enabled: bool
    ) -> TriggerRecord:
        result = self._session.execute(
            text("""
                UPDATE xioflow_triggers
                SET enabled = :enabled
                WHERE id = :id AND organization_id = :org
                RETURNING id
            """),
            {"enabled": enabled, "id": str(trigger_id), "org": str(ctx.organization_id)},
        ).fetchone()
        if not result:
            raise TriggerNotFoundError(f"Trigger {trigger_id} not found")
        self._session.commit()
        return self.get_trigger(ctx, trigger_id)

    # ── Delete ────────────────────────────────────────────────────────────────

    def delete_trigger(self, ctx: OrgContext, trigger_id: uuid.UUID) -> None:
        result = self._session.execute(
            text("""
                DELETE FROM xioflow_triggers
                WHERE id = :id AND organization_id = :org
                RETURNING id
            """),
            {"id": str(trigger_id), "org": str(ctx.organization_id)},
        ).fetchone()
        if not result:
            raise TriggerNotFoundError(f"Trigger {trigger_id} not found")
        self._session.commit()

    # ── Fire (manual) ─────────────────────────────────────────────────────────

    def fire_now(
        self,
        ctx: OrgContext,
        trigger_id: uuid.UUID,
        extra_context: dict[str, Any] | None = None,
    ) -> str:
        """Immediately enqueue a run for this trigger, return run_id."""
        rec = self.get_trigger(ctx, trigger_id)
        merged_ctx = {**rec.context_defaults, **(extra_context or {})}
        run_id = str(new_id())
        self._session.execute(
            text("""
                INSERT INTO xioflow_runs
                  (id, organization_id, template_id, trigger_id, state, context, started_at)
                VALUES
                  (:id, :org, :tmpl, :trig, 'PENDING', cast(:ctx as jsonb), now())
            """),
            {
                "id": run_id,
                "org": str(ctx.organization_id),
                "tmpl": str(rec.template_id),
                "trig": str(trigger_id),
                "ctx": json.dumps(merged_ctx),
            },
        )
        self._session.execute(
            text("UPDATE xioflow_triggers SET last_fired_at=now() WHERE id=:id"),
            {"id": str(trigger_id)},
        )
        self._session.commit()
        return run_id


# Legacy alias — prevents import crashes in any old code that imports TriggerService
TriggerService = XioflowTriggerService
