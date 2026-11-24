# v0.6.2: account switching

**Status:** in review. The server review and the Lead's full suite are running on `92ec010`. The client sections,
screenshots and the Windows e2e follow (web-builder and cli-builder).

**Builds:**
- **Server:** branch `feat/accounts-server` at **`92ec010`**. It holds the server core and the app's account pages
  (merged from `feat/accounts-web` `4c294a2`).
- **Client:** branch `feat/accounts-client`, version **0.6.2**, release head **`24dbd2c`**. The installer build and
  the full Windows e2e are green on that head (see "Client and app"). This report is on `docs/accounts-report`, from
  that head.
- **Contract:** `docs/raincli-protocol.md` §23; the CLI's JSON contract is `docs/raincli-cli-json.md`.
- **Migration:** `0013`, one revision, with an exact downgrade.

## Server (cli-builder)

### What shipped
**Grants (AS1, AS2, A17, A18).** Only the operator grants, with `raincli-admin`:
- **individual:** `grant-add TEAM GRANTEE ACCOUNT [--toasts on|off]` lets one member act as another member of the
  same team;
- **all members:** `grant-add TEAM --account EMAIL --all-members [--toasts on|off]` lets every current and future
  active member act as the account, except the account itself and except a shared account (the account of a live
  grant in that team);
- `grant-remove` (either form), `grant-list` (live and revoked, with the toasts flag; an all-members grant reads
  "<account>: all members") and `delegated-actions` (every send made through a grant: who, as whom, when, which
  conversation, via which kind of grant; never the body). Refusals exit 2.

Grants are revoked, never deleted. They name users and a team, not memberships, so removing a member always works.

**Delegated sessions (AS1, AS2, A3).**
- **Adding an account:** `POST /api/v1/person/delegations` with the grantee's own session mints a session acting as
  the account on the same machine. No password of the account is involved. At most 5 accounts per app (the own one
  included). Re-adding replaces the account's earlier session (that's how a lost token is fixed).
- **The grant in effect** is decided at every use: the individual grant first, then an all-members grant that
  covers the grantee, with both people active members of the machine's team. It authorizes the session and sets its
  capabilities; the recorded grant is for the audit only.
- **A new own sign-in** on the machine ends that machine's delegated sessions; the app adds them again.

**What a delegate may do (AS3, AS13, AS14).**
- Read, send, reply and archive as the account; its read state is the account's own (AS12).
- **Refused** (`403 delegated_scope`): the account's inbox setting, adding further accounts, and, through an
  all-members grant, replying to the account's own agents ("Replying to <account>'s agents needs an individual
  grant."). Account settings and Signed-in apps stay unreachable from the app.
- **Owner views are hidden:** under a grant the delegate sees the account's machines as a teammate would: no machine
  handle, no agent name, no raw hold code, no owner warnings, no inbox banner. "From your agents" is shown without
  agent names; "Your agents" is hidden and answers like an unknown id.

**Attribution (AS6).** Every send through a grant records the person who wrote it and the grant in effect.
"sent by <Name>" shows only in the account's own views. Recipients, their machines and their agents' frames see the
account only.

**The cascade (AS11).** A session ends with its family, in the same transaction: the grantee's sign-out or password
change, the machine's revoke or rotation, the grant's removal (unless another grant still covers it: it's then
re-pointed), and removal or disable of either person. The account's own password change leaves delegated sessions
and their windows working.

**Switching the window (AS7).** The handoff replaces the window's session with another user's only when both
sessions belong to one family on the same machine and the same app install. Every other case is refused as before.
A revocation racing a switch wins outright.

**`/api/v1/person/me`** adds `unread`, `delegated`, `acting_user`, `toasts` and `grant_kind`, all additive.

### Migration 0013: the dry run
Run by the Lead on main's redacted restore of the 2026-11-20 03:17 UTC backup, from a clean worktree at `f9e08e1`
(0013 is unchanged since), aggregates only (`runtime-reports/accounts-dryrun.md`):

