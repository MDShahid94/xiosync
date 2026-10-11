#!/usr/bin/env bash
# xiogrid/destroy-slot.sh <slot>
# Kills pppd for unit {slot} and removes macvlan mv{slot}.
set -euo pipefail
SLOT="$1"
MV="mv${SLOT}"

# Stop SOCKS5 proxy for this slot before killing pppd
/usr/local/bin/xiogrid/stop-proxy.sh "${SLOT}" >/dev/null 2>&1 || true

# Kill pppd for this slot
pkill -TERM -f "unit ${SLOT}( |$)" 2>/dev/null || true
sleep 1
pkill -KILL -f "unit ${SLOT}( |$)" 2>/dev/null || true

# Flush routing table to prevent stale routes
TABLE=$((1000 + SLOT))
ip rule del oif "ppp${SLOT}" lookup "${TABLE}" priority 40 2>/dev/null || true
ip route flush table "${TABLE}" 2>/dev/null || true

# Remove macvlan
ip link del "$MV" 2>/dev/null || true

echo "destroyed slot=${SLOT}"
