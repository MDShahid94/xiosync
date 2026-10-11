"""colab.agent.browser.uc_launcher — UC Chrome binary selection, chromedriver replacement, and stealth extension generation."""

from __future__ import annotations

import glob
import logging
import os
import shutil
import socket
import subprocess
import zipfile
from typing import Any

from agent.config import _CHROME_VERSION, CHROME_EXECUTABLE
from agent.stealth.stealth_js import _STEALTH_JS

logger = logging.getLogger("xiorun_agent.browser.uc_launcher")

# ── UC Chrome Constants ────────────────────────────────────────────────────────
_DEFAULT_CHROME131 = "/opt/chrome131/chrome"
_DEFAULT_CD131 = "/usr/local/bin/chromedriver131"
_UC_CD_DIR = os.path.expanduser("~/.local/share/undetected_chromedriver")
_UC_CD_PATH = os.path.join(_UC_CD_DIR, "undetected_chromedriver")
_STEALTH_EXT_DIR = "/tmp/xio_stealth_ext"


def _find_chrome() -> tuple[str, int]:
    """Return (binary_path, major_version). Prefers UC-compatible versions (<=133)."""
    # 1. Explicitly installed Chrome 131 (exact UC 3.5.5 compatible) <- PREFERRED
    if os.path.isfile(_DEFAULT_CHROME131) and os.access(_DEFAULT_CHROME131, os.X_OK):
        logger.info(f"uc-launcher: using Chrome 131 (UC-compatible) at {_DEFAULT_CHROME131}")
        return _DEFAULT_CHROME131, 131

    # 2. patchright/playwright Chromium - prefer <=133, but fall back to any version
    _pr_best: tuple[str, int] | None = None
    for path in sorted(
        glob.glob("/root/.cache/ms-patchright/chromium-*/chrome-linux64/chrome"),
        reverse=True,
    ):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            try:
                raw = (
                    subprocess.check_output(
                        [path, "--version"], timeout=5, stderr=subprocess.DEVNULL
                    )
                    .decode()
                    .strip()
                )
                major = int(raw.split()[-1].split(".")[0])
                if major <= 133:
                    logger.info(f"uc-launcher: using patchright Chromium {major} at {path}")
                    return path, major
                else:
                    # Keep as fallback - prefer lowest version > 133
                    if _pr_best is None or major < _pr_best[1]:
                        _pr_best = (path, major)
                    logger.warning(
                        f"uc-launcher: patchright Chromium {major} > 133 - UC detection risk higher"
                    )
            except Exception:
                pass
    if _pr_best:
        logger.warning(
            f"uc-launcher: using patchright Chromium {_pr_best[1]} as last resort "
            "(Chrome 131 not installed - install it for best stealth)"
        )
        return _pr_best

    # 3. System Chrome - ONLY if version <= 133 (skip 153 - Google detects it)
    for path in ["/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"]:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            try:
                raw = (
                    subprocess.check_output(
                        [path, "--version"], timeout=5, stderr=subprocess.DEVNULL
                    )
                    .decode()
                    .strip()
                )
                major = int(raw.split()[-1].split(".")[0])
                if major <= 133:
                    logger.info(f"uc-launcher: using system Chrome {major} at {path}")
                    return path, major
                else:
                    logger.warning(
                        f"uc-launcher: SKIPPING system Chrome {major} - UC 3.5.5 cannot "
                        "fully patch Chrome 134+. Google detects navigator.webdriver -> "
                        "only 3 cookies issued. Use Chrome <=133."
                    )
            except Exception:
                pass

    # 4. xio-pkgs bundled Chrome
    for path in sorted(
        glob.glob("/tmp/xio-pkgs/**/chrome", recursive=True),
        reverse=True,
    ):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            try:
                ver_dir = (
                    path.split("/browser/")[0].split("/")[-1] if "/browser/" in path else ""
                )
                major = int(ver_dir.split(".")[0]) if ver_dir else 131
                logger.info(f"uc-launcher: using xio-pkgs Chrome {major} at {path}")
                return path, major
            except Exception:
                pass

    if CHROME_EXECUTABLE and os.path.isfile(CHROME_EXECUTABLE):
        logger.info(f"uc-launcher: using fallback CHROME_EXECUTABLE at {CHROME_EXECUTABLE}")
        return CHROME_EXECUTABLE, 131

    raise FileNotFoundError(
        "No Chrome/Chromium binary found. "
        "Expected patchright Chromium, google-chrome-stable, or xio-pkgs Chrome."
    )


