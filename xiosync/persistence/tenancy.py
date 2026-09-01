"""The org-scoped persistence boundary (docs 05 §3.2, 06; C1/C2).

Every tenant-touching database access goes through ``org_scoped_session``:
it opens one transaction and binds ``app.current_org`` — the GUC that the
revision-0002 Row-Level Security policies key on — to the ``OrgContext``'s
organization for exactly that transaction (``SET LOCAL``).

Fail-closed properties:

- The GUC is bound with ``set_config($1, $2, true)`` (transaction-local,
  parameterized — no SQL string interpolation of the org id).
- ``SET LOCAL`` scope means the setting evaporates at COMMIT/ROLLBACK; a
  connection returned to the pool carries no residual org.
- Without this boundary the GUC is unset and the rev-0002 policies return
  zero rows / reject writes — RLS itself is the backstop (doc 05 §3.2
  layer 2), this module is the only sanctioned way to open the gate.

INV-TENANT-3: repositories take ``OrgContext`` as their first parameter and
obtain their SQLAlchemy session *only* from this context manager.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as OrmSession

from xiosync.domain.context import OrgContext

#: The PostgreSQL setting the rev-0002 RLS policies key on. One name,
#: defined once — policies, tests, and this boundary all agree.
RLS_ORG_SETTING = "app.current_org"

_SET_ORG_LOCAL = text("SELECT set_config(:setting, :org_id, true)")


@contextmanager
def org_scoped_session(engine: Engine, context: OrgContext) -> Iterator[OrmSession]:
    """One transaction, RLS-scoped to ``context.organization_id``.

    Commits on clean exit, rolls back on exception; either way the
    transaction-local GUC dies with the transaction. Holding a valid
    ``OrgContext`` is a construction-time guarantee (C1), so there is no
    placeholder-org path into this function.
    """
    with OrmSession(engine) as session, session.begin():
        session.execute(
            _SET_ORG_LOCAL,
            {"setting": RLS_ORG_SETTING, "org_id": str(context.organization_id)},
        )
        yield session
