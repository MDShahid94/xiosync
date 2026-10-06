#!/usr/bin/env bash
# /usr/local/bin/xiogrid/vm-startup.sh
#
# XIOGRID VM startup script — runs on every VM boot via systemd.
# Performs:
#   1. PROMISC guard on PPPoE parent interface
#   2. Ensures ppp_generic kernel module is loaded
#   3. Self-registers with XIOSYNC (idempotent — safe on every boot)
#   4. Signals XIOSYNC to begin warm-pool provisioning
#      once the Tailscale overlay is up
#
# NO pre-set HOST_ID required — the VM registers itself and gets its HOST_ID back.
#
# Install:
#   sudo cp vm-startup.sh /usr/local/bin/xiogrid/vm-startup.sh
#   sudo chmod +x /usr/local/bin/xiogrid/vm-startup.sh
#   sudo cp xiogrid-startup.service /etc/systemd/system/
#   sudo systemctl enable xiogrid-startup
#   sudo systemctl start xiogrid-startup

set -euo pipefail

# ── Config (injected at deploy via _deploy_scripts_to_host) ───────────────────
PARENT_IFACE="__PARENT_IFACE__"          # enp26s0 — PPPoE parent NIC
PPPOE_USER="__PPPOE_USER__"              # PPPoE CHAP username
PPPOE_PASS="__PPPOE_PASS__"              # PPPoE CHAP password
XIOSYNC_URL="__XIOSYNC_URL__"            # http://100.86.149.127:8000
XIOSYNC_TOKEN="__XIOSYNC_TOKEN__"        # API bearer token for /api/v1/...
VM_REGISTRATION_TOKEN="__VM_REG_TOKEN__" # Shared secret for /pppoe/hosts/register
MAX_SLOTS="__MAX_SLOTS__"                # Default: 981
WARM_POOL_TARGET="__WARM_POOL_TARGET__"  # Default: 50
VM_SCRIPTS_DIR="__VM_SCRIPTS_DIR__"      # Default: /usr/local/bin/xiogrid
LOG="/var/log/xiogrid-startup.log"

exec >> "${LOG}" 2>&1
echo "[$(date -Iseconds)] xiogrid-startup begin"

# ── 1. PROMISC on parent ───────────────────────────────────────────────────────
echo "[$(date -Iseconds)] Setting PROMISC on ${PARENT_IFACE}"
ip link set "${PARENT_IFACE}" promisc on

# ── 2. Kernel modules ─────────────────────────────────────────────────────────
modprobe ppp_generic 2>/dev/null || true
modprobe pppoe      2>/dev/null || true

# ── 3. Wait for Tailscale overlay (up to 60s) ─────────────────────────────────
echo "[$(date -Iseconds)] Waiting for Tailscale..."
TS_IP=""
for i in $(seq 1 30); do
    if tailscale status &>/dev/null 2>&1; then
        TS_IP=$(tailscale ip -4 2>/dev/null || true)
        if [[ -n "${TS_IP}" ]]; then
            echo "[$(date -Iseconds)] Tailscale up: ${TS_IP}"
            break
        fi
    fi
    sleep 2
done

if [[ -z "${TS_IP}" ]]; then
    echo "[$(date -Iseconds)] ERROR: Tailscale not up after 60s — aborting"
    exit 1
fi

VM_NAME=$(hostname -s)
SSH_HOST="${TS_IP}"

# ── 4. Self-register with XIOSYNC ─────────────────────────────────────────────
# Idempotent: on every boot the VM tells XIOSYNC its current Tailscale IP.
# XIOSYNC creates the host on first call; on subsequent boots it just updates the IP.
echo "[$(date -Iseconds)] Self-registering with XIOSYNC as ${VM_NAME} (${TS_IP})..."
REGISTER_RESPONSE=""
HOST_ID=""
for i in $(seq 1 10); do
    REGISTER_RESPONSE=$(curl -s --max-time 10 \
        -X POST "${XIOSYNC_URL}/api/v1/pppoe/hosts/register" \
        -H "Authorization: Bearer ${XIOSYNC_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{
            \"name\": \"${VM_NAME}\",
            \"tailscale_vm_ts_ip\": \"${TS_IP}\",
            \"vm_ssh_host\": \"${SSH_HOST}\",
            \"pppoe_username\": \"${PPPOE_USER}\",
            \"pppoe_password\": \"${PPPOE_PASS}\",
            \"pppoe_parent_iface\": \"${PARENT_IFACE}\",
            \"vm_scripts_dir\": \"${VM_SCRIPTS_DIR}\",
            \"max_slots\": ${MAX_SLOTS},
            \"warm_pool_target\": ${WARM_POOL_TARGET},
            \"registration_token\": \"${VM_REGISTRATION_TOKEN}\"
        }" 2>/dev/null || echo "")

    HOST_ID=$(echo "${REGISTER_RESPONSE}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('host_id',''))" 2>/dev/null || true)
    ACTION=$(echo "${REGISTER_RESPONSE}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('action',''))" 2>/dev/null || true)

    if [[ -n "${HOST_ID}" ]]; then
        echo "[$(date -Iseconds)] Registration OK: host_id=${HOST_ID} action=${ACTION}"
        break
    fi
    echo "[$(date -Iseconds)] XIOSYNC not ready (attempt ${i}/10): ${REGISTER_RESPONSE:-no response}"
    sleep 10
done

if [[ -z "${HOST_ID}" ]]; then
    echo "[$(date -Iseconds)] ERROR: Could not register with XIOSYNC after 10 attempts"
    exit 1
fi

# ── 5. Trigger warm-pool provisioning ─────────────────────────────────────────
# Runs in the background on XIOSYNC (returns immediately).
echo "[$(date -Iseconds)] Triggering warm-pool (target=${WARM_POOL_TARGET}) for host ${HOST_ID}..."
WARMUP_RESPONSE=$(curl -s --max-time 10 \
    -X POST "${XIOSYNC_URL}/api/v1/pppoe/hosts/${HOST_ID}/warm-up?target=${WARM_POOL_TARGET}" \
    -H "Authorization: Bearer ${XIOSYNC_TOKEN}" \
    2>/dev/null || echo "")
echo "[$(date -Iseconds)] Warm-up response: ${WARMUP_RESPONSE}"

echo "[$(date -Iseconds)] xiogrid-startup complete (host_id=${HOST_ID})"
