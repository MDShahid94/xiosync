"""Per-profile hardware fingerprint generation, catalog presets, and persistence."""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from typing import Any

from colab.agent.stealth.stealth_js import _CHROME_VERSION

logger = logging.getLogger("xiorun_agent.stealth.fingerprint")

# ── Per-profile fingerprint persistence ───────────────────────────────────────
# Maps each Chrome profile (PRFL-001 etc.) to a stable device identity stored
# alongside its .tar.gz on Drive as PRFL-NNN.fingerprint.json.
# Keys match exactly what the existing launch_browser() machinery expects.
#
# Only the STATIC identity fields are persisted (canvas/audio seeds, UA, screen,
# hardware). Runtime-dynamic fields (timezone, locale, lat/lon) are resolved
# live from the exit-node IP by the existing _resolve_ip_geo() — same as always.

_PROFILE_FINGERPRINT_CACHE: dict[str, dict[str, Any]] = {}

# Deterministic base presets per profile number — each profile = distinct persona
_PRFL_PRESETS: dict[int, dict[str, Any]] = {
    1: {  # PRFL-001: Indian professional, Windows 10 laptop, GTX 1050
        "canvas_seed": 0x1A2B3C,
        "audio_seed": 0x4D5E6F,
        "ua_template": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{_CHROME_VERSION} Safari/537.36"
        ),
        "platform": "Win32",
        "ch_platform": "Windows",
        "ch_arch": "x86",
        "width": 1920,
        "height": 1080,
        "cores": 8,
        "ram": 8,
        "is_mobile": False,
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    2: {  # PRFL-002: Indian student, macOS Sequoia, M1 MacBook Air
        "canvas_seed": 0x7A8B9C,
        "audio_seed": 0xABCDEF,
        "ua_template": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "platform": "MacIntel",
        "ch_platform": "macOS",
        "ch_arch": "arm",
        "width": 2560,
        "height": 1600,
        "cores": 10,
        "ram": 16,
        "is_mobile": False,
        "webgl_renderer": "ANGLE (Apple, Apple M1, OpenGL 4.1)",
    },
    5: {  # PRFL-005: Developer, Linux Mint, Intel UHD
        "canvas_seed": 0xC0FFEE,
        "audio_seed": 0xDEADBE,
        "ua_template": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/129.0.0.0 Safari/537.36"
        ),
        "platform": "Linux x86_64",
        "ch_platform": "Linux",
        "ch_arch": "x86",
        "width": 1920,
        "height": 1200,
        "cores": 16,
        "ram": 32,
        "is_mobile": False,
        "webgl_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
}
_PRFL_DEFAULT_PRESET = _PRFL_PRESETS[1]


