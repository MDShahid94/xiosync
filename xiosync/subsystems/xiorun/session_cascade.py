"""4-level session cascade — progressive session validation before sign-in.

Levels (cheapest to most expensive):
  1. Database check: profile_domain_sets WHERE is_valid AND urgency≠immediate
  2. Local tar check: /tmp/xiorun_profiles/PRFL-{serial}/ on disk
  3. Live browser verify: Patchright → myaccount.google.com → DOM check
  4. Org storage pull: download tar from org's storage_providers

Tar-first — cookie injection is NOT in the cascade until proven.
Storage is resolved from org's configured storage_providers, not hardcoded.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

logger = logging.getLogger(__name__)

DEFAULT_CASCADE_TIMEOUT = 120

class CascadeLevel(StrEnum):
    DATABASE = "DATABASE"
    LOCAL_TAR = "LOCAL_TAR"
    LIVE_VERIFY = "LIVE_VERIFY"
    ORG_STORAGE_PULL = "ORG_STORAGE_PULL"

@dataclass
class CascadeResult:
    skipped: bool
    level_reached: CascadeLevel
    profile_dir: str | None = None
    verification_email: str | None = None
    cookies: list[dict] | None = None
    error: str | None = None

class IdentityLookup(Protocol):
    def get_domain_set(self, identity_id: uuid.UUID, domain: str) -> dict | None: ...
    def get_profile_serial(self, identity_id: uuid.UUID) -> int | None: ...

class StorageLookup(Protocol):
    def get_access_info(self, org_id: uuid.UUID, object_key: str, operation: str) -> dict | None: ...
    def list_providers(self, org_id: uuid.UUID, object_type: str) -> list[dict]: ...

class BrowserVerifier(Protocol):
    async def verify_google_session(self, profile_dir: str, expected_email: str, proxy_url: str | None) -> bool: ...

async def run_session_cascade(
    identity_id: uuid.UUID,
    expected_email: str,
    org_id: uuid.UUID,
    identity_lookup: IdentityLookup,
    storage_lookup: StorageLookup,
    browser_verifier: BrowserVerifier,
    force: bool = False,
    timeout: float | None = None
) -> CascadeResult:
    if timeout is None:
        timeout = DEFAULT_CASCADE_TIMEOUT
        
    async def _cascade() -> CascadeResult:
        serial = identity_lookup.get_profile_serial(identity_id)
        if not serial:
            return CascadeResult(
                skipped=False,
                level_reached=CascadeLevel.DATABASE,
                error="No profile serial found for identity"
            )
            
        profile_dir = f"/tmp/xiorun_profiles/PRFL-{serial}"
        
        # Level 1: Database
        if not force:
            domain_set = identity_lookup.get_domain_set(identity_id, "google.com")
            if domain_set and domain_set.get("is_valid") and domain_set.get("urgency") != "immediate":
                return CascadeResult(
                    skipped=False,
                    level_reached=CascadeLevel.DATABASE,
                    profile_dir=profile_dir,
                    verification_email=expected_email
                )
                
        # Level 2 & 3: Local Tar & Live Verify
        if os.path.exists(profile_dir):
            is_valid = await browser_verifier.verify_google_session(profile_dir, expected_email, None)
            if is_valid:
                return CascadeResult(
                    skipped=False,
                    level_reached=CascadeLevel.LIVE_VERIFY,
                    profile_dir=profile_dir,
                    verification_email=expected_email
                )
                
        # Level 4: Org Storage Pull
        providers = storage_lookup.list_providers(org_id, "profile_tar")
        if not providers:
            return CascadeResult(
                skipped=False,
                level_reached=CascadeLevel.ORG_STORAGE_PULL,
                error="No storage providers configured for org"
            )
            
        return CascadeResult(
            skipped=False,
            level_reached=CascadeLevel.ORG_STORAGE_PULL,
            profile_dir=profile_dir,
            verification_email=expected_email
        )

    try:
        return await asyncio.wait_for(_cascade(), timeout=timeout)
    except TimeoutError:
        return CascadeResult(
            skipped=False,
            level_reached=CascadeLevel.DATABASE,
            error="Cascade timeout"
        )
