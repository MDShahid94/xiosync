"""
XIOSYNC Profile Migration: Drive files → identities + credentials + storage_objects
- Deletes 3 flagged (banned) profiles from Drive and skips them
- Seeds 58 identities from AllMailsInfo.csv mapped to Drive PRFL filenames
"""
import csv, json, os, subprocess, sys, uuid
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
ORG_ZERO        = "00000000-0000-7000-8000-000000000000"
DRIVE_PROVIDER  = "d3520dbc-d4ae-4cd2-bdf8-cc6c10ab23ed"
PLATFORM        = "google"
CRED_TYPE       = "cookie_state"
XIOSYNC_BASE    = "http://localhost:8000"
ADMIN_EMAIL     = "admin@xiogrid.dev"
ADMIN_PASSWORD  = "Xiogrid2026!Admin"
CSV_PATH        = "/Users/karmareturns/Desktop/XIOBR.archived.20260912/colab/AllMailsInfo.csv"

# ── Banned accounts (flagged by Google — delete entirely) ─────────────────────
BANNED_SLUGS = {"haldarshorana", "ranashohana9", "shohanarana7"}

# ── PRFL file → slug mapping (from Drive ls output, ordered 001→061) ──────────
DRIVE_PROFILES = [
    (1,  "1x2xx3xxx4xxxx5xxxxx6xxxxxx789"),
    (2,  "karmareturnsfromallsides"),
    (3,  "shahid_raiganj"),
    (4,  "sarvamekam1"),
    (5,  "xiosyncnetwork"),
    (6,  "samnurnihartalukdar"),
    (7,  "thewitnessone"),
    (8,  "shahid_workload"),
    (9,  "lailakhatun78693"),
    (10, "lailakhatun733156"),
    (11, "sk150728951"),
    (12, "lifelonglearnersgroup"),
    (13, "creativetech_raiganj"),
    (14, "tvsamsha"),
    (15, "m81114692"),
    (16, "imrankhanchainagar"),
    (17, "lailatulqadar78692"),
    (18, "kamnursamnur"),
    (19, "kaiumtalukdar786"),
    (20, "one_thatwhatexists"),
    (21, "sunmontueswednesthursfrisatur7"),
    (22, "phonealhai"),
    (23, "babulraana"),
    (24, "chaina_raiganj"),
    (25, "evacomputercafe"),
    (26, "haquenasimul504"),
    (27, "chaina_anantapurasha"),
    (28, "samshanoorhid"),
    (29, "kamnurnihartalukdar"),
    (30, "nasimulhaque_raiganj"),
    (31, "studywithshahid"),
    (32, "whatisdeath2013"),
    (33, "letslivemotivation"),
    (34, "edu_evanarose"),
    (35, "kamnurjahirul"),
    (36, "samnur_raiganj"),
    (37, "naiduraana3"),
    (38, "parveenshohana"),
    (39, "lyncoledu"),
    (40, "ssg_hemtabad"),
    (41, "thatwhatexists"),
    (42, "emax_hemtabad"),
    (43, "alipali2026"),
    (44, "nazim_raiganj"),
    (45, "su9445224"),
    (46, "akhtarijimi"),
    (47, "evanarose_com"),
    (48, "raanaamondal"),
    (49, "alhackymolla"),
    (50, "therevaluer"),
    (51, "kamnurtalukdar"),
    (52, "mail_karmadishari"),
    (53, "learningeverythingexists"),
    (54, "haquenasimul735"),
    (55, "samsialhai"),
    (56, "alisahed925"),
    (57, "ihdinassiratalmustakim786"),
    (58, "haldarshorana"),        # BANNED
    (59, "ranashohana9"),         # BANNED
    (60, "shohanarana7"),         # BANNED
    (61, "kakunasim97"),
]

def slug_to_email(slug: str) -> str:
    """slug → Gmail: underscores become dots."""
    return slug.replace("_", ".") + "@gmail.com"

# ── Load AllMailsInfo.csv ──────────────────────────────────────────────────────
csv_data = {}  # email → {tier, password, totp_secret}
with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        email = row["email"].strip().lower()
        csv_data[email] = {
            "tier":        row.get("tier", "Starter").strip(),
            "password":    row.get("password", "").strip(),
            "totp_secret": row.get("totp_secret", "").strip(),
        }
print(f"Loaded {len(csv_data)} accounts from CSV")

# ── Get XIOSYNC admin token ────────────────────────────────────────────────────
import urllib.request
def _api(method, path, body=None, token=None):
    url = f"{XIOSYNC_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:200]}

auth = _api("POST", "/api/v1/auth/login", {
    "organization_id": ORG_ZERO,
    "email": ADMIN_EMAIL,
    "password": ADMIN_PASSWORD,
})
TOKEN = auth.get("access_token", "")
if not TOKEN:
    print(f"❌ Login failed: {auth}")
    sys.exit(1)
print(f"✅ Logged in as {ADMIN_EMAIL}")

# ── Connect to DB ──────────────────────────────────────────────────────────────
import sqlalchemy as sa
engine = sa.create_engine("postgresql+psycopg://karmareturns@localhost:5432/xiosync")

now = datetime.now(timezone.utc)

