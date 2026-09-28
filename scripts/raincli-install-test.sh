#!/usr/bin/env bash
# Containerized test of deploy/raincli/scripts/install.sh, upgrade, rollback, backup and restore,
# as a non-root service user on Debian bookworm + Python 3.11. systemd is stubbed; the database is a
# throwaway database on the local TEST PostgreSQL (scripts/test-postgres.sh). Never production.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a; . "$HOME/.config/raincli-dev/test-pg.env"; set +a
: "${RAINCLI_TEST_DATABASE_URL:?}"
work="$(mktemp -d)"; trap 'rm -rf -- "${work:?}"; psql "$RAINCLI_TEST_DATABASE_URL" -qc "DROP DATABASE IF EXISTS raincli_install_test" 2>/dev/null' EXIT
chmod 700 "$work"
tar_path="$(cd "$root" && deploy/raincli/scripts/build-release.sh HEAD | awk '{print $2}')"
cp "$root/$tar_path" "$work/raincli-a.tar.gz"; cp "$root/$tar_path" "$work/raincli-b.tar.gz"
mkdir "$work/src"; tar -xzf "$work/raincli-a.tar.gz" -C "$work/src" --strip-components=1
cp "$root/deploy/raincli/test/container-run.sh" "$work/run.sh"
psql "$RAINCLI_TEST_DATABASE_URL" -qc "DROP DATABASE IF EXISTS raincli_install_test" -c "CREATE DATABASE raincli_install_test"
base="${RAINCLI_TEST_DATABASE_URL%/*}"
( umask 077; printf 'RAINCLI_DATABASE_URL=%s/raincli_install_test\nRAINCLI_SECRET_KEY=%s\nRAINCLI_PUBLIC_URL=https://raincli.com\n' \
    "${base/postgresql:/postgresql+psycopg:}" "$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" > "$work/raincli.env" )
docker build -q -t raincli-install-test "$root/deploy/raincli/test" >/dev/null
docker run --rm --network host --entrypoint bash -v "$work":/work raincli-install-test /work/run.sh
