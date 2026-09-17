#!/usr/bin/env bash
# /usr/local/bin/xiogrid/vm-startup.sh
#
# XIOGRID VM startup script — runs on every VM boot via systemd.
# Performs:
#   1. PROMISC guard on PPPoE parent interface
#   2. Ensures ppp_generic kernel module is loaded
#   3. Signals XIOSYNC (via curl) to begin warm-pool provisioning
#      once the Tailscale overlay is up
#
# Install:
#   sudo cp vm-startup.sh /usr/local/bin/xiogrid/
#   sudo chmod +x /usr/local/bin/xiogrid/vm-startup.sh
#   sudo cp xiogrid-startup.service /etc/systemd/system/
#   sudo systemctl enable xiogrid-startup
#   sudo systemctl start xiogrid-startup

set -euo pipefail

PARENT_IFACE="__PARENT_IFACE__"      # enp26s0 — injected at deploy
XIOSYNC_URL="__XIOSYNC_URL__"        # http://100.86.149.127:8000 — Mac's TS IP
XIOSYNC_TOKEN="__XIOSYNC_TOKEN__"    # API bearer token
HOST_ID="__HOST_ID__"                # PPPoEHost UUID in XIOSYNC DB
LOG="/var/log/xiogrid-startup.log"

exec >> "${LOG}" 2>&1
echo "[$(date -Iseconds)] xiogrid-startup begin"

# ── 1. PROMISC on parent ───────────────────────────────────────────────────
echo "[$(date -Iseconds)] Setting PROMISC on ${PARENT_IFACE}"
ip link set "${PARENT_IFACE}" promisc on

# ── 2. Kernel modules ─────────────────────────────────────────────────────
modprobe ppp_generic 2>/dev/null || true
modprobe pppoe      2>/dev/null || true

# ── 3. Wait for Tailscale overlay (up to 60s) ─────────────────────────────
echo "[$(date -Iseconds)] Waiting for Tailscale..."
for i in $(seq 1 30); do
    if tailscale status &>/dev/null 2>&1; then
        TS_IP=$(tailscale ip -4 2>/dev/null || true)
        echo "[$(date -Iseconds)] Tailscale up: ${TS_IP}"
        break
    fi
    sleep 2
done

# ── 4. Notify XIOSYNC to reprovision warm pool ────────────────────────────
# XIOSYNC detects the host came online and re-provisions the warm pool.
# Retry up to 10 times (XIOSYNC may still be starting on Mac).
echo "[$(date -Iseconds)] Notifying XIOSYNC to reprovision warm pool..."
for i in $(seq 1 10); do
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        --max-time 8 \
        -X POST "${XIOSYNC_URL}/api/v1/pppoe/hosts/${HOST_ID}/warm-up" \
        -H "Authorization: Bearer ${XIOSYNC_TOKEN}" \
        -H "Content-Type: application/json" \
        -d '{"target": 50}' 2>/dev/null || echo "000")
    if [[ "${HTTP_CODE}" == "200" || "${HTTP_CODE}" == "202" ]]; then
        echo "[$(date -Iseconds)] XIOSYNC warm-up triggered (HTTP ${HTTP_CODE})"
        break
    fi
    echo "[$(date -Iseconds)] XIOSYNC not ready yet (HTTP ${HTTP_CODE}), retry ${i}/10..."
    sleep 10
done

echo "[$(date -Iseconds)] xiogrid-startup complete"
