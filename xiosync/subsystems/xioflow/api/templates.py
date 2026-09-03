"""XIOFLOW template API router scaffold.

This is a scaffold. Full implementation in Phase 4.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter(tags=["xioflow-templates"])


@router.get(
    "/xioflow/templates",
    summary="List XIOFLOW workflow templates (scaffold)",
    response_model=None,
)
def list_templates() -> dict[str, Any]:
    """Scaffold endpoint — returns empty list until Phase 4 implementation."""
    return {"templates": [], "note": "XIOFLOW Phase 4 — not yet implemented"}
