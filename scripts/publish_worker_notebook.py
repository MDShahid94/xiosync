#!/usr/bin/env python3
"""Upload (or update in-place) the XIOSYNC Worker notebook to Google Drive.

XIOSYNC-native approach:
  - Uses Org Zero's default google_drive storage provider (folder_id from DB)
  - Service account credential from XIOSYNC vault (key: platform/drive_sa_key)
  - Sets public read permission so the Colab link is accessible to anyone
  - Stores the resulting Drive file_id in storage_providers.notebook_file_id
  - Prints the final Colab URL with #forceEdit=true&sandboxMode=true

File ID discovery (XIOBR pattern):
  If the notebook was manually uploaded to Drive, use --fetch-only to scan
  the folder by name and store the file_id without re-uploading.

Usage:
    # Full publish (upload/update + store file_id):
    python3 scripts/publish_worker_notebook.py [--notebook colab/start.ipynb]

    # Only discover and store the file_id (e.g. after manual upload):
    python3 scripts/publish_worker_notebook.py --fetch-only

Environment:
    DATABASE_URL            PostgreSQL connection string
    XIOSYNC_AUTH_SECRET     Vault encryption key

Requirements:
    google-api-python-client  google-auth
    (pip install google-api-python-client google-auth)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

# ── Project root on sys.path ──────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _get_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    url = os.environ["DATABASE_URL"]
    engine = create_engine(url)
    return Session(engine)


_ORG_ZERO = uuid.UUID("00000000-0000-7000-8000-000000000000")


def _get_drive_provider(session):
    """Return (folder_id, provider_id, sa_key_json_or_none) from Org Zero's Drive provider."""
    from sqlalchemy import text
    row = session.execute(
        text("""
            SELECT id, config->>'folder_id' AS folder_id, vault_key
            FROM storage_providers
            WHERE organization_id = :org
              AND provider_type = 'google_drive'
              AND is_default = true
            LIMIT 1
        """),
        {"org": str(_ORG_ZERO)},
    ).mappings().first()
    if not row:
        raise SystemExit(
            "No default google_drive storage provider found for Org Zero.\n"
            "Register one via POST /api/v1/storage/providers"
        )

    sa_credential = None
    if row["vault_key"]:
        from xiosync.subsystems.vault.service import VaultService
        from xiosync.domain.context import OrgContext
        ctx = OrgContext(organization_id=_ORG_ZERO, actor_id=uuid.UUID(int=0))
        try:
            sa_credential = VaultService(session).get_secret(ctx, row["vault_key"],
                                                              allow_platform=True)
        except Exception:
            pass  # Will fall through to Colab ADC

    return str(row["folder_id"]), str(row["id"]), sa_credential


def _store_file_id(session, provider_id: str, file_id: str) -> None:
    """Persist the notebook Drive file_id into storage_providers.notebook_file_id."""
    from sqlalchemy import text
    session.execute(
        text("""
            UPDATE storage_providers
            SET notebook_file_id = :fid, updated_at = now()
            WHERE id = :pid
        """),
        {"fid": file_id, "pid": provider_id},
    )
    session.commit()


def _build_drive_service(sa_key_json: str | None):
    """Build a Drive API service using service account or ADC."""
    try:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account
        from google.auth import default as google_auth_default
    except ImportError:
        raise SystemExit(
            "Missing dependencies. Run:\n"
            "  pip install google-api-python-client google-auth"
        )

    if sa_key_json:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(sa_key_json),
            scopes=["https://www.googleapis.com/auth/drive"],
        )
    else:
        creds, _ = google_auth_default(
            scopes=["https://www.googleapis.com/auth/drive"]
        )

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _find_existing_file(service, folder_id: str, filename: str) -> str | None:
    """Search for an existing file by name in the folder. Return file_id or None."""
    q = (
        f"name='{filename}' "
        f"and '{folder_id}' in parents "
        f"and mimeType='application/vnd.google.colab' "
        f"and trashed=false"
    )
    results = service.files().list(
        q=q, fields="files(id,name)", spaces="drive"
    ).execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None


