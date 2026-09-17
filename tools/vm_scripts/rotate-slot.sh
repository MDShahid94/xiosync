#!/usr/bin/env bash
# xiogrid/rotate-slot.sh <slot>
# Sends SIGHUP to pppd for slot → triggers graceful reconnect → new public IP.
# Blocks ~30s waiting for new IP. Output: "up <new_ip>" or "timeout"
set -euo pipefail
SLOT="$1"
PPP="ppp${SLOT}"

PID=$(pgrep -f "unit ${SLOT}" 2>/dev/null | head -1 || true)
if [[ -z "$PID" ]]; then
    echo "not_running"
    exit 0
fi

kill -HUP "$PID" 2>/dev/null || true

# Wait for reconnection (up to 35s)
for i in $(seq 1 35); do
    sleep 1
    if ip link show "$PPP" &>/dev/null 2>&1; then
        PUB=$(curl --interface "$PPP" -s --max-time 4 https://api.ipify.org 2>/dev/null || true)
        if [[ -n "$PUB" ]]; then
            echo "up ${PUB}"
            exit 0
        fi
    fi
done

echo "timeout"
