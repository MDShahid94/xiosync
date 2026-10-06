"""Raw (non-RLS-scoped) database session dependency for FastAPI.

This dependency is used by admin/internal routers (batch, execution, dlq)
that operate outside the per-tenant RLS boundary — typically for Org Zero
admin operations or cross-org management queries.

For tenant-scoped access use ``org_scoped_session`` from
``xiosync.persistence.tenancy`` instead.

Usage::

    from xiosync.api.middleware.db import get_db
    from sqlalchemy.orm import Session

    @router.get("/example")
    def example(db: Session = Depends(get_db)):
        ...
"""

from __future__ import annotations

from collections.abc import Generator

from fastapi import Request
from sqlalchemy.orm import Session


def get_db(request: Request) -> Generator[Session]:
    """Yield a raw SQLAlchemy session from the application engine.

    The session auto-commits on clean exit and rolls back on exception.
    RLS GUC is NOT set — callers are responsible for ensuring they have
    appropriate access (e.g. Org Zero admin context).
    """
    engine = request.app.state.engine
    with Session(engine) as session, session.begin():
        yield session
