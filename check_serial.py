import asyncio
from xiosync.persistence.engine import get_engine
from sqlalchemy.orm import Session
from sqlalchemy import text

async def main():
    engine = await get_engine()
    with Session(engine.sync_engine) as sess:
        rows = sess.execute(text("SELECT id, serial, identifier FROM identities WHERE identifier LIKE '%karmareturns%'")).mappings().all()
        for r in rows:
            print(dict(r))

asyncio.run(main())
