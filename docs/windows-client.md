# RainCLI on Windows

**The RainCLI app is the default on Windows.** It is a per-user installer with a tray icon, built from the same stdlib client as the CLI: one client core with two front ends. It needs no Python, no admin rights and no terminal. The [Python client](#python-client-existing-and-advanced-installs) below remains for existing installs and for anyone who prefers it.

## The RainCLI app

### Install
1. Open the [latest release](https://github.com/DylanHallahan/raincli/releases/latest) of `DylanHallahan/raincli` and download `RainCLI-Setup-<X.Y.Z>.exe` and `RainCLI-Setup-<X.Y.Z>.exe.sha256`.
2. Check the download in PowerShell. The two values must be identical:
   ```powershell
   (Get-FileHash "$HOME\Downloads\RainCLI-Setup-X.Y.Z.exe" -Algorithm SHA256).Hash.ToLower()
   (Get-Content "$HOME\Downloads\RainCLI-Setup-X.Y.Z.exe.sha256").Split(' ')[0]
   ```
3. Run the installer. It installs for **your Windows account only**, into `%LOCALAPPDATA%\Programs\RainCLI`, without asking for admin rights.

**SmartScreen.** The installer is **not code-signed**, so Windows may say "Windows protected your PC". Choose **More info → Run anyway** only for a file you downloaded from the release page above whose checksum matched. If anything else shows that warning, don't run it.

The installer:
- starts RainCLI at logon (a `RainCLI` value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, pointing at `RainCLI.exe --background`);
- adds **RainCLI** and **Uninstall RainCLI** to the Start menu;
- puts the `raincli` command first on your user `PATH` (open a new terminal to use it);
- checks for the Microsoft Edge WebView2 Runtime, which the RainCLI window needs (Windows 11 includes it). If it is missing, the installer offers Microsoft's download page; without it the app still delivers messages, and the window opens once the runtime is installed;
- starts the app, whose window signs you in on its first run, or moves an existing install over (below);
- writes `installer-record.log` in the install folder, recording any startup entry for RainCLI that already existed, before it changes anything. It never overwrites a startup entry it hasn't recorded.

### Sign in
On its first run the RainCLI window asks for your RainCLI **email and password** and a **machine name**. The name defaults to the computer's name in handle form (lowercase letters, digits and dashes, for example `desktop-ab12cd`), and you can change it. If you belong to several teams, it asks which one. Signing in creates this machine on your team's **Machines** page, labelled "Signed in from <name>", with its own credential. Your password is sent once to `https://raincli.com` over HTTPS and is never stored or logged; the app never retries with it.

- **"That name is taken"**: a teammate uses it, or a revoked machine had it. Choose another name.
- **"You already have a machine with that name"**: signing in again on the same computer replaces its credential automatically. From another computer, the app asks you to confirm **replace machine <name>**, and that works only for a machine that has never received a message or had an inbox. Otherwise revoke the old machine on the website, then choose a new name.
- After several wrong passwords, sign-in pauses for a few minutes, on the website too.
- You can have up to 20 active machines in a team. Past that, sign-in says so; revoke one on the website first.

Sign-in stores the machine credential in `%USERPROFILE%\.config\raincli\agent.json`, encrypted with Windows DPAPI for your Windows account (`token_dpapi`) and protected by an owner-only ACL. DPAPI protects the file at rest; it does not protect against other programs running as you. Copied to another account or computer, it fails with "sign in again".

`raincli login` does the same from a terminal. It reads the password from a no-echo prompt only (never an argument, the environment or a file) and refuses without one.

### What the app does in this release
The RainCLI window shows your **Inbox** and conversations and the **Agents** in your teams, signed in as you, plus two pages of its own: **This computer** (the connection, the machine name, the version and update state, the routing policy and the coding agents on this machine, with Pause, Open log and Sign out) and **Settings** (who may message this computer's agents, trust, and updates). If RainCLI can't be reached, the window says so and offers **Retry**. Links to other sites open in your default browser.

Closing the window keeps RainCLI running in the tray. Opening **RainCLI** from the Start menu again brings the window back. The tray icon shows whether the machine is **ready**, **offline**, **updating** or in **error**, and its menu has **Open RainCLI**, **Pause/Resume**, **Open log**, **Sign in again** and **Quit**. A new message raises a Windows notification that names only the sender ("New message from …" or "Escalation from …"), never the text; clicking it opens the conversation.

Signing out (This computer) revokes the machine, deletes its credential and clears the window's private browser profile.

A machine signed in through the app runs in **machine mode**: it reports its presence, client version and agent list to your team, and it takes pushed updates. **It does not receive messages yet.** Messages sent to it are stored on the server until message routing arrives in a later release. Machines moved over from an existing connector setup (below) keep delivering exactly as before.

### Updates
Updates are automatic. When your team's operator sets a new version, the app:
1. looks up the stable release with exactly that tag in `DylanHallahan/raincli`;
2. downloads `RainCLI-Setup-<X.Y.Z>.exe` and its `.sha256` from that same release over HTTPS, accepting only `api.github.com`, `github.com`, `objects.githubusercontent.com` and `release-assets.githubusercontent.com` on port 443, checked on every redirect;
3. checks the SHA-256 and installs the new version beside the current one, silently;
4. switches to it and restarts the tray. If the new version doesn't come up within two minutes, it switches back and reports `rolled_back`.

Downgrades need the operator's explicit permission. The current and previous versions are kept; older ones are removed.

**Trust model:** TLS to GitHub plus write access to the repository. **Releases are unsigned.** The checksum detects a corrupted download, not a malicious release, and the release assets are not tied to the tag's commit. The server only names a version; it can never choose where the app comes from.

### Moving an existing install to the app
Run the installer. Its first run finds, in this order:
1. a managed install (`%USERPROFILE%\.raincli\client` and its `RainCLI` Run value);
2. an older pip or venv client (0.1.x or 0.2.x): `agent.json` at `RAINCLI_CONFIG` or `%USERPROFILE%\.config\raincli\agent.json`;
3. the connector configs that use it, and any `runtime.json` beside them.

It keeps your **handle, credential, connector configs and queues**: no new sign-in, no new machine and no new handle. It converts the token to DPAPI, keeps or writes a connector-mode `runtime.json` so delivery continues, starts the new runtime and checks that it's ready, and only then disables the old startup entry (recorded, not deleted). The old install's files stay where they are, but the old pip `raincli` command stops working; use the app's `raincli`. If an old connector window is still running, the app asks you to **close the old RainCLI window** to finish, and never kills it. Everything is logged, without secrets, to `migration.log` in the app's state directory.

### Uninstall
Use **Uninstall RainCLI** in the Start menu or Windows Settings. It first stops the app (`RainCLI.exe --quit`); if the app doesn't stop within two minutes, the uninstaller says so and changes nothing. It then removes the hook entries marked `raincli` from Claude Code and Codex, using the runtime config the app runs (from `app.json`, or the default). Then it asks whether to **sign this computer out** too (default **No**). Yes revokes the machine and deletes its credential; if that fails, the credential is kept and you're told. For a silent uninstall, pass `/SIGNOUT=yes` or `/SIGNOUT=no`, for example `unins000.exe /VERYSILENT /SIGNOUT=yes`.

What happens to each file (protocol §15.8 M10):

| Removed | Kept |
|---|---|
| the `RainCLI` Run value (only if it starts this app) | `agent.json`, unless you signed out |
| the `bin` entry on your user `PATH`, and `bin\` with the `raincli` command | your connector configs and queues |
| the Start menu entries | the runtime's state directory: `machine-salt`, status, `runtime.log` and connector logs |
| `versions\` (every installed version) | `migration.log` |
| the stub, `RainCLI.exe`, with its `_internal\` | `installer-record.log` |
| `install.json` and `install.json.new` | `app.json` (which configs the app runs) and `update-mode.json` (your manual or automatic choice), so a reinstall resumes as before |
| `heartbeat.json`, `update-state.json`, `update-lock\`, `app-lock\` and `state\downloads\` | `state\runtime.log` |
| the hook entries marked `raincli` | an old startup entry that migration disabled (recorded, not restored) |

A reinstall over a running app first stops it the same way (`RainCLI.exe --quit`) and then replaces the stub and the `raincli` command too, so a full install can repair them. If the app won't stop, Setup stops with a message and changes nothing. An update pushed by your team (`/UPDATE`) never replaces the stub or the `raincli` command.

### Install layout
```
%LOCALAPPDATA%\Programs\RainCLI\
  RainCLI.exe, _internal\   the stub: starts the current version and owns rollback; replaced only by a full install
  bin\raincli.exe, bin\_internal\   the CLI on PATH; runs the current version's raincli.exe
  versions\<X.Y.Z>\          each version: RainCLI-app.exe (window and tray) and raincli.exe (CLI)
  install.json               {"current", "previous", "probation"}
  app.json                   the agent and runtime config the app runs
  installer-record.log       startup entries found before the first change
```
Every part is a folder build: nothing runs from `%TEMP%`.

## Python client (existing and advanced installs)

The Python client remains Python 3.11+ with no third-party runtime dependencies. Use a local NTFS drive for credentials and connector state. Network shares, FAT/exFAT, cloud-synced state directories and Windows service installation are not validated.

### Install with PowerShell

The default is a **managed install** with automatic updates, as on Linux ([SETUP.md](../SETUP.md#1-install-the-managed-client-agent)). A bootstrap checkout installs it, and a `raincli` function then runs everything through the stable launcher:

```powershell
gh repo clone DylanHallahan/raincli "$HOME\src\raincli-repo"
Set-Location "$HOME\src\raincli-repo\raincli"
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-deps .
.\.venv\Scripts\raincli.exe runtime update --install          # the latest stable release -> $HOME\.raincli\client
function raincli { py -3 "$HOME\.raincli\client\launch.py" @args }
raincli --version
```

The managed environment has no `raincli.exe`. The function lasts for the current shell; to keep it, add the same line to your PowerShell profile (`notepad $PROFILE`), asking before editing an existing profile. Accept your invitation and **add the machine** as described in [SETUP.md](../SETUP.md#2-accept-the-invitation-and-add-the-machine-you), then replace `your-handle` below:

```powershell
raincli config init --api-url https://raincli.com --token-file "$HOME\Downloads\raincli-your-handle.json"
raincli whoami
# Only after whoami confirms your expected handle and team:
Remove-Item "$HOME\Downloads\raincli-your-handle.json"
raincli agents
raincli inbox --all
```

The config lives at `$HOME\.config\raincli\agent.json`. New private files and directories receive a protected Windows ACL allowing the current user, SYSTEM and Administrators. Credential reads verify the owner and ACL; Windows `chmod` is not used as a substitute. If a config has broader access, re-import your downloaded credential with `config init --force`. Do not paste tokens into command arguments, chat or reports.

The same send/reply/fetch commands work in PowerShell. Use quoted Windows paths and `(New-Guid).ToString()` when supplying `--id`. For the optional skill, copy the packaged `raincli_agent\skill\SKILL.md` to your agent's skill folder; ask before replacing existing instructions. This avoids Windows PowerShell 5.1's UTF-16 output redirection.

### Connector boundary

The native connector has Windows process locks and file handling. Automatic delivery needs Herdr 0.8.2 or later (native Windows support) and an explicitly mapped agent. Delivery through a real Herdr on native Windows is exercised in CI with a pinned Herdr 0.9.3, a headless throwaway session and a self-reporting fake agent ([Actions run 37258463853](https://github.com/DylanHallahan/raincli/actions/runs/37258463853), Windows Server 2022, Python 3.11 and 3.14): byte-exact non-ASCII text, quotes, shell metacharacters and newlines, a 15,900-character body, `blocked` → held, an unknown agent → held offline, the instant inbox in the directory, and an oversize command line held. A real Claude Code or Codex pane on Windows has not been exercised yet.

Private temporary files are flushed before publication. Windows replacement uses `MoveFileExW` with replace-existing and write-through flags, and attachment publication uses NTFS hard links without overwriting. Windows does not expose the POSIX directory-fsync guarantee; the smoke test covers normal operation and process termination, not power-loss recovery. Reparse-point files and managed attachment subdirectories are rejected.

### Runtime and startup

The runtime, presence, the agent list, startup and updates work as described in [SETUP.md](../SETUP.md#5-run-the-runtime-and-start-it-at-login-agent). Presence and the agent list are advisory, not delivery or receipt. On Windows the process-scan fallback uses `tasklist` and reports **type only**: the name is the type, the status is `unknown`, and no path or process id is sent. Use Windows paths in the runtime config, for example `$HOME\.config\raincli\runtime.json`:

```json
{"connectors": ["C:\\Users\\you\\.config\\raincli\\connector.json"], "state_dir": "C:\\Users\\you\\.raincli\\runtime"}
```

```powershell
raincli runtime run --config "$HOME\.config\raincli\runtime.json" --once
raincli runtime status --config "$HOME\.config\raincli\runtime.json"
raincli runtime stop --config "$HOME\.config\raincli\runtime.json"
```

**Logon startup** is part of the default setup, and you install it explicitly. It adds a `RainCLI` value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` for the current user only: no service, no scheduled task and no elevation. It uses `pythonw.exe` when available, so no console window stays open. Every argument is quoted and the config is stored as its resolved long path, for example `"…\pythonw.exe" "…\launch.py" "runtime" "run" "--config" "C:\Users\you\.config\raincli\runtime.json"`. It never contains a credential. Paths containing `"` are refused. Installing does not start the runtime. To start it now without a console, use the launcher directly: `pythonw "%USERPROFILE%\.raincli\client\launch.py" runtime run --config <runtime.json>`. That is `cmd` syntax; in PowerShell, write `"$env:USERPROFILE\.raincli\client\launch.py"`. Started through `pythonw`, the runtime has no console, and its output goes to `runtime.log` (below).

```powershell
raincli runtime startup --config "$HOME\.config\raincli\runtime.json"
Get-ItemProperty -Path HKCU:\Software\Microsoft\Windows\CurrentVersion\Run -Name RainCLI   # inspect
raincli runtime startup --remove          # removes the value; a running runtime continues until `runtime stop`
```

The command stored in the registry is limited to 260 characters. If installation refuses a longer one, use shorter install or config paths.

**Updates** use `$HOME\.raincli\client` and behave as on Linux ([SETUP.md](../SETUP.md#updates)): automatic by default for managed installs, installed immediately when your team's operator sets a new version, from the canonical GitHub repository only, with no periodic pull. `raincli runtime update --manual` opts out and `--automatic` opts back in. A v0.2.0 managed install turns automatic on at its first v0.3.0 run and writes a one-time notice to `runtime.log`; run `--manual` afterwards to stay manual. A pushed version that fails its first start is rolled back by the launcher and reported as `rolled_back`. **Releases are unsigned**; trust rests on TLS to GitHub plus the tag-to-commit resolution. Pushed updates need v0.3.0 or later. After the first managed install, run `runtime startup --config …` again so logon startup uses the launcher.

**Herdr on Windows.** The connector runs Herdr's own CLI (`herdr.exe agent get/list/prompt`), never a socket or pipe of its own, so Herdr handles the named pipe. Each call runs without a console window and in its own process group, and output is read as UTF-8. Recommended connector settings:
- `"expect_pane_id"` as the pin (Herdr's live cwd doesn't follow `cd` on native Windows; keep `expect_cwd` only where it is stable);
- `"herdr_session"` when the inbox lives in a named session, so the app's runtime, started at logon without your shell's environment, reaches the same session;
- leave `"herdr_bin"` unset to use `%LOCALAPPDATA%\Programs\Herdr\bin\herdr.exe` (Herdr's stable alias, kept current by `herdr update`), or set an absolute path.

Messages whose prompt would exceed Windows' command-line limit are held as `too_large_for_command_line`, never truncated.

**Inbox on Windows.** Delivery into Herdr on native Windows is exercised with a fake agent (see below), not yet with a real Claude Code or Codex pane. A Claude Code inbox through hooks (`next-turn`, Claude Code only, [SETUP.md step 4c](../SETUP.md#4c-claude-code-inbox-agent-no-herdr)) uses the same command, `raincli hooks install --claude --config "$HOME\.config\raincli\runtime.json"`. Next-turn delivery waits until that session is next used. A next-turn inbox waits for the session's next turn however long it is idle, as long as the runtime can see the session's process (Linux; Windows and macOS through a process lookup). Where it can't, an idle session counts as offline after 10 minutes and its messages wait for the next session. On Windows the runtime finds the process through a Toolhelp parent walk and its creation time. This is verified in CI on native Windows Server 2022 with both Pythons ([Actions run 36745865730](https://github.com/DylanHallahan/raincli/actions/runs/36745865730)): a recorded session stays live however long it idles and is gone once its process exits. The delivery hands over at most about 10,000 characters per turn, and shows waiting messages to their senders as `held` (`next_turn`). Hook installation and next-turn delivery on native Windows have not been exercised on a real Claude Code installation yet. **Codex hooks on Windows** need Codex 0.145.0 or later: `raincli hooks install --codex --config "$HOME\.config\raincli\runtime.json"`, then trust the hooks once in Codex's `/hooks` view (again after any reinstall that changes the command). The hook runs through `cmd.exe` as `"<root>\bin\raincli.exe" hook codex …`, so the paths may not contain `% ^ & | < > "`. A Codex session can then be a next-turn inbox (`"inbox": {"hook": "codex", …}`), with up to 32 KiB per turn. Until trusted, Codex sessions are listed by the type-only scan. The exact `cmd.exe /C` launch, a profile path with a space, the claim and liveness are exercised in CI against a pinned Codex 0.160.0, without a Codex session ([Actions run 37262063009](https://github.com/DylanHallahan/raincli/actions/runs/37262063009), Windows Server 2022, Python 3.11 and 3.14).

Windows-specific behaviour:
- The launcher stops the runtime by repeating `runtime stop` for up to 120 seconds, then uses `taskkill /T /F` as a last resort so no connector keeps the queue lock. A runtime normally needs at most 100 seconds; no new delivery starts after the stop, and queued messages stay durable.
- Started at logon through `pythonw.exe`, the runtime has no console. The launcher then writes its output to a private `$HOME\.raincli\client\runtime.log`, rotated to `runtime.log.1` once it passes 1 MiB when the runtime starts. Check it if availability never appears.
- It restarts a crashed runtime with backoff, but if the launcher itself is killed, nothing restarts it until the next logon. Connectors can then outlive it until logoff, because no Job object is used.
- Replacing the version pointer retries while the launcher has the file open.

### One-off verification

On September 28, 2026, revision `1390410` passed **10/10 checks on both Python 3.11.9 and 3.14.7** on native Windows Server 2022. [Successful Actions run](https://github.com/DylanHallahan/raincli/actions/runs/36479606073). The same revision passed all 195 existing Linux agent tests. The first Windows run exposed an 8.3 short-path false positive in shareable-context validation; the successful revision fixes it and checks junction ancestors explicitly.

[Manual Windows client smoke](../.github/workflows/windows-client-smoke.yml) has only a `workflow_dispatch` trigger, read-only repository permissions, and no production credentials. It installs the client without server dependencies on Windows Server 2022 with Python 3.11 and 3.14.

The smoke script exercises installed CLI entry points, downloaded-config import, Windows credential ACLs, send/reply/inbox, UTF-8 and CRLF attachment integrity, conflict handling, restart without duplicate submission, competing-process locks, killed-process lock recovery, and symlink/junction refusal. The relay is a local fake API and Herdr is a fake adapter. It does not test production HTTPS, PostgreSQL, a real Herdr session or desktop Windows 10/11.

The same manual workflow also runs `scripts/runtime-platform-smoke.py`. It starts real runtime and connector processes against a fake API with no Herdr available, and checks authenticated presence write-back, the single-runtime lock and graceful stop. It installs and removes a startup value under an isolated HKCU test key, not the real `Run` key, and compares it exactly with the expected quoted command. It then packages the checkout as a synthetic release, with no GitHub lookup, and exercises a staged install without pip plus a live update and rollback handoff through the launcher. Actions run 36583917581 passed all of these checks, including the live update and rollback handoff, on native Windows Server 2022 with Python 3.11 and 3.14. It does not cover a published release, a real Windows Herdr or a real logon.

To run it manually after installing the client and adding it to PATH:

```powershell
python scripts/client-platform-smoke.py
```

Run from the repository root. The script uses a disposable temporary directory and two synthetic identities, not your normal credentials.

## Windows app verification

[Manual Windows app build](../.github/workflows/windows-app-build.yml) builds the installer and its checksum on `windows-2022` and uploads them as a workflow artifact only; it fails if the bundle contains any test hook. [Manual Windows app e2e](../.github/workflows/windows-app-e2e.yml) builds 0.4.0 and 0.4.1 test installers from the same source and checks, against a throwaway in-job server: a silent per-user install, sign-in through `raincli login` on a pseudo console, the Run value, presence, a pushed upgrade, a rollback of a version whose tray never starts, and an explicit downgrade through the installer assets, uninstall with and without sign-out, and migration of both a pip-installed v0.2.0 foreground connector and a managed v0.3.2 install. Its fake release endpoint answers on the real GitHub hostnames through a hosts-file entry and a test root CA on that disposable runner; the shipped app has no override. Both workflows are manual only and use no secrets. See [release testing](release-testing.md).
