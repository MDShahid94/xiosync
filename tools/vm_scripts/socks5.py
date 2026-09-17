#!/usr/bin/env python3
"""
xiogrid-socks5.py <slot>

Pure-Python SOCKS5 proxy that routes outbound connections through ppp{slot}.
Uses SO_BINDTODEVICE (Linux, requires root/CAP_NET_RAW).
Port: 10000 + slot  |  Listen: 0.0.0.0

This replaces dante-server for Ubuntu 26.04+ where dante is not yet packaged.
No external dependencies — stdlib only.

Usage:
  sudo python3 /usr/local/bin/xiogrid/socks5.py 0
  → listens on 0.0.0.0:10000, routes via ppp0

Deploy:
  scp this file to /usr/local/bin/xiogrid/socks5.py on the VM
  called by start-proxy.sh, killed by stop-proxy.sh
"""
from __future__ import annotations
import os
import signal
import socket
import struct
import sys
import threading

# ── Config ──────────────────────────────────────────────────────────────────
SLOT        = int(sys.argv[1])
PPP_IFACE   = f"ppp{SLOT}".encode()
PROXY_PORT  = 10000 + SLOT
LISTEN_ADDR = "0.0.0.0"
PID_FILE    = f"/tmp/xiogrid_proxy/socks5_{SLOT}.pid"
SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)  # Linux = 25

# ── Relay thread ─────────────────────────────────────────────────────────────
def _relay(src: socket.socket, dst: socket.socket) -> None:
    try:
        while chunk := src.recv(8192):
            dst.sendall(chunk)
    except Exception:
        pass
    finally:
        for s in (src, dst):
            try: s.close()
            except Exception: pass


# ── SOCKS5 handler ───────────────────────────────────────────────────────────
def handle(client: socket.socket) -> None:
    try:
        # ── Auth negotiation ────────────────────────────────────────────────
        hdr = client.recv(2)
        if len(hdr) < 2 or hdr[0] != 5:
            return
        client.recv(hdr[1])                  # discard method list
        client.sendall(b"\x05\x00")          # no-auth

        # ── Request ─────────────────────────────────────────────────────────
        req = client.recv(4)
        if len(req) < 4 or req[0] != 5 or req[1] != 1:
            client.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
            return

        atyp = req[3]
        if atyp == 1:        # IPv4
            host = socket.inet_ntoa(client.recv(4))
        elif atyp == 3:      # domain name
            host = client.recv(client.recv(1)[0]).decode()
        elif atyp == 4:      # IPv6
            host = socket.inet_ntop(socket.AF_INET6, client.recv(16))
        else:
            client.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
            return

        port = struct.unpack("!H", client.recv(2))[0]

        # ── Connect via pppN ────────────────────────────────────────────────
        remote = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        remote.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE,
                          PPP_IFACE + b"\x00")  # bind to ppp{slot}
        remote.settimeout(15)
        try:
            ip = socket.gethostbyname(host)
            remote.connect((ip, port))
            remote.settimeout(None)
        except Exception:
            client.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
            remote.close()
            return

        # ── Success reply ───────────────────────────────────────────────────
        bound_ip, bound_port = remote.getsockname()
        client.sendall(
            b"\x05\x00\x00\x01"
            + socket.inet_aton(bound_ip)
            + struct.pack("!H", bound_port)
        )

        # ── Bidirectional relay ─────────────────────────────────────────────
        t1 = threading.Thread(target=_relay, args=(client, remote), daemon=True)
        t2 = threading.Thread(target=_relay, args=(remote, client), daemon=True)
        t1.start(); t2.start()
        t1.join(); t2.join()

    except Exception:
        pass
    finally:
        try: client.close()
        except Exception: pass


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT,  lambda *_: sys.exit(0))

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((LISTEN_ADDR, PROXY_PORT))
    server.listen(256)
    print(
        f"[xiogrid-socks5] slot={SLOT} iface={PPP_IFACE.decode()} "
        f"port={PROXY_PORT} pid={os.getpid()}",
        flush=True,
    )

    while True:
        try:
            client, _ = server.accept()
            threading.Thread(target=handle, args=(client,), daemon=True).start()
        except Exception:
            pass


if __name__ == "__main__":
    main()
