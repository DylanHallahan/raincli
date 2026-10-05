# Windows app and headless login, Phase 1: implementation report

**Status: review-ready; every Phase 1 check passes.** The work is on branch `feat/windows-app`, based on `main` `bf1319a` (v0.3.1), with `main` `b4473cf` (the two manual workflow files) merged in. The code verified and reviewed below is at **`8012aca`**; this report is committed on top of it and changes no code. The version is **0.4.0**.

The main agent owns the merge to `main`, the v0.4.0 release (attaching the installer artifact), deployment with migration `0005`, and the rollout to the pilot Windows machines. None of these has been done.

The binding contract is `docs/raincli-protocol.md` §15, amended by §15.8 (after the contract review) and §15.9 (lead decisions after review rounds 1a and 1b).

## What changed

### One client, two front ends
All behaviour lives in the stdlib client `raincli_agent`: sign-in, credential storage, the runtime, pushed updates and migration. The **CLI** is the headless front end, on Linux over SSH as well as on Windows. The **Windows app** is a thin tray front end that calls the same functions. There is no second implementation.

### Sign-in that registers the machine (server)
- **`POST /api/v1/app/login`:**
  - **Throttling:** it shares the website login's `LoginLimiter` object, so a lockout carries across both in either direction. Only a wrong password counts as a failure; once the password is correct, every other outcome counts as a success. `blocked` is checked before scrypt runs, and the password is capped at 256 characters.
  - **Team:** a user with several teams gets `409 team_choice_required` with the list of teams.
  - **Machine name:** a shared slug algorithm with 22 test vectors.
  - **Credential rotation** needs proof: either a valid `previous_token`, or `replace: true` for a machine with no delivery history (`409 name_in_use` otherwise). Rotations are recorded and shown on the Machines page.
  - **Limits:** at most 20 active machines per user per team (`409 machine_limit`).
  - **The password** is never stored, logged or echoed. A test checks DEBUG logs, the responses and a dump of every table.
- **`POST /api/v1/app/sign-out`** revokes the machine and its credentials.
- **Migration `0005`:** adds `signed_in_from`, `rotated_at`, `rotated_by` and `inbox_role_at`. The backfill counts every existing machine with presence or a used credential as having history, so those machines rotate only with `previous_token`.
- **`GET /api/v1/me`** gains `delivery_history`.
- **Website:** the Machines page shows "Signed in from …" and "Credential replaced by …".

### Client core
- **`raincli login`** (email, then a no-echo password prompt):
  - The password comes only from `getpass` on a real terminal, never from argv, the environment or a file. Without a terminal, the command refuses before prompting.
  - It refuses to reroute delivery: any connector config that references the target `agent.json` blocks it, with or without an existing credential. The one exception is a same-handle rotation with a valid credential.
  - It writes a **machine-mode** `runtime.json`, with no connector.
- **`raincli logout`:** asks for confirmation showing the handle, revokes the credential, and disables logon start. If revocation fails, it keeps the credential and exits non-zero; `--local-only` is an explicit option.
- **Windows credential storage:** DPAPI, CurrentUser scope, with fixed entropy, `CRYPTPROTECT_UI_FORBIDDEN` and `LocalFree`.
- **Machine mode:** publishes presence, the client block and the agent directory with no inbox, and acts on pushed targets. It refuses targets and rollbacks below v0.4.0.
- **Windows app updater:**
  - **Hosts:** an exact allowlist (`api.github.com`, `github.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com`), checked before every redirect hop: https on port 443, no user info, at most 5 hops. These values are constants, and there is **no override** in shipped code.
  - **Assets:** both come from the same stable release object, matched by exact name. The checksum line is parsed strictly.
  - **Install:** the installer is downloaded into a fresh owner-only directory and hashed as it is written, then run by absolute path with `/UPDATE`. The new version is verified, then `install.json` is swapped atomically.
- **Stub (`RainCLI.exe`):**
  - **Probation:** the stub owns it. It waits up to 120 s for a heartbeat, then rolls back and records `rolled_back`.
  - **`--quit`** waits for the stub lock and the tray lock, and for no other process to be running from the install root. Its own ancestors and the uninstaller are excluded. If something still runs after 120 s, it names the blockers.
  - **"Already running"** is exit 76, believed only while the tray lock is really held.
