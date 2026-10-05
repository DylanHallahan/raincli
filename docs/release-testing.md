# Real-release update testing

`scripts/windows-release-update-e2e.py` checks what synthetic smoke tests can't: a **published** GitHub release installed through the product's real updater, then a pushed upgrade and an explicit downgrade, all driven by a server's client target. The server is a **throwaway one started by the script itself**. The script never uses a production server, secret or team, and it never registers anything on the pilot team.

The updater has no source override. It fetches only stable releases of the canonical `DylanHallahan/raincli` from `api.github.com` and `codeload.github.com`, so the releases under test must be published there first.

## What it does
1. **Database.** It uses a disposable PostgreSQL on loopback:
   - a fresh `initdb` cluster on a random port, with a random password (the Windows runner's preinstalled PostgreSQL through `PGBIN`, or any `initdb`/`pg_ctl` found on `PATH` or under `/usr/lib/postgresql/*/bin`);
   - or, if `RAINCLI_TEST_DATABASE_URL` is set, a throwaway database on that local test server (`scripts/test-postgres.sh`), dropped at the end.
2. **Server.** It starts the server from this checkout in a new venv built from the hash-pinned `raincli/requirements.lock` (`uvloop` is skipped on Windows by its platform marker), migrates, and runs uvicorn on `127.0.0.1` with a random `RAINCLI_SECRET_KEY`.
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

# Windows app installer assets (v0.4.0 and later)

From v0.4.0, a release also carries the Windows app installer: `RainCLI-Setup-<X.Y.Z>.exe` and `RainCLI-Setup-<X.Y.Z>.exe.sha256`. App installs update only through these two assets (protocol §15.5, §15.8 M4), so **a release without them can't be pushed to Windows app machines.** No workflow attaches them: the build workflow has a read-only token and only uploads an artifact. The main agent attaches them by hand.

## Building and attaching the assets (main agent)
1. Set `__version__` in `raincli/raincli_agent/__init__.py` (and `version` in `raincli/pyproject.toml`) to `X.Y.Z`, merge, and tag the release commit `vX.Y.Z`.
2. In GitHub, run **Actions → Manual Windows app build → Run workflow** on the tag `vX.Y.Z`. It:
   - installs the hash-pinned build tools (`packaging/windows/requirements-build.txt`), then pywebview for the app window (`requirements-webview.txt`, with `--no-deps --no-build-isolation`, because `proxy_tools` is published only as a source archive);
   - freezes the app (window and tray), the CLI, the stub and the PATH shim with PyInstaller and compiles the Inno Setup installer (`packaging/windows/build.py`);
   - **fails if the bundle contains any test hook** (`packaging/windows/verify_bundle.py`: no `_build_test` module or file, no `TEST_RELEASE_BASE`/`TEST_CERT_SHA256` name, no server or build-tool module, the real tray and stub, the window without tkinter, and no WebView2 debugging switch in RainCLI's own code or pages) and checks the checksum line;
   - uploads the artifact `RainCLI-Setup-X.Y.Z`, holding the two files.
3. Download and check the artifact, then attach both files to the release:
   ```bash
   gh run download <run-id> -R DylanHallahan/raincli -n RainCLI-Setup-X.Y.Z -D /tmp/raincli-setup
   cd /tmp/raincli-setup
   sha256sum -c RainCLI-Setup-X.Y.Z.exe.sha256           # "RainCLI-Setup-X.Y.Z.exe: OK"
   cat RainCLI-Setup-X.Y.Z.exe.sha256                    # exactly: 64 lowercase hex, two spaces, the file name
   gh release upload vX.Y.Z RainCLI-Setup-X.Y.Z.exe RainCLI-Setup-X.Y.Z.exe.sha256 -R DylanHallahan/raincli
   ```
   The names must match exactly: the app looks up each asset by its exact name in the release that `vX.Y.Z` resolves to, and refuses a checksum line naming any other file.
4. Publish the release as **stable** (non-draft, non-prerelease) only after both assets are attached.

## The synthetic app e2e (any ref, before a release)
**Actions → Manual Windows app e2e → Run workflow**, with the inputs left empty, builds 0.4.0 and 0.4.1 test installers from the same ref and runs `scripts/windows-app-e2e.py --installers dist/e2e` on a disposable GitHub-hosted `windows-2022` runner, against a throwaway in-job server (the same PostgreSQL and server setup as the managed e2e above). It also builds a third installer, **0.4.2, from a staging copy of the client whose tray exits 1**. That patch exists only in the job's temporary copy; the shipped source has no hook. It prints a `PASS:` line for each check:

**A. A fresh install, signed in from the CLI**
1. A silent per-user install with no admin: the onedir layout, `install.json`, the HKCU Run value and uninstall key (none under HKLM), the shim first on the user `PATH`, the Start menu entries, and `installer-record.log` with the Run value and the Scheduled Tasks.
2. `RainCLI.exe --quit` exits 0 with nothing left running. Then sign-in through the installed `raincli login`, with the password typed into a pseudo console (ConPTY) at the no-echo prompt, never argv or the environment. `agent.json` holds `token_dpapi` only, and `runtime.json` is machine mode.
3. Launching exactly the Run value's command line, then presence reporting the version, `automatic` and `current`.
4. A pushed upgrade 0.4.0 → 0.4.1 through the installer assets: both assets fetched from the API asset URL with `Accept: application/octet-stream` and redirected to `objects.githubusercontent.com` and `release-assets.githubusercontent.com`. `/UPDATE` changes neither the Run value nor the uninstall key, `install.json` is swapped, and the tray relaunches from `versions\0.4.1`.
5. A pushed 0.4.2 whose tray never starts: the stub's probation rolls it back, the server shows `rolled_back` (`first_start_failed`), and 0.4.1 runs again.
6. The downgrade refused, then allowed with `--allow-downgrade`, then the target cleared.
7. An uninstall with `/SIGNOUT=yes`: the machine is revoked, its credential deleted, and what §15.8 M10 removes is gone. The installer record is kept.

**B. An old pip client's foreground connector**
A **pip-installed v0.2.0** client (installed from its release archive) runs `raincli connector run` in its own console. Its connector config omits `agent_config` and has a relative `state_dir`, and there is no `runtime.json`.
- The app install starts while it runs. For 45 seconds the migration must wait and touch nothing (§15.8 M7).
- Once the old connector is closed, the handle and credential are unchanged, no machine is added, the token is DPAPI-protected, `runtime.json` is connector mode, the Run value starts the stub, a message sent after migration is delivered, and `migration.log` holds no token.
- An uninstall with `/SIGNOUT=no` keeps `agent.json`, the connector config, the queue and `migration.log`.

**C. A managed v0.3.2 install**
A **managed v0.3.2** install is installed through the real updater, in the documented layout. Its credential, connector config and runtime config are in `~/.config/raincli`, and logon start comes from `runtime startup --config ~/.config/raincli/runtime.json`. That default `runtime.json` is also the app's own default (review 2 R1). The old runtime is started by running exactly its HKCU Run value, as logon does.
- The app installer records that Run value.
- The app's migration sends the launcher's stop request; the launcher exits.
- The Run value then starts the stub, and the same handle keeps delivering.

The script refuses to run anywhere but a GitHub-hosted Actions Windows runner (`GITHUB_ACTIONS` and `RUNNER_ENVIRONMENT=github-hosted`).

**Pinned tools.** `build.py` downloads Inno Setup 6.7.3 from its GitHub release, checks its SHA-256, installs it into the build directory and checks `ISCC.exe`'s version. The Python wheels are hash-pinned, and `build.py` prints the exact Python it ran on. It also times `bin\raincli.exe --version` against `raincli.exe --version` in an assembled install root, so the build log shows the PATH shim's start-up cost: the cost of every hook call.

**How the fake release endpoint is reached.** The shipped app has no release-host override (§15.8 M11): `HOSTS`, `API` and `REPO` are constants, `raincli/tests/agent/test_release_constants.py` checks that, and the build refuses test hooks. So the e2e serves its fake release endpoint on the **real hostnames**, on that runner only: a hosts-file entry maps `api.github.com`, `github.com`, `objects.githubusercontent.com` and `release-assets.githubusercontent.com` to `127.0.0.1`, and a throwaway test root CA in the runner's LocalMachine Root store signs a certificate for exactly those names on port 443. The script removes both when it ends, and refuses to run anywhere but a GitHub Actions Windows runner.

## The real-release app e2e (after attaching the assets)
Once two stable releases carry the installer assets, for example `v0.4.0` and `v0.4.1`, run **Manual Windows app e2e** with `real_from` = `v0.4.0` and `real_to` = `v0.4.1`. It builds nothing, adds no hosts entry and no test CA, and skips the rollback case (A5). It downloads both releases' installers, checks them against their published checksums, and runs the remaining phases with real GitHub: the pushed upgrade and downgrade fetch the **published** assets through the exact allowlist. A missing asset fails with `release vX.Y.Z has no asset …; attach it first`. Record the `PASS` lines and the run URL in the release notes, as for the managed e2e.
