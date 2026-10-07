import os

from sqlalchemy import select
from sqlalchemy.orm import Session
from xiosync.persistence.database import create_database_engine
from xiosync.subsystems.xiogrid.models.exit_node import PPPoEExitNode

url = os.environ.get(
    "XIOSYNC_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/xiosync"
)
engine = create_database_engine(url)

with Session(engine) as db:
    nodes = db.scalars(select(PPPoEExitNode)).all()
    print(f"Total: {len(nodes)}")
    for n in nodes:
        print(f"Slot {n.ppp_slot}: {n.public_ip}")
