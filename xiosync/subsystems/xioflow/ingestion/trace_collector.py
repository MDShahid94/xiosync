"""TraceCollector — collects browser and non-browser actions into a linear trace stream.

Designed to be passed into PageProxy and StepContext wrappers during
script execution.  The trace is a pure in-memory data structure that
can later be converted to ``xioflow_memory_nodes`` by ``DAGGraphBuilder``.

Ontology compliance:
  - Each trace action maps to one ``XioflowMemoryNode``
  - ``recording_method='auto_trace'``
  - ``tier='project_experimental'`` (promoted via ConsensusEngine on success)
  - Graph edges are ``GRAPH_CLASS_WORKFLOW`` (acyclic, doc 03 §3)
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TraceAction:
    """A single captured action in the trace stream."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.monotonic)

    # Classification
    category: str = "browser"  # browser | http | system | compute | control
    step_name: str = "root"  # from ctx.step() boundary
    action_type: str = ""  # click, fill, navigate, http_request, ssh_command, ...

    # Locator data (browser actions only)
    place_value: dict[str, Any] | None = None  # 9-tier locators
    face_value: dict[str, Any] | None = None  # visual signature

    # Action parameters
    action_params: dict[str, Any] = field(default_factory=dict)

    # Result (populated after execution)
    success: bool = True
    output: Any = None
    output_var: str | None = None
    duration_ms: float = 0

    # Intent (auto-generated, refined later)
    intent: str = ""

    # Domain context
    url: str = ""
    domain: str = ""


class TraceCollector:
    """Collects both browser and non-browser actions into a linear trace.

    Thread-safe via simple append-only semantics.  The step_stack tracks
    ``ctx.step()`` nesting so each action knows which logical step it
    belongs to.

    Usage::

        trace = TraceCollector(org_id="...", domain="accounts.google.com")
        trace.enter_step("phase1_login")
        trace.record(TraceAction(action_type="navigate", ...))
        trace.exit_step("phase1_login")
        dag_json = DAGGraphBuilder().build(trace, context)
    """

    def __init__(self, org_id: str, domain: str, run_id: str = "") -> None:
        self.org_id = org_id
        self.domain = domain
        self.run_id = run_id
        self.actions: list[TraceAction] = []
        self._step_stack: list[str] = []
        self._started_at = time.monotonic()

    @property
    def current_step(self) -> str:
        """Name of the currently active step, or 'root'."""
        return self._step_stack[-1] if self._step_stack else "root"

    def enter_step(self, step_name: str) -> None:
        """Push a step boundary (called when ``ctx.step()`` begins)."""
        self._step_stack.append(step_name)
        logger.debug("trace.enter_step: %s (depth=%d)", step_name, len(self._step_stack))

    def exit_step(self, step_name: str, success: bool = True) -> None:
        """Pop a step boundary (called when ``ctx.step()`` completes)."""
        if self._step_stack and self._step_stack[-1] == step_name:
            self._step_stack.pop()
        logger.debug("trace.exit_step: %s success=%s", step_name, success)

    def record(self, action: TraceAction) -> None:
        """Append an action to the trace stream."""
        if not action.intent:
            action.intent = self._auto_intent(action)
        action.step_name = self.current_step
        self.actions.append(action)
        logger.debug(
            "trace.record: %s [%s] step=%s intent=%s",
            action.action_type,
            action.category,
            action.step_name,
            action.intent,
        )

    def record_browser_action(
        self,
        *,
        action_type: str,
        place_value: dict[str, Any] | None = None,
        face_value: dict[str, Any] | None = None,
        action_params: dict[str, Any] | None = None,
        url: str = "",
        duration_ms: float = 0,
        output_var: str | None = None,
    ) -> None:
        """Convenience method to record a browser action."""
        from urllib.parse import urlparse

        self.record(
            TraceAction(
                category="browser",
                action_type=action_type,
                place_value=place_value,
                face_value=face_value,
                action_params=action_params or {},
                url=url,
                domain=urlparse(url).netloc if url else self.domain,
                duration_ms=duration_ms,
                output_var=output_var,
            )
        )

    def record_non_browser_action(
        self,
        *,
        action_type: str,
        action_params: dict[str, Any] | None = None,
        output: Any = None,
        output_var: str | None = None,
        duration_ms: float = 0,
    ) -> None:
        """Convenience method to record a non-browser action (HTTP, SSH, etc.)."""
        self.record(
            TraceAction(
                category="http" if action_type == "http_request" else "system",
                action_type=action_type,
                action_params=action_params or {},
                output=output,
                output_var=output_var,
                duration_ms=duration_ms,
            )
        )

    def _auto_intent(self, action: TraceAction) -> str:
        """Generate a human-readable intent string from step + action."""
        step = self.current_step
        idx = len(self.actions)
        return f"{step}__{action.action_type}_{idx}"

    def to_summary(self) -> dict[str, Any]:
        """Summary stats for logging and metadata."""
        return {
            "total_actions": len(self.actions),
            "browser_actions": sum(1 for a in self.actions if a.category == "browser"),
            "non_browser_actions": sum(1 for a in self.actions if a.category != "browser"),
            "steps": list(dict.fromkeys(a.step_name for a in self.actions)),
            "duration_ms": round((time.monotonic() - self._started_at) * 1000),
            "domain": self.domain,
            "run_id": self.run_id,
        }