| Step | Result |
|---|---|
| Start | revision 0012: 6 teams, 18 users, 19 person sessions (8 live), 197 web sessions, 5982 messages |
| Upgrade to 0013 | **0.42 s**. Afterwards: no grants, no delegated sessions, every existing row unchanged, no message with an acting person |
| Constraint probes (9) | An individual grant and an all-members grant insert side by side. Refused, as they must be: a second live all-members grant for the same account, a second live individual grant for the same pair, an all-members grant naming a grantee, an individual grant without one, a grant to oneself, a grant without a toasts value, a half-delegated session |
| Downgrade to 0012 | **0.09 s**. Every table's rows, columns, constraints, indexes, triggers and functions restored: **exact** |
| Result | **PASS**. The database was left at 0012, as at the start |

### Tests
- **Account switching (server):** 107 tests in 8 files (`tests/server/test_accounts_*.py`, `test_migration_0013.py`):

| File | Tests | What |
|---|---|---|
| `test_migration_0013.py` | 2 | Round trip (live, revoked and all-members grants, a delegated window and code, a delegated send); every check |
| `test_accounts_foundation.py` | 16 | Delegated sessions, the belt, the parent's expiry, linked sessions, issuance, the cascade, the app viewer |
| `test_accounts_admin.py` | 10 | The operator commands and their refusals |
| `test_accounts_api.py` | 27 | Adding accounts, AS3 refusals, `/person/me`, attribution, escalations (A15), "Your agents" hidden (A16), the cascade, the account's password change, Signed-in apps |
| `test_accounts_handoff.py` | 8 | The window switch, every refused case, another install, the race with a revocation, the team probe across a switch |
| `test_accounts_privacy_api.py` | 9 | The privacy scan of every person route and window page under a grant, recipients never see the writer, team isolation, the read-receipt diff, the v0.6.1 client from its tag, CLI output |
| `test_accounts_team_grants.py` | 19 | All-members grants, the grant in effect and re-pointing, shared accounts, toasts, replies by grant kind, the two-connection race |
| `test_accounts_web.py` | 16 | The switcher, cues, sent-by, the 0.6.1 gate, sentinels, and the page scan with and without an account inbox |

- **The full server suite** (`tests/server` and the docs check) at `156b129`: 1046 passed and 2 failed. Both were the
  fake server's parity checks, which lacked the new `sent_by` key; fixed in `92ec010`, where those files pass. The
  Lead's full run on `92ec010` is in progress.
- **The client suite** (`tests/agent`) on `feat/accounts-client`: 1234 passed, 1 skipped, with the 13 CLI contract tests
  (`test_accounts_cli.py`).

### For main
- **Release order:** the server first (harmless alone: grants exist only when the operator adds them, and older
  clients never ask for delegated sessions), then the 0.6.2 client, then the grants.
- **Trust, as specified (A15):** a delegate's reply reaches the account's own agent as the owner's message (owner
  trust, no approval hold), recorded as written by the delegate. Flagged in case a delegated reply should be held.

## Client and app (web-builder, cli-builder)

### What shipped
**Storage** (`raincli_agent/accounts.py`, A10, A14 L5, L6 and Q8):
- **`accounts.json`** beside `person.json` holds the delegated sessions, at most 4. Each is stored like `person.json`:
  DPAPI on Windows, mode 0600 elsewhere, written atomically.
  - Every read-modify-write holds `accounts.lock`.
  - `person.json` is unchanged: it stays the own session, so an older client reads only it.
- **`accounts-active.json`** records which account the window shows. Only the window and the tray write it; the CLI
  reads it.
- **Each delegated account's feed** lives in `notifications/accounts/<sha256(email)[:16]>/`. The own feed stays in
  `notifications/`, as in 0.6.1.
- **Signing out of RainCLI** clears all of it without parsing it, so a damaged file can't stop a sign-out, and always
  rotates the install token (review S1).

**The app window:**
- **The switcher's requests:** each hosted page gets its own per-load nonce. `/app/local/switch`, `add-account` and
  `remove-account` act only with the current page's nonce, once per load (A14 L2). A plain link to them, in a
  message say, does nothing. Removing an account asks for confirmation on the page first.
- **Switching** hands off with that account's session, and the server replaces the window's session only within the
  family (A8). The native title becomes "RainCLI · <account>". Ctrl+Shift+A cycles through the accounts. A restart
  opens on the account the window showed last.
