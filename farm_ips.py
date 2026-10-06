import os
import sys
import uuid
import time
from xiosync.persistence.database import create_database_engine
from sqlalchemy.orm import Session
from sqlalchemy import select
from xiosync.subsystems.xiogrid.models.exit_node import PPPoEExitNode, PPPoEHost
from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
from xiosync.domain.context import OrgContext, PlatformRole, MembershipRole

url = os.environ.get("XIOSYNC_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/xiosync")
engine = create_database_engine(url)

with Session(engine) as db:
    host = db.scalars(select(PPPoEHost).where(PPPoEHost.state == "active")).first()
    if not host:
        print("No active host found.")
        sys.exit(1)
        
    print(f"Using host: {host.name} ({host.id})")
    
    null_id = uuid.UUID("00000000-0000-0000-0000-000000000001") # must not be all zeros, context validates it
    
    ctx = OrgContext(
        auth_identity_id=null_id, 
        actor_id=null_id, 
        organization_id=null_id, 
        session_id=null_id,
        platform_role=PlatformRole.PLATFORM_ADMIN,
        membership_role=MembershipRole.ORG_OWNER
    )
    
    svc = PPPoENodeService(db)
    
    slots_to_provision = [2, 3, 4, 5, 6]
    print(f"Provisioning slots: {slots_to_provision}")
    
    records = svc.provision_batch(ctx, host.id, slots_to_provision, batch_size=5)
    
    for r in records:
        print(f"Slot {r.ppp_slot}: {r.public_ip} (State: {r.state})")
    
    db.commit()
