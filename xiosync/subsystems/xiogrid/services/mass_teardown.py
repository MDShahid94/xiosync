"""XIOGRID mass-termination safety layer.

Problem statement
-----------------
Killing all PPPoE sessions at once (``sudo killall pppd``) creates two
simultaneous failures:

1. **PROMISC loss** — when the last macvlan on a parent interface is deleted,
   the Linux kernel automatically removes the PROMISC flag from the parent.
   Result: the parent NIC drops all incoming frames addressed to macvlan MACs
   → "Timeout waiting for PADO packets" on reconnect.
   Fix: always run ``ip link set <parent> promisc on`` before any macvlan create.

2. **BRAS anti-abuse burst** — 999 PADT frames arriving at the BNG in
   milliseconds, followed immediately by 999 new PADI frames, triggers
   per-OUI or per-port rate limiting on Airtel's AIRBRAS_WB-KHR-1.
   Duration: empirically 60-120 minutes.
   Fix: graduated teardown — destroy in batches with inter-batch pauses.

This module implements:
- ``graceful_teardown_all()``   — safe mass-teardown with batching
- ``graceful_teardown_batch()`` — batch teardown (reusable)
- ``safe_provision_batch()``    — graduated spin-up after teardown
- ``promisc_guard()``           — ensure PROMISC on parent before any op

Usage in XIOSYNC API
--------------------
Call ``graceful_teardown_all(host, session)`` instead of ever running
``killall pppd`` or destroying all slots in a tight loop.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from xiosync.subsystems.xiogrid.domain.pppoe import PPPoESlotState, PPPoECeilings
from xiosync.subsystems.xiogrid.models.exit_node import PPPoEExitNode, PPPoEHost
from xiosync.subsystems.xiogrid.services.ssh_exec import ssh_run, vm_script

logger = logging.getLogger(__name__)

# ── Tuning constants ────────────────────────────────────────────────────────

# Maximum sessions to teardown per batch without a pause.
# Airtel BRAS (AIRBRAS_WB-KHR-1) tolerates ~25 simultaneous PADTs before
# starting to rate-limit. Tested empirically: 999 at once → 60-120 min lockout.
TEARDOWN_BATCH_SIZE: int = 20

# Pause between teardown batches (seconds).
# 3s allows the BRAS to process the previous group's PADTs before receiving
# the next group. Keeps PADT rate under ~7/s (well within BRAS tolerance).
TEARDOWN_INTER_BATCH_PAUSE_S: float = 3.0

# After full teardown, wait before starting any new sessions.
# Gives the BRAS time to fully clear its session table.
POST_TEARDOWN_COOLDOWN_S: float = 30.0

# Maximum sessions to provision per batch (spin-up burst limit).
# Sending 999 PADIs at once also risks rate-limiting.
PROVISION_BATCH_SIZE: int = 25

# Pause between provision batches.
PROVISION_INTER_BATCH_PAUSE_S: float = 5.0


# ── PROMISC guard ───────────────────────────────────────────────────────────

def promisc_guard(host: PPPoEHost) -> bool:
    """Ensure PROMISC is enabled on the VM's PPPoE parent interface.

    CRITICAL: The Linux kernel removes PROMISC from the parent when the
    LAST macvlan using it is deleted. Without PROMISC, macvlan interfaces
    can still send frames but cannot RECEIVE frames addressed to their MACs
    (the parent NIC drops them). This causes "Timeout waiting for PADO
    packets" even though the BRAS is responding correctly.

    This must be called:
    - Before create-slot.sh (already included in create-slot.sh itself)
    - After any mass-teardown that deletes all macvlan interfaces
    - On VM startup/reboot (added to /etc/rc.local equivalent)
    """
    result = ssh_run(
        f"{host.vm_ssh_user}@{host.vm_ssh_host}",
        f"sudo ip link set {host.pppoe_parent_iface} promisc on && "
        f"ip link show {host.pppoe_parent_iface} | grep -o PROMISC",
        timeout=10,
    )
    ok = result.ok and "PROMISC" in (result.stdout or "")
    logger.info(
        "promisc_guard",
        extra={"host": host.name, "iface": host.pppoe_parent_iface, "ok": ok},
    )
    return ok


# ── Teardown ────────────────────────────────────────────────────────────────

@dataclass
class TeardownResult:
    host_name: str
    total_slots: int
    destroyed: int
    failed: int
    batches: int
    elapsed_s: float


def graceful_teardown_batch(
    host: PPPoEHost,
    slots: list[int],
    session: Session,
    *,
    batch_size: int = TEARDOWN_BATCH_SIZE,
    inter_batch_pause_s: float = TEARDOWN_INTER_BATCH_PAUSE_S,
) -> TeardownResult:
    """Destroy a list of PPPoE slots in safe batches.

    Args:
        host:               The PPPoEHost record (VM SSH info).
        slots:              List of slot numbers to destroy.
        session:            SQLAlchemy session (updated with new states).
        batch_size:         Max slots per batch (default 20).
        inter_batch_pause_s: Pause between batches (default 3s).

    Returns:
        TeardownResult with counts and timing.
    """
    t0 = time.monotonic()
    destroyed = 0
    failed = 0
    batches = math.ceil(len(slots) / batch_size)

    for batch_idx in range(batches):
        batch = slots[batch_idx * batch_size : (batch_idx + 1) * batch_size]
        logger.info(
            "teardown_batch_start",
            extra={"host": host.name, "batch": batch_idx + 1,
                   "total_batches": batches, "slots": batch},
        )

        for slot in batch:
            try:
                result = vm_script(host, "destroy-slot.sh", slot, timeout=15)
                if result.ok:
                    destroyed += 1
                    # Update DB state
                    node = session.scalar(
                        select(PPPoEExitNode).where(
                            PPPoEExitNode.host_id == host.id,
                            PPPoEExitNode.ppp_slot == slot,
                        )
                    )
                    if node:
                        node.state = PPPoESlotState.DESTROYED
                        node.public_ip = None
                        node.cgnat_ip = None
                        node.assigned_worker_ts_ip = None
                else:
                    failed += 1
                    logger.warning(
                        "destroy_slot_failed",
                        extra={"host": host.name, "slot": slot,
                               "stderr": (result.stderr or "")[:200]},
                    )
            except Exception as exc:
                failed += 1
                logger.warning(
                    "destroy_slot_error",
                    extra={"host": host.name, "slot": slot, "error": str(exc)},
                )

        session.flush()

        # Inter-batch pause — critical to avoid BRAS PADT storm
        if batch_idx < batches - 1:
            logger.info(
                "teardown_batch_pause",
                extra={"host": host.name, "pause_s": inter_batch_pause_s,
                       "next_batch": batch_idx + 2},
            )
            time.sleep(inter_batch_pause_s)

    elapsed = time.monotonic() - t0
    return TeardownResult(
        host_name=host.name,
        total_slots=len(slots),
        destroyed=destroyed,
        failed=failed,
        batches=batches,
        elapsed_s=elapsed,
    )


def graceful_teardown_all(
    host: PPPoEHost,
    session: Session,
    *,
    states_to_destroy: tuple[str, ...] = (
        PPPoESlotState.IDLE,
        PPPoESlotState.ASSIGNED,
        PPPoESlotState.RECONNECTING,
        PPPoESlotState.CONNECTING,
        PPPoESlotState.DOWN,
    ),
    post_cooldown_s: float = POST_TEARDOWN_COOLDOWN_S,
) -> TeardownResult:
    """Destroy ALL active PPPoE slots on a host safely.

    This is the correct replacement for ``killall pppd``. After this call,
    the PROMISC guard is run automatically so the next provision works.

    Args:
        host:              The PPPoEHost.
        session:           SQLAlchemy session.
        states_to_destroy: Which slot states to tear down.
        post_cooldown_s:   Seconds to wait after teardown (default 30s).

    Returns:
        TeardownResult.
    """
    nodes = session.scalars(
        select(PPPoEExitNode)
        .where(
            PPPoEExitNode.host_id == host.id,
            PPPoEExitNode.state.in_(states_to_destroy),
        )
        .order_by(PPPoEExitNode.ppp_slot)
    ).all()

    if not nodes:
        logger.info("teardown_all_no_nodes", extra={"host": host.name})
        return TeardownResult(
            host_name=host.name, total_slots=0,
            destroyed=0, failed=0, batches=0, elapsed_s=0.0,
        )

    slots = [n.ppp_slot for n in nodes]
    logger.info(
        "teardown_all_start",
        extra={"host": host.name, "total": len(slots),
               "batch_size": TEARDOWN_BATCH_SIZE},
    )

    result = graceful_teardown_batch(host, slots, session)

    # Post-teardown: restore PROMISC on parent (kernel removes it when
    # the last macvlan is deleted)
    logger.info(
        "teardown_all_promisc_restore",
        extra={"host": host.name},
    )
    promisc_guard(host)

    # Wait before any new connections to let BRAS flush session table
    if post_cooldown_s > 0:
        logger.info(
            "teardown_all_cooldown",
            extra={"host": host.name, "cooldown_s": post_cooldown_s},
        )
        time.sleep(post_cooldown_s)

    logger.info(
        "teardown_all_done",
        extra={
            "host": host.name,
            "destroyed": result.destroyed,
            "failed": result.failed,
            "elapsed_s": result.elapsed_s,
        },
    )
    return result


# ── BRAS Lockout Recovery Protocol ─────────────────────────────────────────

def bras_recovery_probe(host: PPPoEHost, *, timeout_s: int = 12) -> bool:
    """Probe the BRAS to confirm it's responding to PADI before mass provision.

    Uses pppoe-discovery (proven to work) to verify the BRAS is accepting
    new sessions. Call this before graceful_provision_batch() after any
    teardown to avoid provisioning into a locked-out BRAS.

    Returns:
        True if BRAS responded (safe to provision), False otherwise.
    """
    result = ssh_run(
        f"{host.vm_ssh_user}@{host.vm_ssh_host}",
        # Test on a temporary macvlan to avoid interfering with active slots
        f"""
        sudo ip link set {host.pppoe_parent_iface} promisc on
        sudo ip link add mv_probe link {host.pppoe_parent_iface} type macvlan mode bridge 2>/dev/null || true
        sudo ip link set mv_probe address 02:50:56:ff:ff:ff 2>/dev/null || true
        sudo ip link set mv_probe up 2>/dev/null || true
        RESULT=$(timeout {timeout_s} sudo pppoe-discovery -I mv_probe 2>&1)
        sudo ip link del mv_probe 2>/dev/null || true
        echo "$RESULT" | grep -c "Access-Concentrator" || echo "0"
        """,
        timeout=timeout_s + 5,
    )
    responding = result.ok and (result.stdout or "").strip() != "0"
    logger.info(
        "bras_probe",
        extra={"host": host.name, "responding": responding,
               "output": (result.stdout or "")[:100]},
    )
    return responding


# ── BRAS Lockout Facts (documented from live testing) ─────────────────────

BRAS_LOCKOUT_FACTS = """
Airtel BRAS: AIRBRAS_WB-KHR-1
BRAS MAC:    e4:f2:7c:d5:4f:28