- **Tray:** status icon, status window, Open log, Pause/Resume, Sign in again, Sign out and Quit. The runtime runs in a kill-on-close Job object.
- **Migration** (run from the installer and the tray's first run), in this order:
  1. Detect the managed install and old pip/venv clients (`agent.json` at the default path or `RAINCLI_CONFIG`, connector configs and queues), normalize their configs, and validate the result with the new `load_runtime`. A failure here changes nothing.
  2. Check which queue locks are held. Stop the old runtime only when it can be restarted; otherwise ask the user to close the old window. On cancel or timeout, restart whatever was stopped.
  3. Convert the credentials to DPAPI. Once anything has been converted, point the Run value at the stub (§15.9).
  4. Start the new runtime and confirm it is ready.
  5. Replace the old Run value.

  Migration keeps the handle, the credential and connector delivery, and **never signs in or creates a machine**. When a bare `agent.json` has delivery history, it asks for the connector config instead of choosing machine mode.

### Windows packaging and CI
- **Bundle:** PyInstaller onedir (`RainCLI-app.exe` for the tray, `raincli.exe` for the CLI, a onedir stub and a onedir PATH shim). A bundle check refuses test hooks and requires the real tray and its GUI modules. `RainCLI-app.exe --self-check` runs in every build.
- **Installer:** Inno Setup 6.7.3, pinned and SHA-256 checked, per user (`PrivilegesRequired=lowest`, `%LOCALAPPDATA%\Programs\RainCLI`).
  - `/UPDATE` installs only `versions\<v>`.
  - Any existing Run value, Startup entry or Scheduled Task is recorded before anything is written.
  - The uninstaller removes the hooks before an optional sign-out (`/SIGNOUT=yes|no`).
  - What uninstall removes and keeps is documented in `docs/windows-client.md`.
- **Workflows**, both manual only with `contents: read`, SHA-pinned actions, hash-pinned wheels and no secrets:
  - `windows-app-build.yml` builds the installer and uploads it as an artifact only;
  - `windows-app-e2e.yml` runs the Windows end-to-end test.
- **Docs:** SETUP.md (headless Linux `raincli login`), `docs/windows-client.md`, the README and `docs/release-testing.md` (how to attach the artifact with `gh release upload`).

## Verification (at `8012aca` unless noted)

| Check | Result |
|---|---|
| Full suite, real PostgreSQL, new venv in `~/Projects/raincli` | **943 passed, 2 skipped, 0 failed** |
| Linux `scripts/runtime-platform-smoke.py` (includes headless `raincli login` on a pty with no echo, no password in argv, env or files, and no-TTY refusal; the machine-mode runtime; a machine-mode pushed update under the managed launcher; the systemd unit syntax) | **10/10 PASS** |
| Windows app build, run **37252883015** | **SUCCESS** (Inno Setup 6.7.3 pinned, bundle check, `--version` and `--self-check` smoke, checksum line, artifact `RainCLI-Setup-0.4.0`) |
| Windows app e2e, run **37252881250** | **SUCCESS: every stage, A1–A7, B and C** |
| Windows app e2e at `47e35d2`, the head before the D1 stub fix, run **37252384778** | **PASS, all stages** |

**Stages of the Windows end-to-end test** (each run builds 0.4.0, 0.4.1 and a broken 0.4.2 from the dispatched ref):
- **A1.** A silent per-user install, checked in full: the onedir layout, `install.json`, the Run value, the uninstall key, the shim first on PATH, the Start menu entries, and the record of the Run value and Scheduled Tasks taken before writing.
- **A2.** `--quit` exits 0 with nothing left running. `raincli login` through a pseudo console creates the machine with a DPAPI credential.
- **A3.** The Run value starts the app, and presence shows 0.4.0, `automatic`, `current`.
- **A4.** A pushed 0.4.1 through the fake release assets on the **real hostnames** (hosts file plus a test root CA; no override in the client). `/UPDATE` leaves the Run value and the uninstall key alone.
- **A5.** A pushed 0.4.2 whose tray fails: the stub rolls back, the server shows `rolled_back`, and 0.4.1 runs again.
- **A6.** A downgrade is refused, then allowed with `--allow-downgrade`.
- **A7.** Uninstall with sign-out revokes the machine and removes everything listed.
- **B.** An old **pip v0.2.0** client running as a foreground connector, with no `runtime.json`, no `agent_config` and a relative `state_dir`. Migration waits for the old window, then keeps the handle and credential (now DPAPI), writes a connector-mode `runtime.json`, and delivery continues. No new machine is created. Uninstall without sign-out keeps the credential, the queue and `migration.log`.
- **C.** A **managed v0.3.2** install in the documented layout (default `runtime.json`, started by its Run value): the Run value is recorded, the launcher stops on the stop request, the Run value now starts the stub, and the machine keeps delivering.

**Also measured:** the PATH shim `bin\raincli.exe` takes about 0.21 s median per call, against 0.14 s for `raincli.exe` alone, far under the 5 s hook timeout.

## Independent review

| Round | Scope | Outcome |
|---|---|---|
| 0 | The contract, before the build | 24 findings (8 high), all adopted as §15.8 before the builders relied on them |
| 1a | Client at `419b466` | 12 findings (2 high, both in migration); all fixed |
| 1b | Server, packaging and CI at `3ebc121` | 11 findings (3 high: stub `--quit`, the tray entry point, the old-pip e2e shape); all fixed, with lead decisions in §15.9 |
| 2 | `b5355c8` | 4 findings; R1 (high) was a managed install on the default `runtime.json` never being stopped. All fixed |
| 3 | `a476220` | 2 low; fixed. The e2e then exposed the uninstaller counted as a `--quit` blocker, also fixed |
| 4 | `47e35d2` | D1 (medium): exit 3 collides with `abort()` on Windows, so a fatally crashing version would not roll back. Fixed |
| 4B | `8012aca` | **READY**, no findings |

**Accepted, not fixed (low):** if an orphan tray releases its lock in the few milliseconds between a new tray's exit 76 and the stub's check, a probation could be counted as failed and roll back a good version. The operator can undo that by setting the target again.

## Notes on the evidence
- **The first full e2e (run 37248010041)** failed when the test script itself first connected to its fake release endpoint: Python didn't trust the newly added test root CA. The script now creates and checks the CA before any other TLS connection, in a fresh process. The root cause was **not proven**, because the added diagnostics never ran once it passed. Most likely the test process had read the certificate store before the CA was added. The client and the frozen app are separate processes started afterwards, and use the normal Windows trust store.
- **The workflow files on `main`** (`b4473cf`) predate `b5355c8`, which pins Python 3.14.7 and raises the e2e timeout to 150 minutes. The main agent reviewed that change and takes it with the final merge.

## Not yet verified
1. **Pilot machines:** a real install on the pilot Windows machines, including their migration from the old client and one real pushed update. That is the brief's acceptance step, and the main agent's to run.
2. **The real-release e2e** (`windows-app-e2e.yml` with `real_from`/`real_to`): possible only once installer assets are attached to two published releases (v0.4.0 and a later one).
3. **Production:** migration `0005`'s backfill on the production database, and the Machines-page labels with real data.
4. **Tray GUI dialogs:** the sign-in dialog, team choice, "replace machine", "Sign in again", the connector-config file picker and the "close the old window" notice. The e2e signs in through the CLI and doesn't click dialogs; their logic is unit-tested.
5. **A DPAPI blob from another Windows user:** unit-tested only.
6. **macOS:** not covered by Phase 1.

## Workers and reports
The builders were cli-builder (client core, tray and stub) and web-builder (server, packaging, CI and docs), with an independent reviewer, all in isolated worktrees. Their reports stay outside the repository, under `~/Projects/.worktrees/runtime-reports/`: `windows-app-design.md`, `winapp-client.md`, `winapp-web-builder.md` and `winapp-review-{0,1a,1b,2,3,4}.md`.
