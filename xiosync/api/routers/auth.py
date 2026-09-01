"""Authentication transport contracts; business rules remain in SessionService."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from xiosync.domain.context import OrgContext
from xiosync.platform.clock import Clock
from xiosync.services.identity import AuthenticationError, SessionService, TokenPair

router = APIRouter(prefix="/auth", tags=["authentication"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(StrictModel):
    organization_id: uuid.UUID
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class RefreshRequest(StrictModel):
    refresh_token: str = Field(min_length=1, max_length=2048)


class TokenResponse(StrictModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 — protocol token type
    access_token_expires_at: datetime
    refresh_token: str
    session_id: uuid.UUID
    organization_id: uuid.UUID
    request_id: str


class LogoutResponse(StrictModel):
    status: Literal["logged_out"] = "logged_out"
    request_id: str


def _service(request: Request) -> SessionService:
    return cast(SessionService, request.app.state.session_service)


def _clock(request: Request) -> Clock:
    return cast(Clock, request.app.state.clock)


def _token_response(pair: TokenPair, request: Request) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token,
        access_token_expires_at=pair.access_token_expires_at,
        refresh_token=pair.refresh_token,
        session_id=pair.session_id,
        organization_id=pair.organization_id,
        request_id=request.state.request_id,
    )


def _authentication_problem(request: Request) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        media_type="application/problem+json",
        content={
            "type": "https://xiosync.dev/problems/authentication_failed",
            "title": "Authentication failed",
            "status": 401,
            "code": "authentication_failed",
            "request_id": request.state.request_id,
        },
    )


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    service: Annotated[SessionService, Depends(_service)],
    clock: Annotated[Clock, Depends(_clock)],
) -> TokenResponse | JSONResponse:
    try:
        pair = service.login(
            payload.organization_id, payload.email, payload.password, now=clock.now()
        )
    except AuthenticationError:
        return _authentication_problem(request)
    return _token_response(pair, request)


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    payload: RefreshRequest,
    request: Request,
    service: Annotated[SessionService, Depends(_service)],
    clock: Annotated[Clock, Depends(_clock)],
) -> TokenResponse | JSONResponse:
    try:
        pair = service.refresh(payload.refresh_token, now=clock.now())
    except AuthenticationError:
        return _authentication_problem(request)
    return _token_response(pair, request)


@router.post("/logout", response_model=LogoutResponse)
def logout(
    request: Request,
    service: Annotated[SessionService, Depends(_service)],
    clock: Annotated[Clock, Depends(_clock)],
) -> LogoutResponse:
    context: OrgContext = request.state.org_context
    service.logout(context, now=clock.now())
    return LogoutResponse(request_id=request.state.request_id)
