"""D1 → XIOSYNC: sessions metadata + broker_accounts (Appetize)"""
import json, os, sys, uuid, urllib.request
from datetime import datetime, timezone

CF_TOKEN   = "cfat_02m6YVUxHSKKtNfv1SeaMKg9wU1qIzUe3UavRWzlb1362068"
CF_ACCOUNT = "e63a13ef226ce4708d121b8d27fdc8a5"
D1_DB      = "acfd72de-cc4a-4426-9590-66c47a2e36b5"
D1_URL     = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/d1/database/{D1_DB}/query"
D1_HEADERS = {"Authorization": f"Bearer {CF_TOKEN}", "Content-Type": "application/json"}
XIOSYNC    = "http://localhost:8000"
ORG_ZERO   = "00000000-0000-7000-8000-000000000000"

def d1(sql, params=None):
    body = json.dumps({"sql": sql, "params": params or []}).encode()
    req  = urllib.request.Request(D1_URL, data=body, headers=D1_HEADERS, method="POST")
    resp = urllib.request.urlopen(req, timeout=20)
    data = json.loads(resp.read())
    if not data.get("success"):
        raise RuntimeError(f"D1 error: {data.get('errors')}")
    return data["result"][0].get("results", [])

def xapi(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body else None
    hdrs = {"Content-Type": "application/json"}
    if token: hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{XIOSYNC}{path}", data=data, headers=hdrs, method=method)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read())
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "_body": e.read().decode()[:120]}

TOKEN = xapi("POST", "/api/v1/auth/login", {
    "organization_id": ORG_ZERO, "email": "admin@xiogrid.dev", "password": "Xiogrid2026!Admin",
}).get("access_token", "")
assert TOKEN, "Login failed"

import sqlalchemy as sa
engine = sa.create_engine("postgresql+psycopg://karmareturns@localhost:5432/xiosync")
now = datetime.now(timezone.utc)

# ── Step 2: sessions → identities.metadata ────────────────────────────────────
print("=== Step 2: D1 sessions → identities metadata ===")

# Get actual columns first
cols = [r["name"] for r in d1("PRAGMA table_info(sessions)")]
print(f"  sessions columns: {cols}")

d1_sessions = d1("SELECT * FROM sessions ORDER BY serial")
print(f"  rows: {len(d1_sessions)}")

updated = 0
with engine.begin() as conn:
    for s in d1_sessions:
        email = s.get("account_email") or ""
        if not email:
            # fall back: slug-based lookup
            slug  = s.get("id", "")
            email = slug.replace("_", ".") + "@gmail.com"

        iid_row = conn.execute(sa.text(
            "SELECT id, metadata FROM identities WHERE identifier=:e AND organization_id=:o"
        ), {"e": email, "o": ORG_ZERO}).mappings().first()
        if not iid_row:
            print(f"  ⚠️  not found: {email}")
            continue

        meta = dict(iid_row["metadata"] or {})
        # Merge all D1 session fields into metadata under d1_ namespace
        for k, v in s.items():
            if k not in ("id", "account_email", "serial"):
                meta[f"d1_{k}"] = v

        conn.execute(sa.text(
            "UPDATE identities SET metadata=:m, updated_at=:n WHERE id=:id"
        ), {"m": json.dumps(meta), "n": now, "id": str(iid_row["id"])})
        updated += 1

print(f"  updated={updated}")

# ── Step 3: broker_accounts → credentials + vault ─────────────────────────────
print("\n=== Step 3: D1 broker_accounts → credentials (Appetize) ===")
brokers = d1("SELECT * FROM broker_accounts ORDER BY id")
print(f"  broker rows: {len(brokers)}")

b_upserted = 0
with engine.begin() as conn:
    for b in brokers:
        email    = (b.get("email") or "").strip().lower()
        api_key  = b.get("api_key", "")
        build_id = b.get("build_id", "")
        play_url = b.get("play_url", "")
        is_active= bool(b.get("is_active", 1))

        iid_row = conn.execute(sa.text(
            "SELECT id FROM identities WHERE identifier=:e AND organization_id=:o"
        ), {"e": email, "o": ORG_ZERO}).mappings().first()
        if not iid_row:
            print(f"  ⚠️  not found: {email}")
            continue

        iid       = str(iid_row["id"])
        vault_key = f"identities/{iid}/appetize"
        cred_type = "appetize_broker"
        label     = "appetize"

        # Store api_key + build metadata in vault
        r = xapi("POST", "/api/v1/vault/secrets", {
            "key":   vault_key,
            "value": json.dumps({
                "api_key":  api_key,
                "build_id": build_id,
                "play_url": play_url,
            }),
            "secret_type":    "credential",
            "platform_global": False,
        }, token=TOKEN)
        if r.get("_err") == 409:
            xapi("PUT", f"/api/v1/vault/secrets/{urllib.parse.quote(vault_key, safe='')}",
                {"value": json.dumps({"api_key": api_key, "build_id": build_id, "play_url": play_url}),
                 "secret_type": "credential"}, token=TOKEN)

        # Credentials row with health_score=1.0 (active) or 0.0
        conn.execute(sa.text("""
            INSERT INTO credentials
                (id, organization_id, identity_id, credential_type,
                 vault_key, health_score, label, created_at, updated_at)
            VALUES
                (:id, :org, :iid, :ct, :vk, :hs, :lb, :now, :now)
            ON CONFLICT (identity_id, credential_type, label) DO UPDATE SET
                vault_key    = EXCLUDED.vault_key,
                health_score = EXCLUDED.health_score,
                updated_at   = EXCLUDED.updated_at
        """), {
            "id": str(uuid.uuid4()), "org": ORG_ZERO, "iid": iid,
            "ct": cred_type, "vk": vault_key,
            "hs": 1.0 if is_active else 0.0,
            "lb": label, "now": now,
        })
        print(f"  ✅ {email} → appetize {build_id[:20]}…  play_url stored")
        b_upserted += 1

print(f"  upserted={b_upserted}")

# ── Final counts ───────────────────────────────────────────────────────────────
import urllib.parse
print("\n=== Final XIOSYNC state ===")
with engine.connect() as conn:
    r = conn.execute(sa.text("""
        SELECT
          (SELECT COUNT(*) FROM identities WHERE organization_id=:o) AS identities,
          (SELECT COUNT(*) FROM identities WHERE 'tier:pro' = ANY(tags)) AS pro,
          (SELECT COUNT(*) FROM credentials WHERE organization_id=:o) AS credentials,
          (SELECT COUNT(*) FROM credentials WHERE credential_type='cookie_state') AS chrome_profiles,
          (SELECT COUNT(*) FROM credentials WHERE credential_type='appetize_broker') AS appetize,
          (SELECT COUNT(*) FROM vaulted_secrets WHERE key LIKE 'identities/%') AS vault_identity_secrets
    """), {"o": ORG_ZERO}).mappings().first()
    for k, v in r.items():
        print(f"  {k:<28} {v}")
