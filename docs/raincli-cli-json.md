# RainCLI CLI JSON contract (schema 1)

For tools that render RainCLI for a person (protocol §23, A11). It covers `raincli accounts`, every `raincli me`
command with `--json`, `--as`, and the merged watch. The tool runs `raincli` as the same user that signed the machine
in; sessions stay in the machine's private files (DPAPI on Windows, mode 0600 elsewhere) and never appear in argv,
the environment, a log or any output.

**Stability:** within schema 1, fields are only ever ADDED. A key listed here never changes its meaning or type, and
is never removed. A breaking change would be `schema: 2`. Tests pin every key and type
(`tests/agent/test_accounts_cli.py`).

## Accounts

`raincli accounts --json` prints one object:

```json
{"schema": 1, "accounts": [
  {"email": "maya@example.com", "display_name": "Maya", "team": "pilot", "kind": "own", "grant": null,
   "added": true, "unread": 2, "toasts": null, "active": true},
  {"email": "helpdesk@example.com", "display_name": "Help Desk", "team": "pilot", "kind": "granted",
   "grant": "individual", "added": true, "unread": 3, "toasts": true, "active": false},
  {"email": "release@example.com", "display_name": "Release", "team": "pilot", "kind": "granted",
   "grant": "all_members", "added": false, "unread": null, "toasts": false, "active": false}
]}
```

| Key | Type | Meaning |
|---|---|---|
| `email` | string | The account. Pass it to `--as`. |
| `display_name` | string | Its name in the team. |
| `team` | string | The team's slug (the machine's team; accounts never span teams). |
| `kind` | `"own"` or `"granted"` | The machine owner's own account, or one the operator granted. The own account comes first. |
| `grant` | `"individual"`, `"all_members"` or null | The grant in effect (null for the own account). |
| `added` | bool | Whether this app holds a session for it. A granted account is added by the app's "Add an account", or by the first `--as`. |
| `unread` | int or null | The account's own unread count; null when not added. |
| `toasts` | bool or null | Whether the app shows toasts for it (the grant's flag; null for the own account). |
| `active` | bool | Whether the app window shows it now. Read-only here: only the window and its tray change it. |

## `raincli me … --json`

- **Every JSON object printed** by a `raincli me` command carries `"schema": 1` and `"account": "<email>"` (the
  account the command acted as), beside the command's own keys:
  - `me inbox --json`: one message object per line (NDJSON), each with `schema` and `account`;
  - `me read --json`: `{"message": {...}, "schema", "account"}`;
  - `me send` / `me reply --json`: `{"message": {...}, "created": bool, "schema", "account"}`;
  - `me conversations --json`: `{"conversations": [...], "schema", "account"}`;
  - `me archive` / `me unarchive --json`: `{"conversation": {...}, "schema", "account"}`.
- **Message objects** are the server's person-API message JSON (protocol §21.6, §23.4, §23.5). For a message the
  account sent through a grant, `sent_by` is `{display_name, email}` of the person who wrote it, in the account's own
  view only; otherwise null.

## `--as`

`raincli me <command> --as <email|name> …` acts as that account:
1. an exact email (any case) first, then an exact display name, among the own, added and granted accounts;
2. a granted account not yet added is added on first use (a session is minted and stored, as the app does);
3. ambiguous: exit 2, naming the emails;
4. unknown, or not granted to this machine's person: exit 7.

`raincli me approve` never takes `--as` (exit 2). `raincli me sign-out --as <account>` removes that account from this
app (its session only; the grant stays).

## The merged watch

`raincli me inbox --watch --all-accounts --json` streams NDJSON, one record per new message:

```json
{"schema": 1, "account": "helpdesk@example.com", "message": {"id": "…", "…": "…"}}
```

- One worker per account (the own one and every added one); a message relevant to two accounts appears once per
  account. Records are deduplicated by (account, message id).
- It never acks and never marks anything read.
- `--timeout S` exits 4 when nothing arrived in S seconds; an account whose session ends leaves the stream.

## Errors and exit codes

With `--json`, a refusal of the account (`not_granted`) or of the grant's scope (`delegated_scope`) prints
`{"schema": 1, "error": {"code": "<code>", "message": "<sentence>"}}` on stdout. Other errors go to stderr.

| Exit | Meaning |
|---|---|
| 0 | Done. |
| 1 | An error (the message is on stderr). |
| 2 | Usage: a bad argument, an ambiguous `--as`, `--as` on `approve`, `--all-accounts` without `--watch`. |
| 3 | Forbidden or conflict (an id already used, a file conflict). |
| 4 | `--watch --timeout`: nothing arrived. |
| 5 | Capacity (an inbox or quota is full). |
| 6 | The server is unreachable. |
| 7 | `not_granted`: `--as` names no account this machine may act as; or `delegated_scope`: the grant in effect doesn't allow it (for example, replying to an account's own agents through an all-members grant). |
