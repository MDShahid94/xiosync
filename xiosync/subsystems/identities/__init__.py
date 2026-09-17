"""Identities subsystem — universal external-identity + credential registry."""
from xiosync.subsystems.identities.service import (
    IdentityService, IdentityRecord, CredentialRecord,
    IdentityNotFoundError, IdentityConflictError,
)

__all__ = [
    "IdentityService", "IdentityRecord", "CredentialRecord",
    "IdentityNotFoundError", "IdentityConflictError",
]
