"""
AGY Sidecar — long-running process that executes agy in the GUI session.

Runs as a LaunchAgent (GUI session = Keychain access). Listens on a Unix
domain socket for generation requests from the XIOSYNC server and executes
agy --print=... without any auth issues.

Socket: /tmp/xioai-agy-sidecar.sock
Protocol: newline-delimited JSON
  Request:  {"id": "<uuid>", "prompt": "...", "timeout": 90, ...options}
  Response: {"id": "<uuid>", "text": "...", "success": true}
            {"id": "<uuid>", "error": "...", "success": false}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import uuid

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [agy-sidecar] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("agy-sidecar")

SOCKET_PATH = "/tmp/xioai-agy-sidecar.sock"
AGY_BIN = (
    os.environ.get("XIOAI_AGY_BIN") or shutil.which("agy") or "/Users/karmareturns/.local/bin/agy"
)


async def run_agy(req: dict) -> dict:
    rid = req.get("id", str(uuid.uuid4()))
    prompt = req.get("prompt", "")
    timeout = int(req.get("timeout", 180))  # bumped: creative tasks need ~2min
    model = req.get("model", os.environ.get("XIOAI_AGY_MODEL", ""))
    output_format = req.get("output_format", "text")
    json_schema = req.get("json_schema")

    agy_cmd = [
        AGY_BIN,
        f"--print={prompt}",
        "--dangerously-skip-permissions",
        "--effort=high",
        f"--print-timeout={timeout}s",
    ]
    if output_format == "json":
        agy_cmd.append("--output-format=json")
    if json_schema:
        agy_cmd.append(f"--json-schema={json.dumps(json_schema)}")
    if model:
        agy_cmd.append(f"--model={model}")

    # Wrap in `script -q /dev/null` to give agy a PTY.
    # agy's TUI renderer writes multi-step responses to the PTY (not a plain
    # stdout pipe) — without a PTY, only 1-step responses are captured.
    cmd = ["script", "-q", "/dev/null"] + agy_cmd

    env = os.environ.copy()
    proxy = os.environ.get("XIOAI_AGY_PROXY")
    if proxy:
        env["HTTPS_PROXY"] = proxy
        env["ALL_PROXY"] = proxy

    logger.info("Running agy for request %s (timeout=%ds)", rid, timeout)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge stderr — PTY routes everything here
            env=env,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 30)
        raw = stdout.decode("utf-8", errors="replace")

        # ── Clean PTY output ────────────────────────────────────────────────────
        import re as _re

        # 1. ANSI escape sequences
        raw = _re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])").sub("", raw)
        # 2. Backspace sequences: any char followed by \x08 (collapses to nothing)
        while "\x08" in raw:
            raw = _re.compile(r"[^\x08]\x08").sub("", raw)
            raw = raw.replace("\x08", "")  # leading backspaces with no preceding char
        # 3. Other PTY control chars: ^D (EOF), ^@ (null), carriage returns
        raw = _re.compile(r"[\x00\x04]").sub("", raw)
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        # 4. Strip agy internal status / progress lines
        lines = [
            l
            for l in raw.splitlines()
            if not _re.match(r"^\s*(\[agy\]|✦|Script (started|done))", l)
        ]
        text = "\n".join(lines).strip()
        # `script` emits a stray `^` at the very start of PTY output — strip it
        if text.startswith("^"):
            text = text[1:].lstrip()
        err = ""  # already merged into stdout

        auth_fail = any(
            m in text or m in err
            for m in [
                "authentication timed out",
                "authentication failed",
                "Waiting for authentication",
            ]
        )
        if auth_fail:
            logger.error("agy auth failure for %s", rid)
            return {
                "id": rid,
                "success": False,
                "error": "agy not authenticated. Run 'agy' interactively.",
            }

        if proc.returncode != 0 and not text:
            logger.error("agy exit %d for %s: %s", proc.returncode, rid, err[:200])
            return {
                "id": rid,
                "success": False,
                "error": f"agy exit {proc.returncode}: {err[:200]}",
            }

        logger.info("agy success for %s: %d chars", rid, len(text))
        return {"id": rid, "success": True, "text": text}

    except TimeoutError:
        return {"id": rid, "success": False, "error": f"agy timeout ({timeout}s)"}
    except Exception as exc:
        return {"id": rid, "success": False, "error": str(exc)}


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername", "unknown")
    logger.info("Client connected: %s", peer)
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=120)
            if not line:
                break
            try:
                req = json.loads(line.decode())
            except json.JSONDecodeError as e:
                writer.write(
                    json.dumps({"success": False, "error": f"bad JSON: {e}"}).encode() + b"\n"
                )
                await writer.drain()
                continue

            result = await run_agy(req)
            writer.write(json.dumps(result).encode() + b"\n")
            await writer.drain()
    except (TimeoutError, asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        writer.close()
        logger.info("Client disconnected: %s", peer)


async def main():
    # Remove stale socket
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    server = await asyncio.start_unix_server(handle_client, path=SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o660)

    logger.info("AGY sidecar listening on %s (agy=%s)", SOCKET_PATH, AGY_BIN)

    def _shutdown(sig, _):
        logger.info("Received %s, shutting down", sig)
        server.close()
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
