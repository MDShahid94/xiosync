import asyncio
import uuid
import json
from datetime import datetime, timezone
from xiosync.platform.config import load_config
import os
os.environ["XIOSYNC_ENVIRONMENT"] = "dev"
os.environ["XIOSYNC_INTERNAL_SECRET"] = "xiosync-internal-2026-karma"

from xiosync.persistence.database import create_database_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

async def main():
    cfg = load_config()
    engine = create_database_engine(cfg)
    SessionLocal = sessionmaker(bind=engine, future=True)
    
    with SessionLocal() as db:
        # Get org_id
        org_id_res = db.execute(text("SELECT id FROM organizations LIMIT 1")).fetchone()
        org_id = org_id_res[0] if org_id_res else '00000000-0000-7000-8000-000000000000'

        # Get template
        tmpl = db.execute(text("SELECT id, slug, dag_domain, dag_root_intent, template_type, config FROM workflow_templates WHERE slug = 'google-signin-dag'")).fetchone()
        if not tmpl:
            print("Template google-signin-dag not found")
            return
        template_id = tmpl.id
        
        tmpl_snapshot = json.dumps({
            "slug": tmpl.slug,
            "dag_domain": tmpl.dag_domain,
            "dag_root_intent": tmpl.dag_root_intent,
            "template_type": tmpl.template_type,
            "config": tmpl.config if isinstance(tmpl.config, dict) else {},
        })
        
        run_context = {
            "email": "evanarose.com@gmail.com",
            "password": "TheTruth1!",
            "totp_secret": "wdkpyermwk3o5uwetcck5gbhltprowkq",
            "identity_id": None
        }
        
        run_id = str(uuid.uuid7())
        
        db.execute(text("""
            INSERT INTO xioflow_runs
              (id, organization_id, template_id, state, context, template_snapshot, started_at, created_at, updated_at)
            VALUES
              (:id, :org, :tmpl, 'PENDING', cast(:ctx as jsonb), cast(:snap as jsonb), now(), now(), now())
        """), {
            "id": run_id,
            "org": org_id,
            "tmpl": template_id,
            "ctx": json.dumps(run_context),
            "snap": tmpl_snapshot,
        })
        db.commit()
        print(f"Dispatched run {run_id}")

if __name__ == "__main__":
    asyncio.run(main())
