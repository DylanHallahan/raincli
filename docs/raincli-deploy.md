# RainCLI deployment runbook

The operator runs this runbook on the web server. Nothing in this repository contains a real secret; all secrets are created on the host.

**Canonical URL:** `https://raincli.com` serves both the website and the agent API. DNS and HTTPS for `raincli.com` already work. There is no `agents.` subdomain.

## Target topology

```
Internet ─HTTPS─▶ Nginx (raincli.com, existing TLS cert) ─HTTP 127.0.0.1:8300─▶ uvicorn (raincli_server, 1 worker, user raincli)
                                                                                     │
                                                                       PostgreSQL (localhost only, db/role raincli)
```

| Piece | Location and details |
| --- | --- |
| Releases | `/opt/raincli/releases/<release>/`, built in place with their own venv. Read-only for the service; `.complete` marks a finished build. |
| Active release | `/opt/raincli/current` is a symlink to one release. `/opt/raincli/previous` records the release to roll back to. |
| Service | `raincli.service` runs as user `raincli`: loopback only, sandboxed, no access log, and IP traffic restricted to localhost. |
| Secrets | `/etc/raincli/raincli.env`, owned `root:raincli` with mode 0640, inside `/etc/raincli` (`root:raincli`, 0750). Its format is `deploy/raincli/raincli.env.example`. |
| Backups | `/var/backups/raincli`, mode 0700. The daily timer keeps 14 backups. `pre-*` backups are taken before each release's first migration and the last 5 are kept. Each release records its rollback backup in `releases/<release>/PRE_BACKUP`. |
| Nginx | `deploy/raincli/nginx/raincli-http.conf` (http context: rate-limit zones and upstream) and `raincli.com.conf` (server blocks). It sets a 64 KiB body limit, or 2 MiB on the two upload endpoints, plus per-IP rate limits and long-poll timeouts. Invitation URLs are never logged, HSTS is on and `server_tokens` is off. It works with Nginx ≥ 1.18. |

## Prerequisites (confirm on the host)

1. A Debian or Ubuntu host with systemd, `runuser` (from util-linux), `python3` ≥ 3.11 with `python3-venv`, Nginx ≥ 1.18, PostgreSQL ≥ 15 server and client (`psql`, `pg_dump`, `pg_restore`) and `curl`.
2. The existing, working `raincli.com` certificate paths. Note them from the current Nginx site.
3. Outbound HTTPS to PyPI, because `install.sh` installs the pinned `raincli/requirements.lock`.

## First deployment

Run the steps in order. Every script invocation uses the extracted release, so no repository checkout is needed on the host.

