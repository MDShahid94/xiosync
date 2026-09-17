"""PPPoE host registration + slot lifecycle API.

All endpoints require capability: browser_pool.manage
Multi-host: every endpoint is scoped to a specific host_id
            except /acquire which picks from the global pool.
"""
from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["pppoe"])


# ── Pydantic request models ──────────────────────────────────────────

class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterHostRequest(_M):
    name: str
    vm_ssh_host: str
    pppoe_username: str
    pppoe_password: str
    description: str = ""
    vm_ssh_user: str = "karmantu"
    vm_ssh_port: int = 22
    vm_scripts_dir: str = "/usr/local/bin/xiogrid"
    pppoe_parent_iface: str = "enp26s0"
    mac_oui_prefix: str = "00:50:56:cc"
    tailscale_vm_ts_ip: str | None = None
    max_slots: int = Field(default=981, le=1000)
    warm_pool_target: int = Field(default=50, le=981)
    meta: dict[str, Any] = Field(default_factory=dict)


class UpdateTsIpRequest(_M):
    tailscale_vm_ts_ip: str


class ProvisionRequest(_M):
    host_id: uuid.UUID
    slot: int = Field(ge=0, lt=1000)


class BatchProvisionRequest(_M):
    host_id: uuid.UUID
    slots: list[int]


class AssignRequest(_M):
    worker_ts_ip: str
    session_id: str
    preferred_host_id: uuid.UUID | None = None


# ── Host endpoints ───────────────────────────────────────────────────

@router.post("/pppoe/hosts", status_code=201, summary="Register a new Mac Mini + VM pair")
def register_host(payload: RegisterHostRequest, request: Request) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_hosts import PPPoEHostService
    svc = PPPoEHostService(request.state.org_session)
    host = svc.register(
        request.state.org_context,
        name=payload.name,
        vm_ssh_host=payload.vm_ssh_host,
        pppoe_username=payload.pppoe_username,
        pppoe_password=payload.pppoe_password,
        description=payload.description,
        vm_ssh_user=payload.vm_ssh_user,
        vm_ssh_port=payload.vm_ssh_port,
        vm_scripts_dir=payload.vm_scripts_dir,
        pppoe_parent_iface=payload.pppoe_parent_iface,
        mac_oui_prefix=payload.mac_oui_prefix,
        tailscale_vm_ts_ip=payload.tailscale_vm_ts_ip,
        max_slots=payload.max_slots,
        warm_pool_target=payload.warm_pool_target,
        meta=payload.meta,
    )
    # Deploy scripts to the new VM automatically
    _deploy_scripts_to_host(host, payload)
    return {"id": str(host.id), "name": host.name, "state": host.state,
            "vm_ssh_host": host.vm_ssh_host, "max_slots": host.max_slots}


@router.get("/pppoe/hosts", summary="List all registered hosts")
def list_hosts(
    state: str | None = Query(default=None),
    request: Request = None,
) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_hosts import PPPoEHostService
    hosts = PPPoEHostService(request.state.org_session).list_hosts(state=state)
    return {"count": len(hosts), "hosts": [
        {"id": str(h.id), "name": h.name, "state": h.state,
         "vm_ssh_host": h.vm_ssh_host, "tailscale_vm_ts_ip": h.tailscale_vm_ts_ip,
         "max_slots": h.max_slots, "last_seen": h.last_seen.isoformat() if h.last_seen else None}
        for h in hosts
    ]}


@router.get("/pppoe/hosts/{host_id}", summary="Get host details")
def get_host(host_id: uuid.UUID, request: Request) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_hosts import PPPoEHostService
    h = PPPoEHostService(request.state.org_session).get(host_id)
    return {"id": str(h.id), "name": h.name, "description": h.description,
            "state": h.state, "vm_ssh_host": h.vm_ssh_host, "vm_ssh_port": h.vm_ssh_port,
            "tailscale_vm_ts_ip": h.tailscale_vm_ts_ip, "max_slots": h.max_slots,
            "warm_pool_target": h.warm_pool_target, "meta": h.meta}


@router.patch("/pppoe/hosts/{host_id}/ts-ip", summary="Update VM Tailscale IP")
def update_ts_ip(host_id: uuid.UUID, payload: UpdateTsIpRequest, request: Request) -> dict:
    from xiosync.subsystems.xiogrid.services.pppoe_hosts import PPPoEHostService
    h = PPPoEHostService(request.state.org_session).update_ts_ip(
        host_id, payload.tailscale_vm_ts_ip
    )
    return {"id": str(h.id), "tailscale_vm_ts_ip": h.tailscale_vm_ts_ip}


@router.post("/pppoe/hosts/{host_id}/ping", summary="SSH liveness check")
def ping_host(host_id: uuid.UUID, request: Request) -> dict:
    from xiosync.subsystems.xiogrid.services.pppoe_hosts import PPPoEHostService
    alive = PPPoEHostService(request.state.org_session).ping(host_id)
    return {"host_id": str(host_id), "reachable": alive}


