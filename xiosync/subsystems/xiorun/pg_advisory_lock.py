"""pg_advisory_lock.py — PostgreSQL advisory lock module for XIOSYNC workers.

Ported-from origin:
    Ported from XIODriveFS (colab/xio_drive_fs.py) distributed locking patterns
    and XIOBR lock coordination mechanisms. Provides a robust PostgreSQL-native
    alternative to Redis SETNX locks using session-level advisory locks.

PostgreSQL Advisory Lock Guarantees:
    - Session-level locks (pg_try_advisory_lock / pg_advisory_unlock) take a
      signed 64-bit bigint key.
    - Automatic Dead Worker Protection: If a worker node crashes, experiences network
      partition, or drops its DB connection, PostgreSQL automatically releases all
      session-level advisory locks held by that backend connection.
    - Non-blocking acquisition via pg_try_advisory_lock avoids connection pool starvation.
    - Visibility across all backends via the pg_locks system view.
"""
from __future__ import annotations

import hashlib
import logging
import struct
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

logger = logging.getLogger(__name__)

# Mutex to protect internal in-memory lock tracking tables
_LOCKS_MUTEX = threading.Lock()

# Map of bigint key -> OrmSession holding the active session-level advisory lock
_ACTIVE_LOCK_SESSIONS: dict[int, OrmSession] = {}

# Map of bigint key -> metadata dict (node_name, resource_key, acquired_at)
_ACTIVE_LOCK_METADATA: dict[int, dict[str, Any]] = {}


def _resource_key_to_bigint(resource_key: str) -> int:
    """Hash a string resource key to a signed 64-bit bigint for pg_advisory_lock.

    Uses SHA-256 and converts the first 8 bytes into a big-endian signed 64-bit
    integer via struct.unpack('>q', ...), matching PostgreSQL's bigint type.
    """
    digest = hashlib.sha256(resource_key.encode("utf-8")).digest()
    (bigint_val,) = struct.unpack(">q", digest[:8])
    return bigint_val


def try_acquire_lock(engine: Any, resource_key: str, node_name: str) -> bool:
    """Attempt non-blocking lock via pg_try_advisory_lock.

    Args:
        engine: SQLAlchemy Engine instance.
        resource_key: Logical identifier of the resource to lock.
        node_name: Name of the worker node requesting the lock.

    Returns:
        True if the lock was successfully acquired, False otherwise.
    """
    key = _resource_key_to_bigint(resource_key)

    with _LOCKS_MUTEX:
        if key in _ACTIVE_LOCK_SESSIONS:
            logger.debug(
                "pg_advisory_lock.already_held_locally key=%d resource=%r by=%r",
                key, resource_key, _ACTIVE_LOCK_METADATA.get(key, {}).get("node_name"),
            )
            return False

    session = OrmSession(engine)
    try:
        row = session.execute(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": key},
        ).scalar()
        acquired = bool(row)

        if acquired:
            with _LOCKS_MUTEX:
                _ACTIVE_LOCK_SESSIONS[key] = session
                _ACTIVE_LOCK_METADATA[key] = {
                    "node_name": node_name,
                    "resource_key": resource_key,
                    "acquired_at": time.time(),
                }
            logger.info(
                "pg_advisory_lock.acquired resource=%r node=%r key=%d",
                resource_key, node_name, key,
            )
            return True
        else:
            session.close()
            logger.debug(
                "pg_advisory_lock.denied resource=%r node=%r key=%d",
                resource_key, node_name, key,
            )
            return False
    except Exception:
        session.close()
        raise


def release_lock(engine: Any, resource_key: str) -> bool:
    """Release a PostgreSQL advisory lock via pg_advisory_unlock.

    Args:
        engine: SQLAlchemy Engine instance.
        resource_key: Logical identifier of the resource to unlock.

    Returns:
        True if the lock was successfully released, False otherwise.
    """
    key = _resource_key_to_bigint(resource_key)

    with _LOCKS_MUTEX:
        session = _ACTIVE_LOCK_SESSIONS.pop(key, None)
        _ACTIVE_LOCK_METADATA.pop(key, None)

    if session is not None:
        try:
            row = session.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": key},
            ).scalar()
            logger.info("pg_advisory_lock.released resource=%r key=%d", resource_key, key)
            return bool(row)
        finally:
            session.close()

    # If the session was not tracked locally, attempt unlock via a scoped session
    with OrmSession(engine) as scoped_session:
        row = scoped_session.execute(
            text("SELECT pg_advisory_unlock(:key)"),
            {"key": key},
        ).scalar()
        released = bool(row)
        if released:
            logger.info("pg_advisory_lock.released_external resource=%r key=%d", resource_key, key)
        return released


