import asyncio
from sqlalchemy import text
from xiosync.persistence.database import get_db
from xiosync.platform.config import config

async def main():
    async for db in get_db():
        # Check if identity exists
        res = await db.execute(text("SELECT id, name FROM identities WHERE email = :email"), {"email": "evanarose.com@gmail.com"})
        identity = res.fetchone()
        print(f"Identity: {identity}")
        
        # Check workflow template
        res = await db.execute(text("SELECT id, slug FROM workflow_templates WHERE slug = 'google-signin-dag'"))
        tmpl = res.fetchone()
        print(f"Template: {tmpl}")
        
        # Check profiles
        res = await db.execute(text("SELECT id, serial FROM xiogrid_fingerprint_profiles LIMIT 5"))
        print(f"Profiles: {res.fetchall()}")

if __name__ == "__main__":
    asyncio.run(main())
