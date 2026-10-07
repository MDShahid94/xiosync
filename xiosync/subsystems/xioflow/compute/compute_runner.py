from __future__ import annotations

import asyncio
import logging
import os
import sys
from io import StringIO
from typing import Any

logger = logging.getLogger(__name__)

ALLOWED_RUNTIMES = {"python", "bash", "docker", "wasm"}

# Blocked builtins in Python sandbox — prevent import of dangerous modules
_BLOCKED_MODULES = frozenset(
    {
        "subprocess",
        "shutil",
        "ctypes",
        "importlib",
    }
)


class ComputeNodeRunner:
    """Sandboxed plugin execution for compute_node action types.

    Supports Python (in-process with restricted globals) and Bash
    (subprocess with timeout). Docker and WASM are planned.

    The runner captures stdout and the `result` variable from the
    execution namespace, returning them via the `execute()` method.
    """

    def __init__(self, allowed_plugins: set[str] | frozenset[str] = frozenset()) -> None:
        self.allowed_plugins = set(allowed_plugins)

    async def execute(
        self,
        plugin_name: str,
        runtime: str,
        source_code: str,
        page: Any,
        action_params: dict,
        workflow_vars: dict,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Execute sandboxed plugin code.

        Returns:
            dict with keys:
                success (bool): Whether execution completed without error
                stdout (str): Captured standard output
                result (Any): Value of `result` variable after execution (or None)
                error (str | None): Error message if failed
        """
        if self.allowed_plugins and plugin_name not in self.allowed_plugins:
            return {
                "success": False,
                "stdout": "",
                "result": None,
                "error": f"Plugin {plugin_name!r} is not in allowed list",
            }

        if runtime not in ALLOWED_RUNTIMES:
            return {
                "success": False,
                "stdout": "",
                "result": None,
                "error": f"Runtime {runtime!r} is not supported. Allowed: {ALLOWED_RUNTIMES}",
            }

        if runtime == "python":
            return await self._run_python(
                plugin_name, source_code, action_params, workflow_vars, timeout
            )
        elif runtime == "bash":
            return await self._run_bash(
                plugin_name, source_code, action_params, workflow_vars, timeout
            )
        elif runtime == "docker":
            return await self._run_docker(
                plugin_name, source_code, action_params, workflow_vars, timeout
            )
        else:
            return {
                "success": False,
                "stdout": "",
                "result": None,
                "error": f"Runtime {runtime!r} not yet implemented",
            }

    async def _run_python(
        self,
        plugin_name: str,
        source_code: str,
        action_params: dict,
        workflow_vars: dict,
        timeout: int,
    ) -> dict[str, Any]:
        """Execute Python code in a restricted namespace with stdout capture."""

        def _exec_in_thread():
            # Build restricted namespace
            sandbox_globals = {
                "__builtins__": {
                    k: v
                    for k, v in __builtins__.__dict__.items()
                    if k not in ("exec", "eval", "compile", "__import__", "open")
                }
                if isinstance(__builtins__, type(sys))
                else {
                    k: v
                    for k, v in __builtins__.items()
                    if k not in ("exec", "eval", "compile", "__import__", "open")
                },
                "params": dict(action_params),
                "vars": dict(workflow_vars),
                "result": None,
            }
            # Allow safe imports
            import base64
            import datetime
            import hashlib
            import json
            import math
            import re
            import urllib.parse

            sandbox_globals.update(
                {
                    "json": json,
                    "math": math,
                    "re": re,
                    "datetime": datetime,
                    "hashlib": hashlib,
                    "base64": base64,
                    "urllib": urllib,
                }
            )

            # Capture stdout
            old_stdout = sys.stdout
            sys.stdout = captured = StringIO()
            try:
                exec(source_code, sandbox_globals)  # noqa: S102
                return {
                    "success": True,
                    "stdout": captured.getvalue(),
                    "result": sandbox_globals.get("result"),
                    "error": None,
                }
            except Exception as exc:
                return {
                    "success": False,
                    "stdout": captured.getvalue(),
                    "result": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            finally:
                sys.stdout = old_stdout

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_exec_in_thread),
                timeout=timeout,
            )
        except TimeoutError:
            return {
                "success": False,
                "stdout": "",
                "result": None,
                "error": f"Python plugin {plugin_name!r} timed out after {timeout}s",
            }

    async def _run_bash(
        self,
        plugin_name: str,
        source_code: str,
        action_params: dict,
        workflow_vars: dict,
        timeout: int,
    ) -> dict[str, Any]:
        """Execute bash script as subprocess with env injection."""
        # Inject action_params and workflow_vars as environment variables
        env = {**os.environ}
        for k, v in {**action_params, **workflow_vars}.items():
            if isinstance(v, str):
                env[f"XIOFLOW_{k.upper()}"] = v

        try:
            proc = await asyncio.create_subprocess_shell(
                source_code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                return {
                    "success": False,
                    "stdout": stdout,
                    "result": None,
                    "error": f"Exit code {proc.returncode}: {stderr[:500]}",
                }
            # Try to parse last line as JSON result
            result_val = None
            for line in reversed(stdout.strip().splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        import json

                        result_val = json.loads(line)
                    except Exception:
                        pass
                    break

            return {
                "success": True,
                "stdout": stdout,
                "result": result_val,
                "error": None,
            }
        except TimeoutError:
            return {
                "success": False,
                "stdout": "",
                "result": None,
                "error": f"Bash plugin {plugin_name!r} timed out after {timeout}s",
            }
        except Exception as exc:
            return {
                "success": False,
                "stdout": "",
                "result": None,
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def _run_docker(
        self,
        plugin_name: str,
        source_code: str,
        action_params: dict,
        workflow_vars: dict,
        timeout: int,
    ) -> dict[str, Any]:
        """Execute code inside a Docker container."""
        image = action_params.get("image", "")
        if not image:
            return {
                "success": False,
                "stdout": "",
                "result": None,
                "error": "Docker runtime requires 'image' in action_params",
            }
        # Build docker run command
        env_args = []
        for k, v in action_params.items():
            if isinstance(v, str) and k not in ("image", "command"):
                env_args.extend(["-e", f"XIOFLOW_{k.upper()}={v}"])
        cmd = action_params.get("command", source_code)
        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--memory=512m",
            "--cpus=1",
            *env_args,
            image,
        ]
        if cmd:
            docker_cmd.extend(["sh", "-c", cmd])
        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            stdout = stdout_b.decode("utf-8", errors="replace")
            return {
                "success": proc.returncode == 0,
                "stdout": stdout,
                "result": None,
                "error": stderr_b.decode()[:500] if proc.returncode != 0 else None,
            }
        except TimeoutError:
            return {
                "success": False,
                "stdout": "",
                "result": None,
                "error": f"Docker container timed out after {timeout}s",
            }
        except Exception as exc:
            return {
                "success": False,
                "stdout": "",
                "result": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
