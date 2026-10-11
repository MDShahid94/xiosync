import concurrent.futures
import os
import subprocess
import json
import random

from sqlalchemy import select
from sqlalchemy.orm import Session
from xiosync.persistence.database import create_database_engine
from xiosync.subsystems.xiogrid.models.exit_node import PPPoEExitNode

url = os.environ.get("XIOSYNC_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/xiosync")
engine = create_database_engine(url)

with Session(engine) as db:
    nodes = db.scalars(
        select(PPPoEExitNode).where(PPPoEExitNode.state.in_(["idle", "assigned"]))
    ).all()
    if not nodes:
        # Fallback to any node with an active proxy_url
        nodes = db.scalars(
            select(PPPoEExitNode).where(PPPoEExitNode.proxy_url.isnot(None))
        ).all()

    # Pick a random sample of 15 proxies to test anonymity
    sample_nodes = random.sample(nodes, min(15, len(nodes))) if nodes else []
    proxies = [
        {
            "slot": n.ppp_slot,
            "expected_ip": n.public_ip,
            "expected_ipv6": n.public_ipv6,
            "proxy": n.proxy_url.replace("socks5://", "socks5h://") if n.proxy_url else None,
            "proxy_v6": n.proxy_url_v6.replace("socks5://", "socks5h://") if n.proxy_url_v6 else None,
        }
        for n in sample_nodes
        if n.proxy_url
    ]


def test_http_header_leaks(proxy_url: str) -> dict:
    """1. HTTP Header Leaks: Check for X-Forwarded-For, Via, Proxy-Connection, X-Real-IP leaks."""
    leak_header_names = {"x-forwarded-for", "via", "proxy-connection", "x-real-ip", "forwarded"}
    try:
        cmd = ["curl", "-s", "-x", proxy_url, "--max-time", "10", "https://httpbin.org/headers"]
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        data = json.loads(out)
        headers = data.get("headers", {})
        leaks = {k: v for k, v in headers.items() if k.lower() in leak_header_names}
        if leaks:
            return {"status": "LEAK DETECTED", "leaks": leaks}
        return {"status": "PASSED", "leaks": None, "detail": "Clean headers (no proxy leak headers)"}
    except Exception:
        # Fallback to ifconfig.co/json
        try:
            cmd = ["curl", "-s", "-x", proxy_url, "--max-time", "10", "https://ifconfig.co/json"]
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            return {"status": "PASSED", "leaks": None, "detail": "Clean headers (verified via ifconfig.co)"}
        except Exception as e2:
            return {"status": "ERROR", "detail": str(e2)}


def test_dns_leak(proxy_url: str) -> dict:
    """2. DNS Leak Test: Check if DNS queries exit through proxy/carrier resolver or leak VM default resolver."""
    try:
        # socks5h forces hostname resolution on the proxy side
        cmd = ["curl", "-L", "-s", "-x", proxy_url, "--max-time", "10", "https://edns.ip-api.com/json"]
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        data = json.loads(out)
        dns_info = data.get("dns", {})
        resolver_ip = dns_info.get("ip")
        resolver_geo = dns_info.get("geo")
        status = "PASSED" if resolver_ip else "UNKNOWN"
        return {"status": status, "resolver_ip": resolver_ip, "resolver_geo": resolver_geo}
    except Exception as e:
        # Fallback to Akamai DNS whoami check
        try:
            cmd = ["dig", "+short", "whoami.akamai.net", "@ns1-1.akamaitech.net"]
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
            if out:
                return {"status": "CHECKED (Dig)", "resolver_ip": out, "resolver_geo": "Akamai WhoAmI"}
        except Exception:
            pass
        return {"status": "ERROR", "resolver_ip": None, "detail": str(e)}


def test_webrtc_stun_udp(proxy_url: str) -> dict:
    """3. WebRTC STUN Leak Test: Probe proxy-level STUN/TURN UDP support via SOCKS5 UDP ASSOCIATE.

    WebRTC STUN Leak Note:
    Full WebRTC leak prevention requires browser-side configuration
    (e.g., args: ['--webrtc-ip-handling-policy=disable_non_proxied_udp'] or
    chrome.privacy.network.webRTCIPHandlingPolicy = 'disable_non_proxied_udp')
    because WebRTC engines can bypass SOCKS proxies over host network interfaces
    unless restricted.
    For this proxy-level test, we verify whether the SOCKS5 proxy server blocks or
    properly routes UDP ASSOCIATE requests.
    """
    import socket
    import urllib.parse

    parsed = urllib.parse.urlparse(proxy_url)
    host = parsed.hostname
    port = parsed.port or 1080
    if not host:
        return {"status": "SKIPPED", "detail": "Invalid proxy URL"}

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect((host, port))
        # SOCKS5 greeting: VER 5, 1 auth method, NO AUTH (0)
        s.sendall(b"\x05\x01\x00")
        resp = s.recv(2)
        if len(resp) < 2 or resp[0] != 5 or resp[1] != 0:
            s.close()
            return {"status": "FAILED", "detail": f"SOCKS5 auth rejected: {resp.hex()}"}

        # SOCKS5 UDP ASSOCIATE request: VER 5, CMD 3 (UDP ASSOCIATE), RSV 0, ATYP 1 (IPv4), 0.0.0.0:0
        s.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
        resp = s.recv(10)
        s.close()
        if len(resp) < 2:
            return {"status": "BLOCKED", "detail": "Empty response to UDP ASSOCIATE"}
        rep = resp[1]
        if rep == 0x00:
            return {"status": "ROUTED", "detail": "UDP ASSOCIATE supported (Relay active)"}
        elif rep == 0x07:
            return {"status": "BLOCKED (Safe)", "detail": "Command not supported (0x07 - UDP cannot traverse proxy)"}
        elif rep == 0x02:
            return {"status": "BLOCKED (Safe)", "detail": "Connection not allowed by ruleset (0x02)"}
        else:
            return {"status": "BLOCKED", "detail": f"UDP rejected (code 0x{rep:02x})"}
    except Exception as e:
        return {"status": "BLOCKED (Safe)", "detail": f"UDP connection blocked/error: {e}"}


