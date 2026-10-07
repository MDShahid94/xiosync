import os

from sqlalchemy import text
from sqlalchemy.orm import Session
from xiosync.persistence.database import create_database_engine

url = os.environ.get(
    "XIOSYNC_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/xiosync"
)
engine = create_database_engine(url)
with Session(engine) as sess:
    rows = (
        sess.execute(text("SELECT id, profile_serial, identifier FROM identities")).mappings().all()
    )
    for r in rows:
        print(dict(r))
