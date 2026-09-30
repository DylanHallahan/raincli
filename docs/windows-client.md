# Native Windows client

The client remains Python 3.11+ with no third-party runtime dependencies. Use a local NTFS drive for credentials and connector state. Network shares, FAT/exFAT, cloud-synced state directories and Windows service installation are not validated.

## Install with PowerShell

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

## Connector boundary

The native connector now has Windows process locks and file handling. Automatic delivery still requires a working Herdr executable and explicitly mapped session. A hosted runner's fake Herdr does **not** prove a real Windows Herdr installation. Until that integration is exercised, use the CLI directly on Windows or the previously exercised Linux/Herdr setup.

Private temporary files are flushed before publication. Windows replacement uses `MoveFileExW` with replace-existing and write-through flags, and attachment publication uses NTFS hard links without overwriting. Windows does not expose the POSIX directory-fsync guarantee; the smoke test covers normal operation and process termination, not power-loss recovery. Reparse-point files and managed attachment subdirectories are rejected.

## Runtime and startup

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

**Inbox on Windows.** Delivery into Herdr on native Windows is not yet verified (see below). A Claude Code inbox through hooks (`next-turn`, Claude Code only, [SETUP.md step 4c](../SETUP.md#4c-claude-code-inbox-agent-no-herdr)) uses the same command, `raincli hooks install --claude --config "$HOME\.config\raincli\runtime.json"`. Next-turn delivery waits until that session is next used. An idle session stays the target for as long as its process runs only when the runtime can determine that process on Windows; otherwise, after 10 minutes without a hook event, the session counts as `offline` and messages wait, held `offline`, until its next prompt. The delivery hands over at most about 10,000 characters per turn, and shows waiting messages to their senders as `held` (`next_turn`). Hook installation and next-turn delivery on native Windows have not been exercised on a real Claude Code installation yet. **Codex hooks are Linux and macOS only**, so on Windows Codex sessions are listed by the type-only scan.

Windows-specific behaviour:
- The launcher stops the runtime by repeating `runtime stop` for up to 120 seconds, then uses `taskkill /T /F` as a last resort so no connector keeps the queue lock. A runtime normally needs at most 100 seconds; no new delivery starts after the stop, and queued messages stay durable.
- Started at logon through `pythonw.exe`, the runtime has no console. The launcher then writes its output to a private `$HOME\.raincli\client\runtime.log`, rotated to `runtime.log.1` once it passes 1 MiB when the runtime starts. Check it if availability never appears.
- It restarts a crashed runtime with backoff, but if the launcher itself is killed, nothing restarts it until the next logon. Connectors can then outlive it until logoff, because no Job object is used.
- Replacing the version pointer retries while the launcher has the file open.

## One-off verification

On September 28, 2026, revision `1390410` passed **10/10 checks on both Python 3.11.9 and 3.14.7** on native Windows Server 2022. [Successful Actions run](https://github.com/DylanHallahan/raincli/actions/runs/36479606073). The same revision passed all 195 existing Linux agent tests. The first Windows run exposed an 8.3 short-path false positive in shareable-context validation; the successful revision fixes it and checks junction ancestors explicitly.

[Manual Windows client smoke](../.github/workflows/windows-client-smoke.yml) has only a `workflow_dispatch` trigger, read-only repository permissions, and no production credentials. It installs the client without server dependencies on Windows Server 2022 with Python 3.11 and 3.14.

The smoke script exercises installed CLI entry points, downloaded-config import, Windows credential ACLs, send/reply/inbox, UTF-8 and CRLF attachment integrity, conflict handling, restart without duplicate submission, competing-process locks, killed-process lock recovery, and symlink/junction refusal. The relay is a local fake API and Herdr is a fake adapter. It does not test production HTTPS, PostgreSQL, a real Herdr session or desktop Windows 10/11.

The same manual workflow also runs `scripts/runtime-platform-smoke.py`. It starts real runtime and connector processes against a fake API with no Herdr available, and checks authenticated presence write-back, the single-runtime lock and graceful stop. It installs and removes a startup value under an isolated HKCU test key, not the real `Run` key, and compares it exactly with the expected quoted command. It then packages the checkout as a synthetic release, with no GitHub lookup, and exercises a staged install without pip plus a live update and rollback handoff through the launcher. Actions run 36583917581 passed all of these checks, including the live update and rollback handoff, on native Windows Server 2022 with Python 3.11 and 3.14. It does not cover a published release, a real Windows Herdr or a real logon.

To run it manually after installing the client and adding it to PATH:

```powershell
python scripts/client-platform-smoke.py
```

Run from the repository root. The script uses a disposable temporary directory and two synthetic identities, not your normal credentials.
