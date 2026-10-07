"""Universal Google OAuth relay state machine.

Handles the Google OAuth intermediate pages that appear when signing into
third-party services (v0, Tailscale, etc.) via 'Sign in with Google'.

Not hardcoded to specific services — works for any service that uses
Google OAuth and lands on accounts.google.com/* intermediary pages.

After successful OAuth, resulting cookies are auto-detected and registered
in profile_domain_sets with auth_method='google_oauth'.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class OAuthPageType(StrEnum):
    ACCOUNT_CHOOSER = "ACCOUNT_CHOOSER"
    PASSWORD_ENTRY = "PASSWORD_ENTRY"
    TOTP_CHALLENGE = "TOTP_CHALLENGE"
    DEVICE_PUSH = "DEVICE_PUSH"
    CONSENT_SCREEN = "CONSENT_SCREEN"
    REDIRECT_BACK = "REDIRECT_BACK"
    ERROR_PAGE = "ERROR_PAGE"
    UNKNOWN = "UNKNOWN"


@dataclass
class OAuthLoopOptions:
    expected_email: str
    totp_secret: str | None = None
    max_iterations: int = 20
    iteration_delay: float = 2.0
    timeout: float = 120.0


@dataclass
class OAuthLoopResult:
    success: bool
    page_type_sequence: list[str] = field(default_factory=list)
    cookies_captured: int = 0
    target_url: str | None = None
    error: str | None = None


class OAuthPage(Protocol):
    async def goto(self, url: str, **kwargs) -> Any: ...
    @property
    def url(self) -> str: ...
    async def query_selector(self, selector: str) -> Any: ...
    async def click(self, selector: str, **kwargs) -> None: ...
    async def fill(self, selector: str, value: str, **kwargs) -> None: ...
    async def wait_for_url(self, pattern: str, **kwargs) -> None: ...
    async def wait_for_selector(self, selector: str, **kwargs) -> Any: ...
    async def evaluate(self, expression: str) -> Any: ...


def detect_page_type(url: str, page_content: str | None = None) -> OAuthPageType:
    url_lower = url.lower()

    if "google.com" not in url_lower:
        return OAuthPageType.REDIRECT_BACK

    if "accountchooser" in url_lower or "selectaccount" in url_lower:
        return OAuthPageType.ACCOUNT_CHOOSER

    if "signin/v2" in url_lower or "servicelogin" in url_lower:
        if page_content and "password" in page_content.lower():
            return OAuthPageType.PASSWORD_ENTRY
        return (
            OAuthPageType.ACCOUNT_CHOOSER
        )  # Fallback for entry screen without password field detected yet

    if "/challenge/totp" in url_lower or "/challenge/ipp" in url_lower:
        return OAuthPageType.TOTP_CHALLENGE

    if "/challenge/dp" in url_lower or "/challenge/sk" in url_lower:
        return OAuthPageType.DEVICE_PUSH

    if "consent" in url_lower or "approval" in url_lower or "oauthchooseaccount" in url_lower:
        return OAuthPageType.CONSENT_SCREEN

    if "error" in url_lower or "rejected" in url_lower:
        return OAuthPageType.ERROR_PAGE

    return OAuthPageType.UNKNOWN


def generate_safe_totp(totp_secret: str) -> str:
    """Window-aware TOTP generation. Checks remaining seconds in window."""
    import pyotp

    totp = pyotp.TOTP(totp_secret)
    current_time = int(time.time())
    time_remaining = 30 - (current_time % 30)

    if time_remaining < 5:
        logger.debug(f"TOTP window closing soon ({time_remaining}s). Waiting...")
        time.sleep(time_remaining)

    return totp.now()


async def run_google_oauth_loop(page: OAuthPage, opts: OAuthLoopOptions) -> OAuthLoopResult:
    sequence = []

    for iteration in range(opts.max_iterations):
        current_url = page.url
        try:
            content = await page.evaluate("document.body.innerText")
        except Exception:
            content = ""

        ptype = detect_page_type(current_url, content)
        sequence.append(str(ptype))

        logger.debug(f"OAuth relay iteration {iteration}: Detected {ptype}")

        if ptype == OAuthPageType.REDIRECT_BACK:
            return OAuthLoopResult(
                success=True, page_type_sequence=sequence, target_url=current_url
            )

        if ptype == OAuthPageType.ACCOUNT_CHOOSER:
            try:
                # Basic attempt to click the account that matches expected email
                await page.evaluate(f"""
                    Array.from(document.querySelectorAll('div, span, li')).find(el => el.innerText.includes('{opts.expected_email}')).click();
                """)
            except Exception as e:
                logger.warning(f"Failed to click account chooser: {e}")

        elif ptype == OAuthPageType.PASSWORD_ENTRY:
            logger.warning("Password entry requested, which shouldn't happen for active sessions.")
            return OAuthLoopResult(False, sequence, 0, current_url, "Password requested")

        elif ptype == OAuthPageType.TOTP_CHALLENGE:
            if not opts.totp_secret:
                return OAuthLoopResult(
                    False, sequence, 0, current_url, "TOTP challenged but no secret provided"
                )
            try:
                totp = generate_safe_totp(opts.totp_secret)
                await page.fill('input[type="tel"]', totp)
                await page.click('button:has-text("Next")')
            except Exception as e:
                logger.warning(f"Failed to submit TOTP: {e}")

        elif ptype == OAuthPageType.CONSENT_SCREEN:
            try:
                await page.evaluate("""
                    let btn = Array.from(document.querySelectorAll('button')).find(el => el.innerText.match(/(Allow|Continue|Accept)/i));
                    if (btn) btn.click();
                """)
            except Exception as e:
                logger.warning(f"Failed to approve consent: {e}")

        elif ptype == OAuthPageType.ERROR_PAGE:
            return OAuthLoopResult(False, sequence, 0, current_url, "OAuth error page encountered")

        await asyncio.sleep(opts.iteration_delay)

    return OAuthLoopResult(
        False, sequence, 0, current_url, "Max iterations reached without redirect"
    )
