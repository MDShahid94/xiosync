#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
BACKUP_SCRIPT="${SCRIPT_DIR}/backup.sh"
CRON_LINE="0 2 * * * DATABASE_URL=\"\$DATABASE_URL\" BACKUP_DIR=\"\${BACKUP_DIR:-/var/backups/xiosync}\" ${BACKUP_SCRIPT} >> /var/log/xiosync-backup.log 2>&1"

CURRENT_CRONTAB="$(crontab -l 2>/dev/null || true)"
if printf '%s\n' "${CURRENT_CRONTAB}" | grep -Fq "${BACKUP_SCRIPT}"; then
  printf 'Backup cron entry already exists:\n%s\n' "${CRON_LINE}"
  exit 0
fi

printf '%s\n%s\n' "${CURRENT_CRONTAB}" "${CRON_LINE}" | crontab -
printf 'Installed backup cron entry:\n%s\n' "${CRON_LINE}"
