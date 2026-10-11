"""colab.agent.config — Configuration and environment settings for XIORUN agent."""

from __future__ import annotations

import glob as _glob
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("xiorun_agent.config")

# ── Environment & Ports ───────────────────────────────────────────────────────
AGENT_PORT: int = int(os.environ.get("XIORUN_AGENT_PORT", "9300"))
XIORUN_PORT: int = int(os.environ.get("XIORUN_PORT", os.environ.get("XIORUN_AGENT_PORT", "9300")))
DRIVE_ROOT: str = os.environ.get("XIO_DRIVE_ROOT", "/content/drive/MyDrive/XIOSYNC-Shared")
XIOSYNC_BASE: str = os.environ.get("XIOSYNC_BASE", os.environ.get("XIORUN_XIOSYNC_BASE", ""))
XIOSYNC_TOKEN: str = os.environ.get("WORKER_SECRET", os.environ.get("XIORUN_XIOSYNC_TOKEN", ""))
NODE_NAME: str = os.environ.get("NODE_NAME", os.environ.get("XIO_NODE_NAME", "colab-agent"))
INTERNAL_SECRET: str = os.environ.get(
    "XIORUN_INTERNAL_SECRET", os.environ.get("XIOSYNC_INTERNAL_SECRET", "")
)

# R2 Configuration
R2_ENDPOINT: str = os.environ.get("XIORUN_R2_ENDPOINT", "")
R2_BUCKET: str = os.environ.get("XIORUN_R2_BUCKET", "xio-profiles")
R2_ACCESS_KEY: str = os.environ.get("XIORUN_R2_ACCESS_KEY", "")
R2_SECRET_KEY: str = os.environ.get("XIORUN_R2_SECRET_KEY", "")

# SOCKS5 & Proxy Configuration
_WS_SOCKS5_PORT: int = 19055
PPPOE_PROXY: str = os.environ.get("XIORUN_PPPOE_PROXY", "")
PROXY_SSH_HOST: str = os.environ.get("XIORUN_PROXY_SSH_HOST", "")
PROXY_SSH_USER: str = os.environ.get("XIORUN_PROXY_SSH_USER", "karmareturns")
PROXY_SSH_KEY: str = os.environ.get("XIORUN_PROXY_SSH_KEY", "/root/.ssh/xio_proxy_key")
PROXY_LOCAL_PORT: int = int(os.environ.get("XIORUN_PROXY_LOCAL_PORT", "19056"))

# SSH tunnel process and proxy state
_ssh_tunnel_proc: Any = None
_SSH_PROXY_URL: str | None = None  # set at startup: "socks5://127.0.0.1:1056"
_NOVNC_URL: str | None = None  # set at startup: "http://{tailscale_ip}:6080/vnc.html"

# Profiles directories
PROFILES_BASE: Path = Path(tempfile.gettempdir()) / "xiorun_profiles"
PROFILES_BASE.mkdir(exist_ok=True)

TRIM_DIRS: list[str] = [
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnCache",
    "ShaderCache",
    os.path.join("Service Worker", "CacheStorage"),
    os.path.join("Service Worker", "ScriptCache"),
]

# ── Chrome binary selection ────────────────────────────────────────────────────
# Priority: 1) real google-chrome-stable (undetectable), 2) patchright
# Chromium from ms-patchright cache, 3) let patchright auto-detect.
_REAL_CHROME = "/usr/bin/google-chrome-stable"


def _find_patchright_chromium() -> str | None:
    """Search the ms-patchright cache for the newest installed Chromium binary."""
    candidates = sorted(
        _glob.glob("/root/.cache/ms-patchright/chromium-*/chrome-linux64/chrome"),
        reverse=True,
    )
    return candidates[0] if candidates else None


_PATCHRIGHT_CHROME = _find_patchright_chromium()

# Use real Chrome if available; fall back to patchright; then auto-detect
CHROME_EXECUTABLE: str | None = (
    _REAL_CHROME
    if os.path.isfile(_REAL_CHROME)
    else _PATCHRIGHT_CHROME
    if _PATCHRIGHT_CHROME and os.path.isfile(_PATCHRIGHT_CHROME)
    else None  # let patchright auto-detect via PLAYWRIGHT_BROWSERS_PATH
)

# UA must EXACTLY match the installed Chrome binary version.
# Chrome 131.0.6778.108 = last version UC 3.5.5 fully patches (navigator.webdriver=False confirmed).
# Chrome 153 is NOT used — UC 3.5.5 doesn't patch it and Google gives only 3 cookies.
_CHROME_VERSION = "131.0.6778.108"  # /opt/chrome131/chrome
_STEALTH_UA_CHROME = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{_CHROME_VERSION} Safari/537.36"
)

# Chromium launch args (hardened, stealth, Colab-compatible)
CHROMIUM_ARGS: list[str] = [
    "--no-sandbox",
    # NOTE: --disable-setuid-sandbox removed — shows a visible warning banner
    # ("unsupported command-line flag") which is a bot detection signal.
    # Running as root in Colab, --no-sandbox alone is sufficient.
    "--disable-dev-shm-usage",
    "--ignore-gpu-blocklist",
    # Anti-automation detection
    "--disable-blink-features=AutomationControlled",
    "--disable-automation",
    "--disable-infobars",
    "--remote-debugging-address=0.0.0.0",  # bind to Tailscale IP
    # Language
    "--lang=en-US,en",
    "--accept-lang=en-US,en;q=0.9,en-GB;q=0.8",
    # WebGL: use ANGLE over EGL/Mesa (not SwiftShader Vulkan).
    # SwiftShader-Vulkan is a known headless indicator — EGL-ANGLE is far more
    # realistic and still works on Xvfb with Mesa drivers.
    "--use-gl=angle",
    "--use-angle=gl",  # gl = EGL/Mesa (more realistic than swiftshader)
    "--enable-webgl",
    "--enable-webgl2",
    # Window geometry (800x600 default = instant bot flag)
    "--window-size=1920,1080",
    "--start-maximized",
    # Session stability
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    # Fix: Trusted Types blocks blob Worker() in headless-detector V4 check.
    # Disabling TrustedDOMTypes allows importScripts()-based Worker UA patching.
    "--disable-features=TrustedDOMTypes",
    # NOTE: do NOT add --disable-extensions-except= — it blocks stealth extension
]
