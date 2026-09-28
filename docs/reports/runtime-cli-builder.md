# Runtime fixes: cli-builder report

- **Branch:** `feat/runtime-windows-checks`. The base is `43893dc`; `b52d132` was the pre-restart WIP checkpoint, and the final commit is the one printed in the done line.
- **Scope:** the fixer brief (F1–F4, W1) and every finding in independent review 1 (`runtime-review-1.md`: H1–H4, M1–M6, L1–L6, T1–T5).
- **Boundaries:** fakes only. There were no live Herdr panes, no real systemd, no Actions runs and no pushes.

## Verification
- **Full suite on the test PostgreSQL:** `375 passed, 1 skipped` (baseline `356 passed`).
- **Linux `scripts/runtime-platform-smoke.py`:** exit 0 on the final code. Every section passed:
  - real runtime and connector processes, with bound readiness;
  - authenticated presence;
  - singleton and graceful stop;
  - systemd unit syntax;
  - a staged environment installed **without pip or network**;
  - a live managed update/rollback handoff that releases the queue.
- **Windows:** not run here. **A native Windows Actions rerun is warranted**, and the lead decides when. Until now the smoke stopped at line 91 (W1), so the managed update/rollback handoff has never run on Windows. It is also the first native run of the stop-file, `taskkill /T`, pointer-retry and quoted-Run-value paths.

## Finding-by-finding

