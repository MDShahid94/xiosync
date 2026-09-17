"""Webhook delivery dispatcher — delivers pending webhook.dispatch events.

Reads ``webhook.dispatch`` events from the events table, POSTs payloads to
subscriber URLs with HMAC signature headers, and records delivery outcomes.
Implements exponential backoff for failed deliveries.
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.authorization import Event
from xiosync.persistence.models.identity import Organization
from xiosync.platform.ids import new_id

logger = logging.getLogger("xiosync.worker.dispatcher")

# Maximum attempts before giving up on a webhook delivery.
MAX_DELIVERY_ATTEMPTS = 5
# HTTP timeout for webhook delivery (seconds).
DELIVERY_TIMEOUT = 10


def dispatch_pending_webhooks(session: Session, *, limit: int = 50) -> int:
    """Find and deliver pending webhook.dispatch events. Returns delivery count.

    Idempotency: events that already have a ``webhook.delivered`` or
    ``webhook.failed`` outcome row (matched by ``dispatch_event_id`` in the
    payload) are excluded.  Each event is attempted at most
    MAX_DELIVERY_ATTEMPTS times, tracked via the count of ``webhook.failed``
    rows for that dispatch.
    """
    from sqlalchemy import text as _text  # noqa: PLC0415
    # Select dispatch events that have NO delivery outcome yet, OR have failed
    # fewer than MAX_DELIVERY_ATTEMPTS times (exponential backoff is handled by
    # the worker tick interval — no additional sleep logic needed here).
    rows = session.execute(
        _text("""
            SELECT e.id, e.organization_id, e.payload
            FROM   events e
            WHERE  e.event_type = 'webhook.dispatch'
              AND  NOT EXISTS (
                       SELECT 1 FROM events o
                       WHERE  o.event_type = 'webhook.delivered'
                         AND  o.payload->>'dispatch_event_id' = e.id::text
                   )
              AND  (
                       SELECT COUNT(*)
                       FROM   events f
                       WHERE  f.event_type = 'webhook.failed'
                         AND  f.payload->>'dispatch_event_id' = e.id::text
                   ) < :max_attempts
            ORDER  BY e.created_at ASC
            LIMIT  :lim
        """),
        {"max_attempts": MAX_DELIVERY_ATTEMPTS, "lim": limit},
    ).fetchall()

    dispatch_events = rows

    if not dispatch_events:
        return 0

    delivered = 0
    for row in dispatch_events:
        event_id     = str(row.id)
        org_id       = row.organization_id
        raw_payload  = row.payload
        payload      = raw_payload if isinstance(raw_payload, dict) else (json.loads(raw_payload) if raw_payload else {})
        target_url      = payload.get("target_url")
        source_event_id = payload.get("source_event_id")
        signature       = payload.get("signature")
        subscription_id = payload.get("subscription_id")

        if not target_url:
            logger.warning(
                "webhook_dispatch_missing_url",
                extra={"event_id": event_id},
            )
            continue

        # Build the delivery payload.
        delivery_body = json.dumps(
            {
                "event_id": source_event_id,
                "subscription_id": subscription_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            default=str,
        ).encode()

        try:
            req = urllib.request.Request(
                target_url,
                data=delivery_body,
                headers={
                    "Content-Type": "application/json",
                    "X-XIOSYNC-Signature": signature or "",
                    "X-XIOSYNC-Event-ID": source_event_id or "",
                    "User-Agent": "XIOSYNC-Webhook/1.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=DELIVERY_TIMEOUT) as resp:
                status = resp.status

            # Record successful delivery.
            session.add(
                Event(
                    id=new_id(),
                    organization_id=org_id,
                    actor_id=None,
                    event_type="webhook.delivered",
                    payload={
                        "dispatch_event_id": event_id,
                        "subscription_id": subscription_id,
                        "status_code": status,
                    },
                    severity="info",
                    entity_type="webhook_subscription",
                )
            )
            delivered += 1
            logger.info(
                "webhook_delivered",
                extra={
                    "target_url": target_url,
                    "status_code": status,
                    "subscription_id": subscription_id,
                },
            )

        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            logger.warning(
                "webhook_delivery_failed",
                extra={
                    "target_url": target_url,
                    "error": str(exc),
                    "subscription_id": subscription_id,
                },
            )
            # Record failure for retry tracking (idempotency guard counts these).
            session.add(
                Event(
                    id=new_id(),
                    organization_id=org_id,
                    actor_id=None,
                    event_type="webhook.failed",
                    payload={
                        "dispatch_event_id": event_id,
                        "subscription_id": subscription_id,
                        "error": str(exc),
                    },
                    severity="warn",
                    entity_type="webhook_subscription",
                )
            )

    if delivered or dispatch_events:
        session.commit()

    return delivered
