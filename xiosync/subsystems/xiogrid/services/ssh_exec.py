"""Synchronous SSH command executor for VM-side scripts.

Uses subprocess.run — synchronous and thread-safe.
All calls complete in < 45s (script execution bound).
Parameterized by PPPoEHost so any registered host works identically.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xiosync.subsystems.xiogrid.models.exit_node import PPPoEHost

_SSH_BASE_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "BatchMode=yes",           # never prompt for password
    "-o", "ConnectTimeout=8",
    "-o", "ServerAliveInterval=10",
    "-o", "ServerAliveCountMax=3",
]


@dataclass(frozen=True)
class SSHResult:
    stdout: str
    stderr: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def require_ok(self) -> "SSHResult":
        if not self.ok:
            raise RuntimeError(
                f"SSH command failed (rc={self.returncode}): {self.stderr or self.stdout}"
            )
        return self


def ssh_run(
    user_at_host: str,
    remote_cmd: str,
    port: int = 22,
    timeout: int = 15,
) -> SSHResult:
    """Run any command on a remote host via SSH."""
    result = subprocess.run(
        ["ssh", *_SSH_BASE_OPTS, "-p", str(port), user_at_host, remote_cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return SSHResult(
        stdout=result.stdout.strip(),
        stderr=result.stderr.strip(),
        returncode=result.returncode,
    )


def vm_script(
    host: "PPPoEHost",
    script: str,
    *args: str | int,
    timeout: int = 45,
) -> SSHResult:
    """Call a xiogrid script on the host's Ubuntu VM.

    Scripts live in host.vm_scripts_dir (default: /usr/local/bin/xiogrid/).
    All scripts run as sudo on the VM.
    """
    arg_str = " ".join(str(a) for a in args)
    cmd = f"sudo {host.vm_scripts_dir}/{script} {arg_str}".rstrip()
    return ssh_run(
        user_at_host=f"{host.vm_ssh_user}@{host.vm_ssh_host}",
        remote_cmd=cmd,
        port=host.vm_ssh_port,
        timeout=timeout,
    )


def vm_ping(host: "PPPoEHost") -> bool:
    """Quick liveness check — succeeds if SSH is reachable."""
    result = ssh_run(
        f"{host.vm_ssh_user}@{host.vm_ssh_host}",
        "echo ok",
        port=host.vm_ssh_port,
        timeout=6,
    )
    return result.ok and result.stdout == "ok"
