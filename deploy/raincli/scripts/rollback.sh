#!/usr/bin/env bash
# Roll back to the previous release directory (run as root). Schema downgrades are NOT automatic:
# if the release being abandoned ran new migrations, restore its PRE_BACKUP first
# (see "Rollback checklist" in docs/raincli-deploy.md).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
SYSTEMCTL="${RAINCLI_SYSTEMCTL:-systemctl}"   # test hook; always systemctl in production
cd /
prev="$(cat /opt/raincli/previous 2>/dev/null || true)"
[[ -n "$prev" && -f "$prev/.complete" ]] || { echo "no complete previous release recorded" >&2; exit 1; }
cur="$(readlink -f /opt/raincli/current)"
ln -sfn "$prev" /opt/raincli/current.new && mv -T /opt/raincli/current.new /opt/raincli/current
echo "$cur" > /opt/raincli/previous
$SYSTEMCTL restart raincli.service
"$prev/deploy/raincli/scripts/healthcheck.sh" http://127.0.0.1:8300
echo "rolled back to $prev (the abandoned release's rollback backup: $(cat "$cur/PRE_BACKUP" 2>/dev/null || echo none))"
