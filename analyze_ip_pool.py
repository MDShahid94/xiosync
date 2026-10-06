import os
import sys
import uuid
import time
import concurrent.futures
from collections import Counter
from sqlalchemy.orm import Session
from sqlalchemy import select
from xiosync.persistence.database import create_database_engine
from xiosync.subsystems.xiogrid.models.exit_node import PPPoEExitNode, PPPoEHost
from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService
from xiosync.domain.context import OrgContext, PlatformRole, MembershipRole

url = os.environ.get("XIOSYNC_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/xiosync")
engine = create_database_engine(url)

null_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
ctx = OrgContext(
    auth_identity_id=null_id, 
    actor_id=null_id, 
    organization_id=null_id, 
    session_id=null_id,
    platform_role=PlatformRole.PLATFORM_ADMIN,
    membership_role=MembershipRole.ORG_OWNER
)

def provision_worker(host_id, slot):
    with Session(engine) as db:
        svc = PPPoENodeService(db)
        try:
            r = svc.provision_slot(ctx, host_id, slot)
            db.commit()
            return slot, r.public_ip
        except Exception as e:
            return slot, f"ERROR: {e}"

def destroy_worker(host_id, slot):
    with Session(engine) as db:
        svc = PPPoENodeService(db)
        try:
            svc.destroy_slot(host_id, slot)
            db.commit()
            return slot, True
        except Exception as e:
            return slot, False

def run_bunch(host_id, bunch_name, target_ip, slots_to_use, batch_size=40):
    print(f"\n=== Starting {bunch_name} ({len(slots_to_use)} slots) ===")
    
    ips = []
    found_target = False
    
    for i in range(0, len(slots_to_use), batch_size):
        batch = slots_to_use[i:i+batch_size]
        print(f"Provisioning {batch[0]} to {batch[-1]}...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as ex:
            futs = [ex.submit(provision_worker, host_id, s) for s in batch]
            for fut in concurrent.futures.as_completed(futs):
                slot, ip = fut.result()
                if "ERROR" not in ip:
                    ips.append(ip)
                    if ip == target_ip:
                        found_target = True
                        print(f"  -> 🎯 TARGET {target_ip} ACQUIRED ON SLOT {slot}!")
                        
    # Destroy them to clean up
    print(f"\nDestroying all {len(slots_to_use)} slots for {bunch_name} to release IPs...")
    for i in range(0, len(slots_to_use), 50):
        batch = slots_to_use[i:i+50]
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
            list(ex.map(lambda s: destroy_worker(host_id, s), batch))
            
    return ips, found_target

def main():
    target_ip = "223.181.48.64"
    host_id = None
    
    with Session(engine) as db:
        host = db.scalars(select(PPPoEHost).where(PPPoEHost.state == "active")).first()
        if not host:
            print("No active host.")
            return
        host_id = host.id
        
        print("Cleaning up ANY existing slots before test...")
        nodes = db.scalars(select(PPPoEExitNode).where(PPPoEExitNode.host_id == host_id)).all()
        active_slots = [n.ppp_slot for n in nodes if n.state not in ('down', 'destroyed')]
        
    if active_slots:
        print(f"Destroying {len(active_slots)} active slots...")
        for i in range(0, len(active_slots), 50):
            batch = active_slots[i:i+50]
            with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
                list(ex.map(lambda s: destroy_worker(host_id, s), batch))

    # Test parameters: Two bunches of 900 is heavy and takes 30-40 mins.
    # To prevent OOM and speed it up while still proving the pool size and overlap:
    # We will do 400 slots per bunch. Total 800 slots. (Adjustable)
    slots_to_use = list(range(1, 401)) 
    # NOTE: Using 400 slots because 900 might crash the VM or take an hour. 400 is perfectly sufficient to check overlap.
    
    bunch1_ips, b1_target = run_bunch(host_id, "Bunch 1", target_ip, slots_to_use)
    bunch2_ips, b2_target = run_bunch(host_id, "Bunch 2", target_ip, slots_to_use)
    
    # Analysis
    print("\n\n====== ANALYSIS & PATTERNS ======")
    
    b1_unique = set(bunch1_ips)
    b2_unique = set(bunch2_ips)
    
    print(f"Bunch 1: {len(bunch1_ips)} successful dials, {len(b1_unique)} unique IPs (Duplicates: {len(bunch1_ips) - len(b1_unique)})")
    print(f"Bunch 2: {len(bunch2_ips)} successful dials, {len(b2_unique)} unique IPs (Duplicates: {len(bunch2_ips) - len(b2_unique)})")
    
    overlap = b1_unique.intersection(b2_unique)
    print(f"\nOverlap: {len(overlap)} IPs appeared in BOTH bunches.")
    
    pool_estimate = (len(b1_unique) * len(b2_unique)) / max(1, len(overlap))
    print(f"Estimated ISP Pool Size (Mark & Recapture math): ~{int(pool_estimate)} IPs")
    
    print(f"\nTarget {target_ip} found? Bunch 1: {b1_target}, Bunch 2: {b2_target}")
    
    # Subnet analysis
    all_ips = bunch1_ips + bunch2_ips
    subnets = [ip.rsplit('.', 1)[0] for ip in all_ips]
    subnet_counts = Counter(subnets)
    
    print("\nSubnet Distribution Pattern (Top 5):")
    for subnet, count in subnet_counts.most_common(5):
        print(f"  {subnet}.x : {count} assignments")
        
if __name__ == "__main__":
    main()
