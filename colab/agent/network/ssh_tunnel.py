"""colab.agent.network.ssh_tunnel — SSH SOCKS5 tunnel setup and SSH key management."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any

from agent.config import (
    DRIVE_ROOT,
    NODE_NAME,
    PROXY_LOCAL_PORT,
    PROXY_SSH_HOST,
    PROXY_SSH_KEY,
    PROXY_SSH_USER,
)

logger = logging.getLogger("xiorun_agent.network.ssh_tunnel")


def write_pem_key(key_content: str, key_path: str = PROXY_SSH_KEY) -> str:
    """Write an SSH PEM key to disk with strict 0600 permissions."""
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    with open(key_path, "w") as f:
        f.write(key_content.strip() + "\n")
    os.chmod(key_path, 0o600)
    logger.info(f"Wrote SSH key with 0600 permissions to {key_path}")
    return key_path


def start_ssh_socks5_tunnel(
    ssh_host: str = PROXY_SSH_HOST,
    ssh_user: str = PROXY_SSH_USER,
    ssh_key: str = PROXY_SSH_KEY,
    local_port: int = PROXY_LOCAL_PORT,
) -> tuple[subprocess.Popen | None, str | None]:
    """Start SSH SOCKS5 tunnel process in background.

    Returns:
        (process, proxy_url) if successful, or (None, None) on failure.
    """
    if not (ssh_host and os.path.exists(ssh_key)):
        return None, None

    try:
        proc = subprocess.Popen(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ServerAliveInterval=30",
                "-o",
                "ServerAliveCountMax=3",
                "-o",
                "ExitOnForwardFailure=yes",
                "-D",
                f"127.0.0.1:{local_port}",
                "-N",
                "-i",
                ssh_key,
                f"{ssh_user}@{ssh_host}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(3)
        if proc.poll() is None:
            proxy_url = f"socks5://127.0.0.1:{local_port}"
            logger.info(f"SSH SOCKS5 tunnel up: {ssh_user}@{ssh_host} → {proxy_url}")
            return proc, proxy_url
        else:
            logger.warning(f"SSH SOCKS5 tunnel failed (exit {proc.returncode})")
            return None, None
    except Exception as e:
        logger.warning(f"SSH SOCKS5 tunnel setup failed: {e}")
        return None, None


def save_node_ssh_pubkey_to_drive(
    node_name: str = NODE_NAME,
    drive_root: str = DRIVE_ROOT,
) -> None:
    """Save worker node's SSH public key to Drive ssh_pubkeys/ on agent startup."""
    ssh_id_file = os.environ.get("XIO_NODE_SSH_ID", f"/root/.ssh/{node_name or 'xio_node_id'}")
    pubkey_file = f"{ssh_id_file}.pub"
    if node_name and os.path.isfile(pubkey_file):
        try:
            pubkey_content = open(pubkey_file).read().strip()
            drive_pubkey_path = os.path.join(drive_root, "ssh_pubkeys", f"{node_name}.pub")
            os.makedirs(os.path.dirname(drive_pubkey_path), exist_ok=True)
            existing = ""
            if os.path.isfile(drive_pubkey_path):
                try:
                    existing = open(drive_pubkey_path).read().strip()
                except Exception:
                    pass
            if existing != pubkey_content:
                with open(drive_pubkey_path, "w") as pkf:
                    pkf.write(pubkey_content + "\n")
                logger.info(f"Saved pubkey {node_name}.pub to Drive ssh_pubkeys/")
            else:
                logger.debug(f"Pubkey {node_name}.pub unchanged, skipped")
        except Exception as pke:
            logger.warning(f"Pubkey save failed (non-fatal): {pke}")
