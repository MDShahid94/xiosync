"""PPPoE exit node lifecycle service.

Full slot management: provision → assign → release → destroy → health check.
Multi-host aware: pick from any active host's pool.
All VM operations go through ssh_exec.vm_script(host, ...) — no hardcoded IPs.
"""
from __future__ import annotations

import concurrent.futures
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from xiosync.domain.context import OrgContext
from xiosync.platform.ids import new_id
from xiosync.subsystems.xiogrid.domain.pppoe import (
    PPPoECeilings,
    PPPoESlotState,
    profile_name_for_slot,
)
from xiosync.subsystems.xiogrid.models.exit_node import (
    FingerprintProfile,
    PPPoEExitNode,
    PPPoEHost,
)
from xiosync.subsystems.xiogrid.services.ssh_exec import vm_script

__all__ = ["PPPoENodeService", "PPPoESlotRecord", "FingerprintRecord"]


@dataclass(frozen=True, slots=True)
class PPPoESlotRecord:
    id: uuid.UUID
    host_id: uuid.UUID
    host_name: str
    ppp_slot: int
    state: str
    public_ip: str | None
    cgnat_ip: str | None
    proxy_url: str | None
    proxy_port: int | None
    proxy_state: str | None
    assigned_worker_ts_ip: str | None
    assigned_session_id: str | None
    fingerprint_profile_name: str
    total_sessions_served: int
    reconnect_count: int


@dataclass(frozen=True, slots=True)
class FingerprintRecord:
    id: uuid.UUID
    name: str
    os: str
    cores: int
    ram_gb: int
    webgl_renderer: str
    platform: str
    screen_width: int
    screen_height: int
    canvas_seed: int
    audio_seed: int
    ua_template: str
    ch_platform: str
    ch_version: str
    ch_arch: str
    dpr: float
    cam_name: str
    is_mobile: bool