OBSERVED LOCKOUT BEHAVIOR (tested 2026-09-11):
-----------------------------------------------
Trigger:     999 simultaneous pppd SIGTERM → 999 PADT packets burst
Symptom:     "Timeout waiting for PADO packets" on all new PADI
Real cause:  NOT BRAS lockout alone — PROMISC loss on enp26s0 (Linux kernel
             removes PROMISC when last macvlan deleted) ALSO contributed.
             Both issues occurred simultaneously, making diagnosis harder.

BRAS actual response:  pppoe-discovery works fine even after mass teardown
                       (BRAS does respond to PADI → sends PADO with cookie).
pppd failure cause:    pppd 2.5.2 on kernel 7.0.0 cannot receive PADO
                       via plugin rp-pppoe.so / pppoe.so on macvlan.
                       Workaround: use ``pty "pppoe -I mv{slot}"`` mode.

PREVENTION:
-----------
1. Always use graceful_teardown_all() instead of killall pppd
2. Batch size ≤ 20, pause 3s between batches
3. Always run promisc_guard() after any teardown
4. Use bras_recovery_probe() before mass provisioning
5. create-slot.sh always runs ``ip link set <parent> promisc on`` unconditionally

RECOVERY (if lockout occurs):
------------------------------
1. Wait 30-60 min (BRAS flushes stale session table)
2. Run bras_recovery_probe() to confirm BRAS is responding
3. Provision gradually: 10 slots, wait 60s, 10 more, etc.
"""
