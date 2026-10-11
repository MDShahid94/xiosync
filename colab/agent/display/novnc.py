"""noVNC (x11vnc + websockify) supervisor for direct HITL control."""

from __future__ import annotations

import logging
import os
import subprocess as _sp
import threading as _thr
import time as _t

logger = logging.getLogger("xiorun_agent")

_NOVNC_URL: str | None = None  # set at startup: "http://{tailscale_ip}:6080/vnc.html"


def get_novnc_url() -> str | None:
    """Return the current noVNC URL if started, else None."""
    global _NOVNC_URL
    return _NOVNC_URL


def start_novnc_supervisor() -> None:
    """Start noVNC supervisor (x11vnc + websockify).

    x11vnc mirrors Xvfb :99 → VNC :5900 → websockify :6080 → noVNC HTML
    Gives operators a zero-relay direct X11 stream (50ms latency vs 500ms+ CDP).
    """
    global _NOVNC_URL

    try:
        _sp.run(["pkill", "-f", "x11vnc"], capture_output=True)
        _sp.run(["pkill", "-f", "websockify"], capture_output=True)
        _t.sleep(0.5)

        def _start_novnc():
            """Start noVNC in background — must not block the health check."""
            global _NOVNC_URL
            if _sp.run(["which", "x11vnc"], capture_output=True).returncode != 0:
                # Not installed — install silently in this background thread (takes ~30s)
                _sp.run(
                    ["apt-get", "install", "-y", "-q", "x11vnc", "novnc"],
                    capture_output=True,
                    timeout=90,
                )

            _sp.Popen(
                [
                    "x11vnc",
                    "-display",
                    ":99",
                    "-nopw",
                    "-listen",
                    "0.0.0.0",
                    "-rfbport",
                    "5900",
                    "-forever",
                    "-shared",
                    "-noxdamage",
                    "-bg",
                    "-q",
                ],
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
            _t.sleep(1)

            _novnc_dir = ""
            for _d in ["/usr/share/novnc", "/opt/novnc", "/usr/local/share/novnc"]:
                if os.path.isfile(f"{_d}/vnc.html") or os.path.isfile(f"{_d}/index.html"):
                    _novnc_dir = _d
                    break

            _ws_args = ["websockify", "6080", "localhost:5900", "--daemon"]
            if _novnc_dir:
                _ws_args = ["websockify", "--web", _novnc_dir, "6080", "localhost:5900", "--daemon"]
            _sp.Popen(_ws_args, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            _t.sleep(1)

            # Derive Tailscale IP for noVNC URL
            _ts_ip = ""
            try:
                _ts_out = _sp.run(
                    ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
                )
                _ts_ip = _ts_out.stdout.strip().split("\n")[0]
            except Exception:
                try:
                    _ip_out = _sp.run(["hostname", "-I"], capture_output=True, text=True)
                    for _ip in _ip_out.stdout.split():
                        if _ip.startswith("100."):
                            _ts_ip = _ip
                            break
                except Exception:
                    pass

            if _ts_ip:
                _NOVNC_URL = f"http://{_ts_ip}:6080/vnc.html"
                logger.info(f"lifespan: noVNC started → {_NOVNC_URL}")
            else:
                logger.warning("lifespan: noVNC started but could not determine Tailscale IP")

        # Run noVNC setup in a background thread — health endpoint available immediately
        _thr.Thread(target=_start_novnc, daemon=True, name="novnc-setup").start()

    except Exception as _e:
        logger.warning(f"lifespan: noVNC startup failed: {_e}")
