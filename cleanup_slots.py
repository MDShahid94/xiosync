import concurrent.futures
import os
from sqlalchemy import select
from sqlalchemy.orm import Session
from xiosync.persistence.database import create_database_engine
from xiosync.subsystems.xiogrid.models.exit_node import PPPoEExitNode
from xiosync.subsystems.xiogrid.services.pppoe_nodes import PPPoENodeService

url = os.environ.get("XIOSYNC_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/xiosync")
engine = create_database_engine(url)

with Session(engine) as db:
    nodes = db.scalars(select(PPPoEExitNode).where(PPPoEExitNode.state.in_(["reconnecting", "down"]))).all()
    slots = [(n.host_id, n.ppp_slot) for n in nodes]
    
print(f"Found {len(slots)} slots to destroy.")

def cleanup(host_id, slot):
    with Session(engine) as db:
        svc = PPPoENodeService(db)
        try:
            svc.destroy_slot(host_id, slot)
            db.commit()
            return f"Slot {slot} destroyed."
        except Exception as e:
            db.rollback()
            return f"Slot {slot} destroy failed: {e}"

with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
    futs = [ex.submit(cleanup, host_id, slot) for host_id, slot in slots]
    for fut in concurrent.futures.as_completed(futs):
        print(fut.result())

print("Cleanup complete.")
