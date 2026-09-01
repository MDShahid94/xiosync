"""Tenant-scoped persistence boundary for authorization decisions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.authorization import Capability, Event, Grant
from xiosync.persistence.models.identity import Actor, Organization
from xiosync.platform.ids import new_id


@dataclass(frozen=True)
class ActorRecord:
    id: uuid.UUID
    organization_id: uuid.UUID
    state: str
    trust_tier: str


@dataclass(frozen=True)
class OrganizationRecord:
    id: uuid.UUID
    state: str


@dataclass(frozen=True)
class GrantRecord:
    id: uuid.UUID
    organization_id: uuid.UUID
    actor_id: uuid.UUID
    capability: str
    state: str
    constraints: dict[str, Any]
    expires_at: datetime | None


class AuthorizationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_actor(self, context: OrgContext) -> ActorRecord | None:
        row = self._session.scalar(
            select(Actor).where(
                Actor.organization_id == context.organization_id, Actor.id == context.actor_id
            )
        )
        return (
            None
            if row is None
            else ActorRecord(row.id, row.organization_id, row.state, row.trust_tier)
        )

    def get_organization(self, context: OrgContext) -> OrganizationRecord | None:
        row = self._session.scalar(
            select(Organization).where(Organization.id == context.organization_id)
        )
        return None if row is None else OrganizationRecord(row.id, row.state)

    def list_grants(self, context: OrgContext, capability_name: str) -> list[GrantRecord]:
        rows = self._session.execute(
            select(Grant, Capability.name)
            .join(
                Capability,
                (Capability.id == Grant.capability_id)
                & (Capability.organization_id == Grant.organization_id),
            )
            .where(
                Grant.organization_id == context.organization_id,
                Grant.actor_id == context.actor_id,
                Capability.name == capability_name,
            )
        ).all()
        return [
            GrantRecord(
                g.id, g.organization_id, g.actor_id, name, g.state, g.constraints, g.expires_at
            )
            for g, name in rows
        ]

    def add_policy_event(self, context: OrgContext, payload: dict[str, Any]) -> uuid.UUID:
        event_id = new_id()
        self._session.add(
            Event(
                id=event_id,
                organization_id=context.organization_id,
                actor_id=context.actor_id,
                event_type="policy_decision",
                payload=payload,
            )
        )
        self._session.flush()
        return event_id
