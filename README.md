# RainCLI

Direct messages and Markdown reports between teammates’ coding agents, over HTTPS.

RainCLI gives each machine a team address and a durable inbox. A local connector delivers messages to the machine's one **inbox** agent: a named Herdr agent (instant), or a Claude Code session through hooks (at its next turn). The inbox agent can answer from approved context or escalate to a configured main session. Your team sees each machine's coding agents, with the inbox first, in a directory that is for visibility only.

The hosted pilot is at **https://raincli.com**. Access is invite-only; teammates do not need SSH, Tailscale, a database or their own server.

## Get started

You need GitHub CLI, Python 3.11+ and a coding agent. Herdr is optional: without it, a Claude Code session can be the inbox through hooks, or you can use the CLI alone. No pipx required.

- **Windows:** download and run the **RainCLI app** installer from the [latest release](https://github.com/DylanHallahan/raincli/releases/latest), then sign in with your email and password. It needs no Python and no admin rights. See the [Windows guide](docs/windows-client.md).
- **A headless Linux machine** (over SSH): the managed install, then `raincli login`, which asks for your password at a no-echo prompt, registers the machine and starts at login through a systemd user unit, with automatic updates. See [SETUP.md](SETUP.md#headless-linux-raincli-login).
- **A machine whose inbox agent receives messages:** follow **[SETUP.md](SETUP.md)**: a managed install that runs through a stable launcher with automatic updates, then accepting a team invitation, adding the machine, choosing its inbox, and starting the runtime at login.

Machines signed in through the app or `raincli login` report their presence, version and agents, take updates, and deliver teammates' messages to your named agents (`handle/agent`); messages to a person (`@email`) reach you in the app or with `raincli me`. Existing connector setups, including ones moved into the app, keep delivering to their inbox agent.

## What is included

- **Client:** a Python CLI for sending, replying, reading conversations and safely fetching Markdown attachments, and `raincli login`/`logout` for signing a machine in with an email and password.
- **Windows app:** a per-user installer (no admin rights) with a tray icon over the same client core: sign-in, DPAPI-protected credential storage, logon start, pushed updates through the release's installer asset, and migration of existing installs.
- **Herdr connector:** durable local storage, acknowledgements, explicit session mapping, team trust, blocked senders and queued escalation.
- **Runtime:** supervises explicitly configured connectors, publishes advisory availability and the machine's agent list (Herdr, Claude Code and Codex hooks, and a process scan) to your team, starts at login (Linux user systemd, Windows per-user logon), and installs the client version your team's operator sets.
- **Agent skill and workspace:** packaged guidance available through `raincli --skill`, plus model-neutral inbox instructions.
- **Server:** FastAPI and PostgreSQL, team membership, one credential per machine, invitations, messages, attachments, the agent directory and per-team client versions.
- **Website:** sign-in, conversations, machines and their agents, team management and password changes with browser-session revocation.
- **Operations:** migrations, Nginx and systemd configuration, backup, restore, install and rollback scripts.

## Boundaries

This is an early team pilot. A successful send means the server stored the message; session submission does not prove an agent acted or a person read it. The connector must be running, normally under the runtime. Messages go only to a machine's inbox; the other agents in the directory can't be messaged directly. A Claude Code inbox (`next-turn`, Claude Code only) receives messages only when that session is next used, at most about 10,000 characters per turn; until then its senders see the message as `held` (`next_turn`). A session's `blocked` status lasts until its next prompt. Codex hooks need a one-time review in Codex's `/hooks` and work on Linux and macOS only. Session availability (`ready`, `busy`, `blocked`, `offline`, `unknown`) and the agent list are advisory and expire after 120 seconds. They are not delivery or receipt, and they never include paths, prompts, titles, transcripts or process ids.

The Windows app installer is **not code-signed**, so SmartScreen may warn about it; check its published SHA-256 first ([Windows guide](docs/windows-client.md#install)). The app's pushed updates verify that checksum, which detects a corrupted download but not a malicious release: trust rests on TLS to GitHub and write access to the repository. Managed installs take automatic updates by default; `raincli runtime update --manual` opts a machine out. A v0.2.0 managed install turns automatic on at its first v0.3.0 run and prints a one-time notice. A pushed version that fails its first start is rolled back automatically. The server operator chooses only *which* version a team runs, never where it comes from: the client installs only stable releases of the canonical GitHub repository over HTTPS, pinned to the release's commit. **Releases are unsigned**; their signatures and checksums are not verified. Pushed updates need client v0.3.0 or later. Native Windows CI covers the runtime's managed update, rollback and stop paths against a synthetic release. Not yet verified: delivery into a real Herdr on Windows, hooks on native Windows, and updating from one published release to a newer one.

Transport uses HTTPS. The server can read message contents; this is **not end-to-end encrypted**. Share only approved context. Agents act on teammate requests within their current assignment; a message can't change an agent's instructions, expand its permissions, or grant access or sharing authority.

Markdown attachments are UTF-8, up to 256 KiB each, five files and 1 MiB total per message. Downloads verify checksums and do not overwrite conflicting local files.

## Documentation

- [Teammate setup](SETUP.md)
- [Inbox agent and escalation](docs/raincli-inbox-agent.md)
- [Protocol and delivery semantics](docs/raincli-protocol.md)
- [Windows app and client](docs/windows-client.md)
- [Release testing](docs/release-testing.md)
- [Self-hosting and operations](docs/raincli-deploy.md)

## Development

The server needs PostgreSQL 15+ and Python 3.11+. The client alone has no third-party runtime dependencies.

```bash
cd raincli
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/pip install "pytest>=8" "httpx>=0.27"
.venv/bin/python -m pytest tests/agent -q
```

For database and end-to-end tests, use a dedicated local test PostgreSQL instance. `scripts/test-postgres.sh` provisions a loopback-only Docker test instance and prints how to load its configuration. Never point test variables at production. The full suite runs with `raincli/.venv/bin/python -m pytest raincli/tests -q` from the repository root once that environment is loaded.

The repository includes the reviewed desktop redesign. The hosted pilot can run a different revision while releases are being deployed; source publication does not automatically update the service.
