"""Branch-isolated variable context for parallel DAG execution.

Strategy: copy-on-branch with last-writer-wins merge.
Each branch receives a shallow copy of workflow_vars at fork point.
After all branches complete, results are merged back into the parent
using last-writer-wins semantics (deterministic by branch order).
"""

from __future__ import annotations
import copy
from typing import Any


class BranchVars:
    """Snapshot-isolated variable context for a parallel branch."""

    __slots__ = ("_parent", "_local", "_writes")

    def __init__(self, parent: dict[str, Any]) -> None:
        self._parent = parent
        self._local = copy.copy(parent)  # shallow copy at fork
        self._writes: dict[str, Any] = {}  # track mutations

    def get(self, key: str, default: Any = None) -> Any:
        return self._local.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._local[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._local[key] = value
        self._writes[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self._local

    def update(self, mapping: dict[str, Any]) -> None:
        self._local.update(mapping)
        self._writes.update(mapping)

    def items(self):
        return self._local.items()

    def keys(self):
        return self._local.keys()

    def values(self):
        return self._local.values()

    def __iter__(self):
        return iter(self._local)

    def __len__(self):
        return len(self._local)

    @property
    def writes(self) -> dict[str, Any]:
        """Return only the keys this branch wrote."""
        return dict(self._writes)

    def as_dict(self) -> dict[str, Any]:
        """Return the full local state."""
        return dict(self._local)


def merge_branch_results(
    parent: dict[str, Any],
    branches: list[BranchVars],
) -> None:
    """Merge branch writes into parent using ordered last-writer-wins.

    Branches are merged in order (branch 0 first, branch N last).
    If two branches write the same key, the later branch wins.
    This is deterministic — same input always produces same output.
    """
    for branch in branches:
        parent.update(branch.writes)
