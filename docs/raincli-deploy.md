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

The owner then signs in at `https://raincli.com/login` and invites the second pilot account from the **Team** page. The invitation is a single-use link that expires in 7 days and is shared out of band; no email is sent. Each person adds their own machines on the **Machines** page (**Add a machine**) and downloads each machine's config once.

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

### Upgrading to the directory release (migration `0004`)

This release adds migration `0004_agent_directory`:
- four nullable columns on `agent_presence`: `client_version`, `update_mode`, `update_state` and `update_error` (a short error code);
- a `machine_agents` table: up to 100 rows per handle, each with an opaque key, name, type, status, the inbox role and reachability, the discovery source and the server receipt time. A handle's rows are **replaced as a whole** by each report that carries an agent list, so the table doesn't grow with traffic. Check constraints and a partial unique index allow at most one inbox per handle, and reachability only on the inbox;
- a `client_targets` table: at most one row per team, holding a version tag, the downgrade flag and `set_at`.

None of these hold paths, working directories, prompts, titles, transcripts, pane ids or process ids; the server rejects any report that tries to send them. `install.sh` takes the `PRE_BACKUP` and migrates as usual. No new secrets, environment variables or Nginx changes are needed. A report is normally a few KiB; 100 agents with 64-character ASCII names stay under 30 KiB, inside the 64 KiB API body limit.

After the upgrade, check with a test machine's credential:
- `PUT /api/v1/presence` with only `{"status": …}` (a v0.2.0 client) still returns 200, now with `"target": null`;
- `GET /api/v1/agents` includes `"machine": null` and `"agents": []` for handles that haven't reported a directory;
- `python -m raincli_server.migrate current` (run like `rc_admin`) shows `0004`.

Existing handles keep working unchanged, and each becomes a machine once its runtime (v0.3.0 or later) reports. Because this release runs a new migration, rolling it back follows checklist item 2 below.

### Client versions (admin CLI)

