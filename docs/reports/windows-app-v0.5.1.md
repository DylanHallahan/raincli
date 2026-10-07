# v0.5.1: fixes from the first real Windows installs

**Status: review-ready; every check passes.**
- **Branch:** `feat/v051`, based on `main` `3bb970a` (v0.5.0).
- **Code:** verified and reviewed at **`4367db5`**. This report is committed on top of it and changes no code.
- **Version:** 0.5.1.
- **Contract:** `docs/raincli-protocol.md` §16.17 to §16.20.

The main agent owns the merge, deploying the server change (the new `409` codes), the v0.5.1 release with the installer asset, and the release notes. None of these has been done.

## What changed, by the problem it fixes

### 1. A migrated stale or foreign credential blocked sign-in (§16.17, §16.18, §16.20)
**What happened:** on the first real v0.5.0 install, migration adopted an old `agent.json`. Its machine had been revoked, and it belonged to another account. The window's sign-in then took the `person_only` path with that credential and failed with a generic "the server refused a person session".

**The fix:**
- **Server:** `person_only` answers `409 machine_credential_invalid` or `409 not_machine_owner`, but only after the email and password are correct. A wrong password stays the generic `401`.
- **Client:** `check_credential` returns `ok`, `invalid`, `not_owner`, `unknown` (offline or an older server) or `unreadable` (a foreign or damaged DPAPI blob, or a damaged file).
- **Migration:** it never adopts an `invalid` or `unreadable` credential. It sets the old setup aside and ends with `fresh_sign_in_needed`. If valid credentials remain, it ends with `migrated_with_stale_set_aside` instead.
- **The window and `raincli login --person`:** they explain the cause in one sentence and offer **"Set up this computer as a new machine"** (`raincli login --new-machine`). It is never automatic, and it asks for the password again.
- **`set_aside`** moves the old setup into a private `replaced-<UTC>` folder, holding the migration lock:
  - **What moves:** the credential, `person.json`, `app-install.json`, notifications, every connector config that names it (found as `migrate.detect` finds them), and its runtime config **together with its state directory**. Old held messages and handover files can therefore never be delivered under the new machine.
  - **Shared runtime configs** are rewritten instead, and restarted.
  - **Refusal:** while another process holds a queue, it refuses and changes nothing.
  - **Rollback:** a failure part-way rolls everything back.
  - **A state directory on another volume** is renamed in place.
- **Which server a fresh sign-in uses:** the default service. Another host from an old setup is offered only as an explicit choice that shows that host, and a backup's server address is never read (§16.20).

### 2. Codex hooks on Windows never ran (§16.19 items 5, 6)
**Root cause, proven with real Codex 0.160.0 on Windows CI:** Codex runs hooks in the **session's shell**, which on Windows defaults to **PowerShell**. PowerShell parsed the v0.5.0 line `"…\raincli.exe" hook codex …` as a string, so the hook never ran ("Unexpected token 'hook'"). The earlier CI had reproduced the `cmd /C` path from a reading of the source, which is why it passed.

**The new line:** `C:\Windows\System32\cmd.exe /d /c call "<raincli.exe>" hook codex <Event> --state-dir "<dir>"`.
- It runs identically under `cmd` (wrapped or not), `powershell` and `pwsh`.
- `cmd` is called by absolute path, and `/d` skips AutoRun.
- Refused in paths: `% ^ & | < > " ! $`, backtick, typographic quotes, controls and a trailing backslash.

**Repair on update:** RainCLI-owned hook entries in `~/.codex/hooks.json` and `~/.claude/settings.json` are regenerated on the first start of a new version.
- **Safety:** a backup first, an atomic write, other entries untouched, and hooks never added.
- **Retries:** a repair that didn't finish is retried.
- **Notice:** a changed Codex command raises a one-time "open /hooks in Codex and trust them again".

### 3. A running Codex wasn't listed at all (§16.19 items 1–3)
- **One failing source no longer hides the directory.** Each discovery source is isolated and logged.
- **The Windows scan:**
  - uses the process table, the **full image path** and a **same-user check by token SID**, with `tasklist` only as a fallback;
  - recognises the real install layouts, which are recorded with their sources in `docs/windows-client.md`;
  - **never classifies the Claude or Codex desktop apps** or their helper processes;
  - lists the desktop app's embedded Claude Code;
  - never reads command lines.
