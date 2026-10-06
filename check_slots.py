import os
from xiosync.persistence.database import create_database_engine
from sqlalchemy.orm import Session
from sqlalchemy import select
from xiosync.subsystems.xiogrid.models.exit_node import PPPoEExitNode

url = os.environ.get("XIOSYNC_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/xiosync")
engine = create_database_engine(url)

with Session(engine) as db:
    nodes = db.scalars(select(PPPoEExitNode)).all()
    active = [n for n in nodes if n.state not in ('down', 'destroyed')]
    print(f"Total slots in DB: {len(nodes)}")
    print(f"Currently open (active/idle/assigned) slots: {len(active)}")
