"""colab_agent.py — XIOSYNC-native Colab worker agent.

Replaces the 2670-line boot.py from XIOBR with a lean, XIOSYNC-native runtime
that derives all config from XIOSYNC rather than a local JSON file.

Boot sequence:
  Phase 1: Mount Google Drive
  Phase 2: Tailscale up (state restored from XIOSYNC storage provider)
  Phase 3: Start xio-browser subprocess (port from worker config)
  Phase 4: Self-enroll with XIOSYNC → pull worker config → fetch vault secrets
  Phase 5: Keep-alive loop
            → heartbeat (every 30s)
            → poll_dag_run() + run workflow + complete_run()
            → Tailscale state sync (every 5min)

Usage (in Colab cell):
    !pip install -q requests
    import subprocess, sys
    subprocess.run([sys.executable, "colab_agent.py"], check=True)

Or configure via environment variables:
    XIOSYNC_URL        — default: https://xiosync.xiogrid.dev
    XIOSYNC_TOKEN      — worker enrollment token
    XIOSYNC_WORKER_SECRET — worker secret for API auth
    XIOBR_GH_PAT       — GitHub PAT to clone xio-browser
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "requests"], check=True)
    import requests

# ── Config ────────────────────────────────────────────────────────────────────

XIOSYNC_URL    = os.environ.get("XIOSYNC_URL", "https://xiosync.xiogrid.dev").rstrip("/")
TOKEN          = os.environ.get("XIOSYNC_TOKEN", "")
WORKER_SECRET  = os.environ.get("XIOSYNC_WORKER_SECRET", "")
GH_PAT         = os.environ.get("XIOBR_GH_PAT", "")
NODE_NAME      = os.environ.get("XIOSYNC_NODE_NAME", "colab-agent")

# Derived at runtime from XIOSYNC
WORKER_ID: str | None = None
WORKER_CFG: dict[str, Any] = {}

DRIVE_ROOT     = Path("/content/drive/MyDrive")
CONTENT        = Path("/content")
XIO_BROWSER    = CONTENT / "xio-browser"
TS_STATE_KEY   = f"ts_states/{NODE_NAME}.state"

SESS = requests.Session()
SESS.headers.update({"Content-Type": "application/json"})


def log(msg: str) -> None:
    print(f"[colab_agent] {msg}", flush=True)


# ── Phase 1: Mount Drive ──────────────────────────────────────────────────────

def phase1_mount_drive() -> bool:
    """Mount Google Drive. Returns True if mounted."""
    try:
        from google.colab import drive  # type: ignore
        drive.mount("/content/drive")
        log(f"✅ Drive mounted at /content/drive")
        return True
    except Exception as exc:
        log(f"⚠️ Drive mount skipped: {exc}")
        return False


# ── Phase 2: Tailscale ────────────────────────────────────────────────────────

def phase2_tailscale(storage_provider_id: str | None, ts_auth_key: str | None) -> None:
    """Install Tailscale and join tailnet. Restore state from XIOSYNC storage if available."""
    log("Installing Tailscale...")
    subprocess.run(
        "curl -fsSL https://tailscale.com/install.sh | sh",
        shell=True, check=False, capture_output=True
    )

    # Try to restore saved state
    state_path = Path("/var/lib/tailscale/tailscaled.state")
    state_path.parent.mkdir(parents=True, exist_ok=True)

    if storage_provider_id:
        drive_path = DRIVE_ROOT / TS_STATE_KEY
        if drive_path.exists():
            log(f"Restoring Tailscale state from Drive: {drive_path}")
            import shutil
            shutil.copy(str(drive_path), str(state_path))

    # Start tailscaled
    subprocess.Popen(
        ["tailscaled", "--state=/var/lib/tailscale/tailscaled.state"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(3)

    # Join tailnet
    if ts_auth_key:
        result = subprocess.run(
            ["tailscale", "up", "--authkey", ts_auth_key, "--accept-routes"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            ip = subprocess.run(
                ["tailscale", "ip", "-4"],
                capture_output=True, text=True
            ).stdout.strip()
            log(f"✅ Tailscale up — IP: {ip}")
        else:
            log(f"⚠️ Tailscale up failed: {result.stderr[:200]}")
    else:
        log("⚠️ No Tailscale auth key — skipping join")


# ── Phase 3: xio-browser ─────────────────────────────────────────────────────

def phase3_start_xio_browser(port: int = 4242) -> subprocess.Popen | None:
    """Clone (if needed) and start xio-browser MCP server."""
    if not XIO_BROWSER.exists():
        if GH_PAT:
            log("Cloning xio-browser from GitHub...")
            subprocess.run(
                f"git clone https://{GH_PAT}@github.com/xiogrid-dev/xio-browser.git {XIO_BROWSER}",
                shell=True, check=False, capture_output=True
            )
        else:
            log("⚠️ GH_PAT not set — skipping xio-browser clone")
            return None

    if not (XIO_BROWSER / "package.json").exists():
        log("⚠️ xio-browser not available")
        return None

    log("Installing xio-browser deps...")
    subprocess.run(["npm", "install", "--prefix", str(XIO_BROWSER)],
                   capture_output=True, check=False)

    db_path = str(CONTENT / "xio-browser.db")
    cmd = [
        "node", str(XIO_BROWSER / "bin" / "xio-browser.mjs"),
        "--http", str(port),
        "--db", db_path,
    ]

    # Pass Drive shared folder from config
    shared_folder_id = WORKER_CFG.get("shared_folder_id")
    if shared_folder_id:
        cmd += ["--drive", shared_folder_id]

    log(f"Starting xio-browser on port {port}...")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(3)

    if proc.poll() is not None:
        log(f"⚠️ xio-browser exited early: {proc.stderr.read()[:200] if proc.stderr else ''}")
        return None

    log(f"✅ xio-browser PID={proc.pid} port={port}")
    return proc


# ── Phase 4: Self-enroll + pull config ────────────────────────────────────────

def phase4_enroll() -> bool:
    """Enroll this worker with XIOSYNC and pull config + secrets."""
    global WORKER_ID, WORKER_CFG

    # Get tailscale IP
    ts_ip = subprocess.run(
        ["tailscale", "ip", "-4"], capture_output=True, text=True
    ).stdout.strip() or ""

    payload = {
        "node_name": NODE_NAME,
        "node_type": "colab",
        "enrollment_token": TOKEN,
        "worker_secret": WORKER_SECRET,
        "tailscale_ip": ts_ip,
        "capabilities": ["dag_executor", "browser_automation"],
    }

    try:
        resp = SESS.post(f"{XIOSYNC_URL}/api/v1/workers/self-enroll", json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        WORKER_ID = data.get("worker_id") or data.get("id")
        log(f"✅ Enrolled — worker_id={WORKER_ID}")
    except Exception as exc:
        log(f"❌ Enrollment failed: {exc}")
        return False

    # Set auth header for subsequent requests
    api_token = data.get("api_token", TOKEN)
    SESS.headers.update({"Authorization": f"Bearer {api_token}"})

    # Pull worker config
    try:
        resp = SESS.get(f"{XIOSYNC_URL}/api/v1/workers/{WORKER_ID}/config", timeout=10)
        if resp.ok:
            WORKER_CFG = resp.json().get("config", {})
            log(f"✅ Worker config pulled: {list(WORKER_CFG.keys())}")
    except Exception as exc:
        log(f"⚠️ Config pull failed: {exc}")

    return True


# ── Keep-alive loop ───────────────────────────────────────────────────────────

def heartbeat() -> None:
    try:
        SESS.post(f"{XIOSYNC_URL}/api/v1/workers/{WORKER_ID}/heartbeat", timeout=5)
    except Exception:
        pass


def sync_tailscale_state(storage_provider_id: str | None) -> None:
    """Push current Tailscale state to Drive storage object."""
    state_path = Path("/var/lib/tailscale/tailscaled.state")
    if not state_path.exists() or not storage_provider_id:
        return
    try:
        drive_dest = DRIVE_ROOT / TS_STATE_KEY
        drive_dest.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy(str(state_path), str(drive_dest))
        # Register/update in XIOSYNC object index
        SESS.post(
            f"{XIOSYNC_URL}/api/v1/storage/objects",
            json={
                "provider_id": storage_provider_id,
                "object_key": TS_STATE_KEY,
                "object_type": "ts_state",
                "size_bytes": state_path.stat().st_size,
                "metadata": {"node_name": NODE_NAME},
            },
            timeout=10,
        )
        log(f"Synced Tailscale state → Drive ({state_path.stat().st_size}B)")
    except Exception as exc:
        log(f"⚠️ TS state sync failed: {exc}")


def poll_and_run_dag() -> bool:
    """Poll for a pending DAG run and execute it. Returns True if a run was processed."""
    try:
        resp = SESS.get(f"{XIOSYNC_URL}/api/v1/xioflow/events/runs/pending-dag", timeout=10)
        if resp.status_code == 204:
            return False   # Nothing pending
        resp.raise_for_status()
        run = resp.json()
    except Exception:
        return False

    run_id   = run.get("run_id")
    task_id  = run.get("task_id")
    template = run.get("template_name", "unknown")
    context  = run.get("context", {})
    log(f"▶ Running DAG: {template}  run_id={run_id}")

    try:
        # Execute the workflow script via xio-browser if available
        xiobr_port = WORKER_CFG.get("xiobr_port", 4242)
        result = _execute_workflow(template, context, xiobr_port)
        SESS.post(
            f"{XIOSYNC_URL}/api/v1/xioflow/events/runs/{run_id}/complete",
            json={"success": True, "task_id": task_id, "result": result},
            timeout=10,
        )
        log(f"✅ DAG complete: {template}")
    except Exception as exc:
        SESS.post(
            f"{XIOSYNC_URL}/api/v1/xioflow/events/runs/{run_id}/complete",
            json={"success": False, "task_id": task_id, "error": str(exc)},
            timeout=10,
        )
        log(f"❌ DAG failed: {exc}")

    return True


def _execute_workflow(template_name: str, context: dict, xiobr_port: int) -> dict:
    """Route workflow to xio-browser HTTP API if available."""
    try:
        resp = requests.post(
            f"http://localhost:{xiobr_port}/run",
            json={"workflow": template_name, "params": context},
            timeout=120,
        )
        if resp.ok:
            return resp.json()
        return {"status": "error", "code": resp.status_code, "body": resp.text[:500]}
    except Exception as exc:
        return {"status": "no_browser", "error": str(exc)}


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log(f"Starting colab_agent — node={NODE_NAME} url={XIOSYNC_URL}")

    # Phase 1: Drive
    phase1_mount_drive()

    # Phase 4: Enroll first (need config to know TS auth key + storage provider)
    if not phase4_enroll():
        log("❌ Could not enroll — aborting")
        sys.exit(1)

    # Resolve storage provider ID from config
    storage_provider_name = WORKER_CFG.get("storage_provider", "primary")
    storage_provider_id: str | None = None
    try:
        resp = SESS.get(f"{XIOSYNC_URL}/api/v1/storage/providers", timeout=10)
        if resp.ok:
            for p in resp.json():
                if p["name"] == storage_provider_name:
                    storage_provider_id = p["id"]
                    break
    except Exception:
        pass

    # Fetch TS auth key from vault
    ts_auth_key: str | None = None
    ts_vault_key = WORKER_CFG.get("ts_auth_vault_key", "tailscale_auth_key")
    try:
        resp = SESS.get(f"{XIOSYNC_URL}/api/v1/vault/secrets/{ts_vault_key}", timeout=10)
        if resp.ok:
            ts_auth_key = resp.json().get("value")
    except Exception:
        pass

    # Phase 2: Tailscale
    phase2_tailscale(storage_provider_id, ts_auth_key)

    # Phase 3: xio-browser
    xiobr_port = WORKER_CFG.get("xiobr_port", 4242)
    xio_proc = phase3_start_xio_browser(port=xiobr_port)

    # Phase 5: Keep-alive loop
    log("=== Keep-alive loop started ===")
    last_ts_sync = 0.0
    heartbeat_interval  = 30
    ts_sync_interval    = 300
    last_heartbeat      = 0.0

    while True:
        now = time.time()

        if now - last_heartbeat >= heartbeat_interval:
            heartbeat()
            last_heartbeat = now

        # Sync TS state every 5 min
        if now - last_ts_sync >= ts_sync_interval:
            sync_tailscale_state(storage_provider_id)
            last_ts_sync = now

        # Poll for DAG runs (non-blocking — returns immediately if none)
        try:
            poll_and_run_dag()
        except Exception as exc:
            log(f"⚠️ poll error: {exc}")

        # Check if xio-browser died and restart
        if xio_proc and xio_proc.poll() is not None:
            log("⚠️ xio-browser exited — restarting...")
            xio_proc = phase3_start_xio_browser(port=xiobr_port)

        time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Shutting down.")
