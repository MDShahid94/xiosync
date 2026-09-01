"""Pure sharing domain — validation and constants (Gap M-2).

No I/O — RULE-ARCH-1.
"""

from __future__ import annotations

__all__ = [
    "InvalidShareTypeError",
    "SHAREABLE_TYPES",
    "SHARE_PERMISSIONS",
    "SHARE_STATES",
    "validate_share_type",
    "validate_share_permissions",
]

#: Resource types eligible for cross-org sharing.
SHAREABLE_TYPES: frozenset[str] = frozenset(
    {"capability", "artifact", "workflow", "plugin"}
)

#: Permissions grantable on a shared resource.
SHARE_PERMISSIONS: frozenset[str] = frozenset({"read", "execute", "fork"})

#: Share lifecycle states.
SHARE_STATES: frozenset[str] = frozenset({"active", "revoked"})


class InvalidShareTypeError(ValueError):
    """Raised when a resource type is not shareable."""


def validate_share_type(resource_type: str) -> None:
    """Reject non-shareable resource types."""
    if resource_type not in SHAREABLE_TYPES:
        raise InvalidShareTypeError(
            f"resource type {resource_type!r} is not shareable; "
            f"expected one of {sorted(SHAREABLE_TYPES)}"
        )


def validate_share_permissions(permissions: list[str]) -> None:
    """Reject unknown share permissions."""
    invalid = set(permissions) - SHARE_PERMISSIONS
    if invalid:
        raise ValueError(
            f"invalid share permissions {sorted(invalid)}; "
            f"expected subset of {sorted(SHARE_PERMISSIONS)}"
        )
