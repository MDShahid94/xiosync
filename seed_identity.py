import asyncio
from xiosync.api.app import app
from xiosync.platform.engine_ref import get_engine, set_engine
from xiosync.persistence.database import create_database_engine
from xiosync.platform.config import load_config
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from xiosync.services.secrets import SecretRefService

async def main():
    cfg = load_config()
    engine = create_database_engine(cfg)
    set_engine(engine)
    
    SessionLocal = sessionmaker(bind=engine, future=True)
    with SessionLocal() as db:
        # Check if organization 00000000-0000-7000-8000-000000000000 exists
        res = db.execute(text("SELECT id FROM organizations WHERE id = '00000000-0000-7000-8000-000000000000'")).fetchone()
        if not res:
            print("Org not found!")
            return
        org_id = res[0]
        
        # Upsert identity
        res = db.execute(text("""
            INSERT INTO identities (organization_id, identifier, platform) 
            VALUES (:org_id, :email, 'google')
            ON CONFLICT (organization_id, identifier, platform) DO UPDATE SET updated_at = now()
            RETURNING id
        """), {"org_id": org_id, "email": "evanarose.com@gmail.com"}).fetchone()
        identity_id = res[0]
        print(f"Identity ID: {identity_id}")
        db.commit()

        # Vault the secrets
        from xiosync.services.secrets import SecretRefService
        # I need to see how Vault/Secrets works... let's just insert into vaulted_secrets directly
        # Or look at how credentials are set up. Let me know what classes are available.

if __name__ == "__main__":
    asyncio.run(main())
