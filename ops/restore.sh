#!/usr/bin/env bash
set -euo pipefail

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if [[ -z "${DATABASE_URL:-}" || $# -ne 1 ]]; then
  printf 'Usage: DATABASE_URL=postgresql://user:password@host:5432/dbname %s /path/to/backup.dump\n' "$0" >&2
  exit 2
fi

DUMP_FILE="$1"
[[ -f "${DUMP_FILE}" ]] || { log "Dump file does not exist: ${DUMP_FILE}" >&2; exit 1; }

read -r DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD < <(python3 - <<'PY'
import os
from urllib.parse import unquote, urlparse
url = urlparse(os.environ["DATABASE_URL"])
print(
    url.hostname or "localhost",
    url.port or 5432,
    (url.path or "/").lstrip("/") or "postgres",
    unquote(url.username or ""),
    unquote(url.password or ""),
)
PY
)
export PGHOST="$DB_HOST" PGPORT="$DB_PORT" PGUSER="$DB_USER" PGPASSWORD="$DB_PASSWORD"
MAINTENANCE_DB="postgres"

log "Starting restore drill from ${DUMP_FILE}"
log "Dropping target database ${DB_NAME}"
psql --dbname="${MAINTENANCE_DB}" --set=ON_ERROR_STOP=1 --command="DROP DATABASE IF EXISTS \"${DB_NAME//\"/\"\"}\";"
log "Recreating target database ${DB_NAME}"
psql --dbname="${MAINTENANCE_DB}" --set=ON_ERROR_STOP=1 --command="CREATE DATABASE \"${DB_NAME//\"/\"\"}\";"
log "Restoring dump into ${DB_NAME}"
pg_restore --exit-on-error --dbname="${DB_NAME}" "${DUMP_FILE}"
log "Restore drill completed successfully"
