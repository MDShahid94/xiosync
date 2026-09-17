"""ScriptRunner — executes XIOSYNC .mjs workflow templates via Node.js subprocess.

Each .mjs script receives:
  - Positional params as JSON via --context=<json>
  - Session identity via --session-id=<uuid>
  - Proxy routing via HTTPS_PROXY / ALL_PROXY env vars (if PPPoE slot assigned)

Expected stdout contract (last JSON line wins):
  {"status": "success", "result": {...}}
  {"status": "error", "error": "...", "detail": "..."}

The runner is intentionally synchronous (called from worker thread).
Node.js subprocesses are bounded by `timeout_seconds`.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Directory where .mjs scripts live.  Configurable via env.
# Default: monorepo tools/workflows/. Override with XIOSYNC_WORKFLOWS_DIR.
_WORKFLOWS_DIR = Path(
    os.environ.get(
        "XIOSYNC_WORKFLOWS_DIR",
        str(Path(__file__).parent.parent.parent.parent.parent / "tools" / "workflows"),
    )
)

# Node binary — prefer the one in PATH; override with XIOSYNC_NODE_BIN
_NODE_BIN = os.environ.get("XIOSYNC_NODE_BIN", "node")

_DEFAULT_TIMEOUT = 180  # seconds


@dataclass
class RunResult:
    success: bool
    status: str               # "success" | "error" | "timeout" | "launch_error"
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None


class ScriptRunnerError(RuntimeError):
    """Raised when the script cannot be launched at all (Node not found, etc.)."""


class ScriptRunner:
    """Executes a XIOSYNC .mjs workflow template in a subprocess."""

    def __init__(
        self,
        *,
        workflows_dir: Path | None = None,
        node_bin: str | None = None,
        default_timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        self._workflows_dir = workflows_dir or _WORKFLOWS_DIR
        self._node_bin = node_bin or _NODE_BIN
        self._default_timeout = default_timeout

    # ── Public API ────────────────────────────────────────────────────────────

    def run(
        self,
        *,
        script_ref: str,
        context: dict[str, Any],
        session_id: uuid.UUID | None = None,
        proxy_url: str | None = None,
        timeout_seconds: int | None = None,
    ) -> RunResult:
        """Execute `script_ref` (e.g. 'google-signin.mjs') with `context` params.

        Args:
            script_ref:     Filename relative to workflows_dir (e.g. 'google-signin.mjs').
            context:        Arbitrary dict passed as --context=<json>.
            session_id:     Browser session UUID passed as --session-id=<id>.
            proxy_url:      socks5://... or http://... proxy URL.  Injected via env vars.
            timeout_seconds: Override the default subprocess timeout.

        Returns:
            RunResult with success/error, stdout/stderr captured.
        """
        script_path = self._workflows_dir / script_ref
        if not script_path.exists():
            return RunResult(
                success=False,
                status="launch_error",
                error=f"Script not found: {script_path}",
            )

        cmd = self._build_cmd(script_path, context, session_id)
        env = self._build_env(proxy_url)
        timeout = timeout_seconds or self._default_timeout

        logger.info(
            "script_runner.start",
            extra={"script": script_ref, "session_id": str(session_id), "timeout": timeout},
        )

        try:
            # start_new_session=True creates a new process group (setsid).
            # On timeout we kill the entire group — this cleans up Playwright/Chromium
            # child processes that would otherwise become zombies.
            proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Kill the whole process group (Node + all children)
                try:
                    import os, signal
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass  # already exited
                proc.wait()
                stdout_bytes, stderr_bytes = proc.communicate()
                logger.warning(
                    "script_runner.timeout",
                    extra={"script": script_ref, "timeout": timeout},
                )
                return RunResult(
                    success=False,
                    status="timeout",
                    error=f"Script exceeded {timeout}s timeout",
                    stdout=stdout_bytes if isinstance(stdout_bytes, str) else "",
                    stderr=stderr_bytes if isinstance(stderr_bytes, str) else "",
                )

            # Build a CompletedProcess-like object for _parse_result
            class _Completed:
                returncode = proc.returncode

            _Completed.stdout = stdout  # type: ignore[attr-defined]
            _Completed.stderr = stderr  # type: ignore[attr-defined]

        except FileNotFoundError:
            raise ScriptRunnerError(
                f"Node.js binary not found: {self._node_bin!r}. "
                "Install Node.js or set XIOSYNC_NODE_BIN."
            )

        return self._parse_result(_Completed, script_ref)  # type: ignore[arg-type]

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_cmd(
        self,
        script_path: Path,
        context: dict[str, Any],
        session_id: uuid.UUID | None,
    ) -> list[str]:
        cmd = [self._node_bin, str(script_path)]
        cmd.append(f"--context={json.dumps(context)}")
        if session_id:
            cmd.append(f"--session-id={session_id}")
        return cmd

    def _build_env(self, proxy_url: str | None) -> dict[str, str]:
        env = {**os.environ}
        if proxy_url:
            env["HTTPS_PROXY"] = proxy_url
            env["HTTP_PROXY"] = proxy_url
            env["ALL_PROXY"] = proxy_url
        return env

    def _parse_result(self, proc: subprocess.CompletedProcess, script_ref: str) -> RunResult:
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = proc.returncode

        # Walk stdout lines in reverse — last valid JSON wins
        parsed: dict[str, Any] | None = None
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    parsed = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

        if exit_code == 0 and (parsed is None or parsed.get("status") != "error"):
            return RunResult(
                success=True,
                status="success",
                result=(parsed.get("result", parsed) if parsed else {}),
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
            )

        error_msg = (
            parsed.get("error", parsed.get("detail", "")) if parsed
            else f"exit_code={exit_code}"
        )
        logger.warning(
            "script_runner.failed",
            extra={
                "script": script_ref,
                "exit_code": exit_code,
                "error": error_msg,
                "stderr_tail": stderr[-300:],
            },
        )
        return RunResult(
            success=False,
            status=parsed.get("status", "error") if parsed else "error",
            error=error_msg or stderr[-200:],
            result=parsed.get("result", {}) if parsed else {},
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
        )
