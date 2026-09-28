#!/usr/bin/env bash
# Start (or reuse) a loopback-only PostgreSQL 17 container for RainCLI tests.
# The admin URL is written to ~/.config/raincli-dev/test-pg.env (0600), outside the repo.
# Test runs create and drop their own databases inside it. Never point tests at production.
set -euo pipefail
name=raincli-test-pg
dir="$HOME/.config/raincli-dev"
env_file="$dir/test-pg.env"
mkdir -p "$dir" && chmod 700 "$dir"
if ! docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
  pw="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  umask 077
  printf 'RAINCLI_TEST_DATABASE_URL=postgresql://raincli:%s@127.0.0.1:55432/postgres\n' "$pw" > "$env_file"
  timeout 600 docker run -d --name "$name" --restart unless-stopped \
    -e POSTGRES_USER=raincli -e POSTGRES_PASSWORD="$pw" \
    -p 127.0.0.1:55432:5432 postgres:17-alpine >/dev/null
fi
docker start "$name" >/dev/null
for _ in $(seq 1 30); do
  docker exec "$name" pg_isready -U raincli -q && break
  sleep 1
done
echo "ready; run: set -a; . $env_file; set +a"
