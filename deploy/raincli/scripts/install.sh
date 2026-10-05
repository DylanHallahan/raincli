#!/usr/bin/env bash
# Install or upgrade a RainCLI release ON THE SERVER (run as root).
#   install.sh /path/to/raincli-<rev>.tar.gz
# Does not touch Nginx, DNS or PostgreSQL configuration; see docs/raincli-deploy.md.
# Re-running with the same tarball is safe: a completed release is reused, the migration is a no-op,
# and the first pre-install backup for that release is kept as its rollback point.
set -euo pipefail
tarball="$(readlink -f "${1:?usage: install.sh raincli-<rev>.tar.gz}")"
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
SYSTEMCTL="${RAINCLI_SYSTEMCTL:-systemctl}"   # test hook; always systemctl in production

id raincli >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --no-create-home \
  --shell /usr/sbin/nologin raincli
install -d -o root -g raincli -m 0750 /etc/raincli
[[ -f /etc/raincli/raincli.env ]] || {
  echo "missing /etc/raincli/raincli.env (copy deploy/raincli/raincli.env.example and fill it in)" >&2; exit 1; }
chown root:raincli /etc/raincli/raincli.env && chmod 0640 /etc/raincli/raincli.env
install -d -o root -g root -m 0755 /opt/raincli /opt/raincli/releases
install -d -o raincli -g raincli -m 0700 /var/backups/raincli

as_raincli() {  # run a command as the service user with the environment file loaded
  # cd / first: the operator's cwd (e.g. a 0700 home) is not readable by the service user.
  ( cd / && runuser -u raincli -- env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/nonexistent \
    bash -c 'cd / && set -a && . /etc/raincli/raincli.env && set +a && exec "$@"' raincli-env "$@" )
}
cd /

name="$(basename "$tarball" .tar.gz)"
dest="/opt/raincli/releases/$name"
if [[ ! -f "$dest/.complete" ]]; then
  # Build in place at the final path: venvs are not relocatable.
  rm -rf -- "$dest"
  install -d -o root -g root -m 0755 "$dest"
  tar -xzf "$tarball" -C "$dest" --strip-components=1 --no-same-owner
  python3 -m venv "$dest/venv"
  pip=("$dest/venv/bin/python" -m pip --disable-pip-version-check --no-cache-dir --no-input)
  "${pip[@]}" install --quiet --progress-bar off --upgrade pip
  "${pip[@]}" install --quiet --progress-bar off --require-hashes --only-binary=:all: -r "$dest/raincli/requirements.lock"
  "${pip[@]}" install --quiet --progress-bar off --no-deps "$dest/raincli"
  rm -rf -- "$dest/raincli/build"
  chown -R root:root "$dest"
  chmod -R u=rwX,go=rX "$dest"
  touch "$dest/.complete"
fi
"$dest/venv/bin/python" -c 'import raincli_server, raincli_agent' >/dev/null

# Rollback point: back up before the first migration of this release; keep that path.
if [[ ! -s "$dest/PRE_BACKUP" ]]; then
  pre="$(as_raincli env RAINCLI_BACKUP_DIR=/var/backups/raincli RAINCLI_BACKUP_TAG="pre-$name" \
    "$dest/deploy/raincli/scripts/backup.sh")"
  printf '%s\n' "$pre" > "$dest/PRE_BACKUP"
  chmod 0644 "$dest/PRE_BACKUP"
fi
as_raincli "$dest/venv/bin/python" -m raincli_server.migrate upgrade head

previous=""; [[ -L /opt/raincli/current ]] && previous="$(readlink -f /opt/raincli/current)"
ln -sfn "$dest" /opt/raincli/current.new && mv -T /opt/raincli/current.new /opt/raincli/current
if [[ -n "$previous" && "$previous" != "$dest" ]]; then echo "$previous" > /opt/raincli/previous; fi

install -m 0644 "$dest/deploy/raincli/systemd/raincli.service" /etc/systemd/system/raincli.service
install -m 0644 "$dest/deploy/raincli/systemd/raincli-backup.service" /etc/systemd/system/raincli-backup.service
install -m 0644 "$dest/deploy/raincli/systemd/raincli-backup.timer" /etc/systemd/system/raincli-backup.timer
$SYSTEMCTL daemon-reload
$SYSTEMCTL enable --now raincli-backup.timer
$SYSTEMCTL enable raincli.service
$SYSTEMCTL restart raincli.service
"$dest/deploy/raincli/scripts/healthcheck.sh" http://127.0.0.1:8300
echo "installed $name (previous: ${previous:-none}; rollback backup: $(cat "$dest/PRE_BACKUP"))"
