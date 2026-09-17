#!/usr/bin/env python3
"""
PPPoE Transparent Bridge Relay v3 — Crash-resistant version.
Fixes: 'Layer [Ether] not found' crash by catching exceptions and restarting.
"""

import threading
import sys
import signal
import logging
import time
from scapy.all import sniff, sendp, Ether, conf

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)

WAN_IFACE = "en0"
LAN_IFACE = "bridge100"
VM_MAC    = "00:50:56:23:5a:24"

running = True
stats = {"lan2wan": 0, "wan2lan": 0, "errors": 0, "restarts": 0}


def vm_to_wan(pkt):
    try:
        if not running:
            return
        if not pkt.haslayer(Ether):
            return
        if pkt[Ether].src != VM_MAC:
            return
        stats["lan2wan"] += 1
        log.debug(f"[LAN→WAN #{stats['lan2wan']}] {pkt[Ether].src}→{pkt[Ether].dst} type={hex(pkt[Ether].type)}")
        sendp(pkt, iface=WAN_IFACE, verbose=False)
    except Exception as e:
        stats["errors"] += 1
        log.warning(f"[LAN→WAN error] {e}")


def wan_to_vm(pkt):
    try:
        if not running:
            return
        if not pkt.haslayer(Ether):
            return
        src = pkt[Ether].src
        dst = pkt[Ether].dst
        if src == VM_MAC:
            return
        if dst != VM_MAC and dst != "ff:ff:ff:ff:ff:ff":
            return
        stats["wan2lan"] += 1
        etype = hex(pkt[Ether].type)
        log.info(f"[WAN→LAN #{stats['wan2lan']}] {src}→{dst} type={etype}")
        sendp(pkt, iface=LAN_IFACE, verbose=False)
    except Exception as e:
        stats["errors"] += 1
        log.warning(f"[WAN→LAN error] {e}")


def run_sniffer(iface, callback, promisc, name):
    """Run sniffer with auto-restart on crash."""
    while running:
        try:
            log.info(f"[{name}] Sniffer starting on {iface} (promisc={promisc})")
            sniff(
                iface=iface,
                prn=callback,
                store=False,
                promisc=promisc,
            )
        except Exception as e:
            if not running:
                break
            stats["restarts"] += 1
            log.warning(f"[{name}] Sniffer crashed: {e} — restarting in 1s (restart #{stats['restarts']})")
            time.sleep(1)


def stats_loop():
    while running:
        time.sleep(15)
        log.info(f"[STATS] LAN→WAN: {stats['lan2wan']} | WAN→LAN: {stats['wan2lan']} | errors: {stats['errors']} | restarts: {stats['restarts']}")


def main():
    global running

    log.info("=== PPPoE Bridge Relay v3 (crash-resistant) ===")
    log.info(f"WAN={WAN_IFACE} (en0 MAC: d0:11:e5:d2:88:c2)")
    log.info(f"LAN={LAN_IFACE}  VM_MAC={VM_MAC}")
    log.info("")
    log.info("On Ubuntu VM run:")
    log.info("  sudo pppd pty '/usr/sbin/pppoe -I enp26s0 -C AIRBRAS_WB-KHR-1' \\")
    log.info("    user '0352317734739_wifi@airtelbroadband.in' \\")
    log.info("    password '20015653797' noauth defaultroute usepeerdns nodetach")

    def handle_exit(sig, frame):
        global running
        running = False
        log.info(f"Shutdown. LAN→WAN: {stats['lan2wan']} | WAN→LAN: {stats['wan2lan']}")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    t1 = threading.Thread(
        target=run_sniffer,
        args=(LAN_IFACE, vm_to_wan, False, "LAN→WAN"),
        daemon=True, name="LAN→WAN"
    )
    t2 = threading.Thread(
        target=run_sniffer,
        args=(WAN_IFACE, wan_to_vm, True, "WAN→LAN"),
        daemon=True, name="WAN→LAN"
    )
    t3 = threading.Thread(target=stats_loop, daemon=True, name="Stats")

    t1.start(); t2.start(); t3.start()
    log.info("Relay running. Press Ctrl+C to stop.")
    t1.join(); t2.join()


if __name__ == "__main__":
    main()
