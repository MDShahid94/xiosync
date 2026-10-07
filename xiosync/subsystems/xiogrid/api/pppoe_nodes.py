"""PPPoE host registration + slot lifecycle API.

All endpoints require capability: browser_pool.manage
Multi-host: every endpoint is scoped to a specific host_id
            except /acquire which picks from the global pool.
"""

from __future__ import annotations

import uuid
from typing import Any

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
    # Pass the Google account email to enable per-account IP pinning.
    # "etathyaghar@gmail.com" → always returns same PPPoE slot → same residential IP.
    google_account: str | None = None


# ── Host endpoints ───────────────────────────────────────────────────


@router.post("/pppoe/hosts", status_code=201, summary="Register a new Mac Mini + VM pair")
def register_host(payload: RegisterHostRequest, request: Request) -> dict[str, Any]:
    import logging as _log

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
    # Deploy scripts — non-fatal: VM may not be SSH-reachable at registration time
    scripts_warning = None
    try:
        _deploy_scripts_to_host(host, payload)
    except Exception as _e:
        scripts_warning = str(_e)[:200]
        _log.getLogger(__name__).warning(
            f"register_host: script deploy to {host.vm_ssh_host} failed "
            f"(non-fatal, retry with POST /pppoe/hosts/{host.id}/deploy-scripts): {_e}"
        )
    resp = {
        "id": str(host.id),
        "name": host.name,
        "state": host.state,
        "vm_ssh_host": host.vm_ssh_host,
        "max_slots": host.max_slots,
    }
    if scripts_warning:
        resp["scripts_warning"] = scripts_warning
    return resp


@router.get("/pppoe/hosts", summary="List all registered hosts")
def list_hosts(
    state: str | None = Query(default=None),
    request: Request = None,
) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_hosts import PPPoEHostService

    hosts = PPPoEHostService(request.state.org_session).list_hosts(state=state)
    return {
        "count": len(hosts),
        "hosts": [
            {
                "id": str(h.id),
                "name": h.name,
                "state": h.state,
                "vm_ssh_host": h.vm_ssh_host,
                "tailscale_vm_ts_ip": h.tailscale_vm_ts_ip,
                "max_slots": h.max_slots,
                "last_seen": h.last_seen.isoformat() if h.last_seen else None,
            }
            for h in hosts
        ],
    }


@router.get("/pppoe/hosts/{host_id}", summary="Get host details")
def get_host(host_id: uuid.UUID, request: Request) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_hosts import PPPoEHostService

    h = PPPoEHostService(request.state.org_session).get(host_id)
    return {
        "id": str(h.id),
        "name": h.name,
        "description": h.description,
        "state": h.state,
        "vm_ssh_host": h.vm_ssh_host,
        "vm_ssh_port": h.vm_ssh_port,
        "tailscale_vm_ts_ip": h.tailscale_vm_ts_ip,
        "max_slots": h.max_slots,
        "warm_pool_target": h.warm_pool_target,
        "meta": h.meta,
    }


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


@router.post("/pppoe/hosts/{host_id}/deploy-scripts", summary="Re-deploy xiogrid scripts to VM")
def deploy_scripts(host_id: uuid.UUID, request: Request) -> dict:
    from xiosync.subsystems.xiogrid.services.pppoe_hosts import PPPoEHostService

    h = PPPoEHostService(request.state.org_session).get(host_id)
    _deploy_scripts_to_host_by_record(h)
    return {"deployed": True, "host": h.name}


# ── Slot lifecycle endpoints ─────────────────────────────────────────


@router.post(
    "/pppoe/nodes/provision",
    status_code=201,
    summary="Provision (create) one PPPoE slot on a specific host",
)
def provision_slot(payload: ProvisionRequest, request: Request) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService

    node = PPPoENodeService(request.state.org_session).provision_slot(
        request.state.org_context, payload.host_id, payload.slot
    )
    return _slot_json(node)


