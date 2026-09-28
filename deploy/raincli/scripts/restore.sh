#!/usr/bin/env bash
# Restore a backup, REPLACING everything in the target database's public schema, atomically.
#   RAINCLI_RESTORE_DATABASE_URL=... restore.sh BACKUP.dump      (default target: $RAINCLI_DATABASE_URL)
# The target URL is read from the environment, never argv (it contains the password).
# Stop the service first (systemctl stop raincli) when restoring into the live database, and take a
# fresh backup first. Drop + recreate of schema public and the restore run in ONE transaction: if
# anything fails, the database is left exactly as it was.
set -euo pipefail
dump="${1:?usage: RAINCLI_RESTORE_DATABASE_URL=... restore.sh BACKUP.dump}"
(( $# == 1 )) || { echo "restore.sh takes only the backup path; pass the target URL via RAINCLI_RESTORE_DATABASE_URL" >&2; exit 2; }
target="${RAINCLI_RESTORE_DATABASE_URL:-${RAINCLI_DATABASE_URL:-}}"
[[ -n "$target" ]] || { echo "set RAINCLI_RESTORE_DATABASE_URL (or RAINCLI_DATABASE_URL)" >&2; exit 2; }
[[ -f "$dump" ]] || { echo "no such backup: $dump" >&2; exit 2; }
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=pgenv.sh
. "$here/pgenv.sh" "$target"
pg_restore --list "$dump" >/dev/null
{
  printf 'SET client_min_messages = warning;\nDROP SCHEMA public CASCADE;\nCREATE SCHEMA public;\n'
  pg_restore --no-owner --file=- "$dump"
} | psql -X -q -1 -v ON_ERROR_STOP=1 >/dev/null
psql -X -Atc "SELECT 'restored: ' || (SELECT count(*) FROM messages) || ' messages, schema ' || (SELECT version_num FROM alembic_version)"
