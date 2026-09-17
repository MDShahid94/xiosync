#!/usr/bin/env bash
# xiogrid/assign-route.sh <worker_ts_ip> <slot>
# Adds an ip rule so traffic FROM worker_ts_ip exits via ppp{slot}.
# Table = 1000 + slot (avoids clash with system tables 0-255).
set -euo pipefail
WORKER_TS_IP="$1"
SLOT="$2"
TABLE=$((1000 + SLOT))
PPP="ppp${SLOT}"

# Ensure ppp interface is up
if ! ip link show "$PPP" &>/dev/null 2>&1; then
    echo "ERROR: ${PPP} is not up" >&2
    exit 1
fi

# Add route into private table (idempotent)
ip route replace default dev "$PPP" table "$TABLE" 2>/dev/null || true
# Add rule: from worker_ts_ip → lookup table
ip rule add from "$WORKER_TS_IP" lookup "$TABLE" priority 50 2>/dev/null || true

echo "assigned slot=${SLOT} worker=${WORKER_TS_IP} table=${TABLE}"
