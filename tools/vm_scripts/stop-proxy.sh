#!/usr/bin/env bash
# xiogrid/stop-proxy.sh <slot>
# Stops the pure-Python SOCKS5 proxy for a specific PPPoE slot.
# PID file: /tmp/xiogrid_proxy/socks5_<slot>.pid  (written by socks5.py)
set -euo pipefail

SLOT="$1"
PID_DIR="/tmp/xiogrid_proxy"
PID_FILE="${PID_DIR}/socks5_${SLOT}.pid"

if [[ -f "${PID_FILE}" ]]; then
    PID=$(cat "${PID_FILE}" 2>/dev/null || true)
    if [[ -n "${PID}" ]]; then
        sudo kill "${PID}" 2>/dev/null && echo "proxy_stopped slot=${SLOT} pid=${PID}" || true
    fi
    rm -f "${PID_FILE}"
else
    # Also try to kill by port in case PID file was lost
    PORT=$((10000 + SLOT))
    PIDS=$(sudo lsof -ti tcp:"${PORT}" 2>/dev/null || true)
    if [[ -n "${PIDS}" ]]; then
        echo "${PIDS}" | xargs -r sudo kill 2>/dev/null || true
        echo "proxy_stopped slot=${SLOT} port=${PORT} (by port)"
    else
        echo "proxy_not_running slot=${SLOT}"
    fi
fi
