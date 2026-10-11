"""Background reaper for expired grants with cascaded revocation."""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def tick_grant_reaper(session: Session) -> int:
    """Transition expired grants (and their delegation descendants) to 'revoked'.

    Uses a recursive CTE to find all active grants whose expires_at has passed,
    then cascades revocation to any child grants delegated from them.
    """
    result = session.execute(
        text("""
        WITH RECURSIVE expired_tree AS (
            SELECT id, organization_id
            FROM grants
            WHERE state = 'active'
              AND expires_at IS NOT NULL
              AND expires_at <= now()
            UNION ALL
            SELECT g.id, g.organization_id
            FROM grants g
            JOIN expired_tree et ON g.parent_grant_id = et.id
                                 AND g.organization_id = et.organization_id
            WHERE g.state = 'active'
        )
        UPDATE grants SET state = 'revoked', revoked_at = now()
        WHERE id IN (SELECT id FROM expired_tree)
          AND state = 'active'
        """)
    )
    count = result.rowcount
    if count:
        logger.info("grant_reaper: revoked %d expired grants (with cascades)", count)
    session.commit()
    return count
