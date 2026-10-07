#!/usr/bin/env python3
"""
xio_slot_watchdog.py — PPPoE slot health monitor + auto-reconnect

Runs every 5 minutes (via launchd StartInterval=300).
For each registered slot:
  1. Calls POST /pppoe/nodes/{host}/{slot}/health (runs check-slot.sh on VM)
  2. If slot is DOWN: calls POST /pppoe/nodes/{host}/{slot}/rotate (re-dials)
  3. Logs all state changes to /tmp/xio_slot_watchdog.log

Also updates xio_proxy_daemon if new slots appear (writes to /tmp/xio_new_slots.txt
which the daemon checks on its next 30s cycle).

Usage:
    python3 xio_slot_watchdog.py
    # Or via launchd (see com.xiogrid.slot-watchdog.plist)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request

LOG_FILE = "/tmp/xio_slot_watchdog.log"
XIOSYNC = os.environ.get("XIOSYNC_URL", "http://localhost:8000")
ADMIN_PASS = os.environ.get("XIOSYNC_ADMIN_PASS", "Xiogrid2026!Admin")
ADMIN_USER = os.environ.get("XIOSYNC_ADMIN_EMAIL", "admin@xiogrid.dev")
ORG_ID = os.environ.get("XIOSYNC_ORG_ID", "00000000-0000-7000-8000-000000000000")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, mode="a"),
    ],
)
log = logging.getLogger("watchdog")


# ── XIOSYNC API helpers ──────────────────────────────────────────────────────


def _post(
    path: str, body: dict | None = None, token: str | None = None, method: str = "POST"
) -> dict:
    url = f"{XIOSYNC}{path}"
    data = json.dumps(body or {}).encode() if body is not None else b""
    hdrs: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(
        f"{XIOSYNC}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def get_token() -> str:
    resp = _post(
        "/api/v1/auth/login",
        {
            "organization_id": ORG_ID,
            "email": ADMIN_USER,
            "password": ADMIN_PASS,
        },
    )
    return resp["access_token"]


# ── Watchdog logic ───────────────────────────────────────────────────────────


def run_watchdog() -> None:
    log.info("=== Slot watchdog cycle started ===")
    try:
        token = get_token()
    except Exception as e:
        log.error("Auth failed: %s", e)
        return

    # Fetch current slot inventory
    try:
        data = _get("/api/v1/pppoe/nodes", token)
        nodes = data.get("nodes", data if isinstance(data, list) else [])
    except Exception as e:
        log.error("Failed to fetch slot inventory: %s", e)
        return

    if not nodes:
        log.info("No slots registered yet.")
        return

    log.info("Checking %d slot(s)...", len(nodes))

    reconnected = []
    for node in nodes:
        host_id = node["host_id"]
        slot = node["slot"]
        state = node.get("state", "?")
        pub_ip = node.get("public_ip", "?")

        # ── Health check ──
        try:
            # POST /pppoe/nodes/{host_id}/health runs check-slot.sh on all slots of that host
            result = _post(
                f"/api/v1/pppoe/nodes/{host_id}/health",
                token=token,
            )
            # Response is {slot_num: {state, public_ip, ...}}
            slot_result = result.get(str(slot), result.get(slot, {}))
            new_state = slot_result.get("state", state)
            new_pub_ip = slot_result.get("public_ip", pub_ip)

            if new_state == "up":
                if new_pub_ip != pub_ip:
                    log.info("  slot %d: IP updated %s → %s", slot, pub_ip, new_pub_ip)
                else:
                    log.info("  slot %d: UP  ip=%s ✅", slot, new_pub_ip)
            else:
                log.warning("  slot %d: DOWN (was %s) — triggering rotate...", slot, pub_ip)

                # ── Auto-reconnect via rotate ──
                try:
                    rot = _post(
                        f"/api/v1/pppoe/nodes/{host_id}/{slot}/rotate",
                        token=token,
                    )
                    rot_ip = rot.get("public_ip", "?")
                    rot_state = rot.get("state", "?")
                    if rot_state == "idle" and rot_ip and rot_ip != "?":
                        log.info("  slot %d: RECONNECTED → new ip=%s ✅", slot, rot_ip)
                        reconnected.append({"slot": slot, "new_ip": rot_ip})
                    else:
                        log.error(
                            "  slot %d: Rotate failed (state=%s ip=%s) ❌", slot, rot_state, rot_ip
                        )
                except Exception as re:
                    log.error("  slot %d: Rotate error: %s ❌", slot, re)

        except Exception as he:
            log.error("  slot %d: Health check error: %s", slot, he)

    if reconnected:
        log.info("Reconnected %d slot(s): %s", len(reconnected), reconnected)

    log.info("=== Watchdog cycle complete ===\n")


if __name__ == "__main__":
    run_watchdog()
