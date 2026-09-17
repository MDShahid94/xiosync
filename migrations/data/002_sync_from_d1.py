"""
D1 → XIOSYNC full sync:
  1. accounts       → update vault google_auth secrets (passwords + TOTP)
  2. sessions       → update identities.metadata (exit_node, notes, is_persisted, tier)
  3. session_services → upsert credentials rows per service
"""
import json, os, sys, uuid, urllib.request, urllib.parse
from datetime import datetime, timezone

CF_TOKEN    = "cfat_02m6YVUxHSKKtNfv1SeaMKg9wU1qIzUe3UavRWzlb1362068"
CF_ACCOUNT  = "e63a13ef226ce4708d121b8d27fdc8a5"
D1_DB_ID    = "acfd72de-cc4a-4426-9590-66c47a2e36b5"
D1_URL      = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/d1/database/{D1_DB_ID}/query"
D1_HEADERS  = {"Authorization": f"Bearer {CF_TOKEN}", "Content-Type": "application/json"}

XIOSYNC     = "http://localhost:8000"
ORG_ZERO    = "00000000-0000-7000-8000-000000000000"

# ── D1 query helper ────────────────────────────────────────────────────────────
def d1(sql, params=None):
    body = json.dumps({"sql": sql, "params": params or []}).encode()
    req  = urllib.request.Request(D1_URL, data=body, headers=D1_HEADERS, method="POST")
    resp = urllib.request.urlopen(req, timeout=20)
    data = json.loads(resp.read())
    if not data.get("success"):
        raise RuntimeError(f"D1 error: {data.get('errors')}")
    return data["result"][0].get("results", [])

# ── XIOSYNC API helper ─────────────────────────────────────────────────────────
def xapi(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body else None
    hdrs = {"Content-Type": "application/json"}
    if token: hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{XIOSYNC}{path}", data=data, headers=hdrs, method=method)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read())
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "_body": e.read().decode()[:120]}

# ── Login ──────────────────────────────────────────────────────────────────────
TOKEN = xapi("POST", "/api/v1/auth/login", {
    "organization_id": ORG_ZERO,
    "email": "admin@xiogrid.dev",
    "password": "Xiogrid2026!Admin",
}).get("access_token", "")
assert TOKEN, "Login failed"
print(f"✅ XIOSYNC login OK")

# ── SQLAlchemy for direct DB updates ──────────────────────────────────────────
import sqlalchemy as sa
engine = sa.create_engine("postgresql+psycopg://karmareturns@localhost:5432/xiosync")
from xiosync.subsystems.vault.crypto import VaultCrypto
crypto = VaultCrypto(os.environ["XIOSYNC_AUTH_SECRET"])
now = datetime.now(timezone.utc)

# ── Step 1: Pull accounts from D1 → update vault google_auth ──────────────────
print("\n=== Step 1: D1 accounts → vault password sync ===")
d1_accounts = d1("SELECT email, tier, password, totp_secret FROM accounts")
print(f"  D1 accounts: {len(d1_accounts)}")

pwd_updated = 0
pwd_same    = 0
pwd_missing = 0

with engine.begin() as conn:
    for row in d1_accounts:
        email    = (row.get("email") or "").strip().lower()
        password = (row.get("password") or "").strip()
        totp     = (row.get("totp_secret") or "").strip()
        d1_tier  = (row.get("tier") or "Starter").strip()

        # Find identity in XIOSYNC
        iid_row = conn.execute(sa.text(
            "SELECT id FROM identities WHERE identifier=:e AND organization_id=:o"
        ), {"e": email, "o": ORG_ZERO}).mappings().first()

        if not iid_row:
            pwd_missing += 1
            continue

        iid       = str(iid_row["id"])
        vault_key = f"identities/{iid}/google_auth"

        # Read existing vault value
        v_row = conn.execute(sa.text(
            "SELECT ciphertext, iv, auth_tag, organization_id FROM vaulted_secrets WHERE key=:k LIMIT 1"
        ), {"k": vault_key}).mappings().first()

        existing_pwd = ""
        if v_row:
            try:
                org = uuid.UUID(str(v_row["organization_id"])) if v_row["organization_id"] else None
                existing = json.loads(crypto.decrypt(
                    bytes(v_row["ciphertext"]), bytes(v_row["iv"]), bytes(v_row["auth_tag"]), org
                ))
                existing_pwd = existing.get("password", "")
            except Exception:
                pass

        if existing_pwd == password and totp == (existing.get("totp_secret","") if v_row else ""):
            pwd_same += 1
            continue

        # Update vault via API (handles encryption)
        r = xapi("POST", "/api/v1/vault/secrets", {
            "key":           vault_key,
            "value":         json.dumps({"password": password, "totp_secret": totp}),
            "secret_type":   "credential",
            "platform_global": False,
        }, token=TOKEN)
        if r.get("_err") == 409:
            # Already exists — use PUT/PATCH to update
            r = xapi("PUT", f"/api/v1/vault/secrets/{urllib.parse.quote(vault_key, safe='')}",
                {"value": json.dumps({"password": password, "totp_secret": totp}),
                 "secret_type": "credential"},
                token=TOKEN)

        pwd_updated += 1
        print(f"  🔑 Updated: {email} (pwd changed)")

