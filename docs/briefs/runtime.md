# Runtime increment

Outcome: keep configured agent inboxes reachable across login/restart, publish their availability to teammates, and provide a controlled client update path on Linux and Windows.

Use the existing registered RainCLI handle as the destination. Every published session must have an explicit connector config and credential. Do not scrape or publish unrelated sessions, working directories, transcripts, pane IDs or private context. A sender chooses among registered handles using reported status; the connector always rechecks its exact local mapping before delivery.

Presence: authenticated write for the credential's own agent, team-scoped directory read, server receipt timestamps, 120-second expiry, ready/busy/blocked/offline/unknown. The runtime checks configured sessions every 30 seconds. Hooks may later request an early refresh but cannot replace expiry/heartbeat. Message delivery receipts retain their existing meaning.

Runtime: supervise explicitly configured connector processes with bounded restart backoff, one runtime per state directory, no token in arguments, and persistent queues. State/status remains local and private. Linux user systemd and Windows per-user logon startup are opt-in. Startup must not invent a Herdr environment or silently retarget sessions.

Updates: explicit operator opt-in; release-based only, never arbitrary branch tips or message-provided update URLs. Use the canonical public GitHub repository. Stage a separate environment, verify the installed CLI, switch only after checks, preserve config and queues, retain the previous environment for rollback. Automatic checks may notify before automatic installation is enabled.

Verification: real isolated PostgreSQL API tests for ownership, scope, validation and expiry; fake Herdr/runtime boundaries; Linux regression tests; manual native Windows Actions. Production credentials and messages excluded from tests. Publish verified changes and document any remaining live-Herdr boundary.
