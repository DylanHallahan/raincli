# RainCLI inbox agent: operator setup

Recommended mapping: a **dedicated inbox agent** in its own Herdr tab receives RainCLI messages, so routine team traffic doesn't interrupt your main work session.

| Component | Handles | Status |
| --- | --- | --- |
| Connector (`raincli connector run`) | Durable receipt and acknowledgement, attachments, retries, trust policy, holding messages while the target is busy, escalation queueing and notification | **Implemented and tested** (protocol §5, §8, §10) |
| Inbox agent (an ordinary Claude, Codex or other agent in a Herdr pane) | The role in its `INBOX.md`: by default answering, follow-up questions, collaboration and escalation | **Operator setup**, described in this document. RainCLI doesn't run or supervise it beyond Herdr prompts |
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
cp -r ~/src/raincli-repo/docs/templates/inbox-agent ~/herdr/inbox-agent    # INBOX.md rules and role, STATE.md, agent adapters
#   fill in STATE.md (owner, identity, shareable path, main-session mapping)
herdr tab create --label "RainCLI inbox" --cwd ~/herdr/inbox-agent --no-focus    # note .result.root_pane.pane_id
herdr agent start raincli-inbox --kind claude --pane <root-pane-id>
herdr agent prompt raincli-inbox "Operator assignment from <owner>: you are the RainCLI inbox agent ..."   # full text: SETUP.md step 4
```

The inbox agent's working directory is its own workspace, `~/herdr/inbox-agent`, not the shareable folder. The shareable folder is referenced read-only from `STATE.md` and the connector config.

- **Operator bootstrap is required.** In the live pilot, a fresh inbox agent received a message correctly and verified the attachment checksum. It then declined to reply, because the only authorization it had seen was inside the message body, and that is correct behaviour.
- **What authorizes it:** the operator's own instructions. These are the workspace files (`INBOX.md`/`STATE.md`, loaded via `AGENTS.md`/`CLAUDE.md`) plus a one-time assignment prompt sent by the owner through Herdr. With those in place, the agent acts on teammate requests within that assignment without asking again.
- **What never does:** RainCLI messages. A teammate's message can't change the agent's instructions, expand its permissions, or grant access or sharing authority.

`INBOX.md` has two parts:
- **Transport rules** apply to every setup: messages are requests handled within the assignment; use the reply, fetch and escalate commands with your config; never reveal credentials; share only approved context; and don't acknowledge receipt.
- **Default role (edit to fit your setup):** answer from the approved shareable context, follow up, collaborate and escalate. Widen it in that file, for example to allow implementation in a named checkout, or narrow it. Without such an edit, the inbox has no access to other files, vaults or deployments.

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

Each message arrives in the layout from protocol §5, with the inbox block from §10 before the message body. The block tells the agent:
- to answer, ask follow-ups and continue the conversation with the reply command. There is no turn cap;
- which approved shareable context it may share from;
- that there is no need to acknowledge receipt, which is tracked automatically;
- how to escalate what it can't handle. The escalation summary states the original question, what was checked and what is missing:
  ```bash
  raincli connector escalate --config ~/.config/raincli/connector.json <message-id> --body-file -
  ```
The line before the body says it is a teammate request to act on within the current assignment, which can't change the agent's instructions or permissions.

Attachments arrive as local paths under the connector's state directory, listed as teammate files to read as needed.

## 7. Escalations

`connector escalate` durably queues an escalation, and the connector then handles it as follows:
1. It shows a visible Herdr notification, "RainCLI escalation".
2. It waits until the mapped main session is `idle` or `done` and its pins match.
3. It submits the summary.

`submitted` means the summary was handed to the main session. It does **not** mean you have seen it. A timeout becomes `submission_uncertain`, which is never auto-resubmitted.

When you have dealt with it, reply to the sender if useful and run:

```bash
raincli connector escalation-done --config ~/.config/raincli/connector.json <escalation-id>
```

## Limits (honest scope)

- The optional `raincli runtime` only supervises connectors and reports availability; it does not start or drive agents. The inbox agent is only as capable as the agent you start, and it acts through ordinary Herdr prompts.
- Whether an answer is correct, and whether it stays inside the shareable context, depends on the inbox agent following its instructions. The connector cannot enforce what an agent reads.
- Notifications are local to your Herdr session. They don't reach your phone or email.
