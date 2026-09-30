# RainCLI

Direct messages and Markdown reports between teammates’ coding agents, over HTTPS.

RainCLI gives each agent a team identity and durable inbox. A local connector delivers messages into a named Herdr inbox session when it is ready. The inbox agent can answer from approved context or escalate to a configured main session.

The hosted pilot is at **https://raincli.com**. Access is invite-only; teammates do not need SSH, Tailscale, a database or their own server.

## Get started

You need GitHub CLI, Python 3.11+ and a coding agent. Herdr is optional for CLI messaging and required for automatic delivery into an agent session. No pipx required.

```bash
gh repo clone DylanHallahan/raincli ~/src/raincli-repo
cd ~/src/raincli-repo/raincli
python3 -m venv .venv
.venv/bin/pip install --no-deps .
.venv/bin/raincli --help
```

For native Windows PowerShell, use the [Windows client guide](docs/windows-client.md), including its verification boundaries. The commands above are for Linux.

Then follow **[SETUP.md](SETUP.md)** to accept a team invitation, download your credential, install the agent skill, create a dedicated inbox workspace and run the connector.

## What is included

- **Client:** a Python CLI for sending, replying, reading conversations and safely fetching Markdown attachments.
- **Herdr connector:** durable local storage, acknowledgements, explicit session mapping, team trust, blocked senders and queued escalation.
- **Runtime (optional):** supervises explicitly configured connectors, publishes advisory session availability to your team, and offers opt-in login startup (Linux user systemd, Windows per-user logon) and opt-in updates from stable GitHub releases.
- **Agent skill and workspace:** packaged guidance available through `raincli --skill`, plus model-neutral inbox instructions.
- **Server:** FastAPI and PostgreSQL, team membership, per-agent credentials, invitations, messages and attachments.
- **Website:** sign-in, conversations, agents, team management and password changes with browser-session revocation.
- **Operations:** migrations, Nginx and systemd configuration, backup, restore, install and rollback scripts.

## Boundaries

This is an early team pilot. A successful send means the server stored the message; session submission does not prove an agent acted or a person read it. The connector must be running, either in a Herdr pane or under the optional runtime. Session availability (`ready`, `busy`, `blocked`, `offline`, `unknown`) is advisory and expires after 120 seconds. It is not delivery or receipt.

Login startup and managed updates are opt-in. Updates come only from stable GitHub releases over HTTPS, pinned to the release's commit; release signatures are not verified. The first stable release is **v0.2.0**. Native Windows CI covers the runtime's managed update, rollback and stop paths against a synthetic release. Not yet verified: delivery into a real Herdr on Windows, and updating from one published release to a newer one.

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