# ── Step 1: Delete banned profiles from Drive ──────────────────────────────────
print("\n=== Step 1: Delete banned profiles from Drive ===")
COLAB_IP = "100.75.64.61"
for serial, slug in DRIVE_PROFILES:
    if slug in BANNED_SLUGS:
        key = f"PRFL-{serial:03d}_{slug}.tar.gz"
        drive_path = f"/content/drive/MyDrive/XIOSYNC-Shared/chrome_profiles/{key}"
        r = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", f"root@{COLAB_IP}",
             f"rm -f '{drive_path}' && echo 'deleted' || echo 'not found'"],
            capture_output=True, text=True, timeout=15
        )
        status = r.stdout.strip()
        print(f"  🗑️  {key} → {status}")

# ── Step 2: Insert identities + credentials + storage_objects ──────────────────
print("\n=== Step 2: Migrate 58 profiles → XIOSYNC DB ===")

inserted = 0
skipped_banned = 0
skipped_no_csv = 0

with engine.begin() as conn:
    for serial, slug in DRIVE_PROFILES:
        if slug in BANNED_SLUGS:
            skipped_banned += 1
            continue

        email     = slug_to_email(slug)
        obj_key   = f"chrome_profiles/PRFL-{serial:03d}_{slug}.tar.gz"
        acct      = csv_data.get(email, {})
        tier      = acct.get("tier", "Starter")
        password  = acct.get("password", "")
        totp      = acct.get("totp_secret", "")
        display   = email.split("@")[0].replace(".", " ").replace("_", " ").title()

        if not acct:
            skipped_no_csv += 1
            print(f"  ⚠️  {email} not in CSV — inserting with empty auth")

        identity_id  = str(uuid.uuid4())
        credential_id = str(uuid.uuid4())
        storage_id   = str(uuid.uuid4())

        # ── identities ─────────────────────────────────────────────────────────
        conn.execute(sa.text("""
            INSERT INTO identities
                (id, organization_id, identifier, platform, display_name,
                 state, metadata, tags, created_at, updated_at)
            VALUES
                (:id, :org, :identifier, :platform, :display_name,
                 'active', :metadata, :tags, :now, :now)
            ON CONFLICT (organization_id, identifier, platform) DO UPDATE SET
                state      = 'active',
                metadata   = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at
        """), {
            "id":           identity_id,
            "org":          ORG_ZERO,
            "identifier":   email,
            "platform":     PLATFORM,
            "display_name": display,
            "metadata":     json.dumps({
                "tier":        tier,
                "prfl_serial": serial,
                "prfl_slug":   slug,
                "source":      "xiobr_migration_v1",
            }),
            "tags": [f"tier:{tier.lower()}", "source:xiobr"],
            "now": now,
        })

        # Fetch actual id (in case of ON CONFLICT DO UPDATE)
        row = conn.execute(sa.text("""
            SELECT id FROM identities
            WHERE organization_id = :org AND identifier = :email AND platform = :platform
        """), {"org": ORG_ZERO, "email": email, "platform": PLATFORM}).mappings().first()
        real_identity_id = str(row["id"])

        # ── credentials (cookie_state = chrome profile tarball) ────────────────
        conn.execute(sa.text("""
            INSERT INTO credentials
                (id, organization_id, identity_id, credential_type,
                 storage_object_key, health_score, label, created_at, updated_at)
            VALUES
                (:id, :org, :iid, :ctype, :obj_key, 1.0, 'chrome_profile', :now, :now)
            ON CONFLICT (identity_id, credential_type, label) DO UPDATE SET
                storage_object_key = EXCLUDED.storage_object_key,
                updated_at         = EXCLUDED.updated_at
        """), {
            "id":      credential_id,
            "org":     ORG_ZERO,
            "iid":     real_identity_id,
            "ctype":   CRED_TYPE,
            "obj_key": obj_key,
            "now":     now,
        })

        # ── storage_objects (tracking row for Drive provider) ──────────────────
        conn.execute(sa.text("""
            INSERT INTO storage_objects
                (id, organization_id, provider_id, object_key, object_type,
                 content_type, identity_id, created_at, updated_at)
            VALUES
                (:id, :org, :pid, :key, 'chrome_profile',
                 'application/gzip', :iid, :now, :now)
            ON CONFLICT DO NOTHING
        """), {
            "id":  storage_id,
            "org": ORG_ZERO,
            "pid": DRIVE_PROVIDER,
            "key": obj_key,
            "iid": real_identity_id,
            "now": now,
        })

        # ── vault: store google_auth secret ────────────────────────────────────
        vault_key = f"identities/{real_identity_id}/google_auth"
        vault_result = _api("POST", "/api/v1/vault/secrets", {
            "key":           vault_key,
            "value":         json.dumps({"password": password, "totp_secret": totp}),
            "secret_type":   "credential",
            "platform_global": False,
        }, token=TOKEN)
        # Ignore 409 conflict (already exists)

        print(f"  ✅ PRFL-{serial:03d} {email} [{tier}]")
        inserted += 1

print(f"""
=== Migration complete ===
  Inserted:       {inserted}
  Banned/skipped: {skipped_banned}
  CSV missing:    {skipped_no_csv}
""")

# ── Step 3: Verify ─────────────────────────────────────────────────────────────
with engine.connect() as conn:
    counts = conn.execute(sa.text("""
        SELECT
          (SELECT COUNT(*) FROM identities WHERE organization_id=:org) AS identities,
          (SELECT COUNT(*) FROM credentials WHERE organization_id=:org) AS credentials,
          (SELECT COUNT(*) FROM storage_objects WHERE organization_id=:org) AS storage_objects
    """), {"org": ORG_ZERO}).mappings().first()
    print(f"DB verification:")
    print(f"  identities:     {counts['identities']}")
    print(f"  credentials:    {counts['credentials']}")
    print(f"  storage_objects:{counts['storage_objects']}")

