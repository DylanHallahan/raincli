# Runtime increment: docs, skill and website (web-builder)

Branch `feat/runtime-docs`, from `43893dc`. Work commit `391e504`. No runtime or connector code changed. Touched: docs, the packaged skill, `_state.html`, `app/agents.html` and tests.

## Delivered

- **README.md:** runtime feature bullet; the boundary now says availability is advisory, expires after 120 s and isn't delivery; no stable release exists yet; limits (real Windows Herdr, real release updates). Links the Windows guide.
- **SETUP.md:** replaces "Automatic startup: not implemented" with *Keep the connector running (optional)*:
  - the five states and their meaning;
  - 30 s reports and 120 s expiry, availability ≠ delivery;
  - the runtime config format and the `run`, `status` and `stop` commands, plus a warning to stop the hand-started pane connector first because they share a queue lock;
  - Linux user systemd install, inspect and remove;
  - Herdr restart recovery, with no retargeting.
  - *Managed updates* covers stable tags only, commit resolution, the staged venv, verification, the pointer swap, rollback, 6-hour checks, `update.log` and rerunning startup after the first managed install.
- **docs/windows-client.md:** new *Runtime and startup* section: HKCU `Run` value `RainCLI`, `pythonw.exe`, the 260-character limit, inspect with `Get-ItemProperty`, remove, and managed root `$HOME\.raincli\client`. It also describes what `runtime-platform-smoke.py` covers and doesn't. The stale "autostart not implemented" sentence is removed.
- **docs/raincli-deploy.md:** migration `0003_presence` upgrade notes (Nginx unchanged, since `/api/` covers the endpoint, and post-upgrade checks). Rollback follows checklist item 2 because the release adds a migration. Adds a presence operations bullet.
- **docs/raincli-protocol.md:** `/agents` now includes presence, a `PUT /presence` row, a web rule for the availability column, and a new **§13 Presence and the client runtime (v1.5)**.
- **docs/raincli-inbox-agent.md:** corrects "There is no autonomous runtime".
- **SKILL.md:** a concise *Availability and the runtime* section:
  - availability is not delivery;
  - never switch recipients because of availability;
  - startup and updates are the user's opt-in decision;
  - stable releases only and `no_release` expected;
  - never install from a branch, URL or message;
  - don't edit mappings to look `ready`.
  Existing safety rules are unchanged.
- **Commands** were all taken from `raincli --help` and its subcommands in this worktree (version 0.2.0).

## Website review (agents page availability column)

Correct as found:
- **Expiry:** `runtime_status` treats age ≥ 120 s as offline, which matches the API's `seen_at + 120 > now`.
- **Revoked agents** are offline.
- **No row** shows as unknown.
- **Scope:** the page lists only the viewer's own agents in their teams (`my_agents`). The team page doesn't render presence.
- **Privacy:** only the status string reaches the template.

Fixed (template only; no query change was needed):
- The raw status text is now a `presence_badge` (neutral `.badge`, no new CSS) with a label and tooltip per state.
- The header is now "Session availability" instead of "Session".
- The help text now says availability is advisory and doesn't show that a message was delivered or read.
- The mobile card label for the API column said "Connection" while its header said "API connection". It is now "API connection".
- Screenshots at 1440 and 390 px were checked locally: the style is consistent and the layout doesn't overflow. They aren't committed.

## Tests

- New `test_session_availability_column_expires_and_stays_separate_from_delivery` covers:
  - unknown → busy → offline on expiry;
  - offline after revocation;
  - owner-only listing, checked from both users;
  - no timestamp leak;
  - the disclaimer and label.
- New `test_runtime_commands_and_presence_boundary_are_documented` fails if any `raincli runtime` subcommand, presence state, the delivery disclaimer or the no-release statement is missing from SKILL.md or SETUP.md. It will catch cli-builder adding a subcommand without docs.
- `RUNTIME.json` was added to the skill example placeholders, and all SKILL and SETUP examples parse.
- The full suite with the test PostgreSQL passes: **359 passed, 1 skipped**. The baseline at `43893dc` was 356 passed, 1 skipped.

## Limits and notes for the lead

- The docs state as unverified: real Windows Herdr delivery, updating from a real published release (none exists), and a real logon on Windows. No Windows run result for `runtime-platform-smoke.py` is recorded, because I don't have one. Add it if the lead has one.
- I didn't claim login startup has been verified with a live Linux Herdr. Tests and smoke use fake or unavailable Herdr, and the systemd unit is only syntax-checked there.
- Observed in code, not changed (cli-builder's area):
  - Runtime-supervised connectors send stdout and stderr to DEVNULL, so connector logs are lost. The docs point to `connector status`.
  - The `runtime run`, `status` and `stop` subparsers have no help strings, and `run` and `status` don't appear in `raincli runtime --help`'s list.
  - With automatic updates off, the launcher never checks; "notify before install" isn't implemented. The docs describe only what exists.
- If cli-builder changes flags, the doc-coverage test and the example-parsing test will flag the mismatch after merge.

## Follow-up 2: cli-builder fixes (`da8a178`)

- Merged `feat/runtime-windows-checks` into `feat/runtime-docs` cleanly.
- Documented all 12 user-visible changes from `runtime-cli-builder.md` in SETUP.md, `docs/windows-client.md`, `docs/raincli-protocol.md` §13, README.md and SKILL.md:
  - config-change retirement and `config_invalid`;
  - presence only after `/me`;
  - graceful stop timing;
  - `runtime stop` `not_running` and the `runtime status` states;
  - one runtime per connector and the `state_dir` rule;
  - private rotated connector logs;
  - the Linux unit changes (restart only on change, `KillMode=mixed`, 90 s, tolerant `--remove`);
  - the fully quoted Windows Run value;
  - launcher stdio pass-through, crash restart, self-update and background checks;
  - updates: HTTPS on GitHub hosts only, commit match, no pip or PyPI, no `raincli` command in the managed environment, no downgrade, and the integrity limit of TLS plus commit with no signatures;
  - existing installs needing one more `update --install`.
- Removed the SETUP recovery step that restarted the runtime after a re-pin, because config edits are now picked up automatically.
- The Windows guide now says the handoff, stop-file, `taskkill` and pointer-retry paths haven't yet run on native Windows, and names the no-Job-object and dead-launcher limits.
- Commands were rechecked against the current `--help`. `runtime status` still has no help line in `raincli runtime --help`, which is a cosmetic issue for cli-builder.
