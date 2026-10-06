import os
import sys
from xiosync.persistence.database import create_database_engine
from sqlalchemy.orm import Session
from sqlalchemy import select, text
from xiosync.subsystems.xiogrid.models.exit_node import PPPoEExitNode, PPPoEHost

url = os.environ.get("XIOSYNC_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/xiosync")
engine = create_database_engine(url)

with Session(engine) as db:
    nodes = db.scalars(select(PPPoEExitNode)).all()
    print(f"Total nodes in DB: {len(nodes)}")
    for node in nodes:
        print(f"Slot {node.ppp_slot}: {node.public_ip} (State: {node.state})")
