"""colab.agent.network.geo — IP geolocation, timezone resolution, and public IP detection."""

from __future__ import annotations

import json
import logging
import subprocess
import urllib.request
from typing import Any

logger = logging.getLogger("xiorun_agent.network.geo")

# ── Dynamic IP → Timezone resolver ────────────────────────────────────────────
# Uses ip-api.com (free, no API key, 45 req/min limit).
# Results are cached forever per IP — geo of a given IP never changes.
_IP_TZ_CACHE: dict[str, str] = {}
_IP_GEO_CACHE: dict[str, dict[str, Any]] = {}


def _resolve_ip_geo(public_ip: str | None) -> dict[str, Any]:
    """Resolve full geo profile for a public IP using ip-api.com.

    Returns dict with: timezone, lat, lon, country, countryCode, city, isp, org, locale
    Falls back to safe defaults on any error.  Never raises.
    """
    _DEFAULTS: dict[str, Any] = {
        "timezone": "UTC",
        "lat": 0.0,
        "lon": 0.0,
        "country": "United States",
        "countryCode": "US",
        "city": "New York",
        "isp": "Comcast Cable",
        "org": "AS7922 Comcast Cable Communications",
        "locale": "en-US",
    }
    if not public_ip or public_ip.startswith(("10.", "192.168.", "100.", "127.")):
        return _DEFAULTS
    if public_ip in _IP_GEO_CACHE:
        return _IP_GEO_CACHE[public_ip]
    try:
        fields = "status,timezone,lat,lon,country,countryCode,city,isp,org"
        url = f"http://ip-api.com/json/{public_ip}?fields={fields}"
        req = urllib.request.Request(url, headers={"User-Agent": "xiorun-agent/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        if data.get("status") == "success":
            cc = data.get("countryCode", "US")
            # Map country code to primary locale
            _CC_LOCALE = {
                "IN": "en-IN",
                "GB": "en-GB",
                "AU": "en-AU",
                "CA": "en-CA",
                "US": "en-US",
                "PK": "en-PK",
                "NG": "en-NG",
                "PH": "en-PH",
                "DE": "de-DE",
                "FR": "fr-FR",
                "JP": "ja-JP",
                "CN": "zh-CN",
                "BR": "pt-BR",
                "RU": "ru-RU",
                "MX": "es-MX",
                "KR": "ko-KR",
            }
            result: dict[str, Any] = {
                "timezone": data.get("timezone", "UTC"),
                "lat": float(data.get("lat", 0.0)),
                "lon": float(data.get("lon", 0.0)),
                "country": data.get("country", "United States"),
                "countryCode": cc,
                "city": data.get("city", ""),
                "isp": data.get("isp", ""),
                "org": data.get("org", ""),
                "locale": _CC_LOCALE.get(cc, "en-US"),
            }
        else:
            result = _DEFAULTS.copy()
    except Exception as e:
        logger.warning(f"ip-geo-lookup failed for {public_ip}: {e}")
        result = _DEFAULTS.copy()
    _IP_GEO_CACHE[public_ip] = result
    _IP_TZ_CACHE[public_ip] = result["timezone"]
    logger.info(
        f"ip-geo: {public_ip} → tz={result['timezone']} loc={result['city']},{result['countryCode']} lat={result['lat']:.2f} lon={result['lon']:.2f}"
    )
    return result


def _resolve_ip_timezone(public_ip: str | None) -> str:
    """Convenience wrapper — returns just timezone string."""
    return _resolve_ip_geo(public_ip).get("timezone", "UTC")


def _get_proxied_public_ip(socks5h_port: int = 19055, timeout: int = 8) -> str | None:
    """Query public IP through the WS SOCKS5 bridge → PPPoE residential exit.

    Returns the residential IP that the browser actually exits from — NOT the
    Colab datacenter IP. This is what must be used for geo-resolve so that the
    stealth JS timezone/locale/lat/lon match the proxy exit, not the GCP datacenter.

    Uses socks5h (remote hostname resolution) so ipify.org domain is sent to the
    PPPoE proxy and resolved from the residential connection — identical to how
    Chrome sends traffic with --proxy-server=socks5://.

    Falls back to direct (Colab IP) only if the bridge is not reachable.
    IP changes on every Mac reboot — never cached, always live per call.
    """
    try:
        import socket as _s
        import struct as _struct

        def _socks5h_get(
            host: str, port: int, target_host: str, target_port: int, timeout: int
        ) -> bytes:
            """Open a SOCKS5h connection through host:port to target_host:target_port."""
            sock = _s.create_connection((host, port), timeout=timeout)
            sock.settimeout(timeout)
            # Greeting: no auth
            sock.sendall(b"\x05\x01\x00")
            if sock.recv(2) != b"\x05\x00":
                raise ConnectionError("SOCKS5 auth failed")
            # CONNECT with ATYP=3 (domain) — remote hostname resolution
            host_b = target_host.encode()
            sock.sendall(
                b"\x05\x01\x00\x03"
                + bytes([len(host_b)])
                + host_b
                + _struct.pack("!H", target_port)
            )
            rep = sock.recv(10)
            if len(rep) < 2 or rep[1] != 0:
                raise ConnectionError(f"SOCKS5 CONNECT failed: {rep!r}")
            # HTTP/1.0 GET
            sock.sendall(b"GET / HTTP/1.0\r\nHost: api.ipify.org\r\n\r\n")
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            sock.close()
            # Response body is after \r\n\r\n
            return data.split(b"\r\n\r\n", 1)[-1].strip()

        ip = _socks5h_get("127.0.0.1", socks5h_port, "api.ipify.org", 80, timeout)
        if ip and len(ip) < 40:
            return ip.decode()
    except Exception:
        pass

    # Fallback: direct (Colab datacenter IP) — geo will be wrong but better than None
    try:
        import urllib.request as _ur

        with _ur.urlopen("https://api.ipify.org", timeout=5) as resp:
            return resp.read().decode().strip()
    except Exception:
        return None


def _get_runtime_public_ip() -> str | None:
    """Detect this runtime's public IP via proxy (residential exit) if available,
    else direct. Prefer _get_proxied_public_ip() for all geo-resolve calls.
    Returns None on total failure — caller falls back to 'UTC'.
    """
    return _get_proxied_public_ip()


# ── Tailscale IP helper ────────────────────────────────────────────────────────
_my_ts_ip: str | None = None


def _my_tailscale_ip() -> str:
    global _my_ts_ip
    if _my_ts_ip:
        return _my_ts_ip
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
        )
        ip = result.stdout.strip()
        if ip:
            _my_ts_ip = ip
            return ip
    except Exception:
        pass
    return "127.0.0.1"
