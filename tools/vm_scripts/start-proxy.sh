#!/usr/bin/env bash
# xiogrid/start-proxy.sh <slot>
#
# Starts a pure-Python SOCKS5 proxy for a PPPoE slot.
# Proxy listens on 0.0.0.0:(10000+slot), routes via ppp{slot}.
# Requires root (SO_BINDTODEVICE needs CAP_NET_RAW).
#
# Called automatically after pppd connects in create-slot.sh.
# Killed by destroy-slot.sh / stop-proxy.sh.

set -euo pipefail

SLOT="$1"
PPP_IFACE="ppp${SLOT}"
PROXY_PORT=$((10000 + SLOT))
PID_DIR="/tmp/xiogrid_proxy"
PID_FILE="${PID_DIR}/socks5_${SLOT}.pid"
LOG_FILE="/tmp/ppp_logs/proxy_${SLOT}.log"
SCRIPT="/usr/local/bin/xiogrid/socks5.py"

mkdir -p "${PID_DIR}" /tmp/ppp_logs

# ── Kill any existing proxy on this slot ─────────────────────────────────────
if [[ -f "${PID_FILE}" ]]; then
    OLD_PID=$(cat "${PID_FILE}" 2>/dev/null || true)
    [[ -n "${OLD_PID}" ]] && kill "${OLD_PID}" 2>/dev/null || true
    rm -f "${PID_FILE}"
fi

# ── Wait for ppp interface to be up ─────────────────────────────────────────
for i in $(seq 1 15); do
    ip link show "${PPP_IFACE}" &>/dev/null 2>&1 && break
    sleep 1
done

if ! ip link show "${PPP_IFACE}" &>/dev/null 2>&1; then
    echo "ERROR: ${PPP_IFACE} not up after 15s" >&2
    exit 1
fi

# ── Start proxy ──────────────────────────────────────────────────────────────
nohup python3 "${SCRIPT}" "${SLOT}" \
    >> "${LOG_FILE}" 2>&1 &
PROXY_PID=$!
echo "${PROXY_PID}" > "${PID_FILE}"

# ── Verify startup ───────────────────────────────────────────────────────────
sleep 1
if kill -0 "${PROXY_PID}" 2>/dev/null; then
    echo "proxy_up socks5://$(tailscale ip -4 2>/dev/null || echo 0.0.0.0):${PROXY_PORT} pid=${PROXY_PID}"
else
    echo "proxy_failed"
    exit 1
fi
