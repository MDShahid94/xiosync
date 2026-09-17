"""PPPoE host registration service.

Manages PPPoEHost records — create, list, update, ping.
Any new Mac Mini + VM pair is registered here before slots can be provisioned.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.persistence.models.operations import Operation
from xiosync.platform.ids import new_id
from xiosync.subsystems.xiogrid.models.exit_node import PPPoEHost
from xiosync.subsystems.xiogrid.services.ssh_exec import vm_ping

__all__ = ["PPPoEHostNotFoundError", "PPPoEHostRecord", "PPPoEHostService"]


@dataclass(frozen=True, slots=True)
class PPPoEHostRecord:
    id: uuid.UUID
    name: str
    description: str
    vm_ssh_user: str
    vm_ssh_host: str
    vm_ssh_port: int
    vm_scripts_dir: str
    pppoe_parent_iface: str
    pppoe_username: str
    tailscale_vm_ts_ip: str | None
    max_slots: int
    warm_pool_target: int
    state: str
    registered_at: datetime
    last_seen: datetime | None
    meta: dict[str, Any]


class PPPoEHostNotFoundError(ValueError):
    def __init__(self, host_id: uuid.UUID) -> None:
        super().__init__(f"PPPoE host {host_id} not found")


def _record(row: PPPoEHost) -> PPPoEHostRecord:
    return PPPoEHostRecord(
        id=row.id, name=row.name, description=row.description,
        vm_ssh_user=row.vm_ssh_user, vm_ssh_host=row.vm_ssh_host,
        vm_ssh_port=row.vm_ssh_port, vm_scripts_dir=row.vm_scripts_dir,
        pppoe_parent_iface=row.pppoe_parent_iface,
        pppoe_username=row.pppoe_username,
        tailscale_vm_ts_ip=row.tailscale_vm_ts_ip,
        max_slots=row.max_slots, warm_pool_target=row.warm_pool_target,
        state=row.state, registered_at=row.registered_at,
        last_seen=row.last_seen, meta=dict(row.meta),
    )


class PPPoEHostService:
    """Register and manage Mac Mini + VM pairs."""

    def __init__(self, session: Session) -> None:
        self._db = session

    def register(
        self,
        ctx: OrgContext,
        *,
        name: str,
        vm_ssh_host: str,
        pppoe_username: str,
        pppoe_password: str,
        description: str = "",
        vm_ssh_user: str = "karmantu",
        vm_ssh_port: int = 22,
        vm_scripts_dir: str = "/usr/local/bin/xiogrid",
        pppoe_parent_iface: str = "enp26s0",
        mac_oui_prefix: str = "00:50:56:cc",
        tailscale_vm_ts_ip: str | None = None,
        max_slots: int = 981,
        warm_pool_target: int = 50,
        meta: dict[str, Any] | None = None,
    ) -> PPPoEHostRecord:
        """Register a new Mac Mini + VM pair. SSH must be reachable."""
        now = datetime.now(UTC)
        row = PPPoEHost(
            id=new_id(),
            name=name,
            description=description,
            vm_ssh_user=vm_ssh_user,
            vm_ssh_host=vm_ssh_host,
            vm_ssh_port=vm_ssh_port,
            vm_scripts_dir=vm_scripts_dir,
            pppoe_parent_iface=pppoe_parent_iface,
            pppoe_username=pppoe_username,
            pppoe_password=pppoe_password,
            mac_oui_prefix=mac_oui_prefix,
            tailscale_vm_ts_ip=tailscale_vm_ts_ip,
            max_slots=max_slots,
            warm_pool_target=warm_pool_target,
            state="active",
            registered_at=now,
            meta=meta or {},
        )
        self._db.add(row)
        self._db.add(Operation(
            id=new_id(), organization_id=ctx.organization_id,
            actor_id=ctx.actor_id, operation="pppoe.host.register",
            trigger="api", initiated_by=ctx.actor_id,
            outcome="success", started_at=now, completed_at=now,
            rationale=f"Registered PPPoE host: {name} ({vm_ssh_host})",
        ))
        self._db.flush()
        return _record(row)

    def list_hosts(self, state: str | None = None) -> list[PPPoEHostRecord]:
        stmt = select(PPPoEHost)
        if state:
            stmt = stmt.where(PPPoEHost.state == state)
        return [_record(r) for r in self._db.scalars(stmt).all()]

    def get(self, host_id: uuid.UUID) -> PPPoEHostRecord:
        row = self._db.get(PPPoEHost, host_id)
        if not row:
            raise PPPoEHostNotFoundError(host_id)
        return _record(row)

    def update_ts_ip(self, host_id: uuid.UUID, tailscale_vm_ts_ip: str) -> PPPoEHostRecord:
        """Update the VM's Tailscale IP after `tailscale up` completes."""
        row = self._db.get(PPPoEHost, host_id)
        if not row:
            raise PPPoEHostNotFoundError(host_id)
        row.tailscale_vm_ts_ip = tailscale_vm_ts_ip
        self._db.flush()
        return _record(row)

    def set_state(self, host_id: uuid.UUID, state: str) -> PPPoEHostRecord:
        row = self._db.get(PPPoEHost, host_id)
        if not row:
            raise PPPoEHostNotFoundError(host_id)
        row.state = state
        self._db.flush()
        return _record(row)

    def ping(self, host_id: uuid.UUID) -> bool:
        """SSH liveness check — updates last_seen if reachable."""
        row = self._db.get(PPPoEHost, host_id)
        if not row:
            raise PPPoEHostNotFoundError(host_id)
        alive = vm_ping(row)
        if alive:
            row.last_seen = datetime.now(UTC)
            row.state = "active"
        else:
            row.state = "offline"
        self._db.flush()
        return alive