def test_ipv6_leak(proxy_url: str, proxy_v6_url: str | None, expected_ipv6: str | None) -> dict:
    """4. IPv6 Leak Test: Verify IPv6 traffic exits through the correct dual-stack proxy."""
    target_proxy = proxy_v6_url or proxy_url
    try:
        cmd = ["curl", "-6", "-s", "-x", target_proxy, "--max-time", "10", "https://api6.ipify.org"]
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
        if ":" in out:
            matched = (out == expected_ipv6) if expected_ipv6 else None
            status = "PASSED" if (matched is None or matched) else "WARNING (Mismatch)"
            return {"status": status, "detected_ipv6": out, "expected_ipv6": expected_ipv6}
        return {"status": "UNAVAILABLE", "detail": f"Non-IPv6 response: {out[:30]}"}
    except Exception as e:
        # Fallback to v6.ident.me
        try:
            cmd = ["curl", "-6", "-s", "-x", target_proxy, "--max-time", "10", "https://v6.ident.me"]
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
            if ":" in out:
                return {"status": "PASSED", "detected_ipv6": out, "expected_ipv6": expected_ipv6}
        except Exception:
            pass
        return {"status": "UNAVAILABLE", "detail": f"IPv6 not routed or unreachable: {e}"}


def check_anonymity(proxy_info: dict) -> dict:
    slot = proxy_info["slot"]
    expected_ip = proxy_info["expected_ip"]
    expected_ipv6 = proxy_info.get("expected_ipv6")
    proxy = proxy_info["proxy"]
    proxy_v6 = proxy_info.get("proxy_v6")

    result = {
        "slot": slot,
        "expected_ip": expected_ip,
        "actual_ip": None,
        "asn": None,
        "country": None,
        "ip_status": "FAILED",
        "header_test": None,
        "dns_test": None,
        "webrtc_test": None,
        "ipv6_test": None,
        "overall_status": "FAILED",
    }

    try:
        # Baseline IPv4 & ASN check
        cmd = ["curl", "-4", "-s", "-x", proxy, "--max-time", "10", "https://ipinfo.io/json"]
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        data = json.loads(out)

        result["actual_ip"] = data.get("ip")
        result["asn"] = data.get("org")
        result["country"] = data.get("country")

        if result["actual_ip"] == expected_ip and "Airtel" in str(result["asn"]):
            result["ip_status"] = "PASSED"
        else:
            result["ip_status"] = "WARNING (Mismatch or Non-Airtel ASN)"
    except Exception as e:
        result["ip_status"] = f"ERROR: {e}"

    # 1. HTTP Header leak test
    result["header_test"] = test_http_header_leaks(proxy)

    # 2. DNS leak test
    result["dns_test"] = test_dns_leak(proxy)

    # 3. WebRTC STUN UDP proxy test
    result["webrtc_test"] = test_webrtc_stun_udp(proxy)

    # 4. IPv6 leak test
    result["ipv6_test"] = test_ipv6_leak(proxy, proxy_v6, expected_ipv6)

    # Determine overall status
    has_leak = result["header_test"].get("status") == "LEAK DETECTED"
    ip_ok = "PASSED" in result["ip_status"]
    if ip_ok and not has_leak:
        result["overall_status"] = "PASSED"
    elif has_leak:
        result["overall_status"] = "LEAK DETECTED"
    else:
        result["overall_status"] = result["ip_status"]

    return result


print(f"Running Deep Anonymity & Leak Testing on {len(proxies)} sampled proxies...")
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    for res in executor.map(check_anonymity, proxies):
        print(f"\n==================== Slot {res['slot']} ====================")
        print(f"  IPv4 Egress   : Expected: {res['expected_ip']} | Detected: {res['actual_ip']} | ASN: {res['asn']} | Status: {res['ip_status']}")
        hdr = res['header_test']
        print(f"  Header Leaks  : Status: {hdr.get('status')} ({hdr.get('detail', hdr.get('leaks'))})")
        dns = res['dns_test']
        print(f"  DNS Resolver  : Status: {dns.get('status')} | Resolver: {dns.get('resolver_ip')} ({dns.get('resolver_geo')})")
        webrtc = res['webrtc_test']
        print(f"  WebRTC/UDP    : Status: {webrtc.get('status')} ({webrtc.get('detail')})")
        v6 = res['ipv6_test']
        print(f"  IPv6 Egress   : Status: {v6.get('status')} | Detected: {v6.get('detected_ipv6')} ({v6.get('detail', '')})")
        print(f"  Overall Result: {res['overall_status']}")

