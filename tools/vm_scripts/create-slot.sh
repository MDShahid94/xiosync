#!/usr/bin/env bash
# xiogrid/create-slot.sh <slot>
# Creates macvlan mv{slot} + starts pppd unit {slot}.
# Blocks up to 35s waiting for public IP.
# Output: "up <public_ip> <cgnat_ip>"  or  "connecting unknown unknown"
#
# PPPoE credentials injected by XIOSYNC at deploy time (sed replacement):
PPPOE_USER="__PPPOE_USER__"
PPPOE_PASS="__PPPOE_PASS__"
PARENT_IFACE="__PARENT_IFACE__"
MAC_OUI="__MAC_OUI__"
set -euo pipefail

SLOT="$1"
MAC=$(printf "${MAC_OUI}:%02x:%02x" $((SLOT / 256)) $((SLOT % 256)))
MV="mv${SLOT}"
PPP="ppp${SLOT}"
LOG="/tmp/ppp_logs/${PPP}.log"

mkdir -p /tmp/ppp_logs

# ── PROMISC guard ───────────────────────────────────────────────────────────
# CRITICAL: The kernel removes PROMISC from the parent when the LAST macvlan
# on that parent is deleted (e.g., after mass-teardown of all sessions).
# Without PROMISC, enp26s0 drops PADO frames addressed to macvlan MACs →
# "Timeout waiting for PADO packets" even though the BRAS is responding.
# We set it unconditionally here — idempotent and takes <1ms.
ip link set "${PARENT_IFACE}" promisc on

# ── Create macvlan ─────────────────────────────────────────────────────────
if ! ip link show "${MV}" &>/dev/null 2>&1; then
    ip link add "${MV}" link "${PARENT_IFACE}" type macvlan mode bridge
    # MAC must be set SEPARATELY after creation (inline fails on this kernel)
    ip link set "${MV}" address "${MAC}"
fi
ip link set "${MV}" up 2>/dev/null || true

# ── Kernel settle delay ────────────────────────────────────────────────────
# pppd races the kernel if started immediately after ip link up.
# ip link show forces a kernel round-trip, giving the macvlan driver ~150ms
# to fully register the interface before pppd opens its raw socket.
ip link show "${MV}" > /dev/null 2>&1
sleep 0.2

# ── Start pppd ─────────────────────────────────────────────────────────────
if ! pgrep -f "unit ${SLOT}[^0-9]" &>/dev/null; then
    # Use nohup (not setsid) to survive SSH session end while keeping
    # the process properly supervised. debug flag removed for production.
    # maxfail 0 = unlimited retries under persist mode.
    # holdoff 10 = 10s between reconnect attempts (avoids BRAS rate-limit).
    nohup pppd plugin rp-pppoe.so "${MV}" \
        user "${PPPOE_USER}" password "${PPPOE_PASS}" \
        noauth nodefaultroute noipdefault \
        persist maxfail 0 holdoff 10 \
        lcp-echo-interval 20 lcp-echo-failure 4 \
        unit "${SLOT}" logfile "${LOG}" \
        < /dev/null >> /tmp/ppp_logs/pppd_stdout.log 2>&1 &
fi

# ── Wait for IP (up to 35s) ────────────────────────────────────────────────
for i in $(seq 1 35); do
    sleep 1
    if ip link show "${PPP}" &>/dev/null 2>&1; then
        CGNAT=$(ip addr show "${PPP}" 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 || true)
        PUB=$(curl --interface "${PPP}" -s --max-time 4 https://api.ipify.org 2>/dev/null || true)
        if [[ -n "${PUB}" ]]; then
            # ── Start per-slot SOCKS5 proxy ─────────────────────────────────
            # Each slot gets its own Python SOCKS5 proxy on port 10000+slot.
            # Routes via ppp{slot} using SO_BINDTODEVICE.
            # Colab workers use proxy_url for per-context exit IP isolation.
            PROXY_PORT=$((10000 + SLOT))
            TS_IP=$(tailscale ip -4 2>/dev/null || echo "0.0.0.0")
            /usr/local/bin/xiogrid/start-proxy.sh "${SLOT}" > /dev/null 2>&1 || true
            PROXY_URL="socks5://${TS_IP}:${PROXY_PORT}"

            echo "up ${PUB} ${CGNAT:-unknown} ${PROXY_URL}"
            exit 0
        fi
    fi
done

echo "connecting unknown unknown none"
