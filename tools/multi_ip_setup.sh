#!/usr/bin/env bash
# multi_ip_setup.sh — 5 simultaneous public IPs via kernel bridge + macvlan
#
# Architecture:
#   bridge200 = en0 (physical Airtel) + vmenet3 (VM NIC port)  ← macOS kernel bridge
#   Mac  pppd on en0's MAC          → ppp0 → IP #1
#   VM   pppd on enp26s0's MAC      → ppp0 → IP #2
#   VM   macvlan mac1               → ppp1 → IP #3
#   VM   macvlan mac2               → ppp2 → IP #4
#   VM   macvlan mac3               → ppp3 → IP #5
#
# Usage:
#   sudo bash multi_ip_setup.sh start     — create bridge + dial all 5 IPs
#   sudo bash multi_ip_setup.sh stop      — disconnect all + restore state
#   sudo bash multi_ip_setup.sh status    — show all current public IPs
#   sudo bash multi_ip_setup.sh watchdog  — auto-reconnect dropped sessions

VM_SSH="karmantu@192.168.54.132"
BRIDGE="bridge200"
WAN_IF="en0"
VM_PORT="vmenet3"
VM_BRIDGE="bridge100"
MAC_PPP_SERVICE="Airtel Direct"

G='\033[0;32m'; R='\033[0;31m'; B='\033[0;34m'; N='\033[0m'
ok()   { echo -e "${G}✅ $*${N}"; }
fail() { echo -e "${R}❌ $*${N}"; }
info() { echo -e "${B}$*${N}"; }

setup_vm() {
    ssh -o StrictHostKeyChecking=no $VM_SSH bash << 'VMEOF'
# macvlan interfaces
declare -A MACS=([mac1]="00:50:56:aa:bb:11" [mac2]="00:50:56:aa:bb:02" [mac3]="00:50:56:aa:bb:03")
for iface in mac1 mac2 mac3; do
    sudo ip link delete ${iface} 2>/dev/null
    sudo ip link add ${iface} link enp26s0 type macvlan
    sudo ip link set ${iface} address ${MACS[$iface]}
    sudo ip link set ${iface} up
    echo "  macvlan ${iface} → ${MACS[$iface]}"
done

# Peer configs
sudo bash -c 'cat > /etc/ppp/peers/airtel-vm << EOF
plugin rp-pppoe.so
enp26s0
user "0352317734739_wifi@airtelbroadband.in"
noauth noipdefault defaultroute persist maxfail 5 holdoff 10
lcp-echo-interval 20 lcp-echo-failure 4
EOF'

for iface in mac1 mac2 mac3; do
    sudo bash -c "cat > /etc/ppp/peers/airtel-${iface} << EOF
plugin rp-pppoe.so
nic-${iface}
user \"0352317734739_wifi@airtelbroadband.in\"
noauth noipdefault defaultroute persist maxfail 5 holdoff 10
lcp-echo-interval 20 lcp-echo-failure 4
EOF"
done

grep -q 'airtelbroadband' /etc/ppp/pap-secrets 2>/dev/null || \
    echo '"0352317734739_wifi@airtelbroadband.in" * "20015653797" *' | sudo tee -a /etc/ppp/pap-secrets > /dev/null
echo "VM ready"
VMEOF
}

