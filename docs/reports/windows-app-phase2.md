# Messaging and send-to-any-agent routing, Phase 2: implementation report

**Status: review-ready; every Phase 2 check passes.**
- **Branch:** `feat/phase2`, based on `main` (latest merge `7c20428`). The code verified and reviewed below is at **`ca2097b`**; this report is committed on top of it and changes no code.
- **Version:** 0.5.0.
- **Size:** 170 files changed, +17,855 / −1,111.

The main agent owns the merge to `main` (including the two approved workflow changes, `5755a8b` and `73c8896`), the v0.5.0 release with its release notes, deployment (migration `0006` and both Nginx files) and the rollout. None of these has been done.

The binding contract is `docs/raincli-protocol.md` **§16** with its amendments **§16.12–§16.16**. The approved design, main's answers and the user's decisions are in the design document.

## What changed

### Routing: send to any agent (§16.1, §16.2, §16.7)
- **Endpoints:** a machine (its inbox, exactly as before), an **agent** (`handle/name`, a Herdr agent name or a hook session name), or a **person** (`@email` within the team).
- **Every agent reports reachability:**
  - `instant`: a named Herdr agent;
  - `next-turn`: a named Claude Code or Codex hook session;
  - `listed`: anything else. Duplicate names are `listed` and `ambiguous`.
- **The server routes at send time, in this order:**
  1. `inbox-only` machines refuse agent endpoints;
  2. a live, deliverable agent is accepted;
  3. a `listed` or ambiguous agent is refused with the reason;
  4. an agent known within 14 days but offline is accepted and held offline;
  5. otherwise `unknown_agent`.
  
  Machines default to `routing: all`. `raincli routing --inbox-only` is the opt-out.
- **Delivery:** one connector per machine delivers to any named local agent: Herdr by name, and hooks through a handover box keyed by name. There is never a fallback, and duplicate names hold as `target_ambiguous`. Machine-mode machines (app or `raincli login`) get this connector automatically.
- **Old clients:**
  - **The capability gate:** only a v0.5 delivering connector polls with `routing=1`, and only a machine whose presence reports v0.5.0+ is marked routing-capable.
  - **The floors:** named-agent messages live only in local states that v0.4 never delivers, and a routing-capable runtime refuses targets and rollbacks below v0.5.0 (C1).
  - **The test:** proves that the real v0.4.0 queue loader delivers none of them, using a vendored, byte-checked fixture.

### Messaging as a person (§16.3, §16.4, §16.6)
- **The person session:**
  - issued by `/app/login` on request, or added to an existing or migrated machine with `raincli login --person`, without rotating the credential;
  - bound to the user and the issuing machine, with 30 days idle and 180 days absolute;
  - revoked by sign-out, revoking the machine, any credential rotation, a password change, deactivation, and the website's **Signed-in apps** list.
