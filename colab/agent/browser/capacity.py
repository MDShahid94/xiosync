"""Dynamic runtime capacity detection for Colab environments.

Detects CPU / T4 GPU / TPU Colab runtime and computes safe max Chrome sessions.
Policy: CPU, T4 GPU, and TPU runtimes are supported.
Formula:
  CPU / TPU: max_sessions = min(cpu_count * SESSIONS_PER_VCPU_CPU, sys_ram_sessions)
  T4:        max_sessions = min(cpu_count * SESSIONS_PER_VCPU_GPU, sys_ram_sessions, gpu_sessions)
Chrome headless sys RAM: ~180 MB/session (CPU/TPU), ~130 MB/session (T4, GPU handles rendering)
Chrome GPU VRAM: ~120 MB/session on T4. TPU VRAM is irrelevant (Chrome is CPU-only).
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any
import psutil

logger = logging.getLogger("xiorun_agent.browser.capacity")

_SESSIONS_PER_VCPU_CPU = 3  # CPU/TPU runtime: conservative (JS engine contention)
_SESSIONS_PER_VCPU_GPU = 6  # T4 runtime: GPU handles rendering → more headroom
_CHROME_SYS_MB_CPU = 180  # sys RAM per Chrome session (CPU/TPU runtime)
_CHROME_SYS_MB_GPU = 130  # sys RAM per Chrome session (T4 — GPU offloads renderer)
_CHROME_GPU_MB = 120  # GPU VRAM per Chrome session (T4 only)
_OS_HEADROOM_MB = 1536  # 1.5 GB reserved for OS + agent + Colab kernel


def _detect_runtime_capacity() -> dict[str, Any]:
    """Detect Colab runtime type and compute safe max Chrome sessions.

    Returns:
        {runtime_type, sys_ram_gb, gpu_vram_gb, cpu_count, max_sessions}
    """
    sys_ram_mb = psutil.virtual_memory().total // (1024 * 1024)
    cpu_count = psutil.cpu_count(logical=True) or 2
    gpu_vram_mb = 0
    gpu_name = ""
    runtime_type = "cpu"

    # ── TPU detection (check before nvidia-smi; TPU runtimes have no CUDA GPU) ─
    _tpu_name = (
        os.environ.get("TPU_NAME")
        or os.environ.get("TPU_ACCELERATOR_TYPE")
        or os.environ.get("TPU_WORKER_ID")
        or os.environ.get("COLAB_TPU_ADDR")
    )
    _has_tpu_device = os.path.exists("/dev/accel0")
    if _tpu_name or _has_tpu_device:
        # TPU runtimes cannot use TPU VRAM for Chrome (no XLA for V8/Blink).
        # Use CPU formula. gpu_vram stays 0.
        runtime_type = "tpu"
        tpu_label = _tpu_name or "tpu"
        logger.info(f"TPU runtime detected: {tpu_label} — applying CPU session limits")
    else:
        # ── GPU detection (nvidia-smi) ─────────────────────────────────────────
        try:
            out = (
                subprocess.check_output(
                    [
                        "nvidia-smi",
                        "--query-gpu=name,memory.total",
                        "--format=csv,noheader,nounits",
                    ],
                    timeout=5,
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .strip()
            )
            if out:
                parts = [p.strip() for p in out.split(",")]
                gpu_name = parts[0].lower()
                gpu_vram_mb = int(parts[1]) if len(parts) > 1 else 0
                if "t4" in gpu_name:
                    runtime_type = "t4"
                else:
                    # A100, V100, etc. — not supported by policy; treat as CPU limit
                    logger.warning(f"Unsupported GPU runtime: {parts[0]} — applying CPU limits")
                    runtime_type = "cpu"
        except FileNotFoundError:
            pass  # no nvidia-smi = CPU runtime
        except Exception as e:
            logger.warning(f"nvidia-smi error: {e}")

    available_sys_mb = max(0, sys_ram_mb - _OS_HEADROOM_MB)

    if runtime_type == "t4":
        available_gpu_mb = max(0, gpu_vram_mb - 512)  # 512 MB GPU OS headroom
        cpu_limit = cpu_count * _SESSIONS_PER_VCPU_GPU
        sys_ram_limit = available_sys_mb // _CHROME_SYS_MB_GPU
        gpu_limit = available_gpu_mb // _CHROME_GPU_MB
        max_sessions = max(1, min(cpu_limit, sys_ram_limit, gpu_limit))
    else:
        # CPU-only and TPU both use the CPU formula (no GPU VRAM for Chrome)
        cpu_limit = cpu_count * _SESSIONS_PER_VCPU_CPU
        sys_ram_limit = available_sys_mb // _CHROME_SYS_MB_CPU
        max_sessions = max(1, min(cpu_limit, sys_ram_limit))

    cap = {
        "runtime_type": runtime_type,
        "gpu_name": gpu_name or "none",
        "sys_ram_gb": round(sys_ram_mb / 1024, 1),
        "gpu_vram_gb": round(gpu_vram_mb / 1024, 1),
        "cpu_count": cpu_count,
        "max_sessions": max_sessions,
    }
    logger.info(f"runtime-capacity: {cap}")
    return cap


detect_runtime_capacity = _detect_runtime_capacity
