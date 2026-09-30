# Machine agent directory and pushed updates: implementation report

**Status: review-ready.** The work is on branch `feat/agent-directory`, based on `main` `5d42d29` (v0.2.0). The code verified and reviewed below is at **`ae700f5`**; this report is committed on top of it and changes no code.

The main agent owns the merge to `main`, the v0.3.0 release, deployment, the webserver migration and the real two-release test. None of these has been done.

The approved brief is "RainCLI agent directory and pushed updates", with the design answers of 2026-09-30. The binding contract is `docs/raincli-protocol.md` §14, with §14.7, §14.8 and §14.9.

## What changed

### Machine agent directory
- **Machine identity:** a machine is its registered handle with **one machine credential**. Existing handles become machines as soon as their runtime reports agents.
- **Discovery:** every 30 s the runtime publishes the agents it finds, **name first**. Sources:
  - Herdr (`herdr agent list`, all agents);
  - Claude Code and Codex hooks (`raincli hook …`, installed with `raincli hooks install --claude|--codex --config …`);
  - a same-uid process scan as a fallback: Linux `/proc` by executable basename or native install path, Windows type-only through `tasklist`. Scan entries are always `status: unknown`.
- **What is sent:** the inbox is marked `role: inbox` with reachability `instant` (Herdr) or `next-turn` (a Claude Code hook). Statuses are working, idle, blocked, offline or **unknown** (Herdr's unknown is kept as unknown).
- **What is never sent or stored:** paths, prompts, titles, transcripts or process ids.
- **Server:**
  - migration `0004` adds `machine_agents` and the client columns on `agent_presence`;
  - reports are replaced as a snapshot, validated all or nothing, and bounded at 100 agents within a 128 KiB body (Nginx allows 128k on `/api/v1/presence`);
  - agents expire after 120 s;
  - reads are team-scoped.
- **Website:** "Add a machine" replaces "Register an agent". The machines page shows every machine in the team, your own first, with the inbox badge, each agent's name, type and status, and the machine's version and update state. `raincli agents` shows machine → agents.
- **Messages:** delivery is unchanged. Messages still go only to the machine's inbox.

### Next-turn inbox (Claude Code outside Herdr)
- **Handover:** the connector writes framed messages for the mapped hook session. The session's next `SessionStart` or `UserPromptSubmit` hook claims them and emits them as additional context. Framing and anti-forgery are identical to Herdr delivery.
- **Bounds and states:**
  - the handover is bounded at ≤ 32 KiB and ≤ 10,000 characters per turn, which is Claude Code's verified context limit;
  - the local state `handed_over` survives restarts, with reconciliation;
  - messages are held with the reason `offline`, `target_ambiguous` or `too_large_for_hook`, and there is no fallback target.
- **Idle sessions:** an idle session stays the target however long it is idle, as long as the runtime can see its process (the pid plus its start time). This is verified on Linux, native installs included, and on Windows; it is implemented for macOS but unverified, with no runner. Otherwise the 10-minute rule applies (§14.9).
- **Codex:** hooks give status only. The user must approve them once in Codex's `/hooks`, and they are Linux and macOS only.

### Pushed updates
- **Target:** `raincli-admin set-client-version --team S vX.Y.Z [--allow-downgrade]`, `--clear`, and `client-status`. The target stores a version only; `allow_downgrade` is kept with it, the CLI warns about it, and a floor of `v0.3.0` applies.
- **Presence:** each report carries the client version, update mode and update state. The reply returns `{version, allow_downgrade, set_at}`, and **never a source**.
- **Install:** a runtime behind the target installs immediately in automatic mode, through the existing canonical-GitHub path:
  1. stable release only;
  2. the tag resolved to its commit, and the archive checked against it;
  3. a staged environment built without pip;
  4. verification;
  5. a probation start with automatic rollback, keeping the previous version.
- **Retries:** transient failures back off, from 5 min doubling up to 6 h. A rolled-back target is not retried until the target is set again.
- **Downgrades:** only when `--allow-downgrade` is set.
- **Launcher safety:** a new launcher is adopted only after it passes the real `runtime run` path, plus the probation, rollback and relaunch phases. The legacy `automatic` key is always written false, so v0.2 launchers never pull on their own.
- **Defaults:** managed installs are automatic by default. `runtime update --manual` or `--automatic` persists the choice. v0.2.0 installs switch to automatic once, with a one-time notice.
- **Trust:** releases are unsigned, and the docs say so.

### Docs and tooling
- `SETUP.md` makes the managed install, the launcher and automatic updates the default.
- The inbox guide, Windows guide, protocol, README and packaged `SKILL.md` are all updated.
- **Webserver client migration:** `docs/raincli-deploy.md` covers moving from the server's release venv to a managed install, with rollback.
- **Manual real-release Windows workflow:** `.github/workflows/windows-release-update.yml` with `scripts/windows-release-update-e2e.py`, documented in `docs/release-testing.md`. It uses a throwaway in-job server, no secrets and read-only permissions.

## Verification (at `ae700f5`)
| Check | Result |
|---|---|
| Full suite from the repository root, on isolated PostgreSQL, including the PG-restart e2e on a dedicated disposable container | **691 passed, 2 skipped, 0 failed** |
| Full suite from `raincli/` (the launcher check must not depend on cwd) | **690 passed, 3 skipped, 0 failed** (the extra skip is the PG-restart test, whose container variable was unset for this run) |
| Linux `scripts/runtime-platform-smoke.py`, from a fresh stdlib client venv | **7/7 PASS**: agent directory through a real hook process; runtime and presence write-back; concurrent-file safety; hook-session liveness; systemd unit syntax; staged install plus rollback handoff; pushed target installed and handed over, with a failing first start rolled back |
| Native Windows manual Actions run **36754822478** | **SUCCESS on Python 3.11 and 3.14, 17/17 each.** It covers the same new stages, including hook-session liveness and the pushed-target install and rollback |

## Independent review (Opus reviewer)
| Round | Scope | Outcome |
|---|---|---|
| 0 | Contract, before the build | 12 gaps, all folded into §14.7 before the builders relied on them |
| 1 | Integration `1eecaca` | 20 findings; 2 decisions made by the lead (§14.9) |
| 2 | `92f238d` | 17/20 fixed; O1 (high) native Linux liveness |
| 3 | `14c4fc9` | O1 verified; blockers O3 (launcher run path) and N1 (macOS time zone) |
| 4 | `85d01b3` | Ready after a cwd fix; probation, rollback and relaunch coverage added |
| 5 | `ae700f5` | **Final verdict: READY.** No blocking items |

**Accepted:**
- a deliberate evasion of the behavioural launcher check, since candidates come only from the verified canonical archive;
- the Windows and npm 10-minute fallback when no process is determinable (§14.9).

**Open (low, not blocking):**
- a crashing candidate launcher now takes the full check timeout;
- one foreign-namespace hook record hides the other same-type processes in that container;
- `PYTHONSAFEPATH=1` is inherited by child processes.

## Not yet verified (main agent's steps)
- **The real two-release update**, which needs the published releases:
  - **Webserver:** release v0.3.0, migrate the webserver client to a managed install, publish a docs-only v0.3.1, `set-client-version v0.3.1`, observe the pushed install, then `--allow-downgrade v0.3.0`, then `--clear`.
  - **Windows:** run the manual `windows-release-update` workflow with its default inputs.
- **Live sessions:** a live Claude Code and Codex session using the installed hooks, and macOS liveness.
- **Deployment:** the production server deploy with migration `0004`.

## Workers and reports
The builders were cli-builder (client and runtime) and web-builder (server, website, docs and the release workflow), with an independent reviewer. The worker and review reports stay outside the repository, under `~/Projects/.worktrees/runtime-reports/`: `directory-{design,client,server,review-0..5}.md`.
