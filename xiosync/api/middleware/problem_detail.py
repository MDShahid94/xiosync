"""RFC 7807 Problem Detail global error handler.

Converts all unhandled exceptions and FastAPI HTTPExceptions into
consistent application/problem+json responses.

https://www.rfc-editor.org/rfc/rfc7807
"""
from __future__ import annotations

import logging
import traceback
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

_BASE_TYPE = "https://xiosync.dev/problems"


def _problem_response(
    status: int,
    *,
    title: str,
    detail: str | None = None,
    type_suffix: str = "error",
    instance: str | None = None,
    extensions: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"{_BASE_TYPE}/{type_suffix}",
        "title": title,
        "status": status,
    }
    if detail:
        body["detail"] = detail
    if instance:
        body["instance"] = instance
    if extensions:
        body.update(extensions)
    return JSONResponse(
        status_code=status,
        content=body,
        media_type="application/problem+json",
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    return _problem_response(
        exc.status_code,
        title=exc.detail if isinstance(exc.detail, str) else "HTTP Error",
        type_suffix=f"http_{exc.status_code}",
        instance=str(request.url),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return _problem_response(
        422,
        title="Validation Error",
        detail="One or more request fields are invalid.",
        type_suffix="validation_error",
        instance=str(request.url),
        extensions={"errors": exc.errors()},
    )


async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    logger.exception(
        "Unhandled exception on %s %s", request.method, request.url,
        exc_info=exc,
    )
    return _problem_response(
        500,
        title="Internal Server Error",
        detail="An unexpected error occurred. Please try again later.",
        type_suffix="internal_error",
        instance=str(request.url),
    )
