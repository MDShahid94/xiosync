#!/usr/bin/env python3
"""
xio_warmpool_cron.py — PPPoE warm pool maintainer

Runs every 2 minutes (via launchd StartInterval=120).
Ensures there are always >= WARM_TARGET idle slots pre-dialed on the VM so
that `acquire_any()` can return instantly without waiting for PPPoE dial (~35s).

Strategy:
  1. Fetch current slot inventory from XIOSYNC
  2. Count idle slots
  3. If idle < WARM_TARGET: trigger warm-up on each active host
     (POST /pppoe/hosts/{host_id}/warm-up?target=N runs async in background)
  4. Log results

Usage:
    python3 xio_warmpool_cron.py
    WARM_TARGET=10 python3 xio_warmpool_cron.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request

LOG_FILE = "/tmp/xio_warmpool.log"
XIOSYNC = os.environ.get("XIOSYNC_URL", "http://localhost:8000")
ADMIN_PASS = os.environ.get("XIOSYNC_ADMIN_PASS", "Xiogrid2026!Admin")
ADMIN_USER = os.environ.get("XIOSYNC_ADMIN_EMAIL", "admin@xiogrid.dev")
ORG_ID = os.environ.get("XIOSYNC_ORG_ID", "00000000-0000-7000-8000-000000000000")
WARM_TARGET = int(os.environ.get("WARM_TARGET", "5"))  # keep at least 5 idle slots

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, mode="a"),
    ],
)
log = logging.getLogger("warmpool")


def _post(path: str, body: dict | None = None, token: str | None = None) -> dict:
    url = f"{XIOSYNC}{path}"
    data = json.dumps(body or {}).encode()
    hdrs: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
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


def run() -> None:
    log.info("=== Warm pool check (target=%d idle slots) ===", WARM_TARGET)
    try:
        token = get_token()
    except Exception as e:
        log.error("Auth failed: %s", e)
        return

    # Count current idle slots
    try:
        data = _get("/api/v1/pppoe/nodes", token)
        nodes = data.get("nodes", data if isinstance(data, list) else [])
    except Exception as e:
        log.error("Failed to fetch nodes: %s", e)
        return

    idle_count = sum(1 for n in nodes if n.get("state") == "idle")
    total_count = len(nodes)
    log.info("Current: %d total slots, %d idle", total_count, idle_count)

    if idle_count >= WARM_TARGET:
        log.info(
            "Pool is warm (idle=%d >= target=%d) ✅ — no action needed", idle_count, WARM_TARGET
        )
        return

    deficit = WARM_TARGET - idle_count
    log.info("Pool needs %d more idle slot(s) — triggering warm-up...", deficit)

    # Get active hosts
    try:
        hosts_data = _get("/api/v1/pppoe/hosts", token)
        hosts = hosts_data if isinstance(hosts_data, list) else hosts_data.get("hosts", [])
        active_hosts = [h for h in hosts if h.get("state") == "active"]
    except Exception as e:
        log.error("Failed to fetch hosts: %s", e)
        return

    if not active_hosts:
        log.warning("No active PPPoE hosts found — cannot warm pool")
        return

    # Trigger warm-up on each host (endpoint runs async, returns immediately)
    new_target = total_count + deficit
    for host in active_hosts:
        host_id = host["id"]
        host_name = host.get("name", host_id)
        try:
            resp = _post(
                f"/api/v1/pppoe/hosts/{host_id}/warm-up",
                body={"warm_pool_target": new_target},
                token=token,
            )
            log.info(
                "  Host %s: warm-up triggered (target=%d) — %s",
                host_name,
                new_target,
                resp.get("status", "?"),
            )
        except Exception as e:
            log.error("  Host %s: warm-up failed: %s", host_name, e)

    log.info("=== Warm pool check complete ===\n")


if __name__ == "__main__":
    run()