@router.post(
    "/pppoe/nodes/batch",
    status_code=201,
    summary="Provision multiple slots on a host in batches of 25",
)
def provision_batch(payload: BatchProvisionRequest, request: Request) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService

    nodes = PPPoENodeService(request.state.org_session).provision_batch(
        request.state.org_context, payload.host_id, payload.slots
    )
    return {"provisioned": len(nodes), "nodes": [_slot_json(n) for n in nodes]}


@router.post(
    "/pppoe/nodes/acquire",
    summary="Acquire any idle slot from the global pool (auto-provisions if empty)",
)
def acquire_any(payload: AssignRequest, request: Request) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService

    node = PPPoENodeService(request.state.org_session).acquire_any(
        request.state.org_context,
        session_id=payload.session_id,
        worker_ts_ip=payload.worker_ts_ip,
        preferred_host_id=payload.preferred_host_id,
        google_account=payload.google_account,
    )
    return _slot_json(node)


@router.delete(
    "/pppoe/nodes/{host_id}/{slot}", summary="Destroy slot — kills pppd + deletes macvlan on VM"
)
def destroy_slot(host_id: uuid.UUID, slot: int, request: Request) -> dict:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService

    PPPoENodeService(request.state.org_session).destroy_slot(host_id, slot)
    return {"host_id": str(host_id), "slot": slot, "destroyed": True}


@router.post(
    "/pppoe/nodes/{host_id}/{slot}/assign", summary="Assign a specific idle slot to a Colab worker"
)
def assign_slot(
    host_id: uuid.UUID,
    slot: int,
    payload: AssignRequest,
    request: Request,
) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService

    node = PPPoENodeService(request.state.org_session).assign_to_worker(
        host_id, slot, payload.worker_ts_ip, payload.session_id
    )
    return _slot_json(node)


@router.delete(
    "/pppoe/nodes/{host_id}/{slot}/assign",
    summary="Release slot from worker → returns to idle pool",
)
def release_slot(host_id: uuid.UUID, slot: int, request: Request) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService

    node = PPPoENodeService(request.state.org_session).release_from_worker(host_id, slot)
    return _slot_json(node)


@router.post(
    "/pppoe/nodes/{host_id}/{slot}/rotate",
    summary="Force IP rotation (pppd SIGHUP → new public IP, ~30s)",
)
def rotate_ip(host_id: uuid.UUID, slot: int, request: Request) -> dict:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService

    return PPPoENodeService(request.state.org_session).rotate_ip(host_id, slot)


@router.get("/pppoe/nodes", summary="List all slots (optionally filter by host or state)")
def list_slots(
    host_id: uuid.UUID | None = Query(default=None),
    state: str | None = Query(default=None),
    request: Request = None,
) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService

    nodes = PPPoENodeService(request.state.org_session).list_slots(host_id=host_id, state=state)
    return {"count": len(nodes), "nodes": [_slot_json(n) for n in nodes]}


@router.post("/pppoe/nodes/{host_id}/health", summary="Health-check all slots on one host")
def health_check_host(host_id: uuid.UUID, request: Request) -> dict:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService

    return PPPoENodeService(request.state.org_session).health_check_host(host_id)


@router.post("/pppoe/nodes/health", summary="Health-check ALL slots across ALL active hosts")
def health_check_all(request: Request) -> dict:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService

    return PPPoENodeService(request.state.org_session).health_check_all_hosts()


