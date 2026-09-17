"""XIOGRID PPPoE warm pool top-up — runs as part of the worker ticker loop.

Called every 120s by the worker.
For each active host, counts IDLE slots and provisions new ones until
warm_pool_target is reached (or max_slots is hit).

Provisions at most BATCH_SIZE slots per tick to avoid saturating the VM
SSH connection and pppd subsystem.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from xiosync.subsystems.xiogrid.domain.pppoe import PPPoESlotState
from xiosync.subsystems.xiogrid.models.exit_node import PPPoEExitNode, PPPoEHost
from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService

logger = logging.getLogger(__name__)

_LAST_RUN: datetime | None = None
_INTERVAL_S: float = 120.0   # run at most once per 2 minutes
BATCH_SIZE: int = 3          # max slots to provision per tick per host


def tick_pppoe_warmpool(session: Session) -> int:
    """Top up idle slot pool toward warm_pool_target. Returns slots provisioned."""
    global _LAST_RUN

    now = datetime.now(UTC)
    if _LAST_RUN and (now - _LAST_RUN).total_seconds() < _INTERVAL_S:
        return 0

    _LAST_RUN = now

    hosts = session.scalars(
        select(PPPoEHost).where(PPPoEHost.state == "active")
    ).all()

    total_provisioned = 0
    svc = PPPoENodeService(session)

    for host in hosts:
        try:
            idle_count = session.scalar(
                select(func.count(PPPoEExitNode.id)).where(
                    PPPoEExitNode.host_id == host.id,
                    PPPoEExitNode.state == PPPoESlotState.IDLE,
                )
            ) or 0

            deficit = host.warm_pool_target - idle_count
            if deficit <= 0:
                continue

            to_provision = min(deficit, BATCH_SIZE)
            logger.info(
                "pppoe_warmpool_top_up",
                extra={
                    "host": host.name,
                    "idle": idle_count,
                    "target": host.warm_pool_target,
                    "provisioning": to_provision,
                },
            )

            for _ in range(to_provision):
                # _next_free_slot is a private helper; use it via the service
                slot = svc._next_free_slot(host.id)
                try:
                    svc.provision_slot(None, host.id, slot)
                    total_provisioned += 1
                    logger.info(
                        "pppoe_warmpool_provisioned",
                        extra={"host": host.name, "slot": slot},
                    )
                except Exception as exc:
                    logger.warning(
                        "pppoe_warmpool_provision_error",
                        extra={"host": host.name, "slot": slot, "error": str(exc)},
                    )
                    break  # stop provisioning this host on first error

        except Exception as exc:
            logger.warning(
                "pppoe_warmpool_host_error",
                extra={"host": host.name if host else "?", "error": str(exc)},
            )

    return total_provisioned