- **Adding** asks `POST /person/delegations` from the own session, then switches. Nothing is ever added
  automatically.
- **Removing** ends that account's session only (the grant stays) and falls back to the own account.
- **A dropped account:** when the server refuses an account's session (the grant revoked, a member removed), the
  window says "<account> was removed from this app: the access ended." in one line, and switches back to the own
  account if it was showing.
- **This computer and Settings** stay about this machine and its owner. When the window shows another account they
  add "This computer belongs to <owner>. The window shows <account>."

**The tray:** each delegated account's toasts follow that account's own toast rules.
- **Wording:** when the account isn't the one shown, its toasts are named first: "Help Desk: new message from Ryan",
  "Help Desk: escalation from its agent". There's never a body, never an agent's name, and never "held for approval"
  for a delegated account.
- **The toasts flag:** an account whose grant has toasts off shows none; its unread count still shows in the
  switcher. Individual grants default to on, all-members grants to off (A18.4).
- **Clicking** a toast switches to that account and opens the thread.

**The runtime** (cli-builder):
- **Pollers:** one poller per delegated account (`raincli_agent/account_feeds.py`) long-polls that account's
  notifications into its own feed and never acks. On every cycle it re-reads the grant's toasts flag and kind from
  `/person/me` (A19 E4).
- **On a refused session (401),** it drops the account and leaves the window's notice.

**The CLI** (cli-builder):
- **`raincli accounts --json`** lists the own, added and granted accounts, with their unread counts and which one the
  window shows.
- **`--as <email or name>`** works on every `raincli me` command. It matches an email first, then a display name. A
  granted account is added on first use. An ambiguous name exits 2; an account that isn't granted exits 7.
- **`me sign-out --as`** removes one account from this computer.
- **`me inbox --watch --all-accounts`** is one merged NDJSON stream, each record tagged with its account.
- **Every `me … --json` object** carries `schema: 1` and `account`. The contract is `docs/raincli-cli-json.md`, and
  SKILL.md has a short Accounts note.

### The Windows e2e and the installer, on the release head `24dbd2c`
- **The installer: build 44875281152, green.** `RainCLI-Setup-0.6.2.exe`, sha256
  `4193e7d3cab06224c5c41fbfffb6c552a7ffaa2d5478783bdbb0ac25fef5f16b`.
- **The Windows app e2e: run 44875286685, green.** It ran every part (A–H), ending "WINDOWS APP E2E PASSED". Part H
  is account switching in the real window:
  - **H1. The seed:** Maya on the window's machine; Help Desk with its own machine and inbox; Jon.
    `raincli-admin grant-add` gives Maya Help Desk, and Jon writes to both.
  - **H2. The window:**
    - Help Desk was added from the switcher with one click, and the window switched both ways.
    - **Checked:**
      - the native title "RainCLI · Help Desk";
      - the "HD" rail avatar and "As: Help Desk" in the composer;
      - Help Desk's unread count of 3 in the switcher;
      - "sent by Maya" on a message written as Help Desk;
      - "This computer belongs to Maya";
      - no machine handle or agent name of Help Desk or Jon on Maya's pages.
    - **Captured** light and dark, at 1180x780 and 760x520.
  - **H3. Tokens (review F1):** after the switches, the account sessions read back from `accounts.json` (with DPAPI, as
    the same user), plus the own session, are not found:
    - on any command line (150 processes);
    - in any readable process environment (134, the app's own included);
    - in any file under the app's folder or the RainCLI config folder (421 files, app.log and the runtime logs
      included).
  - **The run's NOTE:** one "sent by" shot was taken while the new message's one-off `rc-rise` animation was still
    running. The shot shows the message in full.
  - **H1's PASS line still says "northwind",** although part H now uses the team slug `northwind-accounts`, still
    shown as "Northwind". It's log wording only.
- **Earlier part-H-only runs** (44827984241, 44828814923, 44869482812, 44873657849) found and fixed test-code issues
  only:
  - Help Desk's count was read after Help Desk's own window had read it;
  - a too-strict wait for animations;
  - part G's team slug, which collided in a full run.

