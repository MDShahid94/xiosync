"""Google session verification via myaccount.google.com DOM check.

Ported from XIOBR src/core/session-verifier.mjs.
Navigates to myaccount.google.com and checks if the expected email
is displayed in the page body (SPA rendering with waitForFunction).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

VERIFY_URL = "https://myaccount.google.com/"
VERIFY_TIMEOUT_MS = 15000
SPA_HYDRATION_DELAY = 1.5


class Page(Protocol):
    @property
    def url(self) -> str: ...
    async def goto(self, url: str, *, wait_until: str, timeout: float) -> None: ...
    async def wait_for_function(self, expression: str, arg: Any, *, timeout: float) -> None: ...


def extract_email_prefix(email: str) -> str:
    return email.split("@")[0].lower() if "@" in email else email.lower()


async def verify_google_session(page: Page, expected_email: str) -> bool:
    try:
        await page.goto(VERIFY_URL, wait_until="networkidle", timeout=VERIFY_TIMEOUT_MS)
        await asyncio.sleep(SPA_HYDRATION_DELAY)

        prefix = extract_email_prefix(expected_email)

        expression = """
        (prefix) => {
            return document.body.innerText.toLowerCase().includes(prefix);
        }
        """

        await page.wait_for_function(expression, prefix, timeout=VERIFY_TIMEOUT_MS)
        return True
    except Exception as e:
        logger.warning(f"Google session verification failed: {e}")
        return False
