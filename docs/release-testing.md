# Real-release update testing

`scripts/windows-release-update-e2e.py` checks what synthetic smoke tests can't: a **published** GitHub release installed through the product's real updater, then a pushed upgrade and an explicit downgrade, all driven by a server's client target. The server is a **throwaway one started by the script itself**. The script never uses a production server, secret or team, and it never registers anything on the pilot team.

The updater has no source override. It fetches only stable releases of the canonical `DylanHallahan/raincli` from `api.github.com` and `codeload.github.com`, so the releases under test must be published there first.

## What it does
1. **Database.** It uses a disposable PostgreSQL on loopback:
   - a fresh `initdb` cluster on a random port, with a random password (the Windows runner's preinstalled PostgreSQL through `PGBIN`, or any `initdb`/`pg_ctl` found on `PATH` or under `/usr/lib/postgresql/*/bin`);
   - or, if `RAINCLI_TEST_DATABASE_URL` is set, a throwaway database on that local test server (`scripts/test-postgres.sh`), dropped at the end.
2. **Server.** It starts the server from this checkout in a new venv built from `raincli/requirements.lock` (without `uvloop` on Windows), migrates, and runs uvicorn on `127.0.0.1` with a random `RAINCLI_SECRET_KEY`.
3. **Enrolment.** It enrols a disposable user, the team `release-e2e` and the machine `e2e-machine` with `raincli-admin`. The machine config is written to the temporary directory.
4. **Managed install.** It installs `from_version` **managed** through the real updater (`updates.resolve` and `updates.install` for that exact tag), then checks the launcher reports that version and the mode is `automatic`.
5. **Runtime.** It starts the runtime through the managed launcher with one connector (Herdr deliberately absent, so no real session is touched), a private `HOME`, and no inherited `HERDR_*` or `PYTHONPATH`.
6. **Pushed upgrade.** It runs `set-client-version --team release-e2e <to_version>`, then waits, bounded at 15 minutes per phase, until:
   - the pointer names `to_version`;
   - `status.json` reports `to_version` as `current`, from a process running the pointer's interpreter;
   - the connector has reported, and the server's `GET /api/v1/agents` shows the machine at `to_version`, `current`.
7. **Downgrade** (unless `--no-downgrade`):
   - pushing `from_version` without `--allow-downgrade` must be reported as `failed`/`downgrade_not_allowed`, with the version unchanged a report later;
   - with `--allow-downgrade`, the machine must go back to `from_version`, `current`;
   - then `--clear` removes the target.
8. **Output and cleanup.** It prints a `PASS:` line per phase and `RELEASE UPDATE E2E PASSED` or `FAILED`. On failure it prints the status, pointer, update state, launcher, runtime, connector, server and PostgreSQL logs, with every generated secret and token-shaped string redacted. It always stops the runtime, server and database, and deletes the temporary directory (unless `--keep`).

## Running it after publishing releases (main agent)
1. Publish two stable releases, `v0.3.0` and then `v0.3.1` (non-draft, non-prerelease, tags on the intended commits). The client's `__version__` in each must equal its tag, or the updater's verification refuses it.
2. **Windows:**
   - In GitHub, run **Actions → Manual Windows real-release update → Run workflow** with the defaults (`v0.3.0`, `v0.3.1`, downgrade on).
   - It is `workflow_dispatch` only, on `windows-2022`, with `contents: read` and no secrets.
   - Its inputs reach the script as environment variables, never as shell text, and the script validates them as tags.
3. **Linux, locally:**
   ```bash
   set -a; . ~/.config/raincli-dev/test-pg.env; set +a            # or put initdb/pg_ctl on PATH instead
   python3 scripts/windows-release-update-e2e.py --from-version v0.3.0 --to-version v0.3.1
   # faster, reusing a venv that already has raincli/requirements.lock installed:
   python3 scripts/windows-release-update-e2e.py --server-python raincli/.venv/bin/python
   ```
   A local run lists your own coding-agent processes through the process scan, but reports them only to the throwaway server, which is deleted at the end.
4. Record the job's `PASS` lines and run URL in the release notes.

## Releases used

`v0.3.0` is the first target-aware release. `v0.3.1` is a docs-only release: it changes only this note and the version number, and exists to test a real pushed upgrade and an explicit downgrade.

## Before the releases exist
The script stops after enrolment with:

```
FAIL: release v0.3.0 not found in DylanHallahan/raincli (publish it as a stable release first)
```

It exits 1 after cleaning up. That is the expected result until `v0.3.0` is published.

## Limits
- GitHub's anonymous API limit (60 requests an hour per IP) applies, because the updater never sends credentials. A run makes about six API requests. On a shared runner IP, a `403` or `429` shows up as a network failure: re-run later.
- Each phase waits up to 15 minutes. A graceful handover can take up to about 2 minutes, and reports arrive every 30 seconds.
- **Not covered:** a real Herdr, login startup, hooks, and a failing release's automatic rollback. `scripts/runtime-platform-smoke.py` covers the rollback with synthetic archives.
