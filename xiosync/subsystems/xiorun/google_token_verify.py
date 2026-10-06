"""Google runtime identity verification via OAuth tokeninfo.

Verifies the Google account running a Colab runtime by inspecting the
default Application Default Credentials (ADC). Used during boot.py to
resolve the Colab account for mesh node binding.

The verified email is cross-referenced against the identities registry
to confirm the worker is running under an authorized account.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TOKENINFO_URL = "https://www.googleapis.com/oauth2/v1/tokeninfo"
TOKENINFO_TIMEOUT = 10

def verify_runtime_google_identity() -> str | None:
    try:
        import google.auth
        from google.auth.transport.requests import Request
        import requests
        
        creds, _ = google.auth.default()
        creds.refresh(Request())
        token = creds.token
        
        if not token:
            logger.warning("No token available in Google default credentials.")
            return None
            
        response = requests.get(
            f"{TOKENINFO_URL}?access_token={token}",
            timeout=TOKENINFO_TIMEOUT
        )
        response.raise_for_status()
        
        data = response.json()
        return data.get("email")
        
    except Exception as e:
        logger.warning(f"Failed to verify runtime google identity: {e}")
        return None

def detect_runtime_type() -> str:
    if os.path.exists("/content"):
        try:
            subprocess.run(
                ["nvidia-smi"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True
            )
            return "colab_gpu"
        except (subprocess.CalledProcessError, FileNotFoundError):
            return "colab_cpu"
            
    if os.path.exists("/.dockerenv"):
        return "docker"
        
    if os.path.exists("/sys/class/dmi"):
        return "bare_metal"
        
    return "vm"

@dataclass
class RuntimeIdentity:
    email: str | None
    runtime_type: str
    verified: bool

def get_runtime_identity() -> RuntimeIdentity:
    email = verify_runtime_google_identity()
    rtype = detect_runtime_type()
    
    return RuntimeIdentity(
        email=email,
        runtime_type=rtype,
        verified=bool(email)
    )
