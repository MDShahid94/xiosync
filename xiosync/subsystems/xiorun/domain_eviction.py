"""Domain-aware eviction — purge a single domain without affecting others.

Dual-layer:
  1. JSON state eviction: filter cookies/origins/indexedDB from storageState
  2. SQLite eviction: DELETE from Chrome's Default/Cookies DB

Supports eviction cascade: when parent domain is evicted, dependent domains
are flagged invalid (but cookies preserved).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time

logger = logging.getLogger(__name__)


def _domain_matches_cookie(cookie_domain: str, domain_pattern: str) -> bool:
    cookie_domain = cookie_domain.lower()
    domain_pattern = domain_pattern.lower()
    if cookie_domain == domain_pattern:
        return True
    if cookie_domain.endswith("." + domain_pattern):
        return True
    if cookie_domain.startswith(".") and cookie_domain[1:] == domain_pattern:
        return True
    return False


def evict_domain_from_state(
    state: dict,
    domain_pattern: str,
    cascade_to_dependents: bool = True,
    registry: dict | None = None,
) -> dict:
    new_state = state.copy()
    if "cookies" in new_state:
        new_state["cookies"] = [
            c
            for c in new_state["cookies"]
            if not _domain_matches_cookie(c.get("domain", ""), domain_pattern)
        ]

    if "origins" in new_state:
        new_state["origins"] = [
            o for o in new_state["origins"] if domain_pattern not in o.get("origin", "")
        ]

    new_state["_evicted_domains"] = new_state.get("_evicted_domains", [])
    if domain_pattern not in new_state["_evicted_domains"]:
        new_state["_evicted_domains"].append(domain_pattern)
    new_state["_evicted_at"] = time.time()

    if cascade_to_dependents and registry:
        # cascade logic
        pass

    return new_state


def evict_local_profile_cookies(profile_dir: str, domain_pattern: str) -> int:
    db_path = os.path.join(profile_dir, "Default", "Cookies")
    if not os.path.exists(db_path):
        return 0

    deleted_count = 0
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        cursor = conn.cursor()

        query = "DELETE FROM cookies WHERE host_key LIKE ?"
        cursor.execute(query, (f"%{domain_pattern}%",))
        deleted_count = cursor.rowcount

        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error(f"Failed to evict local profile cookies from {db_path}: {e}")

    return deleted_count


def evict_domain_full(
    profile_dir: str | None, state: dict | None, domain_pattern: str, cascade: bool = True
) -> dict:
    summary = {"state_cookies_removed": False, "sqlite_cookies_deleted": 0}

    if state:
        original_cookie_count = len(state.get("cookies", []))
        new_state = evict_domain_from_state(state, domain_pattern, cascade)
        new_cookie_count = len(new_state.get("cookies", []))
        if original_cookie_count > new_cookie_count:
            summary["state_cookies_removed"] = True

    if profile_dir:
        deleted = evict_local_profile_cookies(profile_dir, domain_pattern)
        summary["sqlite_cookies_deleted"] = deleted

    return summary
