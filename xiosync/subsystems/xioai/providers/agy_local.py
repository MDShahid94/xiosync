from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
from typing import Any

from xiosync.subsystems.xioai.providers.base import GenerationProvider, GenerationResult

logger = logging.getLogger(__name__)

# Unix socket path used by the agy sidecar daemon
AGY_SIDECAR_SOCKET = "/tmp/xioai-agy-sidecar.sock"

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_json_fences(text: str) -> str:
    """Strip markdown code fences (```json ... ```) from LLM JSON output."""
    text = text.strip()
    m = _FENCE_RE.match(text)
    if m:
        return m.group(1).strip()
    # Handle leading/trailing backtick lines without consuming embedded JSON
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


async def _call_sidecar(req: dict, timeout: int) -> GenerationResult:
    """Send a generation request to the agy sidecar via Unix socket."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(AGY_SIDECAR_SOCKET),
            timeout=5,
        )
        writer.write(json.dumps(req).encode() + b"\n")
        await writer.drain()

        line = await asyncio.wait_for(reader.readline(), timeout=timeout + 20)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

        resp = json.loads(line.decode())

        # ── Sidecar envelope format: {"conversation_id":…, "status":"SUCCESS", "response":"…"} ──
        if "conversation_id" in resp:
            if resp.get("status") == "SUCCESS" and resp.get("response") is not None:
                raw_text = resp["response"]
                if req.get("output_format") == "json":
                    raw_text = _strip_json_fences(raw_text)
                return GenerationResult(
                    text=raw_text,
                    provider="agy_local",
                    model=req.get("model", "") or "agy-default",
                    success=True,
                    usage=resp.get("usage"),
                )
            return GenerationResult(
                success=False,
                error=resp.get("error") or f"sidecar status={resp.get('status')}",
                provider="agy_local",
                model=req.get("model", ""),
            )

        # ── Legacy format: {"success": bool, "text": "…"} ──────────────────────────
        if resp.get("success"):
            raw_text = resp["text"]
            # Strip markdown fences if the caller requested JSON output
            if req.get("output_format") == "json":
                raw_text = _strip_json_fences(raw_text)
            return GenerationResult(
                text=raw_text,
                provider="agy_local",
                model=req.get("model", "") or "agy-default",
                success=True,
            )
        return GenerationResult(
            success=False,
            error=resp.get("error", "sidecar error"),
            provider="agy_local",
            model=req.get("model", ""),
        )
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        # Sidecar not running — fall through to direct execution
        return None  # type: ignore[return-value]
    except asyncio.TimeoutError:
        return GenerationResult(
            success=False,
            error=f"agy sidecar timeout ({timeout}s)",
            provider="agy_local",
        )
    except Exception as exc:
        return GenerationResult(success=False, error=str(exc), provider="agy_local")


class AGYLocalProvider(GenerationProvider):
    @property
    def name(self) -> str:
        return "agy_local"

    @classmethod
    def is_available(cls) -> bool:
        # Available if sidecar socket exists OR agy binary is on PATH
        return (
            os.path.exists(AGY_SIDECAR_SOCKET)
            or bool(os.environ.get("XIOAI_AGY_BIN"))
            or shutil.which("agy") is not None
        )

    async def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        output_format: str = "text",
        json_schema: dict | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        timeout: int = 120,
    ) -> GenerationResult:
        if not self.is_available():
            return GenerationResult(success=False, error="AGY binary not found", provider=self.name)

        model = os.environ.get("XIOAI_AGY_MODEL", "")
        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        # ── Path 1: AGY sidecar (preferred on macOS) ────────────────────────
        # The sidecar is a LaunchAgent that runs in the GUI login session with
        # full Keychain access, so agy can authenticate silently.
        if os.path.exists(AGY_SIDECAR_SOCKET):
            req = {
                "prompt": full_prompt,
                "output_format": output_format,
                "timeout": timeout,
            }
            if model:
                req["model"] = model
            if json_schema:
                req["json_schema"] = json_schema

            result = await _call_sidecar(req, timeout)
            if result is not None:
                return result
            logger.warning("agy_local: sidecar socket present but unreachable, falling back to direct exec")

        # ── Path 2: Direct subprocess (fallback / Linux workers) ────────────
        bin_path = os.environ.get("XIOAI_AGY_BIN") or shutil.which("agy")
        if not bin_path:
            return GenerationResult(success=False, error="AGY binary not found", provider=self.name)

        cmd = [
            bin_path,
            f"--print={full_prompt}",
            "--dangerously-skip-permissions",
            "--effort=high",
            f"--print-timeout={timeout}s",
        ]
        if output_format == "json":
            cmd.append("--output-format=json")
        if json_schema:
            cmd.append(f"--json-schema={json.dumps(json_schema)}")
        if model:
            cmd.append(f"--model={model}")

        env = os.environ.copy()
        proxy = os.environ.get("XIOAI_AGY_PROXY")
        if proxy:
            env["HTTPS_PROXY"] = proxy
            env["ALL_PROXY"] = proxy

        # On Linux (Colab workers), wrap with `script -qc` to provide a PTY
        import platform
        agy_cmd_str = " ".join(shlex.quote(c) for c in cmd)
        if platform.system() == "Linux":
            shell_cmd = f"script -qc {shlex.quote(agy_cmd_str)} /dev/null"
            use_shell = True
        else:
            # macOS direct exec (works when called from a terminal/IDE session)
            shell_cmd = None
            use_shell = False

        try:
            if use_shell:
                proc = await asyncio.create_subprocess_shell(
                    shell_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )

            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 10)
            text = stdout.decode("utf-8", errors="replace").strip().replace("\r", "")
            err_text = stderr.decode("utf-8", errors="replace").strip()

            auth_fail_markers = ["authentication timed out", "authentication failed", "Waiting for authentication"]
            is_auth_fail = any(m in text for m in auth_fail_markers) or any(m in err_text for m in auth_fail_markers)

            if is_auth_fail:
                return GenerationResult(
                    success=False,
                    error="agy not authenticated. Run 'agy' interactively, or ensure the agy sidecar LaunchAgent is loaded.",
                    provider=self.name,
                    model=model,
                )

            if text:
                if output_format == "json":
                    text = _strip_json_fences(text)
                return GenerationResult(
                    text=text,
                    provider=self.name,
                    model=model or "agy-default",
                    success=True,
                )

            return GenerationResult(
                success=False,
                error=f"agy exit {proc.returncode}: {err_text or '(no output)'}",
                provider=self.name,
                model=model,
            )
        except asyncio.TimeoutError:
            return GenerationResult(
                success=False,
                error=f"agy timeout ({timeout}s)",
                provider=self.name,
                model=model,
            )
        except Exception as exc:
            return GenerationResult(
                success=False,
                error=str(exc),
                provider=self.name,
                model=model,
            )
