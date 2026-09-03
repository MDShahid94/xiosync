"""XIOFLOW TemplateService scaffold — per-org isolated script library.

This is a scaffold. Full implementation in Phase 4.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext


@dataclass(frozen=True, slots=True)
class TemplateRecord:
    """Frozen snapshot of a workflow_templates row."""

    id: uuid.UUID
    organization_id: uuid.UUID | None
    project_id: uuid.UUID | None
    name: str
    slug: str
    description: str | None
    script_ref: str
    is_platform_global: bool


class TemplateService:
    """XIOFLOW template management service (scaffold).

    Full implementation deferred to Phase 4.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_templates(
        self,
        context: OrgContext,
        *,
        project_id: uuid.UUID | None = None,
    ) -> list[TemplateRecord]:
        """List templates available to this org (scaffold — always returns [])."""
        return []

    def get_template(
        self,
        context: OrgContext,
        template_id: uuid.UUID,
    ) -> TemplateRecord:
        """Fetch one template (scaffold — always raises NotImplementedError)."""
        raise NotImplementedError("TemplateService.get_template — Phase 4")