def derive_hardware_display(
    ua: str,
    canvas_seed: int = 0,
    audio_seed: int = 0,
    webgl_seed: int = 0,
    timezone: str = "UTC",
    locale: str = "en-US",
    profile_id: str = "",
    profile_serial: int = 0,
    email: str = "",
    identity_id: str = "",
) -> dict[str, Any]:
    """Derive hardware/display attributes from UA string and deterministic noise seeds.

    These must be CONSISTENT across sessions — any mismatch triggers
    Google's new-device signal. Seeds ensure deterministic noise.
    """
    if "Windows" in ua:
        platform = "Win32"
        screen_w, screen_h = 1920, 1080
        color_depth = 24
        hw_concurrency = 8
        device_memory = 8
        max_touch = 0
    elif "Macintosh" in ua:
        platform = "MacIntel"
        screen_w, screen_h = 2560, 1440
        color_depth = 30
        hw_concurrency = 10
        device_memory = 16
        max_touch = 0
    elif "Linux" in ua:
        platform = "Linux x86_64"
        screen_w, screen_h = 1920, 1080
        color_depth = 24
        hw_concurrency = 4
        device_memory = 4
        max_touch = 0
    else:
        platform = "Win32"
        screen_w, screen_h = 1920, 1080
        color_depth = 24
        hw_concurrency = 8
        device_memory = 8
        max_touch = 0

    webgl_renderers = [
        ("ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11)", "Google Inc. (NVIDIA)"),
        ("ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)", "Google Inc. (Intel)"),
        ("ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)", "Google Inc. (AMD)"),
        ("ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)", "Google Inc. (NVIDIA)"),
    ]
    webgl_renderer, webgl_vendor = webgl_renderers[canvas_seed % len(webgl_renderers)]

    return {
        # ── Identity ──────────────────────────────────────────────
        "profile_id": profile_id,
        "profile_serial": profile_serial,
        "email": email,
        "identity_id": identity_id,
        # ── Noise seeds (deterministic per-profile) ────────────────
        "canvas_seed": canvas_seed,
        "audio_seed": audio_seed,
        "webgl_seed": webgl_seed,
        # ── Navigator / UA ─────────────────────────────────────────
        "ua": ua,
        "platform": platform,
        "vendor": "Google Inc.",
        "language": locale,
        "languages": [locale, locale.split("-")[0]],
        "hardware_concurrency": hw_concurrency,
        "device_memory": device_memory,
        "max_touch_points": max_touch,
        # ── Screen / Display ───────────────────────────────────────
        "screen_width": screen_w,
        "screen_height": screen_h,
        "color_depth": color_depth,
        "pixel_ratio": 1.0 if color_depth == 24 else 2.0,
        # ── WebGL ──────────────────────────────────────────────────
        "webgl_renderer": webgl_renderer,
        "webgl_vendor": webgl_vendor,
        # ── Locale / Time ──────────────────────────────────────────
        "timezone": timezone,
        "locale": locale,
        # ── Meta ───────────────────────────────────────────────────
        "created_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "updated_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "schema_version": 2,
    }


def save_fingerprint_to_drive(
    drive_root: str,
    fp_data: dict[str, Any],
    profile_id: str,
    profile_serial: int = 0,
) -> tuple[str, str]:
    """Save fingerprint JSON to Drive FUSE (serial path + legacy hex path symlink)."""
    prfl_label = f"PRFL-{profile_serial:03d}" if profile_serial else f"PRFL_{profile_id}"
    fp_path_serial = os.path.join(drive_root, "profiles", f"{prfl_label}.fingerprint.json")
    fp_path_hex = os.path.join(drive_root, "profiles", f"PRFL_{profile_id}.fingerprint.json")

    os.makedirs(os.path.dirname(fp_path_serial), exist_ok=True)
    with open(fp_path_serial, "w") as ff:
        json.dump(fp_data, ff, indent=2)

    # Symlink hex-id path → serial path for backward compat
    if fp_path_hex != fp_path_serial and not os.path.exists(fp_path_hex):
        try:
            os.symlink(fp_path_serial, fp_path_hex)
        except Exception:
            with open(fp_path_hex, "w") as ff2:
                json.dump(fp_data, ff2, indent=2)

    logger.info(f"fingerprint saved → {fp_path_serial} (schema_v2, {len(fp_data)} fields)")
    return fp_path_serial, fp_path_hex


def vault_fingerprint(
    fp_data: dict[str, Any],
    identity_id: str,
    xiosync_base: str = "",
    internal_secret: str = "",
    org_id: str = "00000000-0000-7000-8000-000000000000",
) -> bool:
    """Vault the fingerprint (encrypted, survives Drive wipe)."""
    if not identity_id or not xiosync_base:
        return False
    try:
        import httpx as _hx_fp  # noqa: PLC0415

        fp_vault_key = f"identities/{identity_id}/fingerprint"
        fp_r = _hx_fp.post(
            f"{xiosync_base}/api/v1/xioflow/events/vault-fingerprint-internal",
            headers={
                "X-XIOSYNC-Internal": internal_secret,
                "Content-Type": "application/json",
            },
            json={
                "fingerprint": fp_data,
                "identity_id": identity_id,
                "org_id": org_id,
            },
            timeout=8,
        )
        if fp_r.status_code == 200:
            logger.info(f"fingerprint vaulted → {fp_vault_key}")
            return True
        logger.warning(f"fingerprint vault HTTP {fp_r.status_code}")
        return False
    except Exception as fve:
        logger.warning(f"fingerprint vault skipped ({fve})")
        return False


