"""WorkflowTemplate service — canonical home for workflow template management.

This module is the authoritative owner of WorkflowTemplateService.
The xiogrid/services/workflow_templates.py re-exports from here for
backward compatibility.

Two execution models coexist under one table (workflow_templates):

  template_type = 'script'
      Runs a .mjs file via Node.js subprocess.
      script_ref = relative path inside tools/workflows/
      This is a permanent, first-class organizational choice — ideal for
      stealth-critical operations (UC engine, TOTP, CDP cookie transfer).

  template_type = 'xioflow_dag'
      Runs via DAGExecutor + MemoryGraph.
      dag_domain + dag_root_intent point to the root XioflowMemoryNode.
      Self-healing, learnable, cross-worker consensus-driven.

Both types are equal citizens. Conversion between them is optional and
non-destructive — both templates coexist and can be compared.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.platform.ids import new_id

__all__ = [
    "WorkflowTemplateNotFoundError",
    "WorkflowTemplateRecord",
    "WorkflowTemplateService",
]


@dataclass(frozen=True, slots=True)
class WorkflowTemplateRecord:
    """Frozen snapshot of a workflow_templates row."""

    id: uuid.UUID
    organization_id: uuid.UUID | None
    project_id: uuid.UUID | None
    name: str
    slug: str
    description: str | None
    script_ref: str
    template_type: str          # 'script' | 'xioflow_dag' | extensible
    dag_domain: str | None
    dag_root_intent: str | None
    category: str | None
    config: dict[str, Any]
    is_platform_global: bool
    created_at: datetime
    updated_at: datetime | None


class WorkflowTemplateNotFoundError(ValueError):
    """Raised when a template cannot be found."""


def _to_record(row: Any) -> WorkflowTemplateRecord:
    return WorkflowTemplateRecord(
        id=row.id,
        organization_id=row.organization_id,
        project_id=row.project_id,
        name=row.name,
        slug=row.slug,
        description=row.description,
        script_ref=row.script_ref,
        template_type=row.template_type,
        dag_domain=row.dag_domain,
        dag_root_intent=row.dag_root_intent,
        category=row.category,
        config=row.config or {},
        is_platform_global=row.is_platform_global,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class WorkflowTemplateService:
    """Use-cases for workflow template management."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _model() -> Any:
        from xiosync.subsystems.xioflow.models.templates import WorkflowTemplate
        return WorkflowTemplate

    # ── write ────────────────────────────────────────────────────────────────

    def register_template(
        self,
        ctx: OrgContext,
        *,
        name: str,
        category: str | None = None,
        script_ref: str = "",
        template_type: str = "script",
        dag_domain: str | None = None,
        dag_root_intent: str | None = None,
        config: dict[str, Any] | None = None,
        project_id: uuid.UUID | None = None,
        is_platform_global: bool = False,
        description: str | None = None,
        steps: list[dict[str, Any]] | None = None,  # legacy compat, ignored
    ) -> WorkflowTemplateRecord:
        """Register or upsert a workflow template for this org."""
        M = self._model()
        slug = name.lower().replace(" ", "_").replace("-", "_")

        stmt = select(M).where(M.organization_id == ctx.organization_id, M.slug == slug)
        existing = self._session.execute(stmt).scalar_one_or_none()

        now = datetime.now(UTC)

        if existing is not None:
            existing.name = name
            existing.category = category
            existing.script_ref = script_ref
            existing.template_type = template_type
            existing.dag_domain = dag_domain
            existing.dag_root_intent = dag_root_intent
            existing.config = config or {}
            existing.description = description
            existing.updated_at = now
            self._session.flush()
            return _to_record(existing)

        row = M(
            id=new_id(),
            organization_id=ctx.organization_id,
            project_id=project_id,
            name=name,
            slug=slug,
            description=description,
            script_ref=script_ref,
            template_type=template_type,
            dag_domain=dag_domain,
            dag_root_intent=dag_root_intent,
            category=category,
            config=config or {},
            is_platform_global=is_platform_global,
            created_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return _to_record(row)

    def delete_template(self, ctx: OrgContext, template_id: uuid.UUID) -> None:
        M = self._model()
        stmt = select(M).where(M.id == template_id, M.organization_id == ctx.organization_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            raise WorkflowTemplateNotFoundError(str(template_id))
        self._session.delete(row)
        self._session.flush()

    # ── read ─────────────────────────────────────────────────────────────────

    def list_templates(
        self,
        ctx: OrgContext,
        *,
        project_id: uuid.UUID | None = None,
        category: str | None = None,
        include_platform_global: bool = True,
    ) -> list[WorkflowTemplateRecord]:
        M = self._model()
        conditions = [
            (M.organization_id == ctx.organization_id)
            | (M.is_platform_global == True)  # noqa: E712
        ]
        if project_id is not None:
            conditions.append((M.project_id == project_id) | (M.project_id.is_(None)))
        if category is not None:
            conditions.append(M.category == category)
        stmt = select(M).where(*conditions).order_by(M.name)
        rows = self._session.execute(stmt).scalars().all()
        return [_to_record(r) for r in rows]

    def get_template(self, ctx: OrgContext, template_id: uuid.UUID) -> WorkflowTemplateRecord:
        M = self._model()
        stmt = select(M).where(
            M.id == template_id,
            (M.organization_id == ctx.organization_id) | (M.is_platform_global == True),  # noqa
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            raise WorkflowTemplateNotFoundError(str(template_id))
        return _to_record(row)

    def get_template_by_slug(self, ctx: OrgContext, slug: str) -> WorkflowTemplateRecord:
        M = self._model()
        stmt = select(M).where(
            M.slug == slug,
            (M.organization_id == ctx.organization_id) | (M.is_platform_global == True),  # noqa
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            raise WorkflowTemplateNotFoundError(slug)
        return _to_record(row)
