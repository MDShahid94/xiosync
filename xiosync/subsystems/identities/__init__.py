"""Identities subsystem — universal external-identity + credential registry."""

from xiosync.subsystems.identities.service import (
    CredentialRecord,
    IdentityConflictError,
    IdentityNotFoundError,
    IdentityRecord,
    IdentityService,
)

__all__ = [
    "IdentityService",
    "IdentityRecord",
    "CredentialRecord",
    "IdentityNotFoundError",
    "IdentityConflictError",
]
