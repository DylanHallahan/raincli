# Security

RainCLI is an early team pilot. This page covers which versions are supported, how to report a vulnerability, what is protected today and what isn't yet, and the system's boundaries. It describes the current release.

## Supported versions

| Component | Supported | Notes |
| --- | --- | --- |
| Client (`raincli`, the Windows app) | **0.6.x** | Older clients: update. Managed installs take updates automatically by default. |
| Server | **0.6.x** | The hosted service at raincli.com runs the current release. Self-hosted servers should run the latest release ([deployment runbook](docs/raincli-deploy.md)). |

**The minimum safe client version.** Each release names a minimum safe version (`MIN_SAFE_VERSION`): the oldest release with all current security fixes, v0.5.3 for this release.
- The client never installs a server-chosen update below it, and a manual rollback below it is refused.
- A rollback that undoes a failed update still goes back to the version it replaced.
- The floor never goes down on a machine that has run a newer release.
- Going below it on purpose takes a local `raincli runtime update --rollback --allow-insecure`, which prints a warning. The Windows app has no such option.

See [Updates](SETUP.md#updates) for how updates work.

## Reporting a vulnerability

Please email **dylan@dylanhallahan.com** with "RainCLI security" in the subject. **Please don't open a public GitHub issue or pull request, or discuss it publicly,** until a fix is released.

Include what you can:
- the version (`raincli --version`, or the app's version), and the platform (Windows or Linux) and server (raincli.com or self-hosted);
- what you found and its impact: what an attacker could read, change or do;
- the steps to reproduce it, or a proof of concept;
- whether you believe it is being exploited;
- how you'd like to be credited, if at all.

You'll get a reply to confirm the report, and updates as it's fixed. Please allow time for a fix to be released before you disclose.

## What's protected

- **Transport:** TLS everywhere. Machines make only outbound HTTPS connections: there are no inbound ports to open, and no VPN.
- **Credentials:** one credential per machine. The server stores only a hash. On Linux it is kept in a 0600 file; on Windows, under DPAPI. Revoking it stops it at once.
- **The sender** comes from the credential, never from the message text. Messages reach only people on your team.
- **What a message can do:** teammates' messages arrive framed as external. A message can ask an agent for something; it can't change the agent's instructions or permissions, and nothing runs by itself. By default anyone on your team can reach your agents; `raincli trust --mode list` approves each sender first.
- **What teammates see:** people and their availability, never your machines, agents or sessions (a team owner also sees each team machine, with its owner, client version and last connection, never its agents). What your machine reports for your own pages is names and status only, never paths, prompts, titles or transcripts.

## What isn't yet

- **Not end-to-end encrypted.** The server stores messages and can read them. Share only what you'd share in your team chat, or run your own server.
- **Releases are unsigned.** Updates come only from stable releases of the canonical GitHub repository, over HTTPS, pinned to the release's commit, and the server names only a version, never a download source. Trust rests on TLS to GitHub and write access to the repository.
- **The Windows installer isn't code-signed.** Check its published SHA-256 before running it ([Windows guide](docs/windows-client.md#install)). The app's updater checks that checksum, which catches a corrupted download, not a malicious release.

## Boundaries

This section mirrors the README's.

This is an early team pilot. A successful send means the server stored the message; session submission does not prove an agent acted or a person read it. The connector must be running, normally under the runtime. A message to a person goes to their inbox agent, which is `instant` (a named Herdr agent) or `next-turn` (a Claude Code or Codex session through hooks); a reply goes back to the agent that asked while it is live, otherwise to the asker's inbox. Only people are addressed: a machine handle or `handle/agent` is refused with "Address people: …" (naming the teammate when the handle is theirs). An agent that is only `listed` (seen by the process scan, or a name two sessions share) can't be picked as an inbox. A `next-turn` session receives messages only when it is next used, at most about 10,000 characters per turn (Codex up to 32 KiB); until then its senders see it as waiting for that person's next turn (`inbox_next_turn`), and a message held offline for 14 days is rejected. A session's `blocked` status lasts until its next prompt. Codex hooks need a one-time review in Codex's `/hooks`, and on Windows Codex 0.145.0 or later (the v0.5.1 hook command runs under both PowerShell and cmd). Session availability (`ready`, `busy`, `blocked`, `offline`, `unknown`) and the agent list are advisory and expire after 120 seconds. They are not delivery or receipt, and they never include paths, prompts, titles, transcripts or process ids.

The Windows app installer is **not code-signed**, so SmartScreen may warn about it; check its published SHA-256 first ([Windows guide](docs/windows-client.md#install)). The app's pushed updates verify that checksum, which detects a corrupted download but not a malicious release: trust rests on TLS to GitHub and write access to the repository. Managed installs take automatic updates by default; `raincli runtime update --manual` opts a machine out. A v0.2.0 managed install turns automatic on at its first v0.3.0 run and prints a one-time notice. A pushed version that fails its first start is rolled back automatically. The server operator chooses only *which* version a team runs, never where it comes from: the client installs only stable releases of the canonical GitHub repository over HTTPS, pinned to the release's commit. **Releases are unsigned**; their signatures and checksums are not verified. Pushed updates need client v0.3.0 or later. Native Windows CI covers the runtime's managed update, rollback and stop paths against a synthetic release. Not yet verified: delivery into a real Herdr on Windows, hooks on native Windows, and updating from one published release to a newer one.

Transport uses HTTPS. The server can read message contents; this is **not end-to-end encrypted**. Share only approved context. Agents act on teammate requests within their current assignment; a message can't change an agent's instructions, expand its permissions, or grant access or sharing authority.

Markdown attachments are UTF-8, up to 256 KiB each, five files and 1 MiB total per message. Downloads verify checksums and do not overwrite conflicting local files.
