from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

class SessionLockConflict(Exception):
    """Raised when a session lock cannot be acquired due to conflict."""
    pass

class SlotManager:
    """Hardware concurrency and distributed session locking."""

    CONCURRENCY_LIMITS = {
        'cpu': {'headed': 3, 'headless': 6, 'appetize': 10},
        'gpu': {'headed': 6, 'headless': 10, 'appetize': 15}
    }

    def __init__(self, runtime_type: str = 'cpu'):
        self.runtime_type = runtime_type
        self._slot_counts = {'headed': 0, 'headless': 0, 'appetize': 0}
        self._session_locks: dict[str, dict] = {}

    def acquire_slot(self, slot_type: str) -> bool:
        """Acquire a slot of the given type."""
        limit = self.CONCURRENCY_LIMITS.get(self.runtime_type, {}).get(slot_type, 0)
        if self._slot_counts.get(slot_type, 0) < limit:
            self._slot_counts[slot_type] = self._slot_counts.get(slot_type, 0) + 1
            return True
        raise RuntimeError(f"Concurrency limit reached for {self.runtime_type}/{slot_type}")

    def release_slot(self, slot_type: str) -> None:
        """Release a slot of the given type."""
        if self._slot_counts.get(slot_type, 0) > 0:
            self._slot_counts[slot_type] -= 1

    def get_available_slots(self) -> dict[str, int]:
        """Return available slots for each type."""
        limits = self.CONCURRENCY_LIMITS.get(self.runtime_type, {})
        return {
            stype: limit - self._slot_counts.get(stype, 0)
            for stype, limit in limits.items()
        }

    async def acquire_session_lock(self, session_id: str, worker_id: str, ttl: int = 300) -> bool:
        """Acquire a session lock for a worker."""
        now = time.time()
        lock = self._session_locks.get(session_id)

        if lock and lock['expires_at'] > now and lock['worker_id'] != worker_id:
            raise SessionLockConflict(f"Session {session_id} is locked by {lock['worker_id']}")

        self._session_locks[session_id] = {
            'worker_id': worker_id,
            'expires_at': now + ttl
        }
        return True

    async def release_session_lock(self, session_id: str, worker_id: str) -> None:
        """Release a session lock."""
        lock = self._session_locks.get(session_id)
        if lock and lock['worker_id'] == worker_id:
            del self._session_locks[session_id]
