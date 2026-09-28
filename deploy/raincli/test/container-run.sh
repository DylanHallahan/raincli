#!/usr/bin/env bash
# Runs INSIDE postgres:17-bookworm + Python 3.11 copied in (root). /work holds the tarballs and the env file.
set -uo pipefail
fail() { echo "INSTALL-TEST FAIL: $*"; exit 1; }
export DEBIAN_FRONTEND=noninteractive
command -v pg_dump >/dev/null || fail "pg_dump missing in test image"
if ! command -v curl >/dev/null; then   # test-harness shim for healthcheck.sh's GET (host has real curl)
  cat > /usr/local/bin/curl <<'SHIM'
#!/usr/local/bin/python3
import sys, urllib.request
url = [a for a in sys.argv[1:] if a.startswith("http")][-1]
try:
    with urllib.request.urlopen(url, timeout=5) as r: sys.stdout.write(r.read().decode())
except Exception: sys.exit(22)
SHIM
  chmod +x /usr/local/bin/curl
fi
export PATH=/usr/local/bin:$PATH

# systemctl stub: on restart, (re)start uvicorn exactly as the unit's ExecStart, as User=raincli.
cat > /usr/local/bin/fake-systemctl <<'STUB'
#!/usr/bin/env bash
echo "systemctl $*" >> /work/systemctl.log
if [[ "$1" == restart && "$2" == raincli.service ]]; then
  [[ -f /work/uvicorn.pid ]] && kill "$(cat /work/uvicorn.pid)" 2>/dev/null && sleep 1
  cmd="$(sed -n '/^ExecStart=/,/[^\\]$/p' /etc/systemd/system/raincli.service | sed 's/^ExecStart=//; s/\\$//' | tr -d '\n')"
  runuser -u raincli -- env -i PATH=/usr/bin:/bin bash -c "set -a; . /etc/raincli/raincli.env; set +a; cd /opt/raincli/current; exec $cmd" \
    > /work/uvicorn.log 2>&1 &
  echo $! > /work/uvicorn.pid
fi
exit 0
STUB
chmod +x /usr/local/bin/fake-systemctl
export RAINCLI_SYSTEMCTL=/usr/local/bin/fake-systemctl

mkdir -p /etc/raincli && cp /work/raincli.env /etc/raincli/raincli.env
A=/work/raincli-a.tar.gz; B=/work/raincli-b.tar.gz

echo "== first install (invoked from a 0700 operator directory the service user cannot read)"
install -d -m 0700 /root/operator-home
( cd /root/operator-home && bash /work/src/deploy/raincli/scripts/install.sh "$A" ) || fail "first install from 0700 cwd"
stat -c '%a %U:%G %n' /etc/raincli /etc/raincli/raincli.env /opt/raincli/releases/raincli-a | sed 's/^/   /'
head -1 /opt/raincli/current/venv/bin/uvicorn | grep -q "/opt/raincli/releases/raincli-a/venv/bin/python" \
  || fail "uvicorn shebang not at final path"
runuser -u raincli -- test -r /opt/raincli/current/deploy/raincli/scripts/backup.sh || fail "release unreadable by raincli"
curl -fsS http://127.0.0.1:8300/api/v1/health | grep -q '"ok":true' || fail "health"
PRE1="$(cat /opt/raincli/releases/raincli-a/PRE_BACKUP)"; [[ -s "$PRE1" ]] || fail "no pre-backup"

echo "== re-run same release (idempotent; keeps first pre-backup)"
bash /work/src/deploy/raincli/scripts/install.sh "$A" >/dev/null || fail "re-run"
[[ "$(cat /opt/raincli/releases/raincli-a/PRE_BACKUP)" == "$PRE1" ]] || fail "PRE_BACKUP changed on re-run"

echo "== operator bootstrap as the service user (password on stdin)"
as() { runuser -u raincli -- env -i PATH=/usr/local/bin:/usr/bin:/bin bash -c 'set -a; . /etc/raincli/raincli.env; set +a; cd /opt/raincli/current; exec "$@"' x "$@"; }
printf 'install test password\n' | as venv/bin/raincli-admin create-user --email owner@example.test --name Owner || fail "create-user"
as venv/bin/raincli-admin create-team --slug pilot --name Pilot --owner owner@example.test >/dev/null || fail "create-team"
as venv/bin/raincli-admin list-agents --team pilot >/dev/null || fail "list-agents"
curl -fsS http://127.0.0.1:8300/ | grep -qi "raincli" || fail "public page"

echo "== upgrade to release B, then rollback"
bash /work/src/deploy/raincli/scripts/install.sh "$B" >/dev/null || fail "upgrade"
[[ "$(readlink -f /opt/raincli/current)" == /opt/raincli/releases/raincli-b ]] || fail "current not B"
( cd /root/operator-home && bash /work/src/deploy/raincli/scripts/rollback.sh ) || fail "rollback from 0700 cwd"
[[ "$(readlink -f /opt/raincli/current)" == /opt/raincli/releases/raincli-a ]] || fail "rollback target"
curl -fsS http://127.0.0.1:8300/api/v1/health | grep -q '"ok":true' || fail "health after rollback"

echo "== restore the pre-install backup into a scratch database as the service user"
ls -la /var/backups/raincli | sed 's/^/   /'
as bash -c 'S=/opt/raincli/current/deploy/raincli/scripts; out=$("$S/backup.sh") && "$S/restore.sh" "$out"' || fail "backup+restore as service user"
grep -q "previous" /work/systemctl.log; [[ "$(cat /opt/raincli/previous)" == /opt/raincli/releases/raincli-b ]] || fail "previous pointer after rollback"
kill "$(cat /work/uvicorn.pid)" 2>/dev/null
echo "INSTALL-TEST PASS"