cmd_start() {
    info "============================================"
    info " Multi-IP PPPoE — START (target: 5 IPs)"
    info "============================================"

    info "[1] Creating kernel bridge $BRIDGE ($WAN_IF ↔ $VM_PORT)..."
    ifconfig $VM_BRIDGE deletem $VM_PORT 2>/dev/null
    ifconfig $BRIDGE destroy 2>/dev/null
    ifconfig $BRIDGE create
    ifconfig $BRIDGE addm $WAN_IF addm $VM_PORT
    ifconfig $BRIDGE up
    ok "bridge200: en0 ↔ vmenet3 active"

    info "[2] Connecting Mac PPPoE..."
    scutil --nc stop "$MAC_PPP_SERVICE" 2>/dev/null; sleep 2
    scutil --nc start "$MAC_PPP_SERVICE"
    for i in $(seq 1 15); do
        sleep 2
        [[ "$(scutil --nc status "$MAC_PPP_SERVICE" | head -1)" == "Connected" ]] && break
    done
    MAC_IP=$(curl --interface ppp0 -s --max-time 5 https://api.ipify.org 2>/dev/null)
    [[ -n "$MAC_IP" ]] && ok "Mac ppp0 → $MAC_IP" || fail "Mac ppp0 failed to connect"

    info "[3] Setting up VM macvlan + peer configs..."
    setup_vm

    info "[4] Dialing VM PPPoE sessions (12s gap each)..."
    ssh -o StrictHostKeyChecking=no $VM_SSH "sudo pkill pppd 2>/dev/null; sleep 2"
    for peer in airtel-vm airtel-mac1 airtel-mac2 airtel-mac3; do
        ssh -o StrictHostKeyChecking=no $VM_SSH "sudo pon $peer"
        echo "  → $peer started, waiting 12s..."
        sleep 12
    done

    echo ""
    cmd_status
}

cmd_status() {
    info "============================================"
    info " SIMULTANEOUS PUBLIC IPs"
    info "============================================"

    STATUS=$(scutil --nc status "$MAC_PPP_SERVICE" 2>/dev/null | head -1)
    if [[ "$STATUS" == "Connected" ]]; then
        IP=$(curl --interface ppp0 -s --max-time 5 https://api.ipify.org 2>/dev/null)
        ok "Mac  ppp0  → $IP"
    else
        fail "Mac  ppp0  → $STATUS"
    fi

    ssh -o StrictHostKeyChecking=no $VM_SSH bash << 'VMEOF'
LABELS=(enp26s0 mac1 mac2 mac3 mac4)
IDX=0
for iface in ppp0 ppp1 ppp2 ppp3 ppp4; do
    INTERNAL=$(ip addr show ${iface} 2>/dev/null | grep 'inet ' | awk '{print $2}')
    if [[ -n "$INTERNAL" ]]; then
        PUBLIC=$(curl --interface ${iface} -s --max-time 5 https://api.ipify.org 2>/dev/null)
        echo "✅ VM   ${iface}  → ${PUBLIC}  (${LABELS[$IDX]})"
        IDX=$((IDX+1))
    fi
done
TOTAL=$IDX
echo ""
echo "Total VM sessions: $TOTAL"
VMEOF
}

cmd_stop() {
    info "Stopping all sessions and restoring..."
    ssh -o StrictHostKeyChecking=no $VM_SSH "sudo pkill pppd 2>/dev/null; sudo ip link delete mac1 mac2 mac3 2>/dev/null" 2>/dev/null
    scutil --nc stop "$MAC_PPP_SERVICE" 2>/dev/null
    ifconfig $BRIDGE destroy 2>/dev/null
    ifconfig $VM_BRIDGE addm $VM_PORT 2>/dev/null
    ok "Done. $VM_PORT restored to $VM_BRIDGE."
}

cmd_watchdog() {
    info "Watchdog running — checking every 30s. Ctrl+C to stop."
    while true; do
        if [[ "$(scutil --nc status "$MAC_PPP_SERVICE" | head -1)" != "Connected" ]]; then
            echo "$(date '+%H:%M:%S') [WATCHDOG] Mac ppp0 down → reconnecting"
            scutil --nc start "$MAC_PPP_SERVICE"; sleep 20
        fi
        for peer in airtel-vm airtel-mac1 airtel-mac2 airtel-mac3; do
            ALIVE=$(ssh -o StrictHostKeyChecking=no $VM_SSH "pgrep -af '$peer' | grep -v grep" 2>/dev/null)
            if [[ -z "$ALIVE" ]]; then
                echo "$(date '+%H:%M:%S') [WATCHDOG] VM $peer down → reconnecting"
                ssh -o StrictHostKeyChecking=no $VM_SSH "sudo pon $peer" 2>/dev/null
                sleep 15
            fi
        done
        sleep 30
    done
}

case "$1" in
    start)    cmd_start ;;
    stop)     cmd_stop ;;
    status)   cmd_status ;;
    watchdog) cmd_watchdog ;;
    *)
        echo "Usage: sudo bash $0 {start|stop|status|watchdog}"
        echo ""
        echo "  start     — kernel bridge + 5 PPPoE sessions"
        echo "  stop      — disconnect all, restore networking"
        echo "  status    — show all public IPs"
        echo "  watchdog  — auto-reconnect dropped sessions"
        ;;
esac