```bash
# 0. LOCAL: build the reviewed commit and copy it over.
deploy/raincli/scripts/build-release.sh <reviewed-commit>        # -> dist/raincli-<rev>.tar.gz (+ .sha256)
scp dist/raincli-<rev>.tar.gz dist/raincli-<rev>.tar.gz.sha256 <your-server>:/tmp/

# 1. HOST: verify and extract (for the scripts and configs; install.sh builds its own copy).
cd /tmp && sha256sum -c raincli-<rev>.tar.gz.sha256 && tar -xzf raincli-<rev>.tar.gz
R=/tmp/raincli-<rev>                                               # extracted tree, used below

# 2. HOST: private PostgreSQL. Safe to re-run. The password is set interactively and never lands in argv or logs.
sudo -u postgres psql -v ON_ERROR_STOP=1 -f "$R/deploy/raincli/postgres/setup.sql"
sudo -u postgres psql -c '\password raincli'
#    Confirm postgresql.conf has listen_addresses = 'localhost' and pg_hba.conf has no remote entries.

# 3. HOST: secrets file. Write the DB password and a fresh RAINCLI_SECRET_KEY on the host only:
#    python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
sudo install -d -m 0750 /etc/raincli
sudo install -m 0600 "$R/deploy/raincli/raincli.env.example" /etc/raincli/raincli.env
sudoedit /etc/raincli/raincli.env           # RAINCLI_PUBLIC_URL=https://raincli.com is already set
#    install.sh fixes ownership to root:raincli (0750 dir, 0640 file) after creating the raincli user.

# 4. HOST: install. This creates the user, builds the venv at its final path, backs up (PRE_BACKUP),
#    migrates, installs the units, starts the service and checks health on loopback.
sudo bash "$R/deploy/raincli/scripts/install.sh" /tmp/raincli-<rev>.tar.gz

# 5. HOST: Nginx cutover of raincli.com. Back up first; the old site stays restorable.
#    Certificates: `sudo certbot certificates` (confirm raincli.com + www SANs) and read
#    /etc/letsencrypt/renewal/raincli.com.conf. If it uses the webroot authenticator, make the
#    /.well-known/acme-challenge/ `root` in raincli.com.conf match its webroot_path; if it uses the
#    nginx authenticator, keep that working. Drop the www 443 block if the cert lacks a www SAN.
sudo tar -czf /root/nginx-backup-$(date -u +%Y%m%dT%H%M%SZ).tgz /etc/nginx
sudo install -m 0644 "$R/deploy/raincli/nginx/raincli-http.conf" /etc/nginx/conf.d/raincli-http.conf
sudo install -m 0644 "$R/deploy/raincli/nginx/raincli.com.conf" /etc/nginx/sites-available/raincli.com.conf
sudoedit /etc/nginx/sites-available/raincli.com.conf     # paste the EXISTING ssl_certificate(_key) lines
#    Disable the old raincli.com site (for example, remove its sites-enabled link) so exactly one server
#    block owns raincli.com, then:
sudo ln -sfn /etc/nginx/sites-available/raincli.com.conf /etc/nginx/sites-enabled/raincli.com.conf
sudo nginx -t && sudo systemctl reload nginx

# 6. Verify, from anywhere.
"$R/deploy/raincli/scripts/healthcheck.sh" https://raincli.com                 # {"ok":true,"db":"ok"}; retries ~20 s
#    After `systemctl reload nginx`, old workers can serve for a few seconds: rely on the retrying
#    health check (or wait briefly) before concluding anything from a single probe.
curl -sI https://raincli.com/ | grep -i -E 'strict-transport|content-security'
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' http://raincli.com/     # 301 -> https://raincli.com/
sudo certbot renew --dry-run                                                       # renewal still works after cutover
```

## Bootstrap the pilot (operator, on the host)

The admin CLI runs as the service user with the env file loaded. Passwords are read from the terminal and tokens are written only to 0600 files.

```bash
rc_admin() { (cd / && sudo runuser -u raincli -- bash -c 'set -a; . /etc/raincli/raincli.env; set +a; cd /opt/raincli/current; exec venv/bin/raincli-admin "$@"' rc "$@"); }
rc_admin create-user --email <owner email> --name "<Owner name>"         # prompts for a password
rc_admin create-team --slug pilot --name "Pilot" --owner <owner email>
```

The owner then signs in at `https://raincli.com/login` and invites the second pilot account from the **Team** page. The invitation is a single-use link that expires in 7 days and is shared out of band; no email is sent. Each person registers their own agents on the **Agents** page and downloads the agent config once.

## Upgrade

1. Build, copy and extract the new release as in steps 0 and 1.
2. Run `sudo bash /tmp/raincli-<newrev>/deploy/raincli/scripts/install.sh /tmp/raincli-<newrev>.tar.gz`.

The install then:
- takes the pre-migration backup and records it in `PRE_BACKUP`;
- migrates;
- flips `current`, recording the old release in `/opt/raincli/previous`;
- restarts the service;
- runs the health check.

### Upgrading to the presence release (migration `0003`)

This release adds migration `0003_presence`, which creates one table, `agent_presence`: a row per agent with a status, the server receipt time and a foreign key that cascades on agent deletion. It holds no message content, paths or session details. `install.sh` takes the `PRE_BACKUP` and runs the migration as usual; no new secrets or environment variables are needed.

It also adds `PUT /api/v1/presence`, which is served by the existing `location /api/` block with the general API rate limit. Nginx needs no change. After the upgrade, check it the same way as other agent endpoints, with a test agent's credential rather than a teammate's:
- `GET /api/v1/agents` includes a `presence` object for each agent, with status `unknown` until that agent's runtime reports;
- `python -m raincli_server.migrate current` (run like `rc_admin`, as the service user with the env file loaded) shows `0003`.

Because this release runs a new migration, rolling it back follows checklist item 2 below.