def _load_profile_fingerprint(profile_id: str, drive_root: str) -> dict[str, Any]:
    """Load or create a persistent fingerprint for a profile.

    Reads Drive/profiles/PRFL-NNN.fingerprint.json if it exists.
    Otherwise generates from _PRFL_PRESETS and saves to Drive.

    Returns a dict compatible with launch_browser() fingerprint format:
    canvas_seed, audio_seed, ua_template, width, height, cores, ram,
    platform, ch_platform, ch_arch, is_mobile, webgl_renderer.
    """
    if profile_id in _PROFILE_FINGERPRINT_CACHE:
        return _PROFILE_FINGERPRINT_CACHE[profile_id]

    fp_path = os.path.join(drive_root, "profiles", f"{profile_id}.fingerprint.json")

    # 1. Try loading from Drive (fast local cache)
    if os.path.isfile(fp_path):
        try:
            with open(fp_path) as _f:
                fp = json.load(_f)
            _PROFILE_FINGERPRINT_CACHE[profile_id] = fp
            logger.info(f"profile_fingerprint: loaded {profile_id} from {fp_path}")
            return fp
        except Exception as _e:
            logger.warning(f"profile_fingerprint: load failed for {profile_id}: {_e}")

    # 2. Drive file missing — try vault (encrypted cross-runtime source of truth)
    _xio = os.environ.get("XIORUN_XIOSYNC_BASE", os.environ.get("XIOSYNC_BASE", ""))
    _secret = os.environ.get("XIORUN_INTERNAL_SECRET", os.environ.get("XIOSYNC_INTERNAL_SECRET", ""))
    if _xio and _secret:
        try:
            import httpx as _hx_lp  # noqa: PLC0415

            if len(profile_id.replace("-", "")) == 16 and not profile_id.startswith("PRFL-"):
                _iid_hint = profile_id
                _vr = _hx_lp.get(
                    f"{_xio}/api/v1/xioflow/events/vault-fingerprint-internal",
                    params={"identity_id": _iid_hint},
                    headers={"X-XIOSYNC-Internal": _secret},
                    timeout=5,
                )
                if _vr.status_code == 200:
                    _vd = _vr.json()
                    if _vd.get("found") and _vd.get("fingerprint"):
                        fp = _vd["fingerprint"]
                        _PROFILE_FINGERPRINT_CACHE[profile_id] = fp
                        try:
                            os.makedirs(os.path.dirname(fp_path), exist_ok=True)
                            with open(fp_path, "w") as _f:
                                json.dump(fp, _f, indent=2)
                        except Exception:
                            pass
                        logger.info(f"profile_fingerprint: restored {profile_id} from vault")
                        return fp
        except Exception as _ve:
            logger.debug(f"profile_fingerprint: vault lookup skipped ({_ve})")

    # 3. Generate from preset
    try:
        _num = int(profile_id.split("-")[-1])
    except (ValueError, IndexError):
        _num = 1
    fp = dict(_PRFL_PRESETS.get(_num, _PRFL_DEFAULT_PRESET))
    fp["profile_id"] = profile_id

    # Persist to Drive (static fields only)
    try:
        os.makedirs(os.path.dirname(fp_path), exist_ok=True)
        with open(fp_path + ".tmp", "w") as _f:
            json.dump(fp, _f, indent=2)
        os.replace(fp_path + ".tmp", fp_path)
        logger.info(f"profile_fingerprint: created + saved {profile_id} → {fp_path}")
    except Exception as _e:
        logger.warning(f"profile_fingerprint: save failed for {profile_id}: {_e}")

    _PROFILE_FINGERPRINT_CACHE[profile_id] = fp
    return fp
