"""XIOGRID PPPoE health check — runs as part of the worker ticker loop.

Called every 60s by the worker.
Checks all ASSIGNED and IDLE nodes across all active hosts.
Moves DOWN nodes to RECONNECTING.
Updates public IPs if they changed (pppd reconnected).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.subsystems.xiogrid.domain.pppoe import PPPoESlotState
from xiosync.subsystems.xiogrid.models.exit_node import PPPoEExitNode, PPPoEHost
from xiosync.subsystems.xiogrid.services.ssh_exec import vm_script

logger = logging.getLogger(__name__)

_LAST_RUN: datetime | None = None
_INTERVAL_S: float = 60.0  # run at most once per 60s


def tick_pppoe_health(session: Session) -> int:
    """Health-check PPPoE nodes. Returns count of nodes checked."""
    global _LAST_RUN

    now = datetime.now(UTC)
    if _LAST_RUN and (now - _LAST_RUN).total_seconds() < _INTERVAL_S:
        return 0

    _LAST_RUN = now

    # Only check nodes that are IDLE or ASSIGNED — skip DESTROYED/CONNECTING
    active_states = (PPPoESlotState.IDLE, PPPoESlotState.ASSIGNED, PPPoESlotState.RECONNECTING)
    nodes = session.scalars(
        select(PPPoEExitNode)
        .where(PPPoEExitNode.state.in_(active_states))
        .join(PPPoEHost, PPPoEExitNode.host_id == PPPoEHost.id)
        .where(PPPoEHost.state == "active")
        .order_by(PPPoEExitNode.last_health_check.asc().nullsfirst())
        .limit(50)  # check at most 50 per tick to avoid blocking the worker
    ).all()

    if not nodes:
        return 0

    checked = 0
    for node in nodes:
        host = session.get(PPPoEHost, node.host_id)
        if not host:
            continue
        try:
            result = vm_script(host, "check-slot.sh", node.ppp_slot, timeout=10)
            parts = result.stdout.split() if result.ok else []
            is_up = parts and parts[0] == "up"
            pub_ip = parts[1] if len(parts) > 1 else None

            node.last_health_check = now
            if is_up:
                if node.public_ip != pub_ip and pub_ip:
                    node.reconnect_count += 1
                    logger.info(
                        "pppoe_slot_ip_changed",
                        extra={"host": host.name, "slot": node.ppp_slot,
                               "old_ip": node.public_ip, "new_ip": pub_ip},
                    )
                node.public_ip = pub_ip
                node.last_seen = now
                if node.state == PPPoESlotState.RECONNECTING:
                    node.state = PPPoESlotState.IDLE
            else:
                if node.state == PPPoESlotState.IDLE:
                    node.state = PPPoESlotState.RECONNECTING
                    logger.warning(
                        "pppoe_slot_down",
                        extra={"host": host.name, "slot": node.ppp_slot},
                    )
                elif node.state == PPPoESlotState.RECONNECTING:
                    # Already reconnecting — if it's been >5 min, mark DOWN
                    if node.last_seen and (now - node.last_seen).total_seconds() > 300:
                        node.state = PPPoESlotState.DOWN
            checked += 1
        except Exception as exc:
            logger.warning(
                "pppoe_health_check_error",
                extra={"host": host.name if host else "?",
                       "slot": node.ppp_slot, "error": str(exc)},
            )

    if checked:
        session.flush()

    # Sync browser pool max_instances with live IDLE slot count
    try:
        reconcile_pool_capacity(session)
    except Exception as _re:
        logger.warning("pool_capacity_reconciliation_failed: %s", _re)

    return checked


def reconcile_pool_capacity(session: Any) -> int:
    """Sync browser_pools.max_instances with total IDLE PPPoE slot count.

    Returns the number of pools updated.

    PPPoE hosts and exit nodes are global infrastructure — they have no
    organization_id.  We count the total IDLE slots across all nodes and
    update every browser_pool's max_instances to that ceiling so the
    dispatcher never over-schedules into a pool whose proxy capacity has
    shrunk due to reconnects or failures.
    """
    from sqlalchemy import text

    # Count total idle PPPoE proxy slots (globally, not per-org)
    idle_slots = session.execute(
        text("""
            SELECT COUNT(*) AS idle_slots
            FROM   xiogrid_pppoe_exit_nodes
            WHERE  state = 'idle'
        """)
    ).scalar() or 0

    updated = session.execute(
        text("""
            UPDATE browser_pools
            SET    max_instances = :idle_slots,
                   updated_at    = now()
            WHERE  max_instances != :idle_slots
            RETURNING id
        """),
        {"idle_slots": idle_slots},
    ).rowcount

    if updated:
        session.commit()
    return updated