print(f"  updated={pwd_updated}  unchanged={pwd_same}  not_in_xiosync={pwd_missing}")

# ── Step 2: Pull sessions from D1 → update identities.metadata ───────────────
print("\n=== Step 2: D1 sessions → identities metadata sync ===")
d1_sessions = d1("""
    SELECT id, serial, display_name, notes, tier, exit_node,
           bound_at, is_persisted, created_at
    FROM sessions ORDER BY serial
""")
print(f"  D1 sessions: {len(d1_sessions)}")

# Build slug→email map (slug = session id in D1)
def slug_to_email(slug):
    return slug.replace("_", ".") + "@gmail.com"

meta_updated = 0
meta_missing = 0

with engine.begin() as conn:
    for s in d1_sessions:
        slug  = s.get("id", "")
        email = slug_to_email(slug)

        iid_row = conn.execute(sa.text(
            "SELECT id, metadata FROM identities WHERE identifier=:e AND organization_id=:o"
        ), {"e": email, "o": ORG_ZERO}).mappings().first()

        if not iid_row:
            meta_missing += 1
            continue

        # Merge D1 session metadata into existing metadata
        meta = dict(iid_row["metadata"] or {})
        meta.update({
            "d1_exit_node":   s.get("exit_node"),
            "d1_bound_at":    s.get("bound_at"),
            "d1_is_persisted": bool(s.get("is_persisted", 0)),
            "d1_notes":       s.get("notes"),
            "d1_display_name": s.get("display_name"),
            "d1_created_at":  s.get("created_at"),
        })

        conn.execute(sa.text("""
            UPDATE identities SET metadata=:m, updated_at=:now
            WHERE id=:id
        """), {"m": json.dumps(meta), "now": now, "id": str(iid_row["id"])})
        meta_updated += 1

print(f"  updated={meta_updated}  not_in_xiosync={meta_missing}")

# ── Step 3: Pull session_services → upsert credentials ────────────────────────
print("\n=== Step 3: D1 session_services → credentials ===")
d1_svc = d1("""
    SELECT session_id, service, account_hint, session_valid, last_checked
    FROM session_services
    ORDER BY session_id, service
""")
print(f"  D1 service rows: {len(d1_svc)}")

svc_upserted = 0
svc_missing  = 0

with engine.begin() as conn:
    for s in d1_svc:
        slug  = s.get("session_id", "")
        email = slug_to_email(slug)
        svc   = s.get("service", "")
        hint  = s.get("account_hint") or email
        valid = bool(s.get("session_valid", 0))

        iid_row = conn.execute(sa.text(
            "SELECT id FROM identities WHERE identifier=:e AND organization_id=:o"
        ), {"e": email, "o": ORG_ZERO}).mappings().first()

        if not iid_row:
            svc_missing += 1
            continue

        iid = str(iid_row["id"])
        cred_type = f"service_session:{svc}"
        label     = svc

        conn.execute(sa.text("""
            INSERT INTO credentials
                (id, organization_id, identity_id, credential_type,
                 health_score, label, created_at, updated_at)
            VALUES
                (:id, :org, :iid, :ctype,
                 :hs, :label, :now, :now)
            ON CONFLICT (identity_id, credential_type, label) DO UPDATE SET
                health_score = EXCLUDED.health_score,
                updated_at   = EXCLUDED.updated_at
        """), {
            "id":    str(uuid.uuid4()),
            "org":   ORG_ZERO,
            "iid":   iid,
            "ctype": cred_type,
            "hs":    1.0 if valid else 0.0,
            "label": label,
            "now":   now,
        })
        svc_upserted += 1

print(f"  upserted={svc_upserted}  not_in_xiosync={svc_missing}")

# ── Final summary ──────────────────────────────────────────────────────────────
print("\n=== D1 → XIOSYNC sync complete ===")
with engine.connect() as conn:
    r = conn.execute(sa.text("""
        SELECT
          (SELECT COUNT(*) FROM identities WHERE organization_id=:o) AS identities,
          (SELECT COUNT(*) FROM credentials WHERE organization_id=:o) AS credentials,
          (SELECT COUNT(*) FROM vaulted_secrets WHERE key LIKE 'identities/%/google_auth') AS vault_secrets
    """), {"o": ORG_ZERO}).mappings().first()
    print(f"  identities:    {r['identities']}")
    print(f"  credentials:   {r['credentials']}")
    print(f"  vault secrets: {r['vault_secrets']}")