class PPPoENodeService:
    """Manages PPPoE slot lifecycle across all registered hosts."""

    def __init__(self, session: Session) -> None:
        self._db = session

    # ─── Provisioning ────────────────────────────────────────────────

    def provision_slot(
        self,
        ctx: OrgContext,
        host_id: uuid.UUID,
        slot: int,
    ) -> PPPoESlotRecord:
        """Create macvlan + pppd on VM for a given slot. Blocks until IP."""
        host = self._get_host(host_id)
        if not (0 <= slot < host.max_slots):
            raise ValueError(f"Slot {slot} out of range 0-{host.max_slots - 1}")

        existing = self._db.scalar(
            select(PPPoEExitNode).where(
                PPPoEExitNode.host_id == host_id,
                PPPoEExitNode.ppp_slot == slot,
            )
        )
        if existing and existing.state not in (
            PPPoESlotState.DOWN, PPPoESlotState.DESTROYED
        ):
            raise ValueError(
                f"Slot {slot} on host {host.name!r} already in state {existing.state!r}"
            )

        result = vm_script(host, "create-slot.sh", slot, timeout=50)
        result.require_ok()

        # Script outputs: "up <public_ip> <cgnat_ip> <proxy_url>"
        #              or "connecting unknown unknown none"
        parts = result.stdout.split()
        state     = PPPoESlotState.IDLE if parts and parts[0] == "up" else PPPoESlotState.CONNECTING
        pub_ip    = parts[1] if len(parts) > 1 and parts[1] != "unknown" else None
        cgnat     = parts[2] if len(parts) > 2 and parts[2] != "unknown" else None
        proxy_url = parts[3] if len(parts) > 3 and parts[3] not in ("none", "unknown") else None
        proxy_port = int(proxy_url.split(":")[-1]) if proxy_url else None

        profile_name = profile_name_for_slot(slot)
        profile = self._db.scalar(
            select(FingerprintProfile).where(FingerprintProfile.name == profile_name)
        )
        if not profile:
            raise RuntimeError(f"Fingerprint profile {profile_name!r} not seeded")

        now = datetime.now(UTC)
        if existing:
            existing.state     = state
            existing.public_ip = pub_ip
            existing.cgnat_ip  = cgnat
            existing.fingerprint_profile_id = profile.id
            existing.last_seen  = now
            existing.proxy_url  = proxy_url
            existing.proxy_port = proxy_port
            existing.proxy_state = "running" if proxy_url else None
            node = existing
        else:
            node = PPPoEExitNode(
                id=new_id(),
                host_id=host_id,
                ppp_slot=slot,
                fingerprint_profile_id=profile.id,
                state=state,
                public_ip=pub_ip,
                cgnat_ip=cgnat,
                last_seen=now,
                proxy_url=proxy_url,
                proxy_port=proxy_port,
                proxy_state="running" if proxy_url else None,
            )
            self._db.add(node)

        self._db.flush()
        return self._to_record(node, host, profile)

    def provision_batch(
        self,
        ctx: OrgContext,
        host_id: uuid.UUID,
        slots: list[int],
        batch_size: int = 25,
        batch_pause_s: float = 2.0,
    ) -> list[PPPoESlotRecord]:
        """Provision N slots. Batched in waves of 25 to respect BRAS pacing."""
        import time
        records: list[PPPoESlotRecord] = []
        for i in range(0, len(slots), batch_size):
            batch = slots[i : i + batch_size]
            with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as ex:
                futs = {
                    ex.submit(self.provision_slot, ctx, host_id, s): s
                    for s in batch
                }
                for fut in concurrent.futures.as_completed(futs):
                    slot = futs[fut]
                    try:
                        records.append(fut.result())
                    except Exception as e:
                        # Log but continue — partial success is acceptable
                        print(f"[WARN] provision_slot({slot}) failed: {e}")
            if i + batch_size < len(slots):
                time.sleep(batch_pause_s)
        return records

    def destroy_slot(self, host_id: uuid.UUID, slot: int) -> None:
        """Kill SOCKS5 proxy, then kill pppd + delete macvlan on VM."""
        host = self._get_host(host_id)
        # Stop proxy first (best-effort — ignore if already dead)
        vm_script(host, "stop-proxy.sh", slot, timeout=8)
        # Tear down pppd + macvlan
        vm_script(host, "destroy-slot.sh", slot, timeout=15).require_ok()
        self._db.execute(
            update(PPPoEExitNode)
            .where(
                PPPoEExitNode.host_id == host_id,
                PPPoEExitNode.ppp_slot == slot,
            )
            .values(
                state=PPPoESlotState.DESTROYED,
                public_ip=None,
                cgnat_ip=None,
                proxy_url=None,
                proxy_port=None,
                proxy_state=None,
                assigned_worker_ts_ip=None,
                assigned_session_id=None,
                assigned_at=None,
            )
        )

    # ─── Assignment ──────────────────────────────────────────────────

    def assign_to_worker(
        self,
        host_id: uuid.UUID,
        slot: int,
        worker_ts_ip: str,
        session_id: str,
    ) -> PPPoESlotRecord:
        """Add policy routing rule on VM and mark slot as assigned."""
        host = self._get_host(host_id)
        node = self._get_node(host_id, slot)

        if node.state != PPPoESlotState.IDLE:
            raise ValueError(
                f"Slot {slot} on {host.name!r} not idle (state={node.state!r})"
            )

        vm_script(host, "assign-route.sh", worker_ts_ip, slot, timeout=8).require_ok()

        now = datetime.now(UTC)
        node.state = PPPoESlotState.ASSIGNED
        node.assigned_worker_ts_ip = worker_ts_ip
        node.assigned_session_id = session_id
        node.assigned_at = now
        node.total_sessions_served += 1
        self._db.flush()

        profile = self._db.get(FingerprintProfile, node.fingerprint_profile_id)
        return self._to_record(node, host, profile)

    def release_from_worker(self, host_id: uuid.UUID, slot: int) -> PPPoESlotRecord:
        """Remove routing rule and return slot to idle pool."""
        host = self._get_host(host_id)
        node = self._get_node(host_id, slot)

        if node.assigned_worker_ts_ip:
            vm_script(
                host, "release-route.sh", node.assigned_worker_ts_ip, slot, timeout=8
            )

        node.state = PPPoESlotState.IDLE
        node.assigned_worker_ts_ip = None
        node.assigned_session_id = None
        node.assigned_at = None
        self._db.flush()

        profile = self._db.get(FingerprintProfile, node.fingerprint_profile_id)
        return self._to_record(node, host, profile)

    def acquire_any(
        self,
        ctx: OrgContext,
        session_id: str,
        worker_ts_ip: str,
        preferred_host_id: uuid.UUID | None = None,
    ) -> PPPoESlotRecord:
        """Acquire LRU idle slot from any active host and assign it to the worker.

        If preferred_host_id is given, try that host first.
        Falls back to any active host if preferred is full.
        Auto-provisions on demand if warm pool is exhausted — then assigns
        immediately so the returned record is always in ASSIGNED state.
        """
        node, host = self._pick_idle_node(preferred_host_id)

        if node is None:
            # Warm pool empty — provision on demand from first available host
            host = self._db.scalar(
                select(PPPoEHost).where(PPPoEHost.state == "active").limit(1)
            )
            if not host:
                raise RuntimeError("No active PPPoE hosts registered")
            slot = self._next_free_slot(host.id)
            # Provision — blocks until pppd connects and slot is IDLE
            self.provision_slot(ctx, host.id, slot)
            # Immediately assign to the requesting worker
            return self.assign_to_worker(host.id, slot, worker_ts_ip, session_id)

        return self.assign_to_worker(host.id, node.ppp_slot, worker_ts_ip, session_id)

    # ─── Health ──────────────────────────────────────────────────────

    def health_check_slot(
        self, host_id: uuid.UUID, slot: int
    ) -> dict[str, Any]:
        host = self._get_host(host_id)
        result = vm_script(host, "check-slot.sh", slot, timeout=12)

        parts = result.stdout.split() if result.ok else []
        state  = "up"   if parts and parts[0] == "up" else "down"
        pub_ip = parts[1] if len(parts) > 1 else None

        node = self._db.scalar(
            select(PPPoEExitNode).where(
                PPPoEExitNode.host_id == host_id,
                PPPoEExitNode.ppp_slot == slot,
            )
        )
        if node:
            now = datetime.now(UTC)
            node.last_health_check = now
            if state == "up":
                if node.public_ip != pub_ip and pub_ip:
                    node.reconnect_count += 1  # IP changed → pppd reconnected
                node.public_ip = pub_ip
                node.last_seen = now
                if node.state in (
                    PPPoESlotState.RECONNECTING,
                    PPPoESlotState.CONNECTING,
                    PPPoESlotState.DOWN,
                ):
                    node.state = PPPoESlotState.IDLE
            else:
                if node.state == PPPoESlotState.IDLE:
                    node.state = PPPoESlotState.RECONNECTING
                elif node.state not in (PPPoESlotState.ASSIGNED,):
                    node.state = PPPoESlotState.DOWN
            self._db.flush()

        return {"state": state, "public_ip": pub_ip, "slot": slot}

    def health_check_host(self, host_id: uuid.UUID) -> dict[int, dict[str, Any]]:
        """Health-check all non-destroyed slots on a specific host (parallel)."""
        nodes = self._db.scalars(
            select(PPPoEExitNode).where(
                PPPoEExitNode.host_id == host_id,
                PPPoEExitNode.state != PPPoESlotState.DESTROYED,
            )
        ).all()
        results: dict[int, dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
            futs = {
                ex.submit(self.health_check_slot, host_id, n.ppp_slot): n.ppp_slot
                for n in nodes
            }
            for fut in concurrent.futures.as_completed(futs):
                slot_n = futs[fut]
                try:
                    results[slot_n] = fut.result()
                except Exception as e:
                    results[slot_n] = {"state": "error", "public_ip": None, "error": str(e)}
        return results

    def health_check_all_hosts(self) -> dict[str, dict[int, dict[str, Any]]]:
        """Health-check every active host in the ecosystem."""
        hosts = self._db.scalars(
            select(PPPoEHost).where(PPPoEHost.state == "active")
        ).all()
        all_results: dict[str, dict[int, dict[str, Any]]] = {}
        for host in hosts:
            all_results[str(host.id)] = self.health_check_host(host.id)
        return all_results

    def rotate_ip(self, host_id: uuid.UUID, slot: int) -> dict[str, Any]:
        """Force pppd reconnect on VM → new public IP. Blocks ~30s."""
        host = self._get_host(host_id)
        vm_script(host, "rotate-slot.sh", slot, timeout=65)
        return self.health_check_slot(host_id, slot)

    # ─── Fingerprint ─────────────────────────────────────────────────

    def get_fingerprint(
        self, host_id: uuid.UUID, slot: int
    ) -> FingerprintRecord | None:
        node = self._db.scalar(
            select(PPPoEExitNode).where(
                PPPoEExitNode.host_id == host_id,
                PPPoEExitNode.ppp_slot == slot,
            )
        )
        if not node:
            return None
        fp = self._db.get(FingerprintProfile, node.fingerprint_profile_id)
        return self._fp_record(fp) if fp else None

    def list_fingerprint_profiles(self) -> list[FingerprintRecord]:
        return [
            self._fp_record(r)
            for r in self._db.scalars(select(FingerprintProfile)).all()
        ]

    # ─── Query ───────────────────────────────────────────────────────

    def list_slots(
        self,
        host_id: uuid.UUID | None = None,
        state: str | None = None,
    ) -> list[PPPoESlotRecord]:
        stmt = select(PPPoEExitNode, PPPoEHost, FingerprintProfile).join(
            PPPoEHost, PPPoEExitNode.host_id == PPPoEHost.id
        ).join(
            FingerprintProfile,
            PPPoEExitNode.fingerprint_profile_id == FingerprintProfile.id,
        )
        if host_id:
            stmt = stmt.where(PPPoEExitNode.host_id == host_id)
        if state:
            stmt = stmt.where(PPPoEExitNode.state == state)
        stmt = stmt.order_by(PPPoEHost.name, PPPoEExitNode.ppp_slot)

        return [
            self._to_record(node, host, fp)
            for node, host, fp in self._db.execute(stmt).all()
        ]

    # ─── Private helpers ─────────────────────────────────────────────

    def _get_host(self, host_id: uuid.UUID) -> PPPoEHost:
        host = self._db.get(PPPoEHost, host_id)
        if not host:
            raise ValueError(f"Host {host_id} not found")
        if host.state != "active":
            raise ValueError(f"Host {host.name!r} is {host.state!r} (not active)")
        return host

    def _get_node(self, host_id: uuid.UUID, slot: int) -> PPPoEExitNode:
        node = self._db.scalar(
            select(PPPoEExitNode).where(
                PPPoEExitNode.host_id == host_id,
                PPPoEExitNode.ppp_slot == slot,
            )
        )
        if not node:
            raise ValueError(f"Slot {slot} on host {host_id} not in DB")
        return node

    def _pick_idle_node(
        self, preferred_host_id: uuid.UUID | None
    ) -> tuple[PPPoEExitNode | None, PPPoEHost | None]:
        stmt = (
            select(PPPoEExitNode, PPPoEHost)
            .join(PPPoEHost, PPPoEExitNode.host_id == PPPoEHost.id)
            .where(
                PPPoEExitNode.state == PPPoESlotState.IDLE,
                PPPoEHost.state == "active",
            )
            .order_by(
                # Prefer preferred_host first
                (PPPoEExitNode.host_id != preferred_host_id).cast(
                    type_=PPPoEExitNode.host_id.type
                )
                if preferred_host_id
                else PPPoEExitNode.last_seen.asc().nullsfirst(),
                PPPoEExitNode.last_seen.asc().nullsfirst(),
            )
            .limit(1)
        )
        row = self._db.execute(stmt).first()
        if not row:
            return None, None
        node, host = row
        return node, host

    def _next_free_slot(self, host_id: uuid.UUID) -> int:
        host = self._db.get(PPPoEHost, host_id)
        used = {
            r[0]
            for r in self._db.execute(
                select(PPPoEExitNode.ppp_slot).where(
                    PPPoEExitNode.host_id == host_id,
                    PPPoEExitNode.state != PPPoESlotState.DESTROYED,
                )
            ).all()
        }
        for s in range(host.max_slots):
            if s not in used:
                return s
        raise RuntimeError(
            f"All {host.max_slots} slots on host {host.name!r} exhausted"
        )

    def _to_record(
        self, node: PPPoEExitNode, host: PPPoEHost, profile: FingerprintProfile | None
    ) -> PPPoESlotRecord:
        return PPPoESlotRecord(
            id=node.id,
            host_id=node.host_id,
            host_name=host.name,
            ppp_slot=node.ppp_slot,
            state=node.state,
            public_ip=node.public_ip,
            cgnat_ip=node.cgnat_ip,
            proxy_url=node.proxy_url,
            proxy_port=node.proxy_port,
            proxy_state=node.proxy_state,
            assigned_worker_ts_ip=node.assigned_worker_ts_ip,
            assigned_session_id=node.assigned_session_id,
            fingerprint_profile_name=profile.name if profile else "unknown",
            total_sessions_served=node.total_sessions_served,
            reconnect_count=node.reconnect_count,
        )

    def _fp_record(self, fp: FingerprintProfile) -> FingerprintRecord:
        return FingerprintRecord(
            id=fp.id, name=fp.name, os=fp.os,
            cores=fp.cores, ram_gb=fp.ram_gb,
            webgl_renderer=fp.webgl_renderer, platform=fp.platform,
            screen_width=fp.screen_width, screen_height=fp.screen_height,
            canvas_seed=fp.canvas_seed, audio_seed=fp.audio_seed,
            ua_template=fp.ua_template, ch_platform=fp.ch_platform,
            ch_version=fp.ch_version, ch_arch=fp.ch_arch,
            dpr=fp.dpr, cam_name=fp.cam_name, is_mobile=fp.is_mobile,
        )