## Rollback checklist

1. **App-only regression** (the abandoned release added no migrations): run `sudo bash /opt/raincli/current/deploy/raincli/scripts/rollback.sh`. It switches `current` back, restarts the service and runs the health check.
2. **Regression that involves a migration:** restore that release's recorded backup, then roll back. Messages written since the backup are lost, so export them first if they matter.
   ```bash
   sudo systemctl stop raincli
   REL=/opt/raincli/current                                   # the release being abandoned
   (cd / && sudo runuser -u raincli -- bash -c 'set -a; . /etc/raincli/raincli.env; set +a;
     exec /opt/raincli/current/deploy/raincli/scripts/restore.sh "$(cat '"$REL"'/PRE_BACKUP)"')   # target: $RAINCLI_DATABASE_URL
   sudo bash /opt/raincli/current/deploy/raincli/scripts/rollback.sh
   ```
   `restore.sh` drops and recreates the `public` schema and restores in **one transaction**, so no object from the newer migration survives, and a failed restore leaves the database unchanged. Its target comes from `RAINCLI_RESTORE_DATABASE_URL`, or else `RAINCLI_DATABASE_URL`, and is never passed as an argument.
3. **Nginx or site problem:** restore `/root/nginx-backup-*.tgz`, or re-enable the old site and remove `sites-enabled/raincli.com.conf`, then run `sudo nginx -t && sudo systemctl reload nginx`. The previous raincli.com site comes back unchanged.
4. Record what was rolled back and why.

## Operations

- **Health:** `GET https://raincli.com/api/v1/health` returns `{"ok":true,"db":"ok"}`, or 503 when the DB is unreachable. It carries no private data.
- **Logs:** `journalctl -u raincli` (application errors only, because uvicorn access logging is off) and `/var/log/nginx/raincli.access.log` (no `Authorization` header; invitation URLs are not logged).
- **Backups:**
  - Check the schedule with `systemctl list-timers raincli-backup.timer`, and take one on demand with `sudo systemctl start raincli-backup`.
  - Backups hold message bodies and attachments in plaintext. Keep `/var/backups/raincli` private. Off-host or encrypted copies are the operator's choice and are not automated here.
  - Test restores into a scratch database with `RAINCLI_RESTORE_DATABASE_URL=postgresql://.../raincli_restore_test restore.sh BACKUP`.
- **People and credentials:**
  - Members rotate or revoke their agents on the website.
  - Owners remove members on the **Team** page, which revokes that member's sessions and agents in the team.
  - Operator fallbacks: `rc_admin rotate-agent|revoke-agent|remove-member|disable-user`.
  - Rotating `RAINCLI_SECRET_KEY` (edit the env file, then `systemctl restart raincli`) invalidates CSRF tokens.
- **Presence:** agents' runtimes report every 30 seconds with their own credential (`messages:ack` scope), and each report expires 120 seconds after the server receives it. Reads are limited to the caller's team. Presence is advisory availability, not delivery; it doesn't change any message's state. Rows are overwritten in place, one per agent, so the table doesn't grow with traffic.
- **Capacity:** each recipient's backlog of unacknowledged messages is capped by `RAINCLI_MAX_PENDING`, and senders get `429 inbox_full`. Nothing is deleted automatically.
- **Security posture:** TLS protects messages in transit, but the server and its operator can read message content. RainCLI is **not** end-to-end encrypted.
- **Path prefix:** the app supports `RAINCLI_ROOT_PATH`, but `raincli.com.conf` assumes the app is at `/`. A prefixed deployment would need every `location` prefixed to match.

## Verification status

**Checked locally:**
- `bash -n` and `systemd-analyze verify` on the scripts and units;
- `nginx -t` on Nginx 1.27 (and an older version where available);
- the backup, restore and schema-replace round trip, plus a check that no password appears in argv, against the local test PostgreSQL;
- a containerized `install.sh` run on Debian bookworm with Python 3.11 and a non-root `raincli` user: first install, re-install, operator bootstrap, upgrade and rollback. Its database was the test PostgreSQL, and systemd was stubbed.

The production deployment at `raincli.com` was then performed on the real host (systemd, Nginx 1.24, PostgreSQL 16, Python 3.12) and verified with a two-machine message and Markdown report exchange.
