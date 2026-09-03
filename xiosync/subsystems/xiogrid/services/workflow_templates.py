"""Workflow Template Service."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.ontology import TypeRegistry
from xiosync.persistence.models.workflows import Workflow
from xiosync.platform.ids import new_id
from xiosync.domain.workflows import WORKFLOW_STATE_DRAFT


@dataclass(frozen=True, slots=True)
class TemplateRecord:
    id: uuid.UUID
    name: str
    category: str
    steps: list[dict[str, Any]]
    config: dict[str, Any]


class WorkflowTemplateService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def register_template(
        self,
        ctx: OrgContext,
        *,
        name: str,
        category: str,
        steps: list[dict[str, Any]],
        config: dict[str, Any],
    ) -> TemplateRecord:
        template_id = new_id()
        definition = {"steps": steps, "config": config}
        row = TypeRegistry(
            id=template_id,
            organization_id=ctx.organization_id,
            namespace="workflow_templates",
            category=category,
            value=name,
            version=1,
            state="active",
            definition=definition,
        )
        self._session.add(row)
        self._session.flush()
        return TemplateRecord(
            id=row.id,
            name=row.value,
            category=row.category,
            steps=steps,
            config=config,
        )

    def list_templates(self, ctx: OrgContext, *, category: str) -> list[TemplateRecord]:
        rows = self._session.scalars(
            select(TypeRegistry).where(
                TypeRegistry.organization_id == ctx.organization_id,
                TypeRegistry.namespace == "workflow_templates",
                TypeRegistry.category == category,
                TypeRegistry.state == "active",
            )
        ).all()
        return [
            TemplateRecord(
                id=r.id,
                name=r.value,
                category=r.category,
                steps=r.definition.get("steps", []),
                config=r.definition.get("config", {}),
            )
            for r in rows
        ]

    def instantiate_template(
        self, ctx: OrgContext, *, template_id: uuid.UUID, params: dict[str, Any]
    ) -> uuid.UUID:
        row = self._session.scalar(
            select(TypeRegistry).where(
                TypeRegistry.organization_id == ctx.organization_id,
                TypeRegistry.id == template_id,
            )
        )
        if not row:
            raise ValueError(f"Template {template_id} not found")

        spec = {
            "nodes": row.definition.get("steps", []),
            "edges": [],
            "params": params,
        }
        
        workflow_id = new_id()
        self._session.add(
            Workflow(
                id=workflow_id,
                organization_id=ctx.organization_id,
                name=f"{row.value} (Instance)",
                version=1,
                spec=spec,
                state=WORKFLOW_STATE_DRAFT,
                created_by=ctx.actor_id,
            )
        )
        self._session.flush()
        return workflow_id
