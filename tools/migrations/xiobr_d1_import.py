"""XIOBR → XIOSYNC Data Migration Tool

One-time migration script that imports data from XIOBR's Cloudflare D1 xio-mesh
database into XIOSYNC's universal identity + credential registry.

This is an Organisation-Zero application tool — NOT a platform subsystem.
It knows XIOBR's specific D1 schema and translates it into XIOSYNC's universal
schema (identities, credentials, vaulted_secrets).

Usage:
    cd /Users/karmareturns/projects/XIOSYNC
    uv run python3 tools/migrations/xiobr_d1_import.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import uuid
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ── Configuration ─────────────────────────────────────────────────────────────

CF_TOKEN    = "cfat_02m6YVUxHSKKtNfv1SeaMKg9wU1qIzUe3UavRWzlb1362068"
CF_ACCOUNT  = "e63a13ef226ce4708d121b8d27fdc8a5"
DB_ID       = "acfd72de-cc4a-4426-9590-66c47a2e36b5"
ORG_ID      = "00000000-0000-7000-8000-000000000000"

# XIOBR-specific platform labels for the universal identity registry
PLATFORM_GOOGLE = "google"
PLATFORM_V0     = "v0"


# ── D1 query helper ────────────────────────────────────────────────────────────

def d1(sql: str, *, token: str = CF_TOKEN) -> list[dict]:
    url  = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/d1/database/{DB_ID}/query"
    body = json.dumps({"sql": sql, "params": []}).encode()
    req  = urllib.request.Request(url, data=body, method="POST",
           headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    if not data.get("success"):
        raise RuntimeError(f"D1 error: {data.get('errors')}")
    return data["result"][0].get("results", [])


# ── Import routines ────────────────────────────────────────────────────────────

def import_google_identities(conn, crypto, *, dry_run: bool) -> dict:
    """Import D1.accounts (Google accounts) as XIOSYNC identities on platform='google'."""
    rows = d1("SELECT email, tier, password, totp_secret, is_active FROM accounts ORDER BY email LIMIT 1000")
    stats = {"imported": 0, "skipped": 0, "vaulted": 0, "errors": []}

    # Desired Pro identifiers (Organisation Zero config)
    PRO_IDENTIFIERS = {
        "1x2xx3xxx4xxxx5xxxxx6xxxxxx789@gmail.com", "chaina.anantapurasha@gmail.com",
        "chaina.raiganj@gmail.com", "evacomputercafe@gmail.com",
        "karmareturnsfromallsides@gmail.com", "lailakhatun733156@gmail.com",
        "lailakhatun78693@gmail.com", "nazim.raiganj@gmail.com",
        "samnur.raiganj@gmail.com", "samnurnihartalukdar@gmail.com",
        "sarvamekam1@gmail.com", "shahid.raiganj@gmail.com", "shahid.workload@gmail.com",
        "thewitnessone@gmail.com", "whatisdeath2013@gmail.com", "xiosyncnetwork@gmail.com",
    }

    for r in rows:
        email  = r["email"]
        state  = "active" if r.get("is_active", 1) else "suspended"
        # Store XIOBR tier as metadata — not a schema property
        meta   = json.dumps({"xiobr_tier": "Pro" if email in PRO_IDENTIFIERS else "Starter"})
        tags   = ["pro"] if email in PRO_IDENTIFIERS else []

        if dry_run:
            stats["imported"] += 1
            continue
        try:
            result = conn.execute(text("""
                INSERT INTO identities
                  (id, organization_id, identifier, platform, display_name,
                   state, metadata, tags, created_at, updated_at)
                VALUES
                  (gen_random_uuid(), :org, :ident, 'google', :ident,
                   :state, cast(:meta as jsonb), :tags, now(), now())
                ON CONFLICT (organization_id, identifier, platform) DO NOTHING
                RETURNING id
            """), {"org": ORG_ID, "ident": email, "state": state,
                   "meta": meta, "tags": tags}).fetchone()

            if result:
                iid = str(result.id)
                # Vault password
                if r.get("password") and crypto:
                    _vault_credential(conn, crypto, ORG_ID, iid, "password",
                                      r["password"], f"Google password for {email}")
                    stats["vaulted"] += 1
                # Vault TOTP secret
                if r.get("totp_secret") and crypto:
                    _vault_credential(conn, crypto, ORG_ID, iid, "totp_secret",
                                      r["totp_secret"], f"TOTP secret for {email}")
                    stats["vaulted"] += 1
            stats["imported"] += 1
        except Exception as exc:
            stats["errors"].append(f"google/{email}: {exc}")
    return stats


def import_v0_identities(conn, crypto, *, dry_run: bool) -> dict:
    """Import D1.v0o_accounts as XIOSYNC identities on platform='v0'."""
    rows = d1("SELECT id, name, api_key, email, total_credits FROM v0o_accounts ORDER BY name LIMIT 500")
    stats = {"imported": 0, "vaulted": 0, "errors": []}

    for r in rows:
        email = r.get("email") or f"{r.get('name', '')}@v0.dev"
        meta  = json.dumps({"v0_id": r.get("id"), "total_credits": r.get("total_credits"),
                             "xiobr_tier": "Starter"})  # All V0 accounts are Starter

        if dry_run:
            stats["imported"] += 1
            continue
        try:
            result = conn.execute(text("""
                INSERT INTO identities
                  (id, organization_id, identifier, platform, display_name,
                   state, metadata, tags, created_at, updated_at)
                VALUES
                  (gen_random_uuid(), :org, :ident, 'v0', :name,
                   'active', cast(:meta as jsonb), '{v0}', now(), now())
                ON CONFLICT (organization_id, identifier, platform) DO NOTHING
                RETURNING id
            """), {"org": ORG_ID, "ident": email, "name": r.get("name", email),
                   "meta": meta}).fetchone()

            if result and r.get("api_key") and crypto:
                iid = str(result.id)
                _vault_credential(conn, crypto, ORG_ID, iid, "api_key",
                                  r["api_key"], f"V0 API key for {r.get('name')}")
                stats["vaulted"] += 1
            stats["imported"] += 1
        except Exception as exc:
            stats["errors"].append(f"v0/{r.get('name')}: {exc}")
    return stats


def import_cookie_sessions(conn, crypto, *, dry_run: bool) -> dict:
    """Import D1.sessions cookie_state → credentials table."""
    rows = d1("""
        SELECT s.id, s.account_email, s.cookie_state, s.is_persisted
        FROM sessions s
        WHERE s.account_email IS NOT NULL
          AND s.cookie_state IS NOT NULL AND s.cookie_state != ''
        LIMIT 1000
    """)
    stats = {"imported": 0, "errors": []}

    for r in rows:
        email = r.get("account_email", "")
        if dry_run:
            stats["imported"] += 1
            continue
        try:
            identity = conn.execute(text("""
                SELECT id FROM identities
                WHERE organization_id=:org AND identifier=:ident AND platform='google'
            """), {"org": ORG_ID, "ident": email}).fetchone()
            if not identity:
                continue

            iid = str(identity.id)
            _vault_credential(conn, crypto, ORG_ID, iid, "cookie",
                              r["cookie_state"], f"Cookie session for {email}")
            stats["imported"] += 1
        except Exception as exc:
            stats["errors"].append(f"session/{r.get('id')}: {exc}")
    return stats


def _vault_credential(conn, crypto, org_id: str, identity_id: str,
                      cred_type: str, value: str, desc: str) -> None:
    """Encrypt value → vaulted_secrets + register in credentials."""
    from xiosync.subsystems.vault.crypto import VaultCrypto
    assert isinstance(crypto, VaultCrypto)
    vault_key = f"identities/{identity_id}/{cred_type}"
    ct, iv, tag = crypto.encrypt(value, uuid.UUID(org_id))
    secret_type = {
        "password": "credential", "totp_secret": "totp",
        "api_key": "api_key", "cookie": "credential",
    }.get(cred_type, "generic")

    conn.execute(text("""
        INSERT INTO vaulted_secrets
          (id, organization_id, key, ciphertext, iv, auth_tag,
           secret_type, description, created_at, updated_at)
        VALUES
          (gen_random_uuid(), :org, :key, :ct, :iv, :tag,
           :stype, :desc, now(), now())
        ON CONFLICT (organization_id, key) DO NOTHING
    """), {"org": org_id, "key": vault_key, "ct": ct, "iv": iv,
           "tag": tag, "stype": secret_type, "desc": desc})
    conn.execute(text("""
        INSERT INTO credentials
          (id, organization_id, identity_id, credential_type,
           vault_key, health_score, last_refreshed_at, created_at, updated_at)
        VALUES
          (gen_random_uuid(), :org, :iid, :ctype,
           :vkey, 1.0, now(), now(), now())
        ON CONFLICT (identity_id, credential_type) DO UPDATE SET
            vault_key=EXCLUDED.vault_key, last_refreshed_at=now(), updated_at=now()
    """), {"org": org_id, "iid": identity_id, "ctype": cred_type, "vkey": vault_key})


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="XIOBR D1 → XIOSYNC migration")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL",
                            "postgresql+psycopg://xiosync:xiosync@localhost:5432/xiosync")
    auth_secret = os.environ.get("XIOSYNC_AUTH_SECRET", "")

    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)

    from xiosync.subsystems.vault.crypto import VaultCrypto
    crypto = VaultCrypto(auth_secret) if auth_secret else None
    if not crypto:
        print("⚠ XIOSYNC_AUTH_SECRET not set — credentials will NOT be vaulted")

    print(f"{'[DRY RUN] ' if args.dry_run else ''}XIOBR D1 → XIOSYNC migration")
    print(f"  Source: D1 xio-mesh ({DB_ID})")
    print(f"  Target: {db_url}")
    print()

    with Session() as conn:
        with conn.begin():
            print("1. Importing Google identities (D1.accounts)…")
            gs = import_google_identities(conn, crypto, dry_run=args.dry_run)
            print(f"   imported={gs['imported']} vaulted={gs['vaulted']} errors={len(gs['errors'])}")
            for e in gs["errors"][:5]:
                print(f"   ⚠ {e}")

            print("2. Importing V0 identities (D1.v0o_accounts)…")
            vs = import_v0_identities(conn, crypto, dry_run=args.dry_run)
            print(f"   imported={vs['imported']} vaulted={vs['vaulted']} errors={len(vs['errors'])}")
            for e in vs["errors"][:5]:
                print(f"   ⚠ {e}")

            print("3. Importing cookie sessions (D1.sessions)…")
            ss = import_cookie_sessions(conn, crypto, dry_run=args.dry_run)
            print(f"   imported={ss['imported']} errors={len(ss['errors'])}")
            for e in ss["errors"][:5]:
                print(f"   ⚠ {e}")

    total_errs = len(gs["errors"]) + len(vs["errors"]) + len(ss["errors"])
    print()
    print(f"{'[DRY RUN] ' if args.dry_run else ''}✅ Done."
          f" Google={gs['imported']} V0={vs['imported']}"
          f" Sessions={ss['imported']} Errors={total_errs}")

    sys.exit(1 if total_errs else 0)


if __name__ == "__main__":
    main()