| ID | Verdict | Resolution | Regression test |
|---|---|---|---|
| **F1 / H1**: readiness bound only to the pid | Confirmed (the reviewer repro'd it live) | **Fixed.** `service.load_bound` hashes the connector and agent config files before and after loading. The child writes `{pid, handle, config, config_sha256, agent_config, agent_config_sha256}` after `api.me()`, and aborts if the files changed during startup. The Worker requires an exact match with its own binding and its server-confirmed `/me` handle, and it re-hashes both files at the start of every tick and again just before publishing. On any change the Worker retires: it stops the child gracefully and publishes `offline` only under the **old** credential. `Supervisor.refresh` then revalidates the whole runtime config through `load_runtime` (distinct credentials and state dirs) before any new worker publishes. An invalid edit leaves the mapping retired and unpublished (`error: config_invalid`) until it is fixed. Runtime `runtime.json` edits (adding or removing connectors) are handled the same way. | `test_runtime.py::test_config_edit_never_republishes_old_readiness_under_new_mapping` (FakeApi, alpha→beta remap, stale record carrying the new pid, invalid edit); `test_worker_waits_for_authenticated_queue_owner`; Linux smoke (real child) |
| **F2 / H2**: launcher discards stdin | Confirmed | **Fixed.** Commands other than `runtime run` inherit stdin, stdout and stderr, and return the child's exit code. A child killed by a signal maps to `128+N`. | `test_launcher_passes_stdio_and_exit_code_through`, `test_launcher_real_client_reads_body_from_stdin` (real client, `send --body-file -` through `launch.py`), `test_launcher_exit_status_for_signals`. Both stdin tests fail against the old launcher. |
| **F3 / M1**: reinstall doesn't restart a changed service | Confirmed | **Fixed.** The unit embeds `# config-sha256:`, a digest of `runtime.json` plus every connector config. If the unit bytes changed, install runs `daemon-reload`. It then runs `enable`, followed by `start` if inactive, `restart` if active and changed, or nothing at all if unchanged. | `test_linux_startup_restarts_only_when_unit_or_config_changes` (fake `systemctl` recorder on `PATH`, temp `HOME`) |
| **F4 / H3**: children hard-killed on stop, update and rollback | Confirmed | **Fixed; the round-2 regression (R2-H1) is fixed too.** `Worker.stop` writes `<ready>.stop`, and the connector checks it between iterations, during its sleeps and **immediately before starting any submission or escalation**, so it never begins one after a stop while one already in progress completes. In runtime mode it long-polls in slices of at most 5 s. The Worker's budget is `min(poll_wait, 5) + prompt_timeout + 15` s (at most 80 s, because runtime connectors need `prompt_timeout` ≤ 60); only after that does it kill the process tree. In runtime mode SIGTERM and SIGINT set the stop flag. The systemd unit uses `KillMode=mixed` and `TimeoutStopSec=150`, and the launcher waits 120 s. The queue lock is released in `finally`. | `test_runtime_handoff.py::test_no_submission_starts_after_a_stop_during_the_long_poll` (review repro 2 with defaults, a real child and a fake `herdr` executable; it fails on the pre-fix code); `test_a_submission_in_progress_at_stop_completes`; `test_stop_request_lets_in_flight_submission_finish`; `test_config_edit_…` (the fake child requires the stop file, `terminated` stays False); `test_connector_stops_between_iterations_on_request` |
| **F4 / H4**: lost stop request, orphaned Windows connector | Confirmed on Linux; the Windows orphan was argued from code | **Fixed.** The runtime writes `status.json` with its **own instance** (`status: starting`) before the first tick. Ticks run as futures while `stop.json` is polled every second, so a stop made during a slow tick is honoured as soon as it returns. `request_stop` returns `not_running` for a stopped, missing or malformed record instead of claiming `stop_requested`. The launcher stops the runtime with SIGTERM on POSIX; on Windows it repeats `runtime stop` every ~2 s until the process exits or 60 s pass. Its last resort on Windows is `taskkill /T /F`, which takes the connector children with it, so no orphan holds the queue lock. Pointer switches happen only after the old runtime has exited. **Not done:** a Windows Job object. `taskkill /T` covers the launcher-driven paths; if the launcher itself is killed externally, children can still outlive it until logoff. | `test_stop_during_slow_first_tick_is_not_lost` (stale `previous-run` record, 1.5 s tick, stop honoured in under 5 s); `test_stop_request_ignores_stopped_runtime`; `test_launcher_stops_runtime_gracefully`; Linux smoke handoffs |
| **F4**: environment retention, rollback, Windows files in use | Sound (review agrees) | Unchanged: versions are never deleted, and `previous` is kept. Pointer replacement on Windows now retries on a sharing violation (`updates.write_pointer`), because the launcher reads the pointer every second. The launcher also treats a transient unreadable pointer as "keep the current version". | Linux smoke |
| **W1 / L1**: Windows Run-value assertion | Explained from code: the check was wrong | **Fixed the check, and hardened the product.** Cause: `TEMP` on the runner is the 8.3 path `C:\Users\RUNNER~1\…`, while the product stores `Path(config).resolve()`, which is the long `runneradmin` path. The smoke now asserts `REG_SZ`, an exact equality with `windows_command_line(command(config))`, and that the quoted `config.resolve()` and `"runtime" "run"` are present. On failure it prints the kind, the value, the expected value and the config (paths only; the value never holds a token). The product now quotes **every** argument, with correct trailing-backslash doubling, instead of `list2cmdline`'s quote-if-needed, and rejects quotes and control characters. | `test_windows_run_value_quotes_every_argument`; Windows Actions rerun **pending (lead)** |
| **M2**: launcher never updated | Confirmed | **Fixed.** Each staged version keeps its own `versions/<v>/launch.py`, copied from the verified archive. After the pointer is written (install, rollback, or "already current"), `sync_launcher` compiles it and atomically replaces `launch.py` if it differs. At its next version switch, a running launcher sees that its file changed and re-executes itself: `os.execv` on POSIX (same pid, so systemd is unaffected), or a detached relaunch on Windows. **Existing installs:** the 43893dc-era `updates.py` only wrote `launch.py` if it was missing, so an install made with it keeps the old launcher until an update is performed by code from this change. The first update, run by the old code, installs the new version; the next install/rollback/"current" check syncs the launcher. To pick it up immediately, run `raincli runtime update --install` again from the new version (it syncs even when current), or delete `launch.py` first. No managed release has been published yet, so no real installs are affected. | `test_install_copies_verified_client_without_pip_and_syncs_launcher`, `test_launcher_reexecutes_itself_after_a_launcher_update` |
| **M3**: updater fetches setuptools from PyPI | Confirmed | **Fixed by removing the build.** The client is pure stdlib. The updater creates `python -m venv --without-pip` and copies `raincli/raincli_agent` from the commit-verified archive into that environment's `purelib`, so no build backend, pip, index or network is involved after the archive download. It still checks `--version` against the tag and runs `runtime --help` before switching. Trade-off: the managed environment has no `raincli` console script or dist-info. The launcher always uses `python -m raincli_agent`, so nothing depends on them. | `test_install_copies_…` (asserts no `-m pip` subprocess and `--without-pip`); Linux smoke (no pip output) |
| **M4**: redirects, scheme, integrity | Confirmed | **Fixed.** `check_url` requires `https`, host ∈ {`api.github.com`, `codeload.github.com`} and port 443, and it is applied to the request URL, **every redirect hop** (before following, via an `HTTPRedirectHandler` subclass) and the final URL. The archive root must equal `raincli-<resolved 40-hex commit>` (GitHub's codeload naming). HTTP and network errors become `ConfigError`. **Documented limit:** integrity rests on verified TLS to GitHub plus the tag→commit resolution and the archive root check. Release signatures and checksum assets are **not** verified. | `test_update_requests_stay_on_https_github_hosts`, `test_network_failures_are_reported_without_traceback`, `test_install_copies_…` (mismatched root refused, pointer unchanged) |
| **M5**: Windows runtime not restarted after a crash | Confirmed | **Fixed.** For `runtime run`, the launcher restarts a runtime that exited non-zero, with backoff `min(300, 2**n)` s, reset after 10 minutes healthy. Exit 0 (a requested stop) ends the launcher. This applies on Linux too, inside systemd's own `Restart=on-failure`. **Limit:** if the Windows launcher itself dies, nothing restarts it until the next logon. | `test_launcher_restarts_a_crashed_runtime` |
| **M6**: connector output discarded | Confirmed | **Fixed.** Each child's stdout and stderr are appended to `<runtime state_dir>/connector-<hash of config path>.log` (created 0600, in the private state dir). The log is rotated to `.1` at child start once it exceeds 1 MiB, with one generation kept. The connector logs escaped lines only, and tokens are never logged. **Limit:** rotation happens at child start, so a very long-lived child can grow the log past 1 MiB until its next restart. | `test_connector_output_goes_to_a_private_rotated_log` (real failing child, rotation, 0600, no token) |
| **L2**: singleton per state dir, not per connector | Confirmed | **Fixed.** Each Worker takes a non-blocking `runtime-owner.lock` in the connector's queue directory (`state_dir`, or the default handle-derived directory once `/me` is known) before starting a child or publishing. A second runtime reports `error: connector_owned_by_another_runtime` and publishes nothing. `load_runtime` also rejects a runtime `state_dir` equal to a connector's queue dir. | `test_one_runtime_owns_a_connector_and_state_dirs_may_not_overlap` |
| **L3**: malformed local files crash the loop | Confirmed | **Fixed.** Ready, `stop.json` and `status.json` records must be dicts of the right shape; otherwise they are ignored or give `not_observed`/`not_running`. `UnicodeDecodeError` is caught. Claim and spawn `OSError`s are reported per connector instead of crashing the loop. | `test_local_state_files_of_the_wrong_shape_are_ignored`, `test_worker_waits_…` |
| **L4**: updater edges | Confirmed | **Mostly fixed.** Automatic/`latest` installs never downgrade or follow a moved tag (`not_newer`); explicit `--rollback` still works. Network errors become `ConfigError`. The automatic update now runs as a non-blocking child that the launcher kills after 420 s or on stop, so SIGTERM is handled promptly. **Deferred: pruning** `versions/`. Deleting an environment that a not-yet-switched launcher still runs is unsafe, especially on Windows. Each version is small (no pip, stdlib only); operators may delete versions that are neither current nor previous. | `test_install_copies_…` (`not_newer`), `test_network_failures_…` |
| **L5**: startup edges | Confirmed | `After=network-online.target` removed (no effect in a user unit). `remove()` tolerates an already-removed unit (`disable --now` with `check=False`, then unlink and reload). **Deferred: minimal PATH.** A reduced PATH could break Herdr setups that rely on other PATH entries. The PATH is captured only at explicit install and isn't secret; reinstall to refresh it. | `test_linux_startup_…` asserts the unit contents |
| **L6**: server clock sources | Confirmed | **Fixed.** `presence.publish` and `directory` use `identity.now()`, the clock the web view uses. The expiry boundaries were already equivalent. | Server suite (presence tests) |
| **T3**: migration round-trip ignores `agent_presence` | Confirmed | **Fixed.** The round-trip test now asserts `agent_presence`. | `tests/server/test_api_admin.py` round-trip |
| **T1, T2, T4, T5**: coverage gaps | — | **Partly addressed:** config edit, launcher stdio, systemctl, graceful stop mid-submit, stop before the first tick, redirect and host rejection, a commit-mismatched archive, no-downgrade, and Run-value quoting are now covered. Launcher handoff is covered by real-process tests and the Linux smoke. **Not added:** a TTL-boundary presence test, fake-`winreg` tests, and zip size-lie/case-duplicate tests; the existing `unpack` checks were judged sound by the reviewer. | — |

## USER-VISIBLE CHANGES (for the docs worker)
1. **Config edits while the runtime runs:** editing a connector config, its agent config (for example a token rotation) or `runtime.json` stops that connector gracefully and marks the old identity offline. The mapping is revalidated before anything more is published. An invalid edit leaves the connector offline, with `"error": "config_invalid"` in `runtime status`, until it is fixed. A `state_dir` change still needs a runtime restart.
2. **Presence is published only after `/me` succeeds** for that connector's credential. Until then, `runtime status` shows the error type and nothing is published.
3. **Graceful stop:** stop, update, rollback, config retirement, `systemctl --user stop` and Ctrl-C let a delivery already in progress finish. No new delivery starts after a stop; messages that arrive meanwhile stay queued for the next start. A stop takes up to `min(poll_wait, 5) + prompt_timeout + 15` s per connector (in parallel), usually under 5 s when idle. **Runtime connectors must have `prompt_timeout` ≤ 60 s** (it's rejected at runtime load). Supervised connectors long-poll in slices of at most 5 s.
4. **Stop and status:**
   - `runtime stop` now prints `{"status": "not_running"}` when no runtime is live, instead of claiming `stop_requested`;
   - `runtime status` shows `"status": "starting" | "running" | "stopped"`;
   - it may carry `"error": "config_invalid"` or, per connector, `"connector_owned_by_another_runtime"` or `"config_changed"`.
5. **One runtime per connector:** a second runtime (another `runtime.json` or state dir) naming the same connector does not start it or publish. A runtime `state_dir` may not equal a connector's queue `state_dir`.
6. **Connector logs:** each connector writes to `<runtime state_dir>/connector-<id>.log` (private, rotated at 1 MiB with one `.1` kept).
7. **Linux startup:**
   - rerunning `runtime startup --config …` restarts the running service only if the unit or any mapped config changed, and otherwise leaves it alone;
   - the unit now has `KillMode=mixed` and `TimeoutStopSec=150`, and no `After=network-online.target`;
   - `--remove` works even if the unit is already gone.
8. **Windows startup:** the HKCU Run value quotes every argument (`"…pythonw.exe" "…launch.py" "runtime" "run" "--config" "C:\…\runtime.json"`), using the resolved long config path. Paths containing `"` are refused.
9. **Managed launcher:**
   - passes stdin, stdout, stderr and exit codes through, so `launch.py send --body-file -` works;
   - restarts a crashed runtime with backoff (up to 5 minutes);
   - installs the launcher from each release, and re-executes itself at the next version switch;
   - runs automatic update checks in the background, logging to `update.log`.
10. **Updates:**
    - https only, GitHub API/codeload hosts only, checked on every redirect;
    - the archive must match the resolved commit;
    - installation copies the client into a fresh environment **without pip or PyPI** (works offline after the download), and that environment has **no `raincli` console script**; use the launcher;
    - `--install` never downgrades and reports `not_newer`, while `--rollback` remains the explicit way back;
    - network failures produce a one-line error.
    - **Integrity limit to document:** TLS to GitHub plus commit resolution; release signatures are not verified.
11. **Existing managed installs** keep their older `launch.py` until code from this change performs an install, rollback or "already current" check (see M2). Run `raincli runtime update --install` once more after upgrading.
12. **Help text** was updated for `runtime run`, `runtime startup` and `runtime update`.

## Remaining limits and boundaries
- **Windows:**
  - no Job object; the external-kill orphan window is described under H4;
  - the launcher isn't restarted if it dies, until the next logon;
  - all Windows paths are unverified until the Actions rerun.
- **Log rotation** happens only at child start.
- **`versions/`** is not pruned automatically.
- **PATH** is captured at install.
- **Live Herdr:** no live-Herdr delivery or login-restart was tested (fakes only), by design.

## Windows run 36492621534 (da8a178): the managed runtime never published

**Result:** W1 fixed, and the direct runtime section passed on 3.11 and 3.14. The first managed-launcher section timed out at `runtime-platform-smoke.py:149`.

**Cause (found in the code, reproduced on Linux):**
- On Windows, a venv's `Scripts\python.exe` is a redirector. It starts the base interpreter as a **child process with a different pid**.
- The runtime spawns each connector with `sys.executable`. Inside a managed venv, that is the redirector, so `Popen(...).pid` is never the connector's `os.getpid()`.
- Readiness compared the two pids (`record == {"pid": process.pid, ...}`; that pid check predates F1). The connector therefore never counted as ready: it was published as `offline`, and the smoke's wait for `unknown` timed out.
- The direct section passed only because it runs the runner's plain `python.exe`, which isn't a venv.

**Fix:** a fresh handshake path per spawn (`Worker._new_handshake`, called at every `_spawn`). Only the child started with that path can write the readiness record there, and the record must still match the exact binding and the confirmed handle. The recorded pid is informational and no longer compared. The Worker's last-resort kill is now `taskkill /T /F` on Windows, so a killed redirector can't leave its interpreter holding the queue lock.

**Regression test:** `test_readiness_through_an_interpreter_redirector` uses a shell wrapper that runs the real interpreter as a child, like the Windows redirector, with a real connector and FakeApi. It fails on the da8a178 `service.py` (it stays at `offline` for 30 s) and passes with the fix. It also asserts the stop stays graceful (exit 0 through the stop file).

**Other suspects considered:**

| Suspect | Assessment |
|---|---|
| `pythonw.exe` / `CREATE_NO_WINDOW` | Not active in the smoke, where the launcher runs from a console (`HIDDEN` is empty). In real logon startup, though, the runtime had no stdout/stderr, so a crash left no trace. **Fixed:** without a console, the launcher sends the runtime's output to a private `runtime.log` in the managed root (0600, rotated at 1 MiB on start). |
| Crash-restart loop | It would mask a crash as a timeout. Not the cause: the redirector issue explains the symptom exactly. The new diagnostics would show restarts. |
| Self-reexec | Not triggered in the smoke: both staged versions ship an identical launcher, so `sync_launcher` makes no change. |
| Path quoting | `--version` through the launcher had already passed. The Run value is covered by W1. |
| Pointer file | The replace already retries on Windows sharing violations, and the launcher keeps its version if the pointer read fails. Not the cause. |
| Locks on the state dir | Different files (`run.lock`, `runtime-owner.lock`); the direct section had already released them. Not the cause. |
| Background update | `automatic` is false in the smoke, so no update runs. |

**Smoke diagnostics** (both runtime sections): on any failure the smoke prints the following, with token-shaped text redacted:
- `runtime status` JSON;
- every `state/*.json` (including ready handshakes);
- `*.stop` files and the connector logs;
- the managed `current.json`, `update.log` and the version directories;
- the captured runtime/launcher stdout and stderr (`runtime-direct.log`, `launcher.log`);
- the process table filtered to `raincli` (PowerShell `Win32_Process` with parent pids, or `ps` on Linux).

The smoke's cleanup now kills whole process trees on Windows.

**Rerun:** warranted. Linux: the full suite passes (376 passed, 1 skipped), and so does the runtime smoke.

### Delta since da8a178 (for the reviewer)
- `service.py`: per-spawn handshake (no pid comparison), and a `kill_tree` fallback in `Worker.stop`.
- `launcher.py`: `runtime_output` (`runtime.log` when there is no console).
- `scripts/runtime-platform-smoke.py`: `diagnose`, `scrub`, captured process output, and tree-kill cleanup.
- Tests: `test_readiness_through_an_interpreter_redirector`; `test_worker_waits_for_authenticated_queue_owner` now checks that a record at an earlier handshake path is ignored.
- User-visible: a managed runtime started without a console (Windows logon) writes errors to `~/.raincli/client/runtime.log`.

## Review round 2 (of da8a178): fixes

| ID | Status | Resolution | Test |
|---|---|---|---|
| **R2-H1**: a submission starts after the stop, then is killed mid-prompt | **Fixed** | `Connector.process()` and `process_escalations()` check `stop_requested()` immediately before `_begin_submit`/`_begin_escalation` and leave the rest queued. The supervised long poll uses slices of at most 5 s (`run_forever(max_wait=)`). **Budgets:** `load_bound` rejects `prompt_timeout` > 60 for runtime connectors, and `Worker.stop_budget()` = `min(poll_wait, 5) + prompt_timeout + 15` ≤ 80 s. With an in-flight tick (≤ 15 s) and the offline publish (≤ 5 s), a runtime stops in ≤ 100 s, inside the launcher's `GRACEFUL_STOP = 120` and systemd's `TimeoutStopSec=150`. | `test_no_submission_starts_after_a_stop_during_the_long_poll` (repro 2: defaults 25/30, stop during the idle long poll, message 1 s later, 40 s fake prompt). Now the child exits 0 in under 15 s, no prompt starts and the record stays `received`. The **pre-fix code fails it.** Also `test_a_submission_in_progress_at_stop_completes` (a 6 s prompt finishes as `submitted`, rc 0) and `test_runtime_rejects_prompt_timeouts_beyond_the_stop_budget`. |
| **R2-L1**: the Linux launcher's last-resort kill isn't tree-wide | **Fixed** | On POSIX the launcher starts the runtime with `start_new_session=True` and its fallback is `os.killpg(..., SIGKILL)`, taking the connectors with it. Windows already used `taskkill /T`. With budgets now bounded (≤ 100 s, launcher 120 s), the fallback should not normally fire. | Covered by the existing launcher tests and the Linux smoke handoffs (graceful path) |
| **R2-L2**: Ctrl-C on a foreground `runtime run` interrupts children | **Fixed** | In runtime mode the connector maps SIGINT, like SIGTERM, to the stop flag. The runtime handles Ctrl-C by retiring its workers through the stop files. Under the launcher, the runtime has its own session, so Ctrl-C reaches only the launcher, which stops the runtime gracefully. | Same graceful-stop tests (the flag path) |
| **R2-L3**: `sync_launcher` runs after the pointer write | **Fixed** | `install` and `configure(rollback=True)` now sync `launch.py` **before** `write_pointer`. | `test_install_copies_verified_client_without_pip_and_syncs_launcher`, `test_launcher_reexecutes_itself_after_a_launcher_update` |
| **R2-L4**: the supervised child loads its config twice | **Fixed** | `cmd_connector_run` in runtime mode builds the connector from the exact `(cfg, agent config)` that `load_bound` loaded and bound (`_connector_parts(loaded=)`). The post-start fingerprint check remains. | `test_readiness_through_an_interpreter_redirector`, the R2-H1 tests (real children) |
| **Accepted limits** (review 2 agrees) | Unchanged | No Job object; a dead Windows launcher isn't restarted until logon; no release signatures; no `versions/` pruning; log rotation only at child start; PATH captured at install. **Readiness latency:** the first `ready` appears on the second 30 s tick after start. | — |

**Verification:** full suite `379 passed, 1 skipped`; Linux runtime smoke exit 0 (all three stages).

**Delta since 20919b1:**
- `connector/runner.py`: `stop_requested` checks before submission and escalation, and `max_wait`.
- `cli.py`: single bound load, SIGINT as the stop flag, and the poll slice.
- `runtime/service.py`: the `prompt_timeout` cap, `stop_budget()`, and budget constants.
- `runtime/launcher.py`: `GRACEFUL_STOP` 120, a new session and killpg.
- `runtime/startup.py`: `TimeoutStopSec` 150.
- `runtime/updates.py`: launcher sync before the pointer.
- Tests: three new tests in `test_runtime_handoff.py`.
