"""API version governance middleware (Gap P-4).

Adds ``X-API-Version`` response header, reads ``Accept-Version`` request
header for future negotiation, and injects ``Sunset`` / ``Deprecation``
headers on configured endpoints.

Configuration is via ``XIOSYNC_API_DEPRECATION_CONFIG`` env var (JSON), e.g.:
``{"POST /api/v1/old-endpoint": {"sunset": "2027-06-01", "deprecation": "2027-01-01"}}``
"""

from __future__ import annotations

import json
import os
from typing import Any, cast

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

_CURRENT_API_VERSION = "1.0"


class VersionGovernanceMiddleware(BaseHTTPMiddleware):
    """Inject API versioning and deprecation governance headers (Gap P-4).

    Response headers:
    - ``X-API-Version``: Current API version (always present).
    - ``Accept-Version``: Acknowledged from request for future negotiation.
    - ``Sunset``: RFC 8594 sunset date (if endpoint is configured for deprecation).
    - ``Deprecation``: Deprecation date (if endpoint is configured).
    """

    def __init__(self, app: Any, deprecation_config: dict[str, Any] | None = None) -> None:
        super().__init__(app)
        self._deprecation_config = deprecation_config or self._load_config()

    @staticmethod
    def _load_config() -> dict[str, Any]:
        """Load deprecation config from env var."""
        raw = os.environ.get("XIOSYNC_API_DEPRECATION_CONFIG", "{}")
        try:
            return cast(dict[str, Any], json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            return {}

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)

        # Always add version header.
        response.headers["X-API-Version"] = _CURRENT_API_VERSION

        # Acknowledge Accept-Version if present.
        accept_version = request.headers.get("Accept-Version")
        if accept_version:
            response.headers["X-Accepted-Version"] = accept_version

        # Check for deprecation/sunset config on this endpoint.
        endpoint_key = f"{request.method} {request.url.path}"
        dep_config = self._deprecation_config.get(endpoint_key)
        if dep_config and isinstance(dep_config, dict):
            sunset = dep_config.get("sunset")
            deprecation = dep_config.get("deprecation")
            if sunset:
                response.headers["Sunset"] = sunset
            if deprecation:
                response.headers["Deprecation"] = deprecation

        return response
