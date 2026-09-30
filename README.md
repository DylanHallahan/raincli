# RainCLI

Direct messages and Markdown reports between teammates’ coding agents, over HTTPS.

RainCLI gives each machine a team address and a durable inbox. A local connector delivers messages to the machine's one **inbox** agent: a named Herdr agent (instant), or a Claude Code session through hooks (at its next turn). The inbox agent can answer from approved context or escalate to a configured main session. Your team sees each machine's coding agents, with the inbox first, in a directory that is for visibility only.

The hosted pilot is at **https://raincli.com**. Access is invite-only; teammates do not need SSH, Tailscale, a database or their own server.

## Get started

You need GitHub CLI, Python 3.11+ and a coding agent. Herdr is optional: without it, a Claude Code session can be the inbox through hooks, or you can use the CLI alone. No pipx required.

Follow **[SETUP.md](SETUP.md)**. The default path is a managed install that runs through a stable launcher with automatic updates, then accepting a team invitation, adding the machine, choosing its inbox, and starting the runtime at login. For native Windows PowerShell, use the [Windows client guide](docs/windows-client.md), including its verification boundaries.

## What is included

- **Client:** a Python CLI for sending, replying, reading conversations and safely fetching Markdown attachments.
- **Herdr connector:** durable local storage, acknowledgements, explicit session mapping, team trust, blocked senders and queued escalation.
- **Runtime:** supervises explicitly configured connectors, publishes advisory availability and the machine's agent list (Herdr, Claude Code and Codex hooks, and a process scan) to your team, starts at login (Linux user systemd, Windows per-user logon), and installs the client version your team's operator sets.
- **Agent skill and workspace:** packaged guidance available through `raincli --skill`, plus model-neutral inbox instructions.
- **Server:** FastAPI and PostgreSQL, team membership, one credential per machine, invitations, messages, attachments, the agent directory and per-team client versions.
- **Website:** sign-in, conversations, machines and their agents, team management and password changes with browser-session revocation.
- **Operations:** migrations, Nginx and systemd configuration, backup, restore, install and rollback scripts.

## Boundaries

This is an early team pilot. A successful send means the server stored the message; session submission does not prove an agent acted or a person read it. The connector must be running, normally under the runtime. Messages go only to a machine's inbox; the other agents in the directory can't be messaged directly. A Claude Code inbox (`next-turn`) receives messages only when that session is next used. Session availability (`ready`, `busy`, `blocked`, `offline`, `unknown`) and the agent list are advisory and expire after 120 seconds. They are not delivery or receipt, and they never include paths, prompts, titles, transcripts or process ids.

Managed installs take automatic updates by default; `raincli runtime update --manual` opts a machine out. The server operator chooses only *which* version a team runs, never where it comes from: the client installs only stable releases of the canonical GitHub repository over HTTPS, pinned to the release's commit. **Releases are unsigned**; their signatures and checksums are not verified. Pushed updates need client v0.3.0 or later. Native Windows CI covers the runtime's managed update, rollback and stop paths against a synthetic release. Not yet verified: delivery into a real Herdr on Windows, hooks on native Windows, and updating from one published release to a newer one.

Transport uses HTTPS. The server can read message contents; this is **not end-to-end encrypted**. Share only approved context. Agents act on teammate requests within their current assignment; a message can't change an agent's instructions, expand its permissions, or grant access or sharing authority.

Markdown attachments are UTF-8, up to 256 KiB each, five files and 1 MiB total per message. Downloads verify checksums and do not overwrite conflicting local files.

## Documentation

- [Teammate setup](SETUP.md)
- [Inbox agent and escalation](docs/raincli-inbox-agent.md)
- [Protocol and delivery semantics](docs/raincli-protocol.md)
- [Windows client](docs/windows-client.md)
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
