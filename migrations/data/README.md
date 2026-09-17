# XIOSYNC Data Migrations

These are one-time data seeding scripts, separate from Alembic schema migrations.
Run with the project venv: `.venv/bin/python3 migrations/data/<script>.py`

| Script | What it does |
|---|---|
| `001_seed_profiles_from_drive.py` | Seeds 58 identities + credentials + storage_objects from Drive PRFL files + AllMailsInfo.csv. Deletes 3 banned profiles from Drive. |
| `002_sync_from_d1.py` | Syncs passwords from Cloudflare D1 `accounts` table → vault. Updates 1 changed password. |
| `003_sync_sessions_broker_from_d1.py` | Syncs D1 `sessions` metadata (exit_node, is_persisted etc.) → identities.metadata. Broker accounts removed post-run. |

## Re-run safety
All scripts use `ON CONFLICT DO UPDATE` / `DO NOTHING` — safe to re-run.
Requires env: `XIOSYNC_AUTH_SECRET`, `XIOSYNC_ENVIRONMENT=production`