def _replace_chromedriver(chrome_ver: int = 131) -> str | None:
    """Atomic replacement of chromedriver binary to bypass UC's segfault-prone patcher in Colab.

    Copies /usr/local/bin/chromedriver{ver} to
    ~/.local/share/undetected_chromedriver/undetected_chromedriver atomically
    via temp file + rename to avoid Linux ETXTBSY (Text file busy).
    """
    our_cd = f"/usr/local/bin/chromedriver{chrome_ver}"
    if not os.path.isfile(our_cd):
        our_cd = _DEFAULT_CD131
    if not os.path.isfile(our_cd):
        return None

    os.makedirs(_UC_CD_DIR, exist_ok=True)
    uc_cd_tmp = _UC_CD_PATH + f".tmp.{os.getpid()}"
    try:
        shutil.copy2(our_cd, uc_cd_tmp)
        os.chmod(uc_cd_tmp, 0o755)
        os.replace(uc_cd_tmp, _UC_CD_PATH)  # atomic POSIX rename
        return _UC_CD_PATH
    except OSError as err:
        try:
            os.unlink(uc_cd_tmp)
        except OSError:
            pass
        logger.warning(f"uc-launcher: chromedriver copy failed ({err}) - using existing")
        return _UC_CD_PATH if os.path.isfile(_UC_CD_PATH) else None


def _create_stealth_extension(
    fingerprint: dict[str, Any] | None = None,
    ext_dir: str = _STEALTH_EXT_DIR,
) -> str:
    """Generate Chrome extension (world: MAIN, run_at: document_start) for stealth injection.

    Returns the directory path containing manifest.json and stealth.js.
    """
    os.makedirs(ext_dir, exist_ok=True)
    fp = fingerprint if isinstance(fingerprint, dict) else {}
    tz = fp.get("timezone", "America/New_York")
    try:
        import datetime as _dtx
        import zoneinfo as _zix

        tz_off = -int(
            _dtx.datetime.now(_zix.ZoneInfo(tz)).utcoffset().total_seconds() // 60
        )
    except Exception:
        tz_off = 300

    wr = fp.get("webgl_renderer", "ANGLE (Apple, Apple M1, OpenGL 4.1)")
    wv = "Apple" if "Apple" in wr else "Google Inc. (NVIDIA)"
    default_ua = (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{_CHROME_VERSION} Safari/537.36"
    )
    ext_js = (
        _STEALTH_JS.replace("{WEBGL_V}", wv)
        .replace("{WEBGL_R}", wr)
        .replace("{UA}", fp.get("ua_template", default_ua))
        .replace("{PLATFORM}", fp.get("platform", "Win32"))
        .replace("{TIMEZONE}", tz)
        .replace("{TZ_OFFSET}", str(tz_off))
        .replace("{CANVAS_SEED}", str(fp.get("canvas_seed", 42)))
        .replace("{AUDIO_SEED}", str(fp.get("audio_seed", 7)))
        .replace("{SCREEN_W}", str(fp.get("width", 1920)))
        .replace("{SCREEN_H}", str(fp.get("height", 1080)))
        .replace("{CORES}", str(fp.get("cores", 8)))
        .replace("{RAM}", str(fp.get("ram", 16)))
        .replace("'{LOCALE}'", "'en-US'")
        .replace("{LOCALE}", "en-US")
        .replace("{LANG}", "en-US")
        .replace("{CH_PLATFORM}", fp.get("ch_platform", "macOS"))
        .replace("{CH_ARCH}", fp.get("ch_arch", "arm"))
        .replace("{LAT}", str(fp.get("lat", 40.7128)))
        .replace("{LON}", str(fp.get("lon", -74.0060)))
        .replace("{IS_MOBILE}", "false")
        .replace("{SCREEN_AH}", str(fp.get("height", 1080)))
    )

    with open(f"{ext_dir}/stealth.js", "w", encoding="utf-8") as ef:
        ef.write(ext_js)

    import json as _json

    manifest = {
        "manifest_version": 3,
        "name": "XIO Stealth",
        "version": "1.0",
        "description": "XIOSYNC fingerprint spoofing",
        "content_scripts": [
            {
                "matches": ["<all_urls>"],
                "js": ["stealth.js"],
                "run_at": "document_start",
                "all_frames": True,
                "world": "MAIN",
            }
        ],
    }
    with open(f"{ext_dir}/manifest.json", "w", encoding="utf-8") as mf:
        _json.dump(manifest, mf, indent=2)

    logger.info(f"uc-launcher: stealth extension generated in {ext_dir}")
    return ext_dir


