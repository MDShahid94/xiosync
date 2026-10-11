import concurrent.futures
import os
import sys
import time
import uuid

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from xiosync.domain.context import MembershipRole, OrgContext, PlatformRole
from xiosync.persistence.database import create_database_engine
from xiosync.platform.ids import new_id
from xiosync.subsystems.xiogrid.domain.pppoe import profile_name_for_slot
from xiosync.subsystems.xiogrid.models.exit_node import (
    FingerprintProfile,
    PPPoEExitNode,
    PPPoEHost,
)
from xiosync.subsystems.xiogrid.seed_pppoe import seed_fingerprint_profiles
from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService

url = os.environ.get(
    "XIOSYNC_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/xiosync"
)
engine = create_database_engine(url)
SessionLocal = sessionmaker(bind=engine)

with SessionLocal() as db:
    host = db.scalars(select(PPPoEHost).where(PPPoEHost.state == "active")).first()
    if not host:
        print("No active host found.")
        sys.exit(1)

    host_id = host.id
    max_slots = host.max_slots if host.max_slots is not None else 981
    print(f"Using host: {host.name} ({host.id}) with max_slots={max_slots}")

    # 1. Query existing slot indices for this host
    existing_nodes = db.scalars(
        select(PPPoEExitNode).where(PPPoEExitNode.host_id == host_id)
    ).all()
    existing_slots = {n.ppp_slot for n in existing_nodes}

    # 2. Compute uninitialized slot indices (0..max_slots - 1)
    missing = sorted(set(range(max_slots)) - existing_slots)

    # 3. For uninitialized indices, create new rows with state 'destroyed'
    if missing:
        print(f"Found {len(missing)} uninitialized slots. Seeding rows as 'destroyed'...")
        # Ensure fingerprint profiles are seeded
        profiles = {p.name: p.id for p in db.scalars(select(FingerprintProfile)).all()}
        if not profiles:
            seed_fingerprint_profiles(db)
            profiles = {p.name: p.id for p in db.scalars(select(FingerprintProfile)).all()}
        default_pid = next(iter(profiles.values())) if profiles else None

        for slot in missing:
            pname = profile_name_for_slot(slot)
            pid = profiles.get(pname, default_pid)
            node = PPPoEExitNode(
                id=new_id(),
                host_id=host_id,
                fingerprint_profile_id=pid,
                ppp_slot=slot,
                state="destroyed",
            )
            db.add(node)
        db.commit()
        print(f"Successfully seeded {len(missing)} uninitialized slots as 'destroyed'.")

    # 4. Proceed with provisioning destroyed slots
    nodes = db.scalars(
        select(PPPoEExitNode).where(
            PPPoEExitNode.host_id == host_id,
            PPPoEExitNode.state == "destroyed",
        )
    ).all()
    slots_to_provision = [n.ppp_slot for n in nodes]

null_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
ctx = OrgContext(
    auth_identity_id=null_id,
    actor_id=null_id,
    organization_id=null_id,
    session_id=null_id,
    platform_role=PlatformRole.PLATFORM_ADMIN,
    membership_role=MembershipRole.ORG_OWNER,
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


print(
    f"Provisioning {len(slots_to_provision)} slots with thread-safe sessions and limited concurrency..."
)

results = []
batch_size = 5
for i in range(0, len(slots_to_provision), batch_size):
    batch = slots_to_provision[i : i + batch_size]
    print(f"Batch {i // batch_size + 1}: provisioning slots {batch}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as ex:
        futs = [ex.submit(provision_one, s) for s in batch]
        for fut in concurrent.futures.as_completed(futs):
            print(fut.result())
    time.sleep(3.0)

print("Provisioning completed.")
