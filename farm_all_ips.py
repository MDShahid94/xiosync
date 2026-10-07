import os
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.persistence.database import create_database_engine
from xiosync.subsystems.xiogrid.models.exit_node import PPPoEExitNode, PPPoEHost
from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService

url = os.environ.get(
    "XIOSYNC_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/xiosync"
)
engine = create_database_engine(url)

with Session(engine) as db:
    host = db.scalars(select(PPPoEHost).where(PPPoEHost.state == "active")).first()
    if not host:
        print("No active host found.")
        sys.exit(1)

    print(f"Using host: {host.name} ({host.id})")

    null_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    ctx = OrgContext(
        auth_identity_id=null_id,
        actor_id=null_id,
        organization_id=null_id,
        session_id=null_id,
        platform_role=PlatformRole.PLATFORM_ADMIN,
        membership_role=MembershipRole.ORG_OWNER,
    )

    svc = PPPoENodeService(db)

    # Get all destroyed nodes
    nodes = db.scalars(select(PPPoEExitNode).where(PPPoEExitNode.state == "destroyed")).all()
    slots_to_provision = [n.ppp_slot for n in nodes]

    print(
        f"Provisioning {len(slots_to_provision)} slots with smaller batch size to avoid SSH limits..."
    )

    # batch_size=5 to avoid SSH max startups. batch_pause_s=3.0 to give it time
    records = svc.provision_batch(ctx, host.id, slots_to_provision, batch_size=5, batch_pause_s=3.0)

    db.commit()

print("Provisioning completed.")
