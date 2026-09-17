#!/usr/bin/env bash
# xiogrid/check-slot.sh <slot>
# Reports live state of one PPPoE slot.
# Output: "up <public_ip> <cgnat_ip>"  or  "down"
set -euo pipefail
SLOT="$1"
PPP="ppp${SLOT}"

if ip link show "$PPP" &>/dev/null 2>&1; then
    CGNAT=$(ip addr show "$PPP" 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 || true)
    PUB=$(curl --interface "$PPP" -s --max-time 5 https://api.ipify.org 2>/dev/null || true)
    if [[ -n "$PUB" ]]; then
        echo "up ${PUB} ${CGNAT:-unknown}"
    else
        echo "connecting unknown ${CGNAT:-unknown}"
    fi
else
    echo "down"
fi
