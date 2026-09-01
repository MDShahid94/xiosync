#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/xiosync}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_FILE="${BACKUP_DIR}/xiosync-${TIMESTAMP}.dump"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
finish() {
  local exit_code=$?
  log "Backup exit code: ${exit_code}"
  exit "${exit_code}"
}
trap finish EXIT

if [[ -z "${DATABASE_URL:-}" ]]; then
  printf 'Usage: export DATABASE_URL=postgresql://user:password@host:5432/dbname && %s\n' "$0" >&2
  exit 2
fi

mkdir -p "${BACKUP_DIR}"
log "Starting PostgreSQL backup to ${BACKUP_FILE}"
pg_dump --format=custom --file="${BACKUP_FILE}" "${DATABASE_URL}"
log "Backup finished: ${BACKUP_FILE} ($(du -h "${BACKUP_FILE}" | cut -f1))"
log "Backup exit code: 0"
trap - EXIT
exit 0
