import os
import sys
import uuid
import time
import concurrent.futures
from xiosync.persistence.database import create_database_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import select
from xiosync.subsystems.xiogrid.models.exit_node import PPPoEExitNode, PPPoEHost
from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
from xiosync.domain.context import OrgContext, PlatformRole, MembershipRole

url = os.environ.get("XIOSYNC_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/xiosync")
engine = create_database_engine(url)
SessionLocal = sessionmaker(bind=engine)

with SessionLocal() as db:
    host = db.scalars(select(PPPoEHost).where(PPPoEHost.state == "active")).first()
    if not host:
        print("No active host found.")
        sys.exit(1)
    
    host_id = host.id
    print(f"Using host: {host.name} ({host.id})")
    
    nodes = db.scalars(select(PPPoEExitNode).where(PPPoEExitNode.state == "destroyed")).all()
    slots_to_provision = [n.ppp_slot for n in nodes]

null_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
ctx = OrgContext(
    auth_identity_id=null_id, 
    actor_id=null_id, 
    organization_id=null_id, 
    session_id=null_id,
    platform_role=PlatformRole.PLATFORM_ADMIN,
    membership_role=MembershipRole.ORG_OWNER
)

def provision_one(slot):
    with SessionLocal() as db_session:
        svc = PPPoENodeService(db_session)
        try:
            r = svc.provision_slot(ctx, host_id, slot)
            db_session.commit()
            return f"Slot {r.ppp_slot}: {r.public_ip} (State: {r.state})"
        except Exception as e:
            db_session.rollback()
            return f"Slot {slot} failed: {e}"

print(f"Provisioning {len(slots_to_provision)} slots with thread-safe sessions and limited concurrency...")

results = []
batch_size = 5
for i in range(0, len(slots_to_provision), batch_size):
    batch = slots_to_provision[i : i + batch_size]
    print(f"Batch {i//batch_size + 1}: provisioning slots {batch}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as ex:
        futs = [ex.submit(provision_one, s) for s in batch]
        for fut in concurrent.futures.as_completed(futs):
            print(fut.result())
    time.sleep(3.0)

print("Provisioning completed.")
