"""geo_lookup.py — Cached IP-to-geolocation resolver for fingerprint injection.

Lookup chain:
  1. Check PPPoEExitNode.meta['geo'] cache (0ms, preferred)
  2. Live query ip-api.com via SOCKS5 proxy (200-500ms, cached on success)
  3. Fallback defaults (0ms, last resort)

The geo data is injected into build_init_script() for:
  - navigator.geolocation.getCurrentPosition()
  - Date.prototype.getTimezoneOffset()
  - Intl.DateTimeFormat timezone consistency
"""

from __future__ import annotations

import logging
import zoneinfo
from dataclasses import dataclass
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

_GEO_API_URL = "http://ip-api.com/json/{ip}?fields=lat,lon,timezone,city,country,query"
_GEO_TIMEOUT = 3.0  # seconds


@dataclass(frozen=True)
class GeoResult:
    """Resolved geolocation for a public IP."""

    lat: float
    lon: float
    timezone: str
    tz_offset_min: int  # Date.getTimezoneOffset()-compatible (minutes west of UTC)
    city: str
    country: str

    @staticmethod
    def fallback(timezone: str = "America/New_York") -> GeoResult:
        """Default fallback when lookup fails."""
        return GeoResult(
            lat=40.7128,
            lon=-74.0060,
            timezone=timezone,
            tz_offset_min=_compute_tz_offset(timezone),
            city="New York",
            country="US",
        )


def _compute_tz_offset(tz_name: str) -> int:
    """Compute Date.getTimezoneOffset()-compatible value.

    getTimezoneOffset() returns minutes *west* of UTC.
    UTC+5:30 (IST) → -330,  UTC-5 (EST) → 300.
    """
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
        offset = datetime.now(tz).utcoffset()
        if offset is None:
            return 0
        return -int(offset.total_seconds() // 60)
    except Exception:
        return 0


async def lookup_geo(
    public_ip: str,
    *,
    exit_node_id: str | None = None,
    engine: object | None = None,
) -> GeoResult:
    """Resolve geolocation for a public IP, with DB caching.

    Args:
        public_ip:     The exit node's public IP address.
        exit_node_id:  UUID of the PPPoEExitNode (for caching in meta JSONB).
        engine:        SQLAlchemy engine (for DB cache read/write).

    Returns:
        GeoResult with coordinates, timezone, and offset.
    """
    # ── 1. Check DB cache ──────────────────────────────────────────────────
    if exit_node_id and engine:
        cached = _read_geo_cache(exit_node_id, engine)
        if cached:
            return cached

    # ── 2. Live lookup ─────────────────────────────────────────────────────
    result = await _live_lookup(public_ip)

    # ── 3. Cache on success ────────────────────────────────────────────────
    if result and exit_node_id and engine:
        _write_geo_cache(exit_node_id, result, engine)

    return result or GeoResult.fallback()


async def _live_lookup(ip: str) -> GeoResult | None:
    """Query ip-api.com for geo data. Returns None on failure."""
    try:
        async with httpx.AsyncClient(timeout=_GEO_TIMEOUT) as client:
            resp = await client.get(_GEO_API_URL.format(ip=ip))
            resp.raise_for_status()
            data = resp.json()

        tz = data.get("timezone", "America/New_York")
        return GeoResult(
            lat=float(data.get("lat", 0)),
            lon=float(data.get("lon", 0)),
            timezone=tz,
            tz_offset_min=_compute_tz_offset(tz),
            city=data.get("city", "Unknown"),
            country=data.get("country", "Unknown"),
        )
    except Exception as exc:
        logger.warning("geo_lookup.live_failed", extra={"ip": ip, "error": str(exc)})
        return None


def _read_geo_cache(exit_node_id: str, engine: object) -> GeoResult | None:
    """Read cached geo from PPPoEExitNode.meta['geo']."""
    try:
        import uuid  # noqa: PLC0415, E402

        from sqlalchemy import text  # noqa: PLC0415
        from sqlalchemy.orm import Session as OrmSession  # noqa: PLC0415

        with OrmSession(engine) as sess:
            row = sess.execute(
                text("SELECT meta FROM xiogrid_pppoe_exit_nodes WHERE id = :nid LIMIT 1"),
                {"nid": uuid.UUID(exit_node_id) if isinstance(exit_node_id, str) else exit_node_id},
            ).scalar()

        if not row or not isinstance(row, dict):
            return None

        geo = row.get("geo")
        if not geo or not isinstance(geo, dict):
            return None

        return GeoResult(
            lat=float(geo["lat"]),
            lon=float(geo["lon"]),
            timezone=geo["timezone"],
            tz_offset_min=_compute_tz_offset(geo["timezone"]),
            city=geo.get("city", "Unknown"),
            country=geo.get("country", "Unknown"),
        )
    except Exception as exc:
        logger.debug(
            "geo_lookup.cache_miss",
            extra={
                "exit_node_id": exit_node_id,
                "error": str(exc),
            },
        )
        return None


def _write_geo_cache(exit_node_id: str, result: GeoResult, engine: object) -> None:
    """Write geo data to PPPoEExitNode.meta['geo']. Best-effort."""
    try:
        import json as _json  # noqa: PLC0415
        import uuid  # noqa: PLC0415

        from sqlalchemy import text  # noqa: PLC0415
        from sqlalchemy.orm import Session as OrmSession  # noqa: PLC0415

        geo_data = {
            "lat": result.lat,
            "lon": result.lon,
            "timezone": result.timezone,
            "city": result.city,
            "country": result.country,
        }

        with OrmSession(engine) as sess:
            sess.execute(
                text("""
                    UPDATE xiogrid_pppoe_exit_nodes
                    SET meta = jsonb_set(COALESCE(meta, '{}'), '{geo}', :geo_json::jsonb)
                    WHERE id = :nid
                """),
                {
                    "nid": (
                        uuid.UUID(exit_node_id) if isinstance(exit_node_id, str) else exit_node_id
                    ),
                    "geo_json": _json.dumps(geo_data),
                },
            )
            sess.commit()
        logger.info(
            "geo_lookup.cached",
            extra={
                "exit_node_id": exit_node_id,
                "city": result.city,
                "tz": result.timezone,
            },
        )
    except Exception as exc:
        logger.warning(
            "geo_lookup.cache_write_failed",
            extra={
                "exit_node_id": exit_node_id,
                "error": str(exc),
            },
        )
