"""Sandboxed-plugin API endpoints — approval-gated install + typed RPC execute.

The HTTP surface over ``PluginService`` (doc 07 §5). It exposes the
approval-gated installation lifecycle and the out-of-process RPC execution
boundary, and it maps every plugin invariant onto an HTTP contract:

* ``POST /plugins/{plugin_id}/install``                       — request an install
* ``POST /plugins/installations/{installation_id}/approve``   — approve (mint grant)
* ``POST /plugins/installations/{installation_id}/activate``  — activate
* ``GET  /plugins/installations/{installation_id}``           — inspect an install
* ``POST /plugins/installations/{installation_id}/rpc``       — execute one RPC method

INV-PLUGIN-3 (installation is approval-gated) is surfaced structurally: the
``install`` endpoint can only ever create a ``pending_approval`` record — there
is no request field that approves or activates in one step. Reaching an
operational (``active``) install requires the two further, separately-invoked
``approve`` and ``activate`` endpoints. Execution refuses any install that is
not ``active`` with a 409 (``plugin_not_operational``).

INV-PLUGIN-2 (narrow, typed host↔plugin RPC) is surfaced on the ``rpc``
endpoint: an undeclared method is refused with 404 (``unknown_rpc_method``) and
``params`` is constrained to a JSON object by the request model, so a
non-object payload never reaches the sandbox.

The router follows the same session/context wiring as the execution and DLQ
routers: ``AuthenticationMiddleware`` writes ``org_context`` and ``org_session``
onto ``request.state`` before any handler runs, so every endpoint here is
implicitly tenant-scoped.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session as OrmSession

from xiosync.domain.context import OrgContext
from xiosync.services.plugins import (
    InstallationNotApprovableError,
    InstallationNotFoundError,
    PluginAlreadyInstalledError,
    PluginExecutionError,
    PluginNotFoundError,
    PluginNotOperationalError,
    PluginService,
    PluginTimeoutError,
    UnknownRpcMethodError,
)

router = APIRouter(prefix="/plugins", tags=["plugins"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _context(request: Request) -> OrgContext:
    return cast(OrgContext, request.state.org_context)


def _session(request: Request) -> OrmSession:
    return cast(OrmSession, request.state.org_session)


def _service(request: Request) -> PluginService:
    return PluginService(_session(request))


def _problem(
    request: Request,
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://xiosync.dev/problems/{code}",
        "title": title,
        "status": status,
        "code": code,
        "request_id": request.state.request_id,
    }
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(status_code=status, media_type="application/problem+json", content=body)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class InstallRequest(StrictModel):
    """Body for ``POST /plugins/{plugin_id}/install`` (INV-PLUGIN-3)."""

    requested_by: uuid.UUID = Field(
        description="The actor ID requesting the installation."
    )


class ApproveRequest(StrictModel):
    """Body for ``POST /plugins/installations/{installation_id}/approve``."""

    approved_by: uuid.UUID = Field(
        description="The actor ID (an org admin) approving the installation."
    )


class InstallationResponse(StrictModel):
    """Read-model for a plugin_installations row (INV-PLUGIN-3)."""

    id: uuid.UUID
    organization_id: uuid.UUID
    plugin_id: uuid.UUID
    state: str
    requested_by: uuid.UUID
    approved_by: uuid.UUID | None = None
    grant_id: uuid.UUID | None = None


class RpcRequest(StrictModel):
    """Body for ``POST /plugins/installations/{installation_id}/rpc`` (INV-PLUGIN-2).

    ``params`` is constrained to a JSON object here so a non-object payload is
    rejected with 422 before any process is spawned (INV-PLUGIN-2). ``method``
    must be one the plugin manifest declared; an undeclared method is refused by
    the service with 404.
    """

    method: str = Field(min_length=1, description="Declared RPC method to invoke.")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON-object arguments for the RPC method (INV-PLUGIN-2).",
    )


class RpcResponse(StrictModel):
    method: str
    output: Any


# ---------------------------------------------------------------------------
# Installation lifecycle endpoints (INV-PLUGIN-3: approval-gated)
# ---------------------------------------------------------------------------


@router.post(
    "/{plugin_id}/install",
    response_model=InstallationResponse,
    status_code=201,
    summary="Request a plugin installation (INV-PLUGIN-3): lands in pending_approval",
)
def install_plugin(
    plugin_id: uuid.UUID,
    payload: InstallRequest,
    request: Request,
) -> InstallationResponse | JSONResponse:
    """Request an installation of a registered plugin into this organization.

    INV-PLUGIN-3: the created record can *only* land in ``pending_approval``.
    There is no request field that can shortcut approval or activation; the
    install must subsequently be approved and activated through the dedicated
    endpoints. A missing plugin returns 404; a plugin already installed in this
    org returns 409.
    """
    context = _context(request)
    service = _service(request)

    try:
        record = service.install_plugin(
            context,
            plugin_id,
            requested_by=payload.requested_by,
        )
    except PluginNotFoundError:
        return _problem(request, 404, "plugin_not_found", "Plugin not found")
    except PluginAlreadyInstalledError:
        return _problem(
            request,
            409,
            "plugin_already_installed",
            "Plugin is already installed in this organization",
        )

    return _installation_response(record)


@router.post(
    "/installations/{installation_id}/approve",
    response_model=InstallationResponse,
    summary="Approve a pending install and mint its grant (INV-PLUGIN-3)",
)
def approve_installation(
    installation_id: uuid.UUID,
    payload: ApproveRequest,
    request: Request,
) -> InstallationResponse | JSONResponse:
    """Approve a ``pending_approval`` install, minting its required-capability grant.

    INV-PLUGIN-3/1: only a ``pending_approval`` install may be approved; approval
    is the explicit, separately-authorized act that mints the capability grant.
    The install advances to ``approved`` — not yet ``active``. A missing install
    returns 404; an install in any non-``pending_approval`` state returns 409.
    """
    context = _context(request)
    service = _service(request)

    try:
        record = service.approve_installation(
            context,
            installation_id,
            approved_by=payload.approved_by,
        )
    except InstallationNotFoundError:
        return _problem(request, 404, "installation_not_found", "Installation not found")
    except InstallationNotApprovableError as exc:
        return _problem(
            request,
            409,
            "installation_not_approvable",
            "Installation cannot be approved in its current state",
            detail=f"state={exc.state!r}",
        )

    return _installation_response(record)


@router.post(
    "/installations/{installation_id}/activate",
    response_model=InstallationResponse,
    summary="Activate an approved install (INV-PLUGIN-3): approved → active",
)
def activate_installation(
    installation_id: uuid.UUID,
    request: Request,
) -> InstallationResponse | JSONResponse:
    """Activate an ``approved`` install so the plugin host may launch it.

    INV-PLUGIN-3: only an ``approved`` install may activate; a
    ``pending_approval`` install can never be activated, so the approval gate
    cannot be bypassed. A missing install returns 404; a non-``approved`` install
    returns 409.
    """
    context = _context(request)
    service = _service(request)

    try:
        record = service.activate_installation(context, installation_id)
    except InstallationNotFoundError:
        return _problem(request, 404, "installation_not_found", "Installation not found")
    except InstallationNotApprovableError as exc:
        return _problem(
            request,
            409,
            "installation_not_activatable",
            "Installation cannot be activated in its current state",
            detail=f"state={exc.state!r}",
        )

    return _installation_response(record)


@router.get(
    "/installations/{installation_id}",
    response_model=InstallationResponse,
    summary="Fetch a plugin installation record",
)
def get_installation(
    installation_id: uuid.UUID,
    request: Request,
) -> InstallationResponse | JSONResponse:
    """Return the current state of one installation record in this organization."""
    context = _context(request)
    service = _service(request)

    record = service.get_installation(context, installation_id)
    if record is None:
        return _problem(request, 404, "installation_not_found", "Installation not found")

    return _installation_response(record)


# ---------------------------------------------------------------------------
# Execution boundary endpoint (INV-PLUGIN-1/2/3)
# ---------------------------------------------------------------------------


@router.post(
    "/installations/{installation_id}/rpc",
    response_model=RpcResponse,
    summary="Invoke a typed RPC method on an active plugin (INV-PLUGIN-1/2/3)",
)
def execute_plugin_rpc(
    installation_id: uuid.UUID,
    payload: RpcRequest,
    request: Request,
) -> RpcResponse | JSONResponse:
    """Invoke one declared RPC method on an *active* plugin, out-of-process.

    Maps every execute-time plugin gate onto an HTTP status:

    * INV-PLUGIN-3 — a non-``active`` install returns 409
      (``plugin_not_operational``) before a process is ever spawned.
    * INV-PLUGIN-2 — an undeclared method returns 404 (``unknown_rpc_method``);
      ``params`` is constrained to a JSON object by the request model (422 on a
      non-object).
    * INV-PLUGIN-1 — the plugin runs under :func:`run_in_sandbox`; a wall-clock
      breach returns 504 (``plugin_timeout``) and any non-zero exit / unreadable
      reply returns 502 (``plugin_execution_failed``).
    """
    context = _context(request)
    service = _service(request)

    try:
        result = service.execute_plugin_rpc(
            context,
            installation_id,
            method=payload.method,
            params=payload.params,
        )
    except InstallationNotFoundError:
        return _problem(request, 404, "installation_not_found", "Installation not found")
    except PluginNotFoundError:
        return _problem(request, 404, "plugin_not_found", "Plugin not found")
    except PluginNotOperationalError as exc:
        return _problem(
            request,
            409,
            "plugin_not_operational",
            "Only an active installation may execute (INV-PLUGIN-3)",
            detail=f"state={exc.state!r}",
        )
    except UnknownRpcMethodError as exc:
        return _problem(
            request,
            404,
            "unknown_rpc_method",
            "RPC method is not declared by this plugin (INV-PLUGIN-2)",
            detail=f"method={exc.method!r}",
        )
    except PluginTimeoutError as exc:
        return _problem(
            request,
            504,
            "plugin_timeout",
            "Plugin exceeded its wall-clock quota (INV-PLUGIN-1)",
            detail=str(exc),
        )
    except PluginExecutionError as exc:
        return _problem(
            request,
            502,
            "plugin_execution_failed",
            "Plugin exited non-zero or produced an unreadable RPC reply",
            detail=str(exc),
        )
    except ValueError as exc:
        return _problem(
            request,
            400,
            "invalid_rpc_params",
            "RPC params are invalid (INV-PLUGIN-2)",
            detail=str(exc),
        )

    return RpcResponse(method=result.method, output=result.output)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _installation_response(record: Any) -> InstallationResponse:
    return InstallationResponse(
        id=record.id,
        organization_id=record.organization_id,
        plugin_id=record.plugin_id,
        state=record.state,
        requested_by=record.requested_by,
        approved_by=record.approved_by,
        grant_id=record.grant_id,
    )
