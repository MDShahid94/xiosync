#!/usr/bin/env bash
# xiogrid/destroy-slot.sh <slot>
# Kills pppd for unit {slot} and removes macvlan mv{slot}.
set -euo pipefail
SLOT="$1"
MV="mv${SLOT}"

# Kill pppd for this slot
pkill -TERM -f "unit ${SLOT}" 2>/dev/null || true
sleep 1
pkill -KILL -f "unit ${SLOT}" 2>/dev/null || true

# Remove macvlan
ip link del "$MV" 2>/dev/null || true

echo "destroyed slot=${SLOT}"
