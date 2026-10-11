"""Antigravity CLI runner and status checks."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil

logger = logging.getLogger("xiorun_agent")

_AGY_BIN = (
    os.environ.get("XIOAI_AGY_BIN")
    or shutil.which("agy")  # search $PATH first
    or os.path.expanduser("~/.local/bin/agy")  # pip install --user
    or "/usr/local/bin/agy"  # system install
)
_AGY_GEMINI_DIR = os.path.expanduser("~/.gemini")
_AGY_STATE_DIR = os.path.join(_AGY_GEMINI_DIR, "antigravity-cli")
_AGY_TOKEN_FILE = os.path.join(_AGY_GEMINI_DIR, "antigravity-cli", "antigravity-oauth-token")


def _is_agy_installed() -> bool:
    """Check if agy binary exists on this worker."""
    return os.path.isfile(_AGY_BIN) and os.access(_AGY_BIN, os.X_OK)


def _is_agy_authenticated() -> bool:
    """Check if agy has valid credentials on this worker."""
    if not _is_agy_installed():
        return False
    # Check composite_token_storage dir (agy >= v1.1.19)
    if os.path.isdir(_AGY_STATE_DIR):
        import re as _re

        for f in os.listdir(_AGY_STATE_DIR):
            if _re.search(r"token|credential|oauth|auth", f, _re.I) and not f.endswith(
                (".log", ".pbtxt")
            ):
                try:
                    raw = open(os.path.join(_AGY_STATE_DIR, f), errors="ignore").read(4096)
                    if "refresh_token" in raw or "access_token" in raw:
                        return True
                except Exception:
                    pass
    # Legacy token path (agy < v1.1.19)
    if os.path.isfile(_AGY_TOKEN_FILE):
        try:
            import json as _json

            t = _json.loads(open(_AGY_TOKEN_FILE).read())
            return bool(t.get("token", {}).get("refresh_token") or t.get("refresh_token"))
        except Exception:
            pass
    return False


async def _run_agy_local(
    prompt: str,
    *,
    system: str = "",
    output_format: str = "text",
    json_schema: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 8192,
    timeout: int = 120,
    model: str | None = None,
) -> dict:
    """Run agy CLI as a subprocess and return the result."""
    import subprocess as _sp

    cmd = [_AGY_BIN, f"--print={prompt}", "--dangerously-skip-permissions", "--effort=high"]
    if output_format == "json":
        cmd.append("--output-format=json")
    if json_schema:
        cmd.append(f"--json-schema={json_schema}")
    mdl = model or os.environ.get("XIOAI_AGY_MODEL")
    if mdl:
        cmd.append(f"--model={mdl}")
    if timeout > 0:
        cmd.append(f"--print-timeout={timeout}s")

    # Environment: set proxy for residential exit + unset DISPLAY to prevent Chrome conflict
    env = dict(os.environ)
    env["DISPLAY"] = ""  # Prevent agy from detecting Chrome CDP
    # agy (Go binary) calls googleapis.com for auth + generation.
    # These are reachable directly from Colab (Google Cloud → Google APIs = same network).
    # SOCKS5 proxy causes TLS EOF in Go's crypto/tls — do NOT set proxy for agy.
    # Only browser/Chrome automation needs the SOCKS5 residential proxy.
    env.pop("HTTPS_PROXY", None)
    env.pop("HTTP_PROXY", None)
    env.pop("ALL_PROXY", None)
    env.pop("https_proxy", None)
    env.pop("http_proxy", None)
    env.pop("all_proxy", None)
    env.setdefault("HOME", os.path.expanduser("~"))
    env["PATH"] = (
        f"{os.path.expanduser('~/.local/bin')}:{env.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"
    )

    logger.info(f"ai_generate: running agy local cmd_len={len(cmd)} timeout={timeout}")
    try:
        proc = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _sp.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout + 10,
            ),
        )
        if proc.returncode == 0:
            return {
                "ok": True,
                "text": proc.stdout.strip(),
                "provider": "agy_local",
                "model": mdl or "default",
            }
        else:
            return {
                "ok": False,
                "error": f"agy exit {proc.returncode}: {proc.stderr[:500]}",
                "provider": "agy_local",
            }
    except _sp.TimeoutExpired:
        return {"ok": False, "error": f"agy timed out after {timeout}s", "provider": "agy_local"}
    except Exception as e:
        return {"ok": False, "error": str(e), "provider": "agy_local"}
