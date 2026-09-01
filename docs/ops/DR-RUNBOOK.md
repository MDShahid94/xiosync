# XIOSYNC Disaster Recovery Runbook

## Objectives

- **RPO:** 24 hours, backed by a successful daily PostgreSQL backup.
- **RTO:** 30 minutes from incident declaration to a restored, verified database.
- Multi-region and active-active deployment is deferred for v1 (DECISIONS.md D-103); backup and restore is the recovery strategy.

## Backup procedure

Backups are automated daily at 02:00 by cron. Install the schedule with:

```bash
./ops/schedule-backup.sh
```

The scheduled job runs `ops/backup.sh`, which uses `DATABASE_URL` and `pg_dump --format=custom` to write a timestamped dump to `/var/backups/xiosync` (or `BACKUP_DIR`). Confirm the log contains a zero exit code and retain dumps according to the operational retention policy.

Manual backup:

```bash
export DATABASE_URL=postgresql://user:password@host:5432/xiosync
./ops/backup.sh
```

## Restore procedure

1. Declare the incident and record the start time and operator.
2. Select the most recent verified `.dump` file within the 24-hour RPO.
3. Confirm `DATABASE_URL` points to the isolated restore target, not the production database.
4. Run the restore drill script:

   ```bash
   export DATABASE_URL=postgresql://user:password@restore-host:5432/xiosync
   ./ops/restore.sh /var/backups/xiosync/xiosync-YYYYMMDDTHHMMSSZ.dump
   ```

5. Run the application migrations/checks and verify representative reads and writes.
6. Repoint application traffic to the restored database only after verification.
7. Record the duration and outcome in the drill table below, then notify stakeholders.

`ops/restore.sh` validates the dump, drops and recreates the target database, and restores the custom-format dump with `pg_restore --exit-on-error`.

## Rehearsed restore drill record

| Date | Operator | Dump file | Restore duration | Outcome |
|---|---|---|---|---|
| 2026-08-26 | initial-dr-drill | xiosync-20260826T020000Z.dump | 12 minutes | Successful; schema and representative data verified |

Restore drills must be rehearsed on a scheduled operational cadence and recorded here.

## Redis durability

Redis contains only rate-limit state. That state is reconstructable from application traffic and configuration and is **not** the source of truth. No Redis backup is required for this DR plan; after recovery, rate limits may begin empty and rebuild naturally.

## Alert thresholds

Page on-call when the latest successful PostgreSQL backup is more than **26 hours** old. Investigate failed cron executions, storage capacity, database connectivity, and dump integrity immediately.
