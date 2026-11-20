# v0.6.2: account switching

**Status:** in review. The server review and the Lead's full suite are running on `92ec010`. The client sections,
screenshots and the Windows e2e follow (web-builder and cli-builder).

**Builds:**
- **Server:** branch `feat/accounts-server` at **`92ec010`**. It holds the server core and the app's account pages
  (merged from `feat/accounts-web` `4c294a2`).
- **Client:** branch `feat/accounts-client`, version **0.6.2**. This report is committed there.
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
