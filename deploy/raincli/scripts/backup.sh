#!/usr/bin/env bash
# pg_dump (custom format) of the RainCLI database. Reads RAINCLI_DATABASE_URL from the environment;
# credentials are passed to pg_dump via libpq environment variables, never argv.
# Keeps the newest $RAINCLI_BACKUP_KEEP daily and $RAINCLI_BACKUP_KEEP_PRE pre-install backups.
# Backups contain message bodies and attachments in plaintext: keep the directory private.
set -euo pipefail
: "${RAINCLI_DATABASE_URL:?}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=pgenv.sh
. "$here/pgenv.sh" "$RAINCLI_DATABASE_URL"
dir="${RAINCLI_BACKUP_DIR:-/var/backups/raincli}"
keep="${RAINCLI_BACKUP_KEEP:-14}"
keep_pre="${RAINCLI_BACKUP_KEEP_PRE:-5}"
tag="${RAINCLI_BACKUP_TAG:-daily}"
[[ "$tag" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "invalid backup tag" >&2; exit 2; }
umask 077
mkdir -p "$dir"
out="$dir/raincli-$tag-$(date -u +%Y%m%dT%H%M%SZ).dump"
trap 'rm -f -- "$out.partial"' EXIT
pg_dump --format=custom --no-owner --file="$out.partial"
pg_restore --list "$out.partial" >/dev/null   # verify the archive is readable
mv -- "$out.partial" "$out"
prune() {  # prune GLOB KEEP: delete all but the newest KEEP matching files
  find "$dir" -maxdepth 1 -name "$1" -printf '%T@ %p\n' | sort -rn | tail -n +"$(( $2 + 1 ))" \
    | cut -d' ' -f2- | xargs -r -d '\n' rm -f --
}
prune 'raincli-daily-*.dump' "$keep"
prune 'raincli-pre-*.dump' "$keep_pre"
find "$dir" -maxdepth 1 -name 'raincli-*.dump.partial' -mmin +60 -delete
echo "$out"