- **This computer** shows Codex and Claude Code with **Connect** and **Disconnect**, bounded status checks, and the `/hooks` approval note. A one-time notice appears after a fresh sign-in, and `raincli login` prints the matching hint. Hooks are never installed automatically.
- **The docs** say what desktop-app sessions do with hooks, citing their sources. Claude desktop's Code tab uses `~/.claude` hooks. Whether the Codex desktop app runs them is unverified, and its `/hooks` trust is reported broken (openai/codex #47283), so trust once from the Codex CLI.

### 4. Conversations open at the newest message (§16.19 item 4)
This applies to the website and to app mode:
- an `#m-<id>` anchor takes precedence;
- the page stays at the bottom only when the user is already near it, and otherwise shows "New messages ↓";
- there is no inline script and the CSP is unchanged, and the page works without JavaScript.

### 5. The logo
The approved mark is the speech bubble with two linked agents, in `#1f5fd1`. It appears on:
- the executables and the installer;
- the tray, with a PNG for each state: ready in brand blue, and paused shown with the offline icon;
- the website and app-mode logo, and the favicon.

The bundle check requires these assets. The product name is still a setting.

## Verification (at `4367db5`)

| Check | Result |
|---|---|
| Full suite, real PostgreSQL, local (Playwright not installed locally) | **1451 passed, 6 skipped, 0 failed** |
| Linux `runtime-platform-smoke.py` | **PASS, all 31 stages** |
| `server-gui-tests.yml`, run **37561795277** | **SUCCESS: 1475 passed, 3 skipped; 25 GUI tests, 0 skipped** |
| Windows app build, run **37561797710** | **SUCCESS** |
| Windows app e2e, run **37561799824** | **SUCCESS: all 25 stages (A1–A7, B, C, D1–D9, E1–E6)** |
| Windows client smoke, run **37561801734** | **SUCCESS on Python 3.11 and 3.14** |

**What the Windows client smoke covers:**
- real Codex 0.160.0 `exec` running the installed hook in PowerShell, with a profile path containing a space;
- the v0.5.0 form reproduced as recording nothing;
- the new line under `cmd` (wrapped and unwrapped), `powershell` and `pwsh`;
- the token-SID scan on real processes;
- Herdr named-agent delivery.

**What the Windows app e2e covers:** A1–A7, B, C, D1–D9, plus the new **E1–E6**:
- E1: another account's revoked credential is set aside;
- E2: another user's live credential leads to the new-machine offer;
- E3: a connector outside the scan directories moves too;
- E4: with two credentials, only the revoked one moves;
- E5: held records and handover boxes are never delivered;
- E6: This computer connects Codex on click only.

**The Linux smoke** adds the stale-setup flow on a pty (`login --person` explains, then `login --new-machine`).

## Independent review

| Round | Outcome |
|---|---|
| Contract (§16.17) | 8 findings (1 high: the old state directory was reused), adopted as §16.18 |
| 1 (client) | 7 (2 medium: desktop Claude Code hidden, a shared runtime not restarted); fixed; recheck **READY** |
| 2 (final) | 3 (1 medium: a fresh sign-in sent the password to the stale setup's server), decided as §16.20 and fixed; recheck **READY**, 0 findings |

## Not yet verified (real machine and production)
1. **On osg-lap143:**
   - the v0.5.1 Codex hook under a real Codex CLI session, with its `/hooks` re-trust after the repair notice;
   - whether a Codex desktop-app session runs the hooks;
   - the Claude desktop app's embedded Claude Code being listed and running `~/.claude` hooks;
   - the real v0.5.0 → v0.5.1 update, including the repair notice;
   - the stale-setup flow with a real migrated config, and `unreadable` with a real foreign DPAPI blob.
2. **The app on a real desktop:** the new icons at 100% and 150% scaling, light and dark, and the Connect flow by hand.
3. **Production:** deploy the server (the `409` codes) before or with the release. A v0.5.1 client against a v0.5.0 server falls back to the generic error, which is harmless.
4. **Release notes (main):**
   - the Codex hook fix, and the one-time `/hooks` re-trust;
   - "Set up this computer as a new machine";
   - Connect Codex / Claude Code;
   - the new logo.
