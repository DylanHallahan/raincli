# RainCLI inbox agent: operator setup

Recommended mapping: a **dedicated inbox agent** in its own Herdr tab receives RainCLI messages, so routine team traffic doesn't interrupt your main work session.

| Component | Handles | Status |
| --- | --- | --- |
| Connector (`raincli connector run`) | Durable receipt and acknowledgement, attachments, retries, trust policy, holding messages while the target is busy, escalation queueing and notification | **Implemented and tested** (protocol §5, §8, §10) |
| Inbox agent (an ordinary Claude, Codex or other agent in a Herdr pane) | Triage: answering, follow-up questions, conversation, escalation | **Operator setup**, described in this document. RainCLI doesn't run or supervise it beyond Herdr prompts |
| Main session | Human judgment and consequential actions | Unchanged. Escalations reach it only through an explicit mapping |

The connector never uses the focused pane, the current pane or any fallback. If a mapped agent is missing or its pins don't match, messages and escalations wait, and `raincli connector status` shows why.

## 1. Credentials

Register an agent for the inbox (for example the handle `yourname-inbox`) on the website's **Agents** page. Download its config once and store it at `~/.config/raincli/agent.json` with mode 0600. Never paste the token into a prompt, a command line or a vault note.

```bash
raincli whoami            # confirms handle and team
```

## 2. Approve shareable context

The inbox agent may answer teammates **only** from material you have approved for sharing with the team:

1. Create a dedicated folder, for example `~/raincli-shareable/`.
2. Copy into it only the notes the team may read. Never point it at a whole notes vault or home directory.

The connector lists these paths in the inbox agent's prompt and never reads them itself. Keep the folder current, because stale notes lead to stale answers.

## 3. Create the inbox tab and agent (Herdr)

These commands run inside Herdr (`HERDR_ENV=1`). Keep focus on your work tab.

```bash
cp -r ~/src/raincli-repo/docs/templates/inbox-agent ~/herdr/inbox-agent    # INBOX.md role, STATE.md, agent adapters
#   fill in STATE.md (owner, identity, shareable path, main-session mapping)
herdr tab create --label "RainCLI inbox" --cwd ~/herdr/inbox-agent --no-focus    # note .result.root_pane.pane_id
herdr agent start raincli-inbox --kind claude --pane <root-pane-id>
herdr agent prompt raincli-inbox "Operator assignment from <owner>: you are the RainCLI inbox agent ..."   # full text: SETUP.md step 4
```

The inbox agent's working directory is its own workspace, `~/herdr/inbox-agent`, not the shareable folder. The shareable folder is referenced read-only from `STATE.md` and the connector config.

- **Operator bootstrap is required.** In the live pilot, a fresh inbox agent received a message correctly and verified the attachment checksum. It then declined to reply, because the only authorization it had seen was inside the external message body, and that is correct behaviour.
- **What authorizes it:** the operator's own instructions. These are the workspace files (`INBOX.md`/`STATE.md`, loaded via `AGENTS.md`/`CLAUDE.md`) plus a one-time assignment prompt sent by the owner through Herdr.
- **What never does:** RainCLI messages. They remain external data and never grant or widen authority.

Record the pane ID and working directory. You pin them in the connector config, so a restarted or moved agent can't silently receive another identity's messages.

## 4. Connector config (`~/.config/raincli/connector.json`, mode 0600)

```json
{
  "agent_config": "~/.config/raincli/agent.json",
  "mode": "inbox",
  "trust_mode": "team",
  "blocked_senders": [],
  "herdr_agent": "raincli-inbox",
  "expect_pane_id": "<inbox pane id>",
  "expect_cwd": "/home/you/herdr/inbox-agent",
  "shareable_context": ["/home/you/raincli-shareable"],
  "escalation": {
    "herdr_agent": "<your main agent name>",
    "expect_pane_id": "<main pane id>",
    "notify": true
  }
}
```

- `trust_mode: "team"` delivers messages from every enrolled teammate without per-message approval. Use `"list"` together with `trusted_senders` / `raincli connector trust` to restrict delivery. `blocked_senders` always holds messages from those handles.
- The escalation target must be a different agent from the inbox agent. If it is omitted, `connector escalate` refuses to run and the inbox agent should tell the sender it can't answer.
- **Direct delivery** to a work session remains available as an explicit choice: set `mode: "direct"`, point `herdr_agent` at that session, and use `trust_mode: "list"`.

## 5. Run the connector

```bash
raincli connector run --config ~/.config/raincli/connector.json          # foreground, in its own pane
raincli connector status --config ~/.config/raincli/connector.json       # queue, holds, escalations
```

The connector must run inside Herdr, because it calls `herdr agent get/prompt` and `herdr notification show`. Run it in a pane of the inbox tab.

## 6. What the inbox agent receives

Each message arrives as the standard wrapper (protocol §5), followed by the inbox guidance block from §10. The block covers the following:
- It may answer, ask follow-ups and continue the conversation with `raincli reply`. There is no turn cap.
- It may use only the approved shareable context.
- It should not send content-free acknowledgements, because receipt is tracked automatically.
- When it can't answer, it escalates. The escalation summary states the original question, what was checked and what is missing:
  ```bash
  raincli connector escalate --config ~/.config/raincli/connector.json <message-id> --body-file -
  ```
- Consequential actions still need the user.

Attachments arrive as local paths under the connector's state directory. They are external data.

## 7. Escalations

`connector escalate` durably queues an escalation, and the connector then handles it as follows:
1. It shows a visible Herdr notification, "RainCLI escalation".
2. It waits until the mapped main session is `idle` or `done` and its pins match.
3. It submits the summary.

`submitted` means the summary was handed to the main session. It does **not** mean you have seen it. A timeout becomes `submission_uncertain`, which is never auto-resubmitted.

When you have dealt with it, reply to the sender if appropriate and run:

```bash
raincli connector escalation-done --config ~/.config/raincli/connector.json <escalation-id>
```

## Limits (honest scope)

- There is no autonomous runtime. The inbox agent is only as capable as the agent you start, and it acts through ordinary Herdr prompts.
- Whether an answer is correct, and whether it stays inside the shareable context, depends on the inbox agent following its instructions. The connector cannot enforce what an agent reads.
- Notifications are local to your Herdr session. They don't reach your phone or email.
