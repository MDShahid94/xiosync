"""Cursor-based pagination utilities (Gap P-1).

Provides a universal pagination pattern for all collection endpoints:
- Cursor is an opaque base64-encoded ``(created_at_iso, id_hex)`` tuple
- Consistent response envelope via ``PaginatedResponse``
- ``PaginationParams`` FastAPI dependency for query param extraction
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

__all__ = [
    "PaginatedResponse",
    "PaginationParams",
    "decode_cursor",
    "encode_cursor",
]

T = TypeVar("T")

#: Maximum items per page.
MAX_PAGE_SIZE = 200
#: Default items per page.
DEFAULT_PAGE_SIZE = 50


class PaginatedResponse(BaseModel, Generic[T]):
    """Standard paginated response envelope."""

    items: list[T]
    cursor: str | None = None
    total_estimate: int | None = None


@dataclass(frozen=True, slots=True)
class PaginationParams:
    """Decoded pagination query parameters."""

    limit: int
    cursor_created_at: datetime | None
    cursor_id: uuid.UUID | None

    @classmethod
    def from_query(
        cls,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> PaginationParams:
        """Parse query parameters into pagination params."""
        effective_limit = min(max(1, limit), MAX_PAGE_SIZE)
        if cursor is None:
            return cls(limit=effective_limit, cursor_created_at=None, cursor_id=None)
        created_at, cursor_uuid = decode_cursor(cursor)
        return cls(
            limit=effective_limit,
            cursor_created_at=created_at,
            cursor_id=cursor_uuid,
        )


def encode_cursor(created_at: datetime, record_id: uuid.UUID) -> str:
    """Encode a ``(created_at, id)`` pair into an opaque cursor string."""
    raw = f"{created_at.isoformat()}|{record_id.hex}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Decode an opaque cursor string into ``(created_at, id)``."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        iso_str, id_hex = raw.split("|", 1)
        return datetime.fromisoformat(iso_str), uuid.UUID(id_hex)
    except Exception as exc:
        raise ValueError(f"invalid cursor: {cursor!r}") from exc
