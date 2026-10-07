#!/usr/bin/env python3
"""
xio_proxy_daemon.py — Mac-side residential SOCKS5 proxy + PPPoE relay daemon

Runs on the Mac. Does three things:
1. Listens on 127.0.0.1:1056 as a SOCKS5 proxy (traffic exits via Mac's ISP)
2. For each enrolled worker, pushes reverse SSH tunnel:
     worker:1056       → Mac:1056       → Mac ISP (223.181.48.195)
3. For each PPPoE slot on the VM, relays via Mac to workers:
     worker:10100+slot → Mac → VM:10000+slot → PPPoE IP (223.181.49.x)

WHY reverse tunnels: Colab's Google NAT blocks UDP hole-punch → Tailscale
can't form a direct path → only DERP relay, which drops inbound TCP SYN to
Mac/VM. Mac initiates SSH to worker (outbound, works fine) and creates a
remote port forward so workers can use any residential IP without needing
direct Tailscale reach to Mac or the PPPoE VM.

Port map on each worker (127.0.0.1):
  :1056       → Mac ISP exit
  :10100      → PPPoE slot 0  (different IP per slot from Airtel pool)
  :10101      → PPPoE slot 1
  :10102      → PPPoE slot 2 ... up to 981 slots

Usage:
    # Push Mac SOCKS5 + all active PPPoE slots to one worker:
    python3 xio_proxy_daemon.py --worker 100.72.164.37 --vm 100.106.81.15

    # Auto-discover all enrolled workers from XIOSYNC:
    python3 xio_proxy_daemon.py --workers-from-xiosync --vm 100.106.81.15
"""

from __future__ import annotations

import argparse
import json
import os
import select
import socket
import struct
import subprocess
import threading
import time
import urllib.request

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 19056  # Mac SOCKS5 exit port on worker (19056 avoids tailscaled range 1055-10xxx)
PPPOE_BASE = 19100  # worker port 19100+slot → VM:10000+slot  (away from tailscaled 10100/10101)
SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")
XIOSYNC = os.environ.get("XIOSYNC_URL", "http://localhost:8000")
ADMIN_PASS = os.environ.get("XIOSYNC_ADMIN_PASS", "Xiogrid2026!Admin")
ADMIN_USER = os.environ.get("XIOSYNC_ADMIN_EMAIL", "admin@xiogrid.dev")
ORG_ID = os.environ.get("XIOSYNC_ORG_ID", "00000000-0000-7000-8000-000000000000")

# {(worker_ip, label): Popen}  — label e.g. "mac" or "slot:0"
_tunnels: dict[tuple[str, str], subprocess.Popen] = {}
_lock = threading.Lock()


# ── SOCKS5 server ────────────────────────────────────────────────────────────