@router.post(
    "/pppoe/hosts/{host_id}/warm-up",
    summary="Trigger warm-pool provisioning on a host — called by vm-startup.sh on boot",
)
def warm_up_host(host_id: uuid.UUID, request: Request, target: int = 50) -> dict:
    """
    Reprovision the warm pool up to `target` idle slots on the given host.
    Called automatically by vm-startup.sh when the VM boots.
    Runs in a background thread so the HTTP call returns immediately.

    Returns {"ok": true, "host_id": ..., "target": N, "status": "triggered"}.
    The actual provisioning runs async — check slot state via GET /pppoe/nodes.
    """
    import threading

    from xiosync.subsystems.xiogrid.services.pppoe_hosts import PPPoEHostService

    db = request.state.org_session
    ctx = request.state.org_context

    try:
        host = PPPoEHostService(db).get(host_id)
    except Exception:
        return JSONResponse(status_code=404, content={"error": f"host {host_id} not found"})

    # Count already-warm slots so we only provision the delta
    from sqlalchemy import func as _func
    from sqlalchemy import select as _sel

    from xiosync.subsystems.xiogrid.domain.pppoe import PPPoESlotState
    from xiosync.subsystems.xiogrid.models.exit_node import PPPoEExitNode

    idle_count = (
        db.scalar(
            _sel(_func.count()).where(
                PPPoEExitNode.host_id == host_id,
                PPPoEExitNode.state.in_([PPPoESlotState.IDLE, PPPoESlotState.ASSIGNED]),
            )
        )
        or 0
    )
    to_provision = max(0, min(target, host.max_slots) - idle_count)

    if to_provision == 0:
        return {
            "ok": True,
            "host_id": str(host_id),
            "target": target,
            "idle": idle_count,
            "status": "pool_already_warm",
        }

    # Background thread — provision without blocking the VM boot
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService as _SVC

    def _bg():
        import sqlalchemy as _sa

        engine = db.get_bind()
        Session = _sa.orm.sessionmaker(engine)
        with Session() as sess:
            svc = _SVC(sess)
            for _ in range(to_provision):
                try:
                    slot = svc._next_free_slot(host_id)
                    svc.provision_slot(ctx, host_id, slot)
                except Exception as e:
                    import logging

                    logging.getLogger(__name__).warning(f"warm-up provision failed: {e}")
                    break

    threading.Thread(target=_bg, daemon=True).start()
    return {
        "ok": True,
        "host_id": str(host_id),
        "target": target,
        "idle_before": idle_count,
        "provisioning": to_provision,
        "status": "triggered",
    }


class SelfRegisterRequest(_M):
    """Sent by vm-startup.sh on first boot — no pre-set HOST_ID needed."""

    name: str = Field(description="Unique VM hostname, e.g. 'xiogrid-vm-01'")
    tailscale_vm_ts_ip: str = Field(description="VM's Tailscale IPv4")
    vm_ssh_host: str = Field(description="SSH host (usually Tailscale IP)")
    pppoe_username: str = Field(description="PPPoE CHAP username")
    pppoe_password: str = Field(description="PPPoE CHAP password")
    pppoe_parent_iface: str = Field(default="enp26s0")
    vm_ssh_user: str = Field(default="karmantu")
    vm_ssh_port: int = Field(default=22)
    vm_scripts_dir: str = Field(default="/usr/local/bin/xiogrid")
    max_slots: int = Field(default=981, le=1000)
    warm_pool_target: int = Field(default=50)
    registration_token: str = Field(description="Shared secret for VM self-registration")
    meta: dict[str, Any] = Field(default_factory=dict)


