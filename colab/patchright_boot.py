"""Boot-time Patchright browser engine setup for Colab workers.

Downloads and caches Patchright's patched Chromium binary (~350MB) at runtime.
The binary is cached at ~/.cache/ms-patchright/ and persists across Colab
runtime restarts if Google Drive FUSE is mounted.

Patchright MUST use its own patched Chromium — passing executablePath to
system Chrome breaks CDP pipe stealth and re-enables bot detection.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from typing import Any

logger = logging.getLogger(__name__)

PATCHRIGHT_CACHE = os.path.expanduser("~/.cache/ms-patchright")
PATCHRIGHT_MARKER = os.path.join(PATCHRIGHT_CACHE, ".xiosync-installed")


def ensure_patchright_pip() -> None:
    """Install patchright Python package via pip if not already importable."""
    try:
        import patchright

        logger.info("Patchright Python package already installed.")
    except ImportError:
        logger.info("Installing Patchright Python package via pip...")
        subprocess.run([sys.executable, "-m", "pip", "install", "patchright"], check=True)
        logger.info("Patchright Python package installed successfully.")


def ensure_patchright_chromium() -> str:
    """Download and cache Patchright's patched Chromium binary."""
    if os.path.exists(PATCHRIGHT_MARKER):
        logger.info("Patchright Chromium binary already installed (marker found).")
        return PATCHRIGHT_CACHE

    start_time = time.time()
    logger.info("Installing Patchright Chromium binary...")

    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = PATCHRIGHT_CACHE

    try:
        # Try npx first
        subprocess.run(["npx", "patchright", "install", "chromium"], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.info("npx not available or failed, trying via python -m patchright...")
        subprocess.run([sys.executable, "-m", "patchright", "install", "chromium"], check=True)

    # Write marker
    os.makedirs(PATCHRIGHT_CACHE, exist_ok=True)
    with open(PATCHRIGHT_MARKER, "w") as f:
        f.write("installed")

    duration = time.time() - start_time
    logger.info(f"Patchright Chromium binary installed successfully in {duration:.2f}s.")
    return PATCHRIGHT_CACHE


def get_patchright_chromium_path() -> str | None:
    """Return the path to the installed Patchright Chromium binary."""
    if not os.path.exists(PATCHRIGHT_CACHE):
        return None

    for root, dirs, files in os.walk(PATCHRIGHT_CACHE):
        for file in files:
            if file == "chrome" or file == "chrome.exe" or file == "Chromium":
                return os.path.join(root, file)

    return None


def verify_patchright_installation() -> dict[str, Any]:
    """Return a dict with installation status."""
    cache_dir = PATCHRIGHT_CACHE
    binary_path = get_patchright_chromium_path()
    installed = binary_path is not None and os.path.exists(PATCHRIGHT_MARKER)

    cache_size_mb = 0.0
    if os.path.exists(cache_dir):
        total_size = sum(
            os.path.getsize(os.path.join(dirpath, filename))
            for dirpath, _, filenames in os.walk(cache_dir)
            for filename in filenames
        )
        cache_size_mb = total_size / (1024 * 1024)

    return {
        "installed": installed,
        "cache_dir": cache_dir,
        "binary_path": binary_path,
        "cache_size_mb": cache_size_mb,
    }


def setup_patchright() -> str:
    """High-level entry point called during boot.py."""
    try:
        ensure_patchright_pip()
        return ensure_patchright_chromium()
    except Exception as e:
        logger.error(f"Failed to setup Patchright: {e}")
        raise RuntimeError(f"Patchright installation failed: {e}") from e
