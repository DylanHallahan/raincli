#!/usr/bin/env bash
# healthcheck.sh [BASE_URL]  (default https://raincli.com). Exit 0 when healthy.
set -euo pipefail
base="${1:-https://raincli.com}"
for i in $(seq 1 20); do
  if body="$(curl -fsS --max-time 5 --proto '=https,http' --max-redirs 0 "$base/api/v1/health" 2>/dev/null)"; then
    echo "$body"
    [[ "$body" == *'"ok":true'* ]] && exit 0
  fi
  sleep 1
done
echo "unhealthy: $base" >&2
exit 1
