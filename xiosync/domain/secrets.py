"""Pure secrets domain (Gap S-1).

Provider-agnostic secret types and validation. No I/O — RULE-ARCH-1.
"""

from __future__ import annotations

__all__ = [
    "InvalidProviderError",
    "InvalidSecretStateError",
    "PROVIDER_TYPES",
    "SECRET_STATES",
    "validate_provider",
    "validate_secret_state",
]

#: Supported secret provider types. The platform stores references only;
#: actual value resolution is delegated to the provider adapter at the worker.
PROVIDER_TYPES: frozenset[str] = frozenset(
    {"env", "vault", "aws-sm", "gcp-sm", "azure-kv", "inline", "custom"}
)

#: Secret reference lifecycle states.
SECRET_STATES: frozenset[str] = frozenset({"active", "rotated", "revoked"})


class InvalidProviderError(ValueError):
    """Raised when a secret provider type is not recognized."""


class InvalidSecretStateError(ValueError):
    """Raised when a secret state is not valid."""


def validate_provider(provider: str) -> None:
    """Reject unknown provider types."""
    if provider not in PROVIDER_TYPES:
        raise InvalidProviderError(
            f"unknown secret provider {provider!r}; "
            f"expected one of {sorted(PROVIDER_TYPES)}"
        )


def validate_secret_state(state: str) -> None:
    """Reject invalid secret states."""
    if state not in SECRET_STATES:
        raise InvalidSecretStateError(
            f"invalid secret state {state!r}; "
            f"expected one of {sorted(SECRET_STATES)}"
        )