def _relay(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            r, _, _ = select.select([src, dst], [], [], 60)
            for s, d in ((src, dst), (dst, src)):
                if s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    d.sendall(data)
    except Exception:
        pass
    finally:
        for s in (src, dst):
            try:
                s.close()
            except Exception:
                pass


def _handle_client(conn: socket.socket) -> None:
    try:
        hdr = conn.recv(256)
        if not hdr or hdr[0] != 5:
            return
        conn.send(b"\x05\x00")  # no-auth

        req = conn.recv(4)
        if len(req) < 4 or req[1] != 0x01:  # only CONNECT
            return
        atype = req[3]
        if atype == 0x01:
            addr = socket.inet_ntoa(conn.recv(4))
        elif atype == 0x03:
            alen = conn.recv(1)[0]
            addr = conn.recv(alen).decode()
        elif atype == 0x04:
            addr = socket.inet_ntop(socket.AF_INET6, conn.recv(16))
        else:
            return
        port = struct.unpack("!H", conn.recv(2))[0]

        try:
            remote = socket.create_connection((addr, port), timeout=10)
        except Exception:
            conn.send(b"\x05\x05\x00\x01" + b"\x00" * 6)
            return
        conn.send(b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0") + b"\x00\x00")
        threading.Thread(target=_relay, args=(conn, remote), daemon=True).start()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def _start_socks5_server() -> None:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((PROXY_HOST, PROXY_PORT))
    srv.listen(128)
    print(f"✅ Mac SOCKS5 proxy on {PROXY_HOST}:{PROXY_PORT}", flush=True)
    while True:
        try:
            conn, _ = srv.accept()
            threading.Thread(target=_handle_client, args=(conn,), daemon=True).start()
        except Exception:
            pass


# ── Reverse tunnel management ────────────────────────────────────────────────


def _ssh_reverse(
    worker_ip: str, remote_port: int, local_host: str, local_port: int, label: str
) -> None:
    """Push ssh -R remote_port:local_host:local_port to worker (idempotent)."""
    key = (worker_ip, label)
    with _lock:
        proc = _tunnels.get(key)
        if proc and proc.poll() is None:
            return

    cmd = [
        "ssh",
        "-i",
        SSH_KEY,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ServerAliveInterval=20",
        "-o",
        "ServerAliveCountMax=5",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "BatchMode=yes",
        "-R",
        f"{remote_port}:{local_host}:{local_port}",
        "-N",
        f"root@{worker_ip}",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        if proc.poll() is None:
            with _lock:
                _tunnels[key] = proc
            print(
                f"  ✅ [{label}] worker:{remote_port} → {local_host}:{local_port} (PID {proc.pid})",
                flush=True,
            )
        else:
            print(f"  ⚠️  [{label}] tunnel to {worker_ip} failed (rc={proc.returncode})", flush=True)
    except Exception as e:
        print(f"  ❌ [{label}] error: {e}", flush=True)


def _push_all_tunnels(worker_ip: str, vm_ip: str | None, active_slots: list[int]) -> None:
    """Push Mac SOCKS5 tunnel + all PPPoE slot relays to one worker."""
    # 1. Mac SOCKS5 exit
    _ssh_reverse(worker_ip, PROXY_PORT, PROXY_HOST, PROXY_PORT, "mac-socks5")

    # 2. PPPoE slot relays: worker:10100+slot → Mac → VM:10000+slot
    if vm_ip:
        for slot in active_slots:
            worker_port = PPPOE_BASE + slot
            vm_port = 10000 + slot
            _ssh_reverse(worker_ip, worker_port, vm_ip, vm_port, f"pppoe-slot{slot}")


# ── XIOSYNC API helpers ──────────────────────────────────────────────────────


def _xiosync_token() -> str:
    body = json.dumps(
        {
            "organization_id": ORG_ID,
            "email": ADMIN_USER,
            "password": ADMIN_PASS,
        }
    ).encode()
    req = urllib.request.Request(
        f"{XIOSYNC}/api/v1/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r).get("access_token", "")


def _active_pppoe_slots() -> list[int]:
    """Return list of idle/assigned slot numbers from XIOSYNC."""
    try:
        token = _xiosync_token()
        req = urllib.request.Request(
            f"{XIOSYNC}/api/v1/pppoe/nodes",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
            nodes = data.get("nodes", data if isinstance(data, list) else [])
            return [
                n["slot"] for n in nodes if n.get("state") in ("idle", "assigned") and "slot" in n
            ]
    except Exception as e:
        print(f"  ⚠️  PPPoE slot discovery failed: {e}", flush=True)
        return []


def _enrolled_worker_ips() -> list[str]:
    """Return Tailscale IPs of all available enrolled workers."""
    try:
        token = _xiosync_token()
        req = urllib.request.Request(
            f"{XIOSYNC}/api/v1/workers",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
            workers = data if isinstance(data, list) else data.get("workers", data.get("items", []))
            return [
                w.get("tailscale_ip") or w.get("ts_ip") or ""
                for w in workers
                if w.get("available") or w.get("active")
            ]
    except Exception as e:
        print(f"  ⚠️  Worker discovery failed: {e}", flush=True)
        return []


# ── Watchdog ─────────────────────────────────────────────────────────────────


def _watchdog(
    static_workers: list[str], auto_discover: bool, vm_ip: str | None, static_slots: list[int]
) -> None:
    while True:
        targets = list(static_workers)
        slots = list(static_slots)

        if auto_discover:
            for ip in _enrolled_worker_ips():
                if ip and ip not in targets:
                    targets.append(ip)
            discovered_slots = _active_pppoe_slots()
            for s in discovered_slots:
                if s not in slots:
                    slots.append(s)

        for ip in targets:
            if not ip:
                continue
            _push_all_tunnels(ip, vm_ip, slots)

        # Reap dead tunnels
        with _lock:
            dead = [k for k, p in _tunnels.items() if p.poll() is not None]
            for k in dead:
                del _tunnels[k]
                if dead:
                    print(f"  🔄 Reaping dead tunnel: {k}", flush=True)

        time.sleep(30)


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="XIOSYNC Mac proxy daemon")
    parser.add_argument(
        "--worker", action="append", default=[], metavar="TS_IP", help="Worker TS IP (repeatable)"
    )
    parser.add_argument(
        "--vm", default=None, metavar="VM_TS_IP", help="PPPoE VM Tailscale IP (e.g. 100.106.81.15)"
    )
    parser.add_argument(
        "--slots",
        default="",
        metavar="0,1,2",
        help="Comma-separated PPPoE slot numbers to relay (default: auto from XIOSYNC)",
    )
    parser.add_argument(
        "--workers-from-xiosync",
        action="store_true",
        help="Auto-discover enrolled workers from XIOSYNC",
    )
    args = parser.parse_args()

    static_slots = (
        [int(s) for s in args.slots.split(",") if s.strip().isdigit()] if args.slots else []
    )

    # Start SOCKS5 server
    threading.Thread(target=_start_socks5_server, daemon=True).start()
    time.sleep(0.3)

    # Self-test
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=8) as r:
            ip = r.read().decode().strip()
        print(f"✅ Mac residential exit IP: {ip}", flush=True)
    except Exception as e:
        print(f"⚠️  Self-test: {e}", flush=True)

    if not args.worker and not args.workers_from_xiosync:
        print("ℹ️  No workers specified. Use --worker <ts_ip> or --workers-from-xiosync")
        print("   SOCKS5 server running. Ctrl-C to stop.")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass
        return

    # Push initial tunnels
    if not static_slots and not args.workers_from_xiosync:
        static_slots = _active_pppoe_slots()
        print(f"  📡 Auto-discovered PPPoE slots from XIOSYNC: {static_slots}", flush=True)

    for ip in args.worker:
        print(f"\n🔗 Pushing tunnels to {ip}...", flush=True)
        _push_all_tunnels(ip, args.vm, static_slots)

    # Watchdog loop
    try:
        _watchdog(args.worker, args.workers_from_xiosync, args.vm, static_slots)
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
        with _lock:
            for proc in _tunnels.values():
                proc.terminate()


if __name__ == "__main__":
    main()