@router.post(
    "/pppoe/hosts/register",
    status_code=201,
    summary="VM self-registration — called by vm-startup.sh on boot (idempotent)",
)
def self_register(payload: SelfRegisterRequest, request: Request) -> dict[str, Any]:
    """
    Idempotent: if a host with the same name already exists, update its Tailscale IP
    and return the existing record. Otherwise, create a new PPPoEHost and deploy scripts.

    Called by vm-startup.sh on VM boot — eliminates the need for HOST_ID to be
    pre-provisioned in the startup script.

    Returns {"host_id": uuid, "name": str, "action": "created" | "updated"}.
    """
    import logging as _log
    import os

    from xiosync.subsystems.xiogrid.models.exit_node import PPPoEHost
    from xiosync.subsystems.xiogrid.services.pppoe_hosts import PPPoEHostService

    # Validate registration token
    expected = os.environ.get("XIOGRID_VM_REGISTRATION_TOKEN", "")
    if expected and payload.registration_token != expected:
        return JSONResponse(status_code=403, content={"error": "invalid_registration_token"})

    db = request.state.org_session
    ctx = request.state.org_context
    svc = PPPoEHostService(db)

    # Idempotent: look up by name
    from sqlalchemy import select as _sel

    existing = db.scalar(_sel(PPPoEHost).where(PPPoEHost.name == payload.name))
    if existing:
        # Update Tailscale IP if it changed (common after VM reboot)
        if existing.tailscale_vm_ts_ip != payload.tailscale_vm_ts_ip:
            existing.tailscale_vm_ts_ip = payload.tailscale_vm_ts_ip
            existing.vm_ssh_host = payload.vm_ssh_host
            db.flush()
        return {
            "host_id": str(existing.id),
            "name": existing.name,
            "action": "updated",
            "state": existing.state,
        }

    # New registration
    host = svc.register(
        ctx,
        name=payload.name,
        vm_ssh_host=payload.vm_ssh_host,
        pppoe_username=payload.pppoe_username,
        pppoe_password=payload.pppoe_password,
        description=f"Auto-registered from {payload.tailscale_vm_ts_ip}",
        vm_ssh_user=payload.vm_ssh_user,
        vm_ssh_port=payload.vm_ssh_port,
        vm_scripts_dir=payload.vm_scripts_dir,
        pppoe_parent_iface=payload.pppoe_parent_iface,
        tailscale_vm_ts_ip=payload.tailscale_vm_ts_ip,
        max_slots=payload.max_slots,
        warm_pool_target=payload.warm_pool_target,
        meta=payload.meta,
    )
    # Deploy scripts — non-fatal on SSH failure (VM may not be reachable at registration time)
    _scripts_warn = None
    try:
        _deploy_scripts_to_host(host, payload)
    except Exception as _se:
        _scripts_warn = str(_se)[:200]
        _log.getLogger(__name__).warning(
            f"self_register: script deploy failed for {host.vm_ssh_host}: {_se}"
        )

    result = {"host_id": str(host.id), "name": host.name, "action": "created", "state": host.state}
    if _scripts_warn:
        result["scripts_warning"] = _scripts_warn
    return result


# ── Fingerprint endpoints ────────────────────────────────────────────


@router.get("/pppoe/fingerprints", summary="List all fingerprint profiles")
def list_fingerprints(request: Request) -> dict[str, Any]:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService

    fps = PPPoENodeService(request.state.org_session).list_fingerprint_profiles()
    return {
        "count": len(fps),
        "profiles": [
            {
                "id": str(f.id),
                "name": f.name,
                "os": f.os,
                "screen": f"{f.screen_width}x{f.screen_height}",
                "platform": f.platform,
            }
            for f in fps
        ],
    }