The operator chooses which client version each team's managed installs run. The server stores and replies with **a version only**; clients install it only from stable releases of the canonical GitHub repository, so the server can't choose where code comes from. Releases are unsigned (see [SETUP.md](../SETUP.md#updates)).

```bash
rc_admin set-client-version --team pilot v0.3.1                    # upsert the team's target; set_at = now()
rc_admin set-client-version --team pilot v0.3.0 --allow-downgrade  # also lets newer machines go back; prints a warning
rc_admin set-client-version --team pilot --clear                   # remove the target; machines keep what they have
rc_admin client-status --team pilot                                # the target, then one line per active handle
```

- The version must be a release tag `vMAJOR.MINOR.PATCH`, **v0.3.0 or later** (the first client that understands targets); `0.3.1` is accepted and stored as `v0.3.1`. Anything else is rejected without changes.
- `--allow-downgrade` is stored with that target only. Setting the target again without it turns downgrades off.
- Every `set-client-version` refreshes `set_at`. A machine that rolled a target back retries it only after the target row changes, so re-running the same command is how you ask for another attempt after fixing the cause.
- Machines act on the target at their next report (within about 30 seconds), and only when their install is managed and set to automatic.
- `client-status` prints tab-separated lines: `target` and the target (or `none`), a header, then `handle`, `version`, `update_mode`, `update_state`, `error` and the last report time. `-` means the handle's client hasn't reported a version (older than v0.3.0, or never run); `never` means no report at all. It never shows keys, agent names or paths.

Publish the release on GitHub before setting it as a target. A target with no matching stable release shows up as `failed` in `client-status`, and the previous version keeps running.

## Webserver client migration (release venv → managed install)

The webserver also runs a RainCLI **client**: a runtime and connector for the operator's own machine handle. Until now that client ran from the **server's release venv** under `/opt/raincli/…`. That couples the client to server deploys: every server install or rollback silently changes the client, pruning a release can break its unit, and it can't take pushed updates. This procedure moves it to a managed install under the client user's home, with a unit that runs the **stable launcher**. The main agent runs it on the host; nothing here was run by its author.

Before you start:
- Deploy the directory release (migration `0004`) first, and publish a v0.3.0-or-later client release on GitHub.
- Do it in a quiet period: the connector stops for up to about 2 minutes, and queued messages wait durably.
- The host needs outbound HTTPS to `api.github.com` and `codeload.github.com` **for the client user**. The server's own sandboxed `raincli.service` is unaffected and keeps its loopback-only IP policy.
- Each step names who runs it: `root`, or the client user `$CU` (the account that owns the client's configs).

### 1. Inventory (read-only)

```bash
CU=<client user>                      # the user that owns ~/.config/raincli/agent.json for the webserver's handle
CH=$(getent passwd "$CU" | cut -d: -f6)                                # its home directory
AS_CU=(sudo -iu "$CU" env XDG_RUNTIME_DIR="/run/user/$(id -u "$CU")")   # reaches the user's systemd manager
"${AS_CU[@]}" systemctl --user list-units 'raincli*' --all; "${AS_CU[@]}" systemctl --user cat raincli-runtime.service
systemctl list-units 'raincli*' --all                                  # a system-level client unit, if that's how it runs
sudo grep -rl 'raincli_agent\|bin/raincli' /etc/systemd/system "$CH/.config/systemd/user" 2>/dev/null
sudo -iu "$CU" bash -lc 'command -v raincli; readlink -f "$(command -v raincli)"; raincli --version; raincli whoami'
loginctl show-user "$CU" -p Linger                                     # a user unit needs Linger=yes on a server
```

Record:
- the unit's name and whether it is a **user** or **system** unit, and its full current text;
- the `ExecStart` interpreter (expected under `/opt/raincli/releases/<release>/venv/` or `/opt/raincli/current/venv/`);
- the absolute paths of `runtime.json`, its `state_dir`, the connector configs and `agent.json`;
- the handle and team from `whoami`.

Stop here if `whoami` fails or the runtime shows connector errors; fix those first.

### 2. Back up the unit and the client configs

```bash
sudo install -d -m 0700 /root/raincli-client-migration
sudo cp -a <unit file path> /root/raincli-client-migration/                     # the file found in step 1
sudo tar -C "$CH" -czf /root/raincli-client-migration/client-config.tgz .config/raincli
sudo chmod 600 /root/raincli-client-migration/*
```

The backup contains the machine credential. Keep it root-only and delete it once the migration is confirmed (step 7).

### 3. Install the managed client (client user)

Use the release venv's client one last time, as a bootstrap only:

```bash
sudo -iu "$CU" /opt/raincli/current/venv/bin/raincli runtime update --install    # -> ~/.raincli/client, latest stable release
sudo -iu "$CU" python3 "$CH/.raincli/client/launch.py" --version                # expect v0.3.0 or later
```

This changes nothing that the running client uses. If it fails (for example, no outbound HTTPS), stop and fix that; the old client keeps running.

### 4. Stop the old client gracefully

Stop it with the **old** client, which knows how its own runtime runs (`<old raincli>` is the release venv's `raincli` from step 1's `ExecStart`):

```bash
sudo -iu "$CU" <old raincli> runtime stop --config <abs runtime.json>
# then disable the old unit so it doesn't restart:
"${AS_CU[@]}" systemctl --user disable --now raincli-runtime.service     # user unit
sudo systemctl disable --now <old system unit>                          # or, if it was a system unit
```

A graceful stop takes up to about 100 seconds. Wait until `<old raincli> runtime status --config <abs runtime.json>` shows `stopped` before disabling the unit, so systemd doesn't kill connectors mid-delivery (the old unit may lack `KillMode=mixed` and the 150-second stop timeout).

### 5. Install the fixed unit

**If the client ran as a user unit** (the normal case), let the managed client write the unit. Run through the launcher, `runtime startup` writes an `ExecStart` that uses the system Python and the stable launcher, never a versioned venv:

```bash
sudo loginctl enable-linger "$CU"      # only if step 1 showed Linger=no; the user manager must run without a login
sudo -iu "$CU" python3 "$CH/.raincli/client/launch.py" runtime run --config <abs runtime.json> --once
"${AS_CU[@]}" python3 "$CH/.raincli/client/launch.py" runtime startup --config <abs runtime.json>
"${AS_CU[@]}" systemctl --user cat raincli-runtime.service
```

The unit must match this shape:

```ini
[Unit]
Description=RainCLI mapped agent runtime

[Service]
Type=simple
ExecStart="/usr/bin/python3" "/home/<client user>/.raincli/client/launch.py" "runtime" "run" "--config" "<abs runtime.json>"
Environment="PATH=…"
Restart=on-failure
RestartSec=10
KillMode=mixed
TimeoutStopSec=150
# config-sha256: …

[Install]
WantedBy=default.target
```

Check that `ExecStart` names `.raincli/client/launch.py` and nothing under `/opt/raincli`, `venv` or `versions/`. `KillMode=mixed` sends the stop signal to the launcher only, which stops the runtime and its connectors gracefully. `TimeoutStopSec=150` covers the launcher's 120-second graceful wait.

**If the client ran as a system unit,** keep its name, `User=` and `Group=`, and replace only these lines (then `systemctl daemon-reload` and `systemctl enable --now <unit>`):

```ini
ExecStart=/usr/bin/python3 /home/<client user>/.raincli/client/launch.py runtime run --config <abs runtime.json>
Environment=HOME=/home/<client user>
KillMode=mixed
TimeoutStopSec=150
Restart=on-failure
RestartSec=10
```

Also point any `raincli` command on the client user's `PATH` that resolves into `/opt/raincli` at the launcher, as in [SETUP.md step 1](../SETUP.md#1-install-the-managed-client-agent). Hooks are not needed on the webserver unless a coding agent runs there.

### 6. Verify

```bash
sudo -iu "$CU" python3 "$CH/.raincli/client/launch.py" runtime status --config <abs runtime.json>    # running
sudo -iu "$CU" python3 "$CH/.raincli/client/launch.py" connector status --config <abs connector.json>
rc_admin client-status --team <slug>        # the webserver's handle: its version, automatic, current
```

Within a minute, the handle's **Machines** page entry shows its client version and agent list. Send a test message to the handle and confirm the reply as usual. Then check that server deploys no longer touch the client: `readlink -f` on everything in the unit's `ExecStart` resolves outside `/opt/raincli`.

### 7. Finish, or roll back

- **Success:** delete `/root/raincli-client-migration` (it holds the credential). The next pushed version (`rc_admin set-client-version`) updates the webserver's client like any other machine.
- **Rollback:** stop and disable the new unit (`runtime startup --remove` for a user unit), restore the backed-up unit file, then `daemon-reload` and `enable --now` it. The old client resumes from the same configs and queues. The managed install under `~/.raincli/client` can stay; nothing uses it until its unit is back.

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
- **Presence and the directory:** each machine's runtime reports every 30 seconds with its own credential (`messages:ack` scope), and each report expires 120 seconds after the server receives it. A report may carry up to 100 agents and the client's version; any invalid entry rejects the whole report. Reads are limited to the caller's team, never return agent keys, and return no agents for revoked handles. Presence and the directory are advisory, not delivery; they don't change any message's state. Presence rows are overwritten in place and directory rows are replaced per report, so neither grows with traffic.
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
