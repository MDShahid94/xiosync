"""UUIDv7 identifier generation (M6, D-013).

One vetted library (``uuid-utils``), no hand-rolled implementation, and no
silent fallback to uuid4: if the library is unavailable this module fails at
import time, which fails startup (INV-STARTUP-1).

Identifiers are returned as ``uuid.UUID`` stdlib values so that no other
module needs to know which library produced them.
"""

from __future__ import annotations

import uuid

import uuid_utils

UUID_VERSION = 7


def new_id() -> uuid.UUID:
    """Return a new time-ordered UUIDv7 as a stdlib ``uuid.UUID``."""
    return uuid.UUID(bytes=uuid_utils.uuid7().bytes)