@router.get("/pppoe/fingerprints/{host_id}/{slot}", summary="Get fingerprint for a specific slot")
def get_slot_fingerprint(host_id: uuid.UUID, slot: int, request: Request) -> dict:
    from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService

    fp = PPPoENodeService(request.state.org_session).get_fingerprint(host_id, slot)
    if not fp:
        return JSONResponse(status_code=404, content={"error": "slot or fingerprint not found"})
    return {
        "id": str(fp.id),
        "name": fp.name,
        "os": fp.os,
        "cores": fp.cores,
        "ram_gb": fp.ram_gb,
        "webgl_renderer": fp.webgl_renderer,
        "platform": fp.platform,
        "ch_platform": fp.ch_platform,
        "ch_version": fp.ch_version,
        "ch_arch": fp.ch_arch,
        "screen_width": fp.screen_width,
        "screen_height": fp.screen_height,
        "dpr": fp.dpr,
        "cam_name": fp.cam_name,
        "is_mobile": fp.is_mobile,
        "ua_template": fp.ua_template,
        "canvas_seed": fp.canvas_seed,
        "audio_seed": fp.audio_seed,
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

    # Template variables to inject into all scripts
    xiosync_base = os.environ.get("XIOSYNC_BASE_URL", "http://100.86.149.127:8000")
    xiosync_tok = os.environ.get("XIOSYNC_WORKER_ORG_SECRET", "")
    vm_reg_tok = os.environ.get("XIOGRID_VM_REGISTRATION_TOKEN", "")
    mac_oui = getattr(payload, "mac_oui_prefix", "00:50:56:cc")

    TEMPLATE_VARS = {
        "__PPPOE_USER__": payload.pppoe_username,
        "__PPPOE_PASS__": payload.pppoe_password,
        "__PARENT_IFACE__": payload.pppoe_parent_iface,
        "__MAC_OUI__": mac_oui,
        "__XIOSYNC_URL__": xiosync_base,
        "__XIOSYNC_TOKEN__": xiosync_tok,
        "__VM_REG_TOKEN__": vm_reg_tok,
        "__MAX_SLOTS__": str(payload.max_slots),
        "__WARM_POOL_TARGET__": str(payload.warm_pool_target),
        "__VM_SCRIPTS_DIR__": payload.vm_scripts_dir,
    }

    all_scripts = [
        "create-slot.sh",
        "destroy-slot.sh",
        "check-slot.sh",
        "assign-route.sh",
        "release-route.sh",
        "rotate-slot.sh",
        "start-proxy.sh",
        "stop-proxy.sh",
        "vm-startup.sh",
        "socks5.py",
    ]

    for script in all_scripts:
        src_path = os.path.join(scripts_src, script)
        if not os.path.exists(src_path):
            continue
        with open(src_path) as f:
            content = f.read()
        for placeholder, value in TEMPLATE_VARS.items():
            content = content.replace(placeholder, value)

        # Write to temp, scp, chmod
        tmp = f"/tmp/xiogrid_{script}"
        with open(tmp, "w") as f:
            f.write(content)
        subprocess.run(
            ["scp", *ssh_opts, tmp, f"{target}:{host_record.vm_scripts_dir}/{script}"],
            check=True,
            timeout=15,
        )
        subprocess.run(
            ["ssh", *ssh_opts, target, f"sudo chmod +x {host_record.vm_scripts_dir}/{script}"],
            check=True,
            timeout=10,
        )


def _deploy_scripts_to_host_by_record(host: Any) -> None:
    """Re-deploy without credentials (for script updates only)."""
    import os
    import subprocess

    scripts_src = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "tools", "vm_scripts")
    )
    target = f"{host.vm_ssh_user}@{host.vm_ssh_host}"
    ssh_opts = ["-o", "StrictHostKeyChecking=no", "-p", str(host.vm_ssh_port)]
    for script in [
        "destroy-slot.sh",
        "check-slot.sh",
        "assign-route.sh",
        "release-route.sh",
        "rotate-slot.sh",
    ]:
        src = os.path.join(scripts_src, script)
        subprocess.run(
            ["scp", *ssh_opts, src, f"{target}:{host.vm_scripts_dir}/{script}"],
            check=True,
            timeout=15,
        )
        subprocess.run(
            ["ssh", *ssh_opts, target, f"sudo chmod +x {host.vm_scripts_dir}/{script}"],
            check=True,
            timeout=10,
        )


# Register router
from xiosync.api.middleware.rbac import require_capability  # noqa: E402
from xiosync.api.router_registry import register_router  # noqa: E402

register_router(
    router,
    prefix="/api/v1",
    tags=["pppoe"],
    dependencies=[require_capability("browser_pool.manage")],
)
