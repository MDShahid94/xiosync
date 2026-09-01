"""Strict CORS middleware — explicit origin allowlist only.

Normative references:
- Blueprint doc 09 §2 (INV-CORS-1): origin allowlist from config only;
  NO allow_origin_regex, NO wildcard.
- Acceptance gate G-OPS-1: non-allowlisted origin rejected.
- Closes invariant C4 (XIOPATH used allow_origin_regex=".*" +
  allow_credentials=True — a critical security bug).
"""

from __future__ import annotations

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp

from xiosync.platform.config import ConfigError, Environment

_CREDENTIAL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def validate_origins(origins: list[str], environment: str) -> None:
    """Raise ConfigError when the allowlist violates INV-CORS-1.

    - "*" is forbidden in every environment (C4 closure).
    - An empty list is forbidden in staging / production.
    """
    if "*" in origins:
        raise ConfigError(
            "Wildcard '*' is forbidden in CORS_ALLOWED_ORIGINS (INV-CORS-1 / C4). "
            "Enumerate explicit origins instead."
        )
    if environment in (Environment.STAGING, Environment.PRODUCTION) and not origins:
        raise ConfigError(
            f"CORS_ALLOWED_ORIGINS must be a non-empty list in the "
            f"{environment!r} environment (INV-CORS-1)."
        )


class StrictCORSMiddleware(CORSMiddleware):
    """CORSMiddleware locked to an explicit origin allowlist.

    Wraps Starlette's CORSMiddleware with:
    - allow_origins = the explicit allowlist (no wildcard, no regex)
    - allow_credentials = True  (safe because we control the allowlist)
    - allow_methods = full set
    - allow_headers = ["*"]
    """

    def __init__(self, app: ASGIApp, *, allowed_origins: list[str]) -> None:
        super().__init__(
            app,
            allow_origins=allowed_origins,
            allow_credentials=True,
            allow_methods=_CREDENTIAL_METHODS,
            allow_headers=["*"],
        )
