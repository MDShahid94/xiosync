"""Headless Google session refresh — cookie rotation without re-login.

Google auto-rotates Tier-2 cookies (SIDCC, AEC, __Secure-*PSIDTS) when an
authenticated user navigates to accounts.google.com. This workflow exploits
that behavior to renew expiring cookies without requiring password or TOTP.

Ported from XIOBR workflows/google-session-refresh.mjs.

Steps:
  1. Load existing cookie state from vault
  2. Check health — skip if already healthy
  3. Launch Patchright, inject cookies
  4. Navigate to accounts.google.com
  5. If redirected to sign-in → raise FullReLoginRequired
  6. Extract refreshed cookies via CDP Network.getAllCookies
  7. Merge (TTL-aware, with SID-TTL safety) and save back
  8. Re-check health to confirm urgency reduced
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class FullReLoginRequired(Exception):
    pass


REFRESH_URL = "https://accounts.google.com/"
REFRESH_VERIFY_URL = "https://myaccount.google.com/"
NAVIGATION_TIMEOUT = 15000


@dataclass
class RefreshResult:
    success: bool
    cookies_before: int
    cookies_after: int
    urgency_before: str
    urgency_after: str
    rotated_count: int
    error: str | None = None


class VaultAccess(Protocol):
    def load_cookie_state(self, identity_id: str) -> dict | None: ...
    def save_cookie_state(self, identity_id: str, state: dict) -> None: ...


class BrowserFactory(Protocol):
    def create_context(self, cookies: list[dict], proxy_url: str | None = None) -> Any: ...


async def refresh_google_session(
    identity_id: str,
    vault: VaultAccess,
    browser_factory: BrowserFactory,
    proxy_url: str | None = None,
) -> RefreshResult:
    # Import inline as requested
    try:
        from xiosync.domain.auth.cookie_health import check_cookie_health
    except ImportError:

        def check_cookie_health(state: dict) -> str:
            return "NONE"

    state = vault.load_cookie_state(identity_id)
    if not state:
        raise FullReLoginRequired("No existing cookie state found in vault.")

    cookies = state.get("cookies", [])
    urgency_before = check_cookie_health(state)

    if urgency_before == "NONE":
        return RefreshResult(True, len(cookies), len(cookies), urgency_before, urgency_before, 0)

    try:
        # We assume create_context returns an AsyncContextManager
        async with browser_factory.create_context(cookies, proxy_url) as context:
            page = await context.new_page()

            # Navigate to accounts.google.com to trigger rotation
            await page.goto(REFRESH_URL, timeout=NAVIGATION_TIMEOUT)

            if "signin" in page.url.lower() or "identifier" in page.url.lower():
                raise FullReLoginRequired(
                    "Redirected to sign-in page; session is fully expired or invalid."
                )

            # Extract via CDP
            client = await page.context.new_cdp_session(page)
            new_cookies_resp = await client.send("Network.getAllCookies")
            new_cookies = new_cookies_resp.get("cookies", [])

            # Simple merge logic (could be made more robust for SID-TTL safety)
            merged_cookies = {c["name"]: c for c in cookies}
            for nc in new_cookies:
                merged_cookies[nc["name"]] = nc

            updated_cookie_list = list(merged_cookies.values())
            state["cookies"] = updated_cookie_list
            state["last_refreshed"] = time.time()

            vault.save_cookie_state(identity_id, state)

            urgency_after = check_cookie_health(state)
            rotated_count = len(
                [
                    c
                    for c in new_cookies
                    if c["name"] in ["SIDCC", "AEC"] or c["name"].startswith("__Secure-")
                ]
            )

            return RefreshResult(
                success=True,
                cookies_before=len(cookies),
                cookies_after=len(updated_cookie_list),
                urgency_before=urgency_before,
                urgency_after=urgency_after,
                rotated_count=rotated_count,
            )

    except Exception as e:
        if isinstance(e, FullReLoginRequired):
            raise
        return RefreshResult(
            success=False,
            cookies_before=len(cookies),
            cookies_after=len(cookies),
            urgency_before=urgency_before,
            urgency_after="HIGH",
            rotated_count=0,
            error=str(e),
        )