def _upload_notebook(
    service,
    folder_id: str,
    notebook_path: str,
    existing_file_id: str | None,
) -> str:
    """Upload or update the notebook. Returns Drive file_id."""
    from googleapiclient.http import MediaFileUpload

    filename = "XIOSYNC Worker.ipynb"
    mime = "application/vnd.google.colab"

    media = MediaFileUpload(
        notebook_path,
        mimetype=mime,
        resumable=True,
    )

    if existing_file_id:
        # Update in-place (same file_id → same Colab link stays valid)
        file = service.files().update(
            fileId=existing_file_id,
            body={"name": filename},
            media_body=media,
            fields="id",
        ).execute()
        print(f"  ✅ Notebook updated in Drive (id={file['id']})")
    else:
        # Create new
        file = service.files().create(
            body={
                "name":    filename,
                "parents": [folder_id],
                "mimeType": mime,
            },
            media_body=media,
            fields="id",
        ).execute()
        print(f"  ✅ Notebook created in Drive (id={file['id']})")

    return file["id"]


def _set_public_read(service, file_id: str) -> None:
    """Make the file publicly readable (anyone with link)."""
    service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()
    print("  ✅ Public read permission set")


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish XIOSYNC Worker notebook to Drive")
    parser.add_argument(
        "--notebook",
        default=os.path.join(_ROOT, "colab", "start.ipynb"),
        help="Path to the notebook file (default: colab/start.ipynb)",
    )
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help=(
            "Don't upload — just search Drive folder for 'XIOSYNC Worker.ipynb' "
            "by name and store its file_id in the DB (XIOBR-style discovery). "
            "Use after a manual upload to register the file_id."
        ),
    )
    args = parser.parse_args()

    print("=== XIOSYNC Worker Notebook Publisher ===")

    with _get_db() as session:
        folder_id, provider_id, sa_key = _get_drive_provider(session)
        print(f"Drive folder: {folder_id}")

        service = _build_drive_service(sa_key)

        if args.fetch_only:
            # ── XIOBR-pattern: discover file_id by name in folder ────────────
            print("Fetching-only mode: searching Drive for 'XIOSYNC Worker.ipynb'…")
            file_id = _find_existing_file(service, folder_id, "XIOSYNC Worker.ipynb")
            if not file_id:
                raise SystemExit(
                    "❌ 'XIOSYNC Worker.ipynb' not found in Drive folder.\n"
                    "   Upload it manually first, then re-run with --fetch-only."
                )
            print(f"  ✅ Found: id={file_id}")
            _set_public_read(service, file_id)
            _store_file_id(session, provider_id, file_id)
            print(f"  ✅ notebook_file_id stored in XIOSYNC DB")
        else:
            # ── Full publish: upload/update then store ───────────────────────
            if not os.path.exists(args.notebook):
                raise SystemExit(f"Notebook not found: {args.notebook}")
            print(f"Notebook: {args.notebook}")

            existing_id = _find_existing_file(service, folder_id, "XIOSYNC Worker.ipynb")
            if existing_id:
                print(f"Found existing notebook (id={existing_id}) — updating in-place…")
            else:
                print("No existing notebook found — creating new…")

            file_id = _upload_notebook(service, folder_id, args.notebook, existing_id)
            _set_public_read(service, file_id)
            _store_file_id(session, provider_id, file_id)

    colab_url = (
        f"https://colab.research.google.com/drive/{file_id}"
        f"#forceEdit=true&sandboxMode=true"
    )

    print()
    print("=" * 60)
    print("  ✅ Done!")
    print()
    print(f"  Colab URL (playground / sandbox mode):")
    print(f"  {colab_url}")
    print()
    print("  This URL is permanent — updates publish to the same link.")
    print("  Generate a worker bootstrap URL via:")
    print("    POST /api/v1/workers/bootstrap-tokens")
    print("=" * 60)


if __name__ == "__main__":
    main()
