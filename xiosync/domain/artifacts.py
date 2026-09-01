"""Pure domain predicates for artifacts (Gap D-1).

This module is pure domain (RULE-ARCH-1): no I/O, no framework imports.
It governs the closed set of provider types and URI scheme validation.
"""

from __future__ import annotations

__all__ = [
    "PROVIDER_TYPES",
    "InvalidProviderTypeError",
    "validate_provider_type",
]

#: Supported storage provider types. The platform never proxies raw bytes;
#: each provider type implies a URI scheme convention (e.g. ``s3://bucket/key``).
PROVIDER_TYPES: frozenset[str] = frozenset(
    {"r2", "s3", "gcs", "azure_blob", "local", "inline", "custom"}
)

#: Expected URI scheme prefixes per provider type (advisory, not strictly enforced).
_SCHEME_HINTS: dict[str, tuple[str, ...]] = {
    "r2": ("r2://", "https://"),
    "s3": ("s3://", "https://"),
    "gcs": ("gs://", "https://"),
    "azure_blob": ("https://", "az://"),
    "local": ("file://", "/"),
    "inline": ("data:", "inline:"),
    "custom": (),  # no scheme restriction
}


class InvalidProviderTypeError(ValueError):
    """Raised when the provider_type is not one of the known types."""

    def __init__(self, provider_type: str) -> None:
        super().__init__(
            f"provider_type {provider_type!r} is not one of "
            f"{sorted(PROVIDER_TYPES)}"
        )
        self.provider_type = provider_type


def validate_provider_type(provider_type: str) -> str:
    """Return ``provider_type`` if it is known, else raise."""
    if provider_type not in PROVIDER_TYPES:
        raise InvalidProviderTypeError(provider_type)
    return provider_type