def is_locked(engine: Any, resource_key: str) -> bool:
    """Check via pg_locks system view whether the resource key is currently locked.

    Args:
        engine: SQLAlchemy Engine instance.
        resource_key: Logical identifier of the resource.

    Returns:
        True if an exclusive advisory lock exists for this key, False otherwise.
    """
    key = _resource_key_to_bigint(resource_key)
    with OrmSession(engine) as session:
        row = session.execute(
            text("""
                SELECT 1
                FROM pg_locks
                WHERE locktype = 'advisory'
                  AND objsubid = 1
                  AND ((classid::bigint << 32) | objid::bigint) = :key
                LIMIT 1
            """),
            {"key": key},
        ).scalar()
        return bool(row)


def get_lock_holder(engine: Any, resource_key: str) -> str | None:
    """Return the node name or backend identifier holding the lock, if any."""
    key = _resource_key_to_bigint(resource_key)

    with _LOCKS_MUTEX:
        meta = _ACTIVE_LOCK_METADATA.get(key)
        if meta and meta.get("node_name"):
            return str(meta["node_name"])

    with OrmSession(engine) as session:
        row = session.execute(
            text("""
                SELECT
                    l.pid,
                    a.application_name
                FROM pg_locks l
                LEFT JOIN pg_stat_activity a ON l.pid = a.pid
                WHERE l.locktype = 'advisory'
                  AND l.objsubid = 1
                  AND ((l.classid::bigint << 32) | l.objid::bigint) = :key
                LIMIT 1
            """),
            {"key": key},
        ).mappings().first()

        if row:
            app_name = row.get("application_name")
            pid = row.get("pid")
            return app_name if app_name else f"pid-{pid}"
        return None


def get_lock_info(engine: Any, resource_key: str) -> dict[str, Any] | None:
    """Return detailed metadata about a lock for status inspection."""
    key = _resource_key_to_bigint(resource_key)

    with _LOCKS_MUTEX:
        meta = _ACTIVE_LOCK_METADATA.get(key)
        if meta:
            return {
                "locked": True,
                "holder": meta.get("node_name"),
                "acquired_at": meta.get("acquired_at"),
            }

    with OrmSession(engine) as session:
        row = session.execute(
            text("""
                SELECT
                    l.pid,
                    a.application_name,
                    EXTRACT(EPOCH FROM a.backend_start) AS backend_start_epoch
                FROM pg_locks l
                LEFT JOIN pg_stat_activity a ON l.pid = a.pid
                WHERE l.locktype = 'advisory'
                  AND l.objsubid = 1
                  AND ((l.classid::bigint << 32) | l.objid::bigint) = :key
                LIMIT 1
            """),
            {"key": key},
        ).mappings().first()

        if row:
            app_name = row.get("application_name")
            pid = row.get("pid")
            holder = app_name if app_name else f"pid-{pid}"
            return {
                "locked": True,
                "holder": holder,
                "acquired_at": row.get("backend_start_epoch"),
            }
        return None


@contextmanager
def advisory_lock_scope(
    engine: Any,
    resource_key: str,
    node_name: str = "",
) -> Iterator[OrmSession]:
    """Context manager for acquiring and releasing a PostgreSQL advisory lock.

    Usage::

        with advisory_lock_scope(engine, "critical-section-key", "worker-1"):
            # perform protected work
            ...
    """
    key = _resource_key_to_bigint(resource_key)
    with OrmSession(engine) as session:
        acquired = session.execute(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": key},
        ).scalar()
        if not acquired:
            raise RuntimeError(f"Could not acquire PostgreSQL advisory lock for {resource_key!r}")
        try:
            yield session
        finally:
            session.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": key},
            )
