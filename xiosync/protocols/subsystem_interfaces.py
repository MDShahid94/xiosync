"""Protocol definitions for subsystem boundaries.

Subsystems depend on these protocols rather than concrete implementations,
enabling independent testing, evolution, and potential deployment separation.
"""
from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ScriptRunnerProtocol(Protocol):
    """Interface for executing scripts on remote hosts (xiogrid boundary)."""

    async def run_script(
        self,
        script_name: str,
        *args: str,
        timeout: int = 30,
    ) -> "ScriptResult":
        ...


@runtime_checkable
class ViewRegistryProtocol(Protocol):
    """Interface for xioview registry lookups (xioview boundary)."""

    def get_view_config(self, view_name: str) -> dict[str, Any] | None:
        ...

    def list_views(self, org_id: uuid.UUID) -> list[dict[str, Any]]:
        ...


@runtime_checkable
class RuntimePoolProtocol(Protocol):
    """Interface for managing browser runtime pools (xiorun boundary)."""

    async def acquire_runtime(self, profile_id: str) -> "RuntimeHandle":
        ...

    async def release_runtime(self, handle: "RuntimeHandle") -> None:
        ...


@runtime_checkable
class DAGExecutorProtocol(Protocol):
    """Interface for executing DAG workflows (xioflow boundary)."""

    async def execute(
        self,
        workflow: dict[str, Any],
        context: dict[str, Any],
    ) -> bool:
        ...


class ScriptResult:
    """Result of a script execution."""

    def __init__(
        self,
        *,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
        result: Any = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.result = result

    @property
    def success(self) -> bool:
        return self.returncode == 0


class RuntimeHandle:
    """Opaque handle to an acquired browser runtime."""

    def __init__(self, runtime_id: str, endpoint: str) -> None:
        self.runtime_id = runtime_id
        self.endpoint = endpoint