def _create_stealth_extension_zip(
    fingerprint: dict[str, Any] | None = None,
    zip_path: str = "/tmp/xio_stealth_ext.zip",
) -> str:
    """Generate stealth extension and pack it into a ZIP archive."""
    ext_dir = _create_stealth_extension(fingerprint=fingerprint)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in ["manifest.json", "stealth.js"]:
            fpath = os.path.join(ext_dir, fname)
            if os.path.exists(fpath):
                zf.write(fpath, arcname=fname)
    logger.info(f"uc-launcher: stealth extension zipped to {zip_path}")
    return zip_path


def _pick_free_port() -> int:
    """Pick an ephemeral free TCP port."""
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _launch_uc_chrome_for_login(
    profile_dir: str,
    proxy_url: str | None,
    fingerprint: dict[str, Any] | None = None,
) -> tuple[Any, int]:
    """Launch UC Chrome (Chrome 131) for login - returns (driver, port).

    Module-level so /run-uc-login can call it directly (no subprocess).
    Uses use_subprocess=False - same config as the working lifespan Chrome.
    All traffic routes through proxy_url; Colab IP is never exposed.
    """
    import undetected_chromedriver as uc  # deferred - not needed until launch

    chrome_bin, chrome_ver = _find_chrome()

    opts = uc.ChromeOptions()
    if proxy_url:
        addr = proxy_url.replace("socks5://", "").replace("socks5h://", "")
        opts.add_argument(f"--proxy-server=socks5://{addr}")

    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--use-gl=angle")
    opts.add_argument("--use-angle=gl")
    opts.add_argument("--enable-webgl")
    opts.add_argument("--enable-webgl2")
    opts.add_argument("--ignore-gpu-blocklist")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--remote-allow-origins=*")
    opts.add_experimental_option(
        "prefs",
        {
            "webrtc.ip_handling_policy": "disable_non_proxied_udp",
            "webrtc.multiple_routes_enabled": False,
            "webrtc.nonproxied_udp_enabled": False,
        },
    )

    # Optional stealth extension loading if fingerprint is present
    if fingerprint:
        try:
            ext_dir = _create_stealth_extension(fingerprint=fingerprint)
            opts.add_argument(f"--load-extension={ext_dir}")
        except Exception as ext_err:
            logger.warning(f"uc-launcher: failed loading stealth extension: {ext_err}")

    port = _pick_free_port()

    # Set up chromedriver binary atomically
    driver_executable_path = _replace_chromedriver(chrome_ver)

    # Clean stale profile locks
    os.makedirs(profile_dir, exist_ok=True)
    for lk in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        try:
            os.remove(os.path.join(profile_dir, lk))
        except FileNotFoundError:
            pass

    logger.info(
        f"_launch_uc_chrome_for_login: Chrome {chrome_ver} proxy={bool(proxy_url)} port={port}"
    )

    driver = uc.Chrome(
        options=opts,
        browser_executable_path=chrome_bin,
        driver_executable_path=driver_executable_path,
        version_main=chrome_ver,
        use_subprocess=False,
        headless=False,
        port=port,
        user_data_dir=profile_dir,
        keep_user_data_dir=True,
    )
    return driver, port
