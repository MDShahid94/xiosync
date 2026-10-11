#!/usr/bin/env bash
# xiogrid/check-slot.sh <slot>
# Reports live state of one PPPoE slot.
# Output: "up <public_ip> <cgnat_ip> <pub_v6> <cgnat_v6>"  or  "down"
set -euo pipefail
SLOT="$1"
PPP="ppp${SLOT}"

if ip link show "$PPP" &>/dev/null 2>&1; then
    CGNAT=$(ip addr show "$PPP" 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 || true)
    PUB=$(curl --interface "$PPP" -s --max-time 5 https://api.ipify.org 2>/dev/null || true)
    # IPv6 health check
    PUB_V6=$(curl --interface "$PPP" -6 -s --max-time 5 https://api6.ipify.org 2>/dev/null || true)
    CGNAT_V6=$(ip addr show "$PPP" 2>/dev/null | awk '/inet6 .* scope global/{print $2}' | cut -d/ -f1 || true)
    if [[ -n "$PUB" ]]; then
        echo "up ${PUB} ${CGNAT:-unknown} ${PUB_V6:-none} ${CGNAT_V6:-none}"
    else
        echo "connecting unknown ${CGNAT:-unknown} ${PUB_V6:-none} ${CGNAT_V6:-none}"
    fi
else
    echo "down"
fi
