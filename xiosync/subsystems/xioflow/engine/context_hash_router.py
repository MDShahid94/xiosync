from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class ContextHashRouter:
    """The 5-Tier Viewport & Device Fallback Matrix."""

    def __init__(self, session: AsyncSession | Any):
        self.session = session

    @staticmethod
    def generate_context_hash(
        device_type: str, os_name: str, browser: str, viewport_w: int, viewport_h: int
    ) -> str:
        """Generate SHA256 hash for exact context."""
        s = f"{device_type}|{os_name}|{browser}|{viewport_w}x{viewport_h}"
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    def generate_hash(self, context: dict) -> str:
        """Adapter: extract context fields and delegate to generate_context_hash."""
        return self.generate_context_hash(
            device_type=context.get("device_type", "desktop"),
            os_name=context.get("os_name", "linux"),
            browser=context.get("browser", "chrome"),
            viewport_w=context.get("viewport_w", context.get("viewport_width", 1920)),
            viewport_h=context.get("viewport_h", context.get("viewport_height", 1080)),
        )

    @staticmethod
    def get_breakpoint_range(viewport_width: int) -> tuple[int, int]:
        """Return (bp_min, bp_max) based on CSS breakpoints."""
        if viewport_width < 768:
            return (0, 767)
        elif 768 <= viewport_width <= 1024:
            return (768, 1024)
        else:
            return (1025, 9999)

    async def query_by_viewport_tier(
        self, domain: str, intent: str, context: dict, tier: int, org_id: str, max_results: int = 20
    ) -> list[dict]:
        """Query memory nodes with fallbacks based on tier."""
        dev = context.get("device_type")
        os_n = context.get("os_name")
        br = context.get("browser")
        vw = context.get("viewport_w", 0)

        where_clauses = [
            "domain = :domain",
            "intent = :intent",
            "organization_id = :org_id",
            "status = 'ACTIVE'",
        ]
        params = {"domain": domain, "intent": intent, "org_id": org_id}

        if tier == 1:
            where_clauses.extend(
                ["device_type = :dev", "os_name = :os", "browser = :br", "viewport_width = :vw"]
            )
            params.update({"dev": dev, "os": os_n, "br": br, "vw": vw})
        elif tier == 2:
            where_clauses.extend(["device_type = :dev", "browser = :br", "viewport_width = :vw"])
            params.update({"dev": dev, "br": br, "vw": vw})
        elif tier == 3:
            where_clauses.extend(["device_type = :dev", "viewport_width = :vw"])
            params.update({"dev": dev, "vw": vw})
        elif tier == 4:
            bmin, bmax = self.get_breakpoint_range(vw)
            where_clauses.extend(
                ["device_type = :dev", "viewport_width >= :bmin", "viewport_width <= :bmax"]
            )
            params.update({"dev": dev, "bmin": bmin, "bmax": bmax})
        elif tier == 5:
            bmin, bmax = self.get_breakpoint_range(vw)
            where_clauses.extend(["viewport_width >= :bmin", "viewport_width <= :bmax"])
            params.update({"bmin": bmin, "bmax": bmax})

        q = (
            "SELECT * FROM xioflow_memory_nodes WHERE "
            + " AND ".join(where_clauses)
            + f" LIMIT {max_results}"
        )

        try:
            if hasattr(self.session, "execute"):
                result = await self.session.execute(text(q), params)
                return [dict(row._mapping) for row in result]
        except Exception as e:
            logger.error("query_tier_failed", error=str(e), tier=tier)

        return []
