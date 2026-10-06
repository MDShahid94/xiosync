import os
import sys
import uuid
import time
import concurrent.futures
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

def main():
    target_ip = "223.181.48.64"
    
    with Session(engine) as db:
        host = db.scalars(select(PPPoEHost).where(PPPoEHost.state == "active")).first()
        if not host:
            print("No active host.")
            return
            
        host_id = host.id
        svc = PPPoENodeService(db)
        print("1. Destroying slot 5 to release the target IP back to ISP pool...")
        try:
            svc.destroy_slot(host_id, 5)
            db.commit()
            print("Slot 5 destroyed.")
        except Exception as e:
            print(f"Slot 5 already destroyed or error: {e}")
            
    print(f"\n2. Mass provisioning slots to hunt for {target_ip} and check for repetitions...")
    
    seen_ips = set()
    duplicates = 0
    target_found = False
    
    slots_to_provision = list(range(10, 60)) # 50 slots should take ~1-2 mins to run
    batch_size = 10
    
    for i in range(0, len(slots_to_provision), batch_size):
        batch = slots_to_provision[i:i+batch_size]
        print(f"\nProvisioning batch: {batch}")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as ex:
            futs = [ex.submit(provision_worker, host_id, s) for s in batch]
            for fut in concurrent.futures.as_completed(futs):
                slot, ip = fut.result()
                if "ERROR" in ip:
                    print(f"Slot {slot} failed: {ip}")
                    continue
                    
                print(f"Slot {slot} acquired IP: {ip}")
                
                if ip == target_ip:
                    print(f"🎯 TARGET IP {target_ip} ACQUIRED ON SLOT {slot}!")
                    target_found = True
                    
                if ip in seen_ips:
                    print(f"⚠️ DUPLICATE IP DETECTED: {ip} (Slot {slot})")
                    duplicates += 1
                else:
                    seen_ips.add(ip)
                    
    print("\n--- RESULTS ---")
    print(f"Total Unique IPs Farmed: {len(seen_ips)}")
    print(f"Target {target_ip} Found: {target_found}")
    print(f"Duplicates Detected: {duplicates}")
    
if __name__ == "__main__":
    main()