- **The person API:** inbox long-poll, conversations, send and reply (the reply's team comes from the parent), attachments, ack, and sign-out. Team scope is checked on every request.
- **The website now sends as the person**, renders Markdown on the server (`markdown-it-py`, HTML off, a link allowlist, hash-pinned), and shows reachability for every agent.
- **The headless CLI has full parity:** `raincli me inbox [--watch] | read | send | reply | fetch | approve | sign-out`, `raincli send <endpoint> [--from-agent]`, `raincli routing` and `raincli trust`. Bodies come from a file or stdin only.
- **Escalation to the owner** (`"escalation": {"to": "owner"}`, `kind: escalation`) is allowed only from a machine to its own owner. Existing Herdr escalations are unchanged.

### Trust and framing (the user's decisions)
- **Trust:** machine-mode machines default to **`team`** (direct delivery). `raincli trust --mode list` holds every other sender for `raincli me approve <id> [--always]`. A message from the same owner's other machine is always trusted (`from_same_owner`, decided by the server).
- **C17 framing,** for every delivery (Herdr and next-turn, inbox and named agents):
  - a header marks the message as coming from an **external teammate, not the user**, with a From line (person, machine, agent, team; display names and agent names JSON-quoted);
  - **one** line says the message carries no authority to approve prompts or change permissions or settings;
  - the teammate's words follow, with every line prefixed `| `.
  
  Pinned tests cover the text, the order, forged headers and end lines, every line-break form, and ANSI and bidi characters. The reviewer checked the framing in every round. It stays light and does not discourage normal collaboration.

### The Windows app window (§16.10, design §10, visual direction A "Quiet")
- **One process:** a single **WebView2** window (pywebview) plus the tray, with the runtime child as before. tkinter is removed.
  - **Local bundled pages:** sign-in (the password goes only to the client core), This computer, Settings (routing, trust, update mode, pause, log, sign out) and a clean **offline** page with Retry.
  - **Hosted pages:** the inbox, threads and agents, in the site's new **app-mode** layout (rail, conversation list, thread, compose).
  - **Design tokens:** neutral `--rc-*` tokens shared by the site and the app, with visual direction A adopted. The product name and logo are settings.
- **Sign-in handoff:** the app gets a single-use, 60-second code tied to its person session and to a random **per-install token**. The app sends that token only on requests to the service, in its WebView2 User-Agent. A link opened in any other browser, or a copied cookie, does nothing. The rail shows "Signed in as …". Nginx and the server never log the token. It rotates on sign-out, sign-in, install and reinstall.
- **Local API and navigation:** the local API needs a per-load nonce and the exact local origin. The window may show only the service and the local origin; anything else opens in the default browser. `app.log` records origins and paths only.
- **Toasts** show only "New message from <name>" or "Escalation from <machine>", never body text, from a private, bounded notification queue.
- **Start menu fix (v0.4.0 defect):** the v0.5 stub opens the window with no arguments or `--open`. Machines updated in place, which keep the v0.4 stub, have their shortcut rewritten to `--background`.
- **Packaging:** pywebview is hash-pinned in the app bundle only; the CLI stays stdlib. The bundle check refuses the WebView2 debugging switches, and `debug=False`. The installer detects a missing WebView2 Runtime.

### Windows delivery
- **Herdr (Herdr ≥ 0.9.3):**
  - UTF-8 decoding, no console window, an explicit `--session`;
  - the stable `herdr.exe` path, an **absolute `.exe` only** (never `.cmd`/`.bat`, never the current directory), and a bound on the command-line length;
  - `agent_not_ready` held offline.
- **Codex hooks on Windows (Codex ≥ 0.145.0):** next-turn only (no `codex queue`). That means `commandWindows` through the PATH shim, cmd-safe paths (including `!`), `additionalContextLimit` 9000, and liveness through `cmd.exe`. The user approves the hooks once in Codex's `/hooks`; RainCLI never writes trust.
- **Claude Code hooks:** the same next-turn path, verified on the Windows runner with the hook command. Real sessions are a manual check.

### Server, deploy and CI
- **Migration `0006`:** endpoints, `known_agents`, routing, person sessions, handoff codes and app-mode web sessions. The downgrade refuses while Phase 2 endpoints exist.
- **Nginx:** the token is redacted in the access log; the handoff is logged without its query string; new person-API locations. `docs/raincli-deploy.md` says v0.5.0 requires reinstalling both Nginx files.
- **Dependencies:** the server's `requirements.lock` is fully hash-pinned, and `install.sh` uses `--require-hashes`.
- **New manual workflow** `server-gui-tests.yml`, on `main`: the full suite with PostgreSQL plus the Playwright GUI tests; it fails if any GUI test is skipped.
- **The Windows app e2e is extended:** the build installs pywebview, part D drives the real app window over CDP, and screenshots are uploaded.

## Verification (all at `ca2097b` unless noted)

| Check | Result |
|---|---|
| Full suite, real PostgreSQL, local (Playwright not installed locally, so its GUI tests skip here) | **1327 passed, 5 skipped, 0 failed** |
| Linux `scripts/runtime-platform-smoke.py` | **PASS, every stage** |
| `server-gui-tests.yml` (full suite plus the Playwright GUI tests, none skipped), run **37409404699** | **SUCCESS: 1342 passed, 3 skipped (Windows file sharing, the PostgreSQL-restart e2e and the vendored-tag comparison, which needs tags a shallow checkout lacks); 16 GUI tests, 0 skipped** |
| Windows app build, run **37409406375** | **SUCCESS** |
| Windows app e2e, run **37409408520** | **SUCCESS: all 19 stages (A1–A7, D1–D9, B, C)** |
| Windows client smoke (Python 3.11 and 3.14), run **37409410394** | **SUCCESS on Python 3.11 and 3.14** |

### The Linux smoke
It includes:
- Codex hooks against the real Codex 0.160.0, with a profile path containing a space; the next-turn claim was 4,548 tokens, byte-exact;
- real Herdr 0.9.3 delivery to two named agents and a named hook session, each framed with C17;
- a person session against a real server: headless `login --person`, then `me inbox --watch`, `read`, `fetch`, `reply` and `send`, with no password, session or token in any output;
- headless login on a pty;
- the machine-mode runtime, pushed updates and rollback.

### The Windows e2e
- **A1–A7:** per-user install, CLI sign-in with DPAPI, logon start, pushed 0.5.1, rollback of a broken 0.5.2, downgrade refused then allowed, and uninstall with sign-out.
- **D1–D9, the real app window over CDP:**
  - a full install writes `stub` 2 and an `install_stamp`;
  - **no debugging port without the job's variable**;
  - the Start menu rewrite;
  - local sign-in, then the install-bound handoff, then "Signed in as …";
  - read, reply and send as the person;
  - This computer and Settings through the sentinel;
  - offline, then Retry;
  - sign-out, with no token in the server log.
- **B:** migration of an old pip v0.2.0 client.
- **C:** migration of a managed v0.3.2 install.

### The Windows client smoke
Real `herdr.exe` 0.9.3 delivery and the Codex hook launch exactly as Codex builds it, on Python 3.11 and 3.14.

### Product bugs found by the tests and fixed
- **e2e part D:** a navigation the window cancels on purpose was shown as offline (`7bcc205`), and a stale load timer could replace a page that had loaded (`e51a5a3`).
- **Server CI:** the C1 test depended on a git tag that a shallow checkout lacks (`b537a88`, test only).

## Independent review

| Round | Scope | Outcome |
|---|---|---|
| Contract 0 | §16 before the build | 16 findings (2 high), all adopted as §16.12 |
| 1 | Herdr adapter | 4 (2 medium security: `.cmd` via `cmd.exe`, current-directory planting); fixed; recheck READY |
| 2 | Codex hooks on Windows | 3 low; fixed |
| 3 | Server half | 6 (1 medium: routing capability from any CLI); fixed, with a stronger install-bound handoff against login CSRF |
| 4 | App window | 2 (the §16.15 Start menu fix, token scope); fixed |
| 5 | Client | 3 low; C17 framing confirmed; fixed |
| 6 | Integration at `b0161ad` | **READY**, 4 low (L1–L4); fixed in `dd731cb` |
| 6B | `b537a88`, `dd731cb` | **READY**, 0 findings |

Main, and through main the user, made five decisions during the build:
- trust defaults to `team`;
- the light C17 framing;
- `routing: all` by default;
- no `codex queue`;
- the Nginx token redaction and token lifetime.

## Not yet verified
1. **Real agents on Windows:** real Claude Code and Codex panes in Herdr, real hook sessions, and the Codex `/hooks` approval. CI uses fake agents and reproduces the exact Codex launch.
2. **A real Windows desktop:** look and feel at 100% and 150%, light and dark, toasts and their click-through, SmartScreen, a missing WebView2 Runtime, the second-launch "show window" path, and an **in-place v0.4 → v0.5 update** (the shortcut rewrite is unit-tested; the e2e installs fresh).
3. **The real-release e2e** (`real_from`/`real_to` v0.5.0 → a later release), once installer assets are attached.
4. **Production:**
   - migration `0006`;
   - **reinstalling both Nginx files**;
   - `install.sh --require-hashes` on the host;
   - confirming that `access.log` contains no `RainCLIApp/` token after deploy;
   - the pilot machines updating in place to v0.5.0.
5. **Release notes (main):** they must state the `routing: all` default and the `raincli routing --inbox-only` opt-out (§16.5), the `team` trust default with `raincli trust --mode list`, Codex ≥ 0.145.0 and the one-time `/hooks` approval on Windows, Herdr ≥ 0.9.3, and the Start menu fix.
6. **macOS:** not covered.

## Workers and reports
The builders were cli-builder (the Herdr adapter, Codex hooks, client routing and delivery, person messaging, framing) and web-builder (the server half, the website, the app window, packaging, the Windows e2e and the site update), with an independent reviewer, each in an isolated worktree. Their reports stay outside the repository, under `~/Projects/.worktrees/runtime-reports/`:
- `phase2-{feasibility,codex,herdr,design}.md`;
- `p2-{survey,herdr,codex,server,client,app}.md`;
- `p2-review-{0..6}.md`.