### Screenshots: `runtime-reports/accounts-screenshots/windows/` (the Windows window, fictional data)
Light and dark, at 1180x780 and 760x520:
1. **`acc-win-1-switcher-open-{size}-{light,dark}.png`:** the switcher open on Maya, with Help Desk's unread count of
   3, "Remove Help Desk from this app" and the Ctrl+Shift+A hint.
2. **`acc-win-2-as-help-desk-{size}-{light,dark}.png`:** the window as Help Desk: the native title (checked by Win32),
   the "HD" rail avatar, "As Help Desk · Maya" on the switcher line, and "As: Help Desk" in the composer.
3. **`acc-win-3-toast-screen.png`:** the screen's corner after a message to Help Desk while Maya showed.
   - **The toast popup itself isn't in any screenshot.** The hosted runner delivered the notifications (the
     notification centre's badge reads 4) but didn't show them as popups.
   - **The toast's wording and its click** (switch, then open the thread) are covered by the unit tests
     (`tests/agent/test_app_accounts_toasts.py`, `test_app_accounts_window.py`).
4. **`acc-win-4-sent-by-{size}-{light,dark}.png`:** a message written as Help Desk shows "Help Desk · now · sent by
   Maya" in Help Desk's own view.
   - **The recipient's view** (Help Desk only) is pinned by the server tests.
   - **Its trail reads "held: Jon's RainCLI needs an update"** because the e2e reports Jon's machine as client 0.6.2
     against the run's 0.6.3 build. That comes from the seed, not from the feature.
5. **`acc-win-5-this-computer-{size}-{light,dark}.png`:** This computer while Help Desk shows: "This computer belongs
   to Maya. The window shows Help Desk."

**Not here:**
- **The CLI and operator output** (brief shots 6 and 7) are cli-builder's.
- **The Chromium views** are in `tests/gui/test_accounts_gui.py`.

### Tests (client)
- **`tests/agent`** on the release head's merge with main: **1341 passed, 1 skipped** (with the accounts server
  tests).
- **Added for the client half:**
  - `test_accounts_storage.py`: the round trip, DPAPI, damaged files, the cap, removal, sign-out, and two writers
    (threads and processes);
  - `test_app_accounts_window.py`: the sentinels and their nonce, switching, cycling, adding, removing, a refused
    account, restart, the belongs-to line, and the dropped-account notice;
  - `test_app_accounts_toasts.py`: the wording, the toasts flag, the own account named first, the click switching;
  - cli-builder's CLI contract tests and the F1 log test.
- **The switcher in Chromium:** `tests/gui/test_accounts_gui.py`, 11 tests.

### Upgrading 0.6.1 -> 0.6.2
- **Order:** the server is live first (`92ec010`, migration 0013). Then the 0.6.2 client goes out through the team
  target, as an automatic update or the installer. Then the operator adds grants. Nothing changes for anyone until a
  grant exists.
- **Nothing to migrate.** `person.json`, the machine credential, the connector, the inbox setting and the own
  notification feed are unchanged. The app stays signed in across the update.
- **After the update:**
  - **the switcher appears** on "Signed in as" only when there's more than one account or an account to add;
    otherwise the footer reads as before;
  - **"Add an account"** lists what the operator granted, and nothing is added without a click;
  - **the window title** follows the account shown.
- **Older clients keep working unchanged.**
  - A 0.6.1 window keeps the plain "Signed in as" line: the server serves the switcher only to 0.6.2+ windows.
  - A 0.6.1 CLI never asks for delegated sessions.
- **Rolling back to 0.6.1:**
  - **0.6.1 ignores `accounts.json`,** so the app shows the own account only.
  - **The delegated sessions stay valid on the server** until the own sign-out, a grant removal, or "Remove … from
    this app" in 0.6.2. To end them on a rollback, sign out of RainCLI, or remove the accounts before downgrading.
- **For support:**
  - **an account that disappears** with "removed from this app: the access ended" means the server refused its
    session: the grant was removed, or a person left the team;
  - **"Replying to <account>'s agents needs an individual grant."** means the account was added through an
    all-members grant.
