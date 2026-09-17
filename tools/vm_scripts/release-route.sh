#!/usr/bin/env bash
# xiogrid/release-route.sh <worker_ts_ip> <slot>
# Removes the ip rule added by assign-route.sh.
set -euo pipefail
WORKER_TS_IP="$1"
SLOT="$2"
TABLE=$((1000 + SLOT))

ip rule del from "$WORKER_TS_IP" lookup "$TABLE" priority 50 2>/dev/null || true
echo "released slot=${SLOT} worker=${WORKER_TS_IP}"
