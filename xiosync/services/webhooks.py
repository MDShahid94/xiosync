"""Webhook subscription management and event delivery (Gap R-2).

``WebhookService`` manages outbound webhook subscriptions. The platform
generates signing secrets and delivers events by POSTing to configured URLs,
signing the payload with HMAC-SHA256.

Actual delivery is asynchronous (via a background worker or queue); this
service handles subscription CRUD and signing secret generation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.webhooks import WebhookSubscription
from xiosync.platform.ids import new_id

__all__ = [
    "WebhookNotFoundError",
    "WebhookRecord",
    "WebhookService",
    "sign_payload",
]


@dataclass(frozen=True, slots=True)
class WebhookRecord:
    """Frozen snapshot of a ``webhook_subscriptions`` row."""

    id: uuid.UUID
    organization_id: uuid.UUID
    url: str
    event_types: list[str]
    signing_secret: str
    state: str
    headers: dict[str, Any]
    created_at: datetime
    updated_at: datetime | None


class WebhookNotFoundError(ValueError):
    """Raised when the requested webhook subscription does not exist."""


def _record(row: WebhookSubscription) -> WebhookRecord:
    return WebhookRecord(
        id=row.id,
        organization_id=row.organization_id,
        url=row.url,
        event_types=list(row.event_types),
        signing_secret=row.signing_secret,
        state=row.state,
        headers=dict(row.headers),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def sign_payload(secret: str, payload: dict[str, Any]) -> str:
    """Sign a webhook payload with HMAC-SHA256, returning the hex digest.

    The consumer verifies: ``hmac.compare_digest(sign_payload(secret, body), sig_header)``.
    """
    body = json.dumps(payload, sort_keys=True, default=str).encode()
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WebhookService:
    """Use cases for outbound webhook subscriptions (Gap R-2)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _generate_signing_secret() -> str:
        """Generate a cryptographically secure signing secret."""
        return secrets.token_urlsafe(32)

    def create_subscription(
        self,
        context: OrgContext,
        *,
        url: str,
        event_types: list[str],
        headers: dict[str, Any] | None = None,
    ) -> WebhookRecord:
        """Create a new webhook subscription with a platform-generated signing secret."""
        sub_id = new_id()
        signing_secret = self._generate_signing_secret()
        row = WebhookSubscription(
            id=sub_id,
            organization_id=context.organization_id,
            url=url,
            event_types=event_types,
            signing_secret=signing_secret,
            headers=headers or {},
        )
        self._session.add(row)
        self._session.flush()
        return _record(row)

    def get_subscription(
        self,
        context: OrgContext,
        subscription_id: uuid.UUID,
    ) -> WebhookRecord:
        """Fetch one subscription, or raise ``WebhookNotFoundError``."""
        row = self._session.scalar(
            select(WebhookSubscription).where(
                WebhookSubscription.organization_id == context.organization_id,
                WebhookSubscription.id == subscription_id,
            )
        )
        if row is None:
            raise WebhookNotFoundError(
                f"webhook {subscription_id} not found in org {context.organization_id}"
            )
        return _record(row)

    def list_subscriptions(
        self,
        context: OrgContext,
        *,
        state: str | None = None,
        limit: int = 50,
    ) -> list[WebhookRecord]:
        """List webhook subscriptions in this org."""
        stmt = (
            select(WebhookSubscription)
            .where(WebhookSubscription.organization_id == context.organization_id)
            .order_by(WebhookSubscription.created_at.desc())
            .limit(limit)
        )
        if state is not None:
            stmt = stmt.where(WebhookSubscription.state == state)
        rows = self._session.scalars(stmt).all()
        return [_record(row) for row in rows]

    def pause_subscription(
        self,
        context: OrgContext,
        subscription_id: uuid.UUID,
    ) -> WebhookRecord:
        """Pause a webhook subscription."""
        row = self._session.scalar(
            select(WebhookSubscription).where(
                WebhookSubscription.organization_id == context.organization_id,
                WebhookSubscription.id == subscription_id,
            )
        )
        if row is None:
            raise WebhookNotFoundError(
                f"webhook {subscription_id} not found in org {context.organization_id}"
            )
        row.state = "paused"
        self._session.flush()
        return _record(row)

    def get_matching_subscriptions(
        self,
        context: OrgContext,
        event_type: str,
    ) -> list[WebhookRecord]:
        """Find all active subscriptions that listen for ``event_type``."""
        stmt = (
            select(WebhookSubscription)
            .where(
                WebhookSubscription.organization_id == context.organization_id,
                WebhookSubscription.state == "active",
            )
        )
        rows = self._session.scalars(stmt).all()
        return [
            _record(row) for row in rows
            if event_type in row.event_types or "*" in row.event_types
        ]
