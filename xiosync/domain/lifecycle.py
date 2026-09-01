"""Pure lifecycle state machines — only declared transitions are legal (doc 03 §4).

This module is pure domain (RULE-ARCH-1): no I/O, no framework imports. It
encodes the state machines doc 03 §4 fixes as the bootstrap default and exposes
a single predicate — ``StateMachine.is_valid_transition`` — plus a fail-closed
``assert_transition`` that the service layer calls *before* it writes anything.

**INV-LC-1:** only a transition declared in a machine's table is legal; any
other (including an unknown source or target state, and a same-state no-op that
is not explicitly declared) is rejected and produces no state change.

The lifecycle *states* are ultimately Type-Registry data (doc 03 §8, category
``lifecycle_state``); the tables here are the seed a fresh install starts from,
kept in one place so the enforcement (INV-LC-1) and the vocabulary never drift
apart. INV-LC-2 (each transition writes an Operation + Event) is a *service*
responsibility and lives in ``services/lifecycle``; this module only decides
*legality*.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "ACTOR_LIFECYCLE",
    "GRANT_LIFECYCLE",
    "SESSION_LIFECYCLE",
    "WORKER_LIFECYCLE",
    "WORKFLOW_RUN_LIFECYCLE",
    "IllegalTransitionError",
    "StateMachine",
    "phase_for_state",
]


class IllegalTransitionError(ValueError):
    """A transition not declared by the machine was attempted (INV-LC-1)."""

    def __init__(self, machine: str, from_state: str, to_state: str) -> None:
        super().__init__(
            f"illegal {machine} transition {from_state!r} -> {to_state!r}: "
            "only declared transitions are legal (INV-LC-1)"
        )
        self.machine = machine
        self.from_state = from_state
        self.to_state = to_state


@dataclass(frozen=True, slots=True)
class StateMachine:
    """A named set of legal directed transitions between states.

    ``transitions`` maps each state to the frozen set of states it may move to.
    A state with an empty set is terminal. Membership in ``transitions`` is the
    definition of a *known* state; a source or target absent from the table is
    therefore illegal, upholding the fail-closed reading of INV-LC-1.
    """

    name: str
    transitions: Mapping[str, frozenset[str]]

    @property
    def states(self) -> frozenset[str]:
        """Every state the machine knows (all sources are keys)."""
        return frozenset(self.transitions)

    def is_known_state(self, state: str) -> bool:
        """Whether ``state`` is part of this machine's vocabulary."""
        return state in self.transitions

    def allowed_from(self, state: str) -> frozenset[str]:
        """The states reachable from ``state`` in one legal step (empty if none)."""
        return self.transitions.get(state, frozenset())

    def is_valid_transition(self, from_state: str, to_state: str) -> bool:
        """Whether ``from_state -> to_state`` is a declared, legal transition."""
        return to_state in self.transitions.get(from_state, frozenset())

    def assert_transition(self, from_state: str, to_state: str) -> None:
        """Raise :class:`IllegalTransitionError` unless the transition is legal."""
        if not self.is_valid_transition(from_state, to_state):
            raise IllegalTransitionError(self.name, from_state, to_state)


def _machine(name: str, transitions: Mapping[str, frozenset[str] | set[str]]) -> StateMachine:
    """Freeze a transition table into a :class:`StateMachine`."""
    return StateMachine(name, {state: frozenset(targets) for state, targets in transitions.items()})


# -- Actor lifecycle (doc 03 §4.1) -----------------------------------------
# proposed -> designing -> implementing -> validating -> initializing -> active
# active -> updating -> active
# active -> suspended -> active
# active -> migrating -> active
# active -> terminating -> terminated -> archived
ACTOR_LIFECYCLE: StateMachine = _machine(
    "actor",
    {
        "proposed": {"designing"},
        "designing": {"implementing"},
        "implementing": {"validating"},
        "validating": {"initializing"},
        "initializing": {"active"},
        "active": {"updating", "suspended", "migrating", "terminating"},
        "updating": {"active"},
        "suspended": {"active"},
        "migrating": {"active"},
        "terminating": {"terminated"},
        "terminated": {"archived"},
        "archived": set(),
    },
)

# -- Actor lifecycle phases (doc 03 §4.1) ----------------------------------
PRE_BIRTH = "pre_birth"
BIRTH = "birth"
OPERATIONAL = "operational"
END_OF_LIFE = "end_of_life"

_STATE_PHASES: Mapping[str, str] = {
    "proposed": PRE_BIRTH,
    "designing": PRE_BIRTH,
    "implementing": PRE_BIRTH,
    "validating": PRE_BIRTH,
    "initializing": BIRTH,
    "active": OPERATIONAL,
    "updating": OPERATIONAL,
    "suspended": OPERATIONAL,
    "migrating": OPERATIONAL,
    "terminating": END_OF_LIFE,
    "terminated": END_OF_LIFE,
    "archived": END_OF_LIFE,
}


def phase_for_state(state: str) -> str:
    """The ``lifecycle_phase`` bucket a given actor ``state`` belongs to.

    Raises :class:`IllegalTransitionError` for an unknown state so a caller can
    never persist a state the actor machine does not recognize.
    """
    phase = _STATE_PHASES.get(state)
    if phase is None:
        raise IllegalTransitionError("actor", state, state)
    return phase


# -- Other lifecycle machines (doc 03 §§4.2–4.5) ---------------------------
# These are pure, declared once here so no subsystem re-invents them. A new
# grant/session is always a new row; there is no path out of a terminal state.
GRANT_LIFECYCLE: StateMachine = _machine(
    "grant",
    {
        "active": {"revoked", "expired"},
        "revoked": set(),
        "expired": set(),
    },
)

SESSION_LIFECYCLE: StateMachine = _machine(
    "session",
    {
        "active": {"revoked", "expired"},
        "revoked": set(),
        "expired": set(),
    },
)

WORKFLOW_RUN_LIFECYCLE: StateMachine = _machine(
    "workflow_run",
    {
        "queued": {"running", "cancelled"},
        "running": {"paused", "succeeded", "failed", "cancelled"},
        "paused": {"running", "cancelled"},
        "succeeded": set(),
        "failed": set(),
        "cancelled": set(),
    },
)

WORKER_LIFECYCLE: StateMachine = _machine(
    "worker",
    {
        "pending": {"enrolled", "revoked"},
        "enrolled": {"active", "revoked"},
        "active": {"draining", "suspended", "revoked"},
        "draining": {"offline", "revoked"},
        "suspended": {"active", "revoked"},
        "offline": {"revoked"},
        "revoked": set(),
    },
)