@router.post("/pppoe/hosts/{host_id}/deploy-scripts",
             summary="Re-deploy xiogrid scripts to VM")
def deploy_scripts(host_id: uuid.UUID, request: Request) -> dict:
    from xiosync.subsystems.xiogrid.services.pppoe_hosts import PPPoEHostService
    h = PPPoEHostService(request.state.org_session).get(host_id)
    _deploy_scripts_to_host_by_record(h)
    return {"deployed": True, "host": h.name}


# ── Slot lifecycle endpoints ─────────────────────────────────────────

@router.post("/pppoe/nodes/provision", status_code=201,
             summary="Provision (create) one PPPoE slot on a specific host")
def provision_slot(payload: ProvisionRequest, request: Request) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
    node = PPPoENodeService(request.state.org_session).provision_slot(
        request.state.org_context, payload.host_id, payload.slot
    )
    return _slot_json(node)


@router.post("/pppoe/nodes/batch", status_code=201,
             summary="Provision multiple slots on a host in batches of 25")
def provision_batch(payload: BatchProvisionRequest, request: Request) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
    nodes = PPPoENodeService(request.state.org_session).provision_batch(
        request.state.org_context, payload.host_id, payload.slots
    )
    return {"provisioned": len(nodes), "nodes": [_slot_json(n) for n in nodes]}


@router.post("/pppoe/nodes/acquire",
             summary="Acquire any idle slot from the global pool (auto-provisions if empty)")
def acquire_any(payload: AssignRequest, request: Request) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
    node = PPPoENodeService(request.state.org_session).acquire_any(
        request.state.org_context,
        session_id=payload.session_id,
        worker_ts_ip=payload.worker_ts_ip,
        preferred_host_id=payload.preferred_host_id,
    )
    return _slot_json(node)


@router.delete("/pppoe/nodes/{host_id}/{slot}",
               summary="Destroy slot — kills pppd + deletes macvlan on VM")
def destroy_slot(host_id: uuid.UUID, slot: int, request: Request) -> dict:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
    PPPoENodeService(request.state.org_session).destroy_slot(host_id, slot)
    return {"host_id": str(host_id), "slot": slot, "destroyed": True}


@router.post("/pppoe/nodes/{host_id}/{slot}/assign",
             summary="Assign a specific idle slot to a Colab worker")
def assign_slot(
    host_id: uuid.UUID, slot: int,
    payload: AssignRequest, request: Request,
) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
    node = PPPoENodeService(request.state.org_session).assign_to_worker(
        host_id, slot, payload.worker_ts_ip, payload.session_id
    )
    return _slot_json(node)


@router.delete("/pppoe/nodes/{host_id}/{slot}/assign",
               summary="Release slot from worker → returns to idle pool")
def release_slot(host_id: uuid.UUID, slot: int, request: Request) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
    node = PPPoENodeService(request.state.org_session).release_from_worker(host_id, slot)
    return _slot_json(node)


@router.post("/pppoe/nodes/{host_id}/{slot}/rotate",
             summary="Force IP rotation (pppd SIGHUP → new public IP, ~30s)")
def rotate_ip(host_id: uuid.UUID, slot: int, request: Request) -> dict:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
    return PPPoENodeService(request.state.org_session).rotate_ip(host_id, slot)


@router.get("/pppoe/nodes",
            summary="List all slots (optionally filter by host or state)")
def list_slots(
    host_id: uuid.UUID | None = Query(default=None),
    state: str | None = Query(default=None),
    request: Request = None,
) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
    nodes = PPPoENodeService(request.state.org_session).list_slots(
        host_id=host_id, state=state
    )
    return {"count": len(nodes), "nodes": [_slot_json(n) for n in nodes]}


@router.post("/pppoe/nodes/{host_id}/health",
             summary="Health-check all slots on one host")
def health_check_host(host_id: uuid.UUID, request: Request) -> dict:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
    return PPPoENodeService(request.state.org_session).health_check_host(host_id)


@router.post("/pppoe/nodes/health",
             summary="Health-check ALL slots across ALL active hosts")
def health_check_all(request: Request) -> dict:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
    return PPPoENodeService(request.state.org_session).health_check_all_hosts()


# ── Fingerprint endpoints ────────────────────────────────────────────

@router.get("/pppoe/fingerprints", summary="List all fingerprint profiles")
def list_fingerprints(request: Request) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
    fps = PPPoENodeService(request.state.org_session).list_fingerprint_profiles()
    return {"count": len(fps), "profiles": [
        {"id": str(f.id), "name": f.name, "os": f.os,
         "screen": f"{f.screen_width}x{f.screen_height}", "platform": f.platform}
        for f in fps
    ]}


@router.get("/pppoe/fingerprints/{host_id}/{slot}",
            summary="Get fingerprint for a specific slot")
def get_slot_fingerprint(host_id: uuid.UUID, slot: int, request: Request) -> dict:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
    fp = PPPoENodeService(request.state.org_session).get_fingerprint(host_id, slot)
    if not fp:
        return JSONResponse(status_code=404, content={"error": "slot or fingerprint not found"})
    return {
        "id": str(fp.id), "name": fp.name, "os": fp.os,
        "cores": fp.cores, "ram_gb": fp.ram_gb,
        "webgl_renderer": fp.webgl_renderer, "platform": fp.platform,
        "ch_platform": fp.ch_platform, "ch_version": fp.ch_version,
        "ch_arch": fp.ch_arch, "screen_width": fp.screen_width,
        "screen_height": fp.screen_height, "dpr": fp.dpr,
        "cam_name": fp.cam_name, "is_mobile": fp.is_mobile,
        "ua_template": fp.ua_template,
        "canvas_seed": fp.canvas_seed, "audio_seed": fp.audio_seed,
    }


# ── Internal helpers ─────────────────────────────────────────────────

def _slot_json(node: Any) -> dict[str, Any]:
    return {
        "host_id": str(node.host_id),
        "host_name": node.host_name,
        "slot": node.ppp_slot,
        "state": node.state,
        "public_ip": node.public_ip,
        "cgnat_ip": node.cgnat_ip,
        "proxy_url": node.proxy_url,
        "proxy_port": node.proxy_port,
        "proxy_state": node.proxy_state,
        "assigned_worker_ts_ip": node.assigned_worker_ts_ip,
        "assigned_session_id": node.assigned_session_id,
        "fingerprint": node.fingerprint_profile_name,
        "total_sessions": node.total_sessions_served,
        "reconnects": node.reconnect_count,
    }


def _deploy_scripts_to_host(host_record: Any, payload: Any) -> None:
    """Deploy xiogrid scripts to a newly registered VM — called on register."""
    import os
    import subprocess
    scripts_src = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "..", "tools", "vm_scripts"
    )
    scripts_src = os.path.normpath(scripts_src)
    target = f"{host_record.vm_ssh_user}@{host_record.vm_ssh_host}"
    ssh_opts = ["-o", "StrictHostKeyChecking=no", "-p", str(host_record.vm_ssh_port)]

    for script in ["create-slot.sh", "destroy-slot.sh", "check-slot.sh",
                   "assign-route.sh", "release-route.sh", "rotate-slot.sh"]:
        src_path = os.path.join(scripts_src, script)
        # Inject PPPoE credentials into create-slot.sh
        with open(src_path) as f:
            content = f.read()
        content = (
            content
            .replace("__PPPOE_USER__", payload.pppoe_username)
            .replace("__PPPOE_PASS__", payload.pppoe_password)
            .replace("__PARENT_IFACE__", payload.pppoe_parent_iface)
            .replace("__MAC_OUI__", payload.mac_oui_prefix)
        )
        # Write to temp, scp, chmod
        tmp = f"/tmp/xiogrid_{script}"
        with open(tmp, "w") as f:
            f.write(content)
        subprocess.run(
            ["scp", *ssh_opts, tmp, f"{target}:{host_record.vm_scripts_dir}/{script}"],
            check=True, timeout=15
        )
        subprocess.run(
            ["ssh", *ssh_opts, target,
             f"sudo chmod +x {host_record.vm_scripts_dir}/{script}"],
            check=True, timeout=10
        )


def _deploy_scripts_to_host_by_record(host: Any) -> None:
    """Re-deploy without credentials (for script updates only)."""
    import os, subprocess
    scripts_src = os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "..", "tools", "vm_scripts"
    ))
    target = f"{host.vm_ssh_user}@{host.vm_ssh_host}"
    ssh_opts = ["-o", "StrictHostKeyChecking=no", "-p", str(host.vm_ssh_port)]
    for script in ["destroy-slot.sh", "check-slot.sh",
                   "assign-route.sh", "release-route.sh", "rotate-slot.sh"]:
        src = os.path.join(scripts_src, script)
        subprocess.run(
            ["scp", *ssh_opts, src, f"{target}:{host.vm_scripts_dir}/{script}"],
            check=True, timeout=15
        )
        subprocess.run(
            ["ssh", *ssh_opts, target,
             f"sudo chmod +x {host.vm_scripts_dir}/{script}"],
            check=True, timeout=10
        )


# Register router
from xiosync.api.router_registry import register_router  # noqa: E402
from xiosync.api.middleware.rbac import require_capability  # noqa: E402

register_router(
    router,
    prefix="/api/v1",
    tags=["pppoe"],
    dependencies=[require_capability("browser_pool.manage")],
)
