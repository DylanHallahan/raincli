"""What the app-mode thread shows beyond the messages themselves (protocol §19.3, §19.4, §19.6, §19.7).

- **Names** (§19.4): from the conversation's endpoints, never from message text. An agent is
  *owner · agent*, a machine inbox *owner · inbox*, a person their display name.
- **The delivery trail** (§19.7): only under the viewer's own messages, with the steps that apply to the
  recipient kind, and held, uncertain or rejected in place of the remaining steps, with the reason in words.
- **Header presence** (§19.3): for an agent recipient only, its reachability chip and Ready, Busy or Offline
  from the same live directory as the Agents page. Offline once its machine hasn't reported for 2 minutes,
  and then no chip. Nothing here says what an agent is doing with a particular message.
- **The sender warning** (§19.6): a machine that has never connected, hasn't connected for a day, or hasn't
  picked up a message for a day. Display only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from raincli_server import identity, messaging, owners, presence
from raincli_server.models import Agent, AgentCredential, AgentPresence, DeliveryEvent, Message, User

WARNING_AFTER = timedelta(hours=24)
OFFLINE_AFTER = timedelta(seconds=presence.TTL_SECONDS)  # 2 minutes

AGENT_STEPS = ("stored", "received", "submitted", "replied")
INBOX_STEPS = ("stored", "received", "replied")  # a person or a machine inbox
STEP_LABELS = {"stored": "stored", "received": "received", "submitted": "submitted", "replied": "replied"}
OUTCOMES = {"held": "held", "submission_uncertain": "uncertain", "rejected": "rejected"}

# The hold reasons a sender can see (§16.12 C8 and the connectors' holds), in words.
HOLD_WORDS = {
    "approval_required": "waiting for approval",
    "sender_blocked": "the recipient doesn't accept messages from this sender",
    "busy": "the session is busy",
    "blocked": "the session is blocked",
    "offline": "the recipient is offline",
    "next_turn": "waiting for the session's next turn",
    "target_ambiguous": "two agents there share that name",
    "too_large_for_command_line": "too long to hand to the session",
    "too_large_for_hook": "too long to hand to the session",
    "owner_unreachable": "the machine's owner can't be reached",
    "client_update_needed": "the recipient's RainCLI needs an update",
}
UNCERTAIN_WORDS = "the hand-off was interrupted; it may not have reached the session"
REJECTED_WORDS = "declined on the recipient's machine"


def hold_words(reason: str | None) -> str:
    if not reason:
        return "waiting on the recipient's machine"
    return HOLD_WORDS.get(reason, reason.replace("_", " "))


# Names (§19.4) -------------------------------------------------------------------------------------

class Names:
    """Shown names for one team, worked out once per request (one owner query per team)."""

    def __init__(self, db: Session):
        self.db = db
        self._teams: dict[uuid.UUID, dict] = {}

    def owner(self, agent: Agent) -> str:
        shown = self._teams.get(agent.team_id)
        if shown is None or agent.owner_user_id not in shown:
            owner = self.db.get(User, agent.owner_user_id)
            shown = self._teams[agent.team_id] = owners.displays(self.db, agent.team_id, [owner])
        return shown[agent.owner_user_id]

    def endpoint(self, ep: messaging.Endpoint) -> str:
        """*owner · agent*, *owner · inbox* or the person's display name."""
        if ep.kind == "person":
            return ep.user.display_name
        if ep.kind == "agent":
            return f"{self.owner(ep.agent)} · {ep.name}"
        return f"{self.owner(ep.agent)} · inbox"

    def sender(self, ep: messaging.Endpoint) -> str:
        """The meta line's name: like ``endpoint``, but a machine sender is *owner · handle*."""
        if ep.kind == "machine":
            return f"{self.owner(ep.agent)} · {ep.agent.handle}"
        return self.endpoint(ep)


def address(ep: messaging.Endpoint) -> str:
    """The header's second line: ``handle/agent``, ``handle`` or the person's email."""
    return ep.label().lstrip("@")


# The delivery trail (§19.7) ------------------------------------------------------------------------

@dataclass
class Step:
    name: str
    reached: bool
    at: datetime | None = None


@dataclass
class Trail:
    steps: list[Step]
    outcome: str | None = None  # held, uncertain or rejected
    reason: str = ""
    events: list[DeliveryEvent] = field(default_factory=list)

    @property
    def phrase(self) -> str:
        """What a screen reader hears, as one phrase."""
        if self.outcome:
            return f"Delivery: {self.outcome}, {self.reason}"
        reached = [s.name for s in self.steps if s.reached]
        pending = [s.name for s in self.steps if not s.reached]
        last = reached[-1] if reached else "not yet stored"
        return f"Delivery: {last}" + (f", not yet {pending[0]}" if pending else "")

    @property
    def tone(self) -> str:
        return {"held": "warn", "uncertain": "warn", "rejected": "danger"}.get(self.outcome or "", "")


def trail_for(message: Message, recipient: messaging.Endpoint, events: list[DeliveryEvent],
              hold_reason: str | None) -> Trail:
    steps = AGENT_STEPS if recipient.kind == "agent" else INBOX_STEPS
    state = message.delivery_state
    shown = "held" if state == "stored" and hold_reason else state
    first = {}
    for ev in events:
        first.setdefault(ev.state, ev.created_at)
    times = {"stored": message.created_at, "received": message.acked_at or first.get("received"),
             "submitted": first.get("submitted"), "replied": first.get("replied")}
    if shown == "replied":
        reached_upto = len(steps)
    elif shown == "submitted":
        reached_upto = steps.index("submitted" if "submitted" in steps else "received") + 1
    elif message.acked_at is not None or shown == "received":
        reached_upto = steps.index("received") + 1  # picked up; held, uncertain and rejected happen after this
    else:
        reached_upto = 1  # stored, including a hold the server derives before it is picked up
    outcome = OUTCOMES.get(shown)
    trail_steps = [Step(name, i < reached_upto, times.get(name) if i < reached_upto else None)
                   for i, name in enumerate(steps)]
    if outcome:
        trail_steps = trail_steps[:reached_upto]  # the outcome replaces the remaining steps
    reason = ""
    if outcome == "held":
        reason = hold_words(hold_reason)
    elif outcome == "uncertain":
        reason = UNCERTAIN_WORDS
    elif outcome == "rejected":
        reason = REJECTED_WORDS
    return Trail(trail_steps, outcome, reason, events)


# Header presence (§19.3) ---------------------------------------------------------------------------

@dataclass
class HeaderPresence:
    chip: str | None  # "Instant" or "Next turn", only for a live agent
    word: str  # Ready, Busy or Offline
    tooltip: str


def header_presence(db: Session, peer: messaging.Endpoint, now: datetime | None = None) -> HeaderPresence | None:
    """For an agent recipient only (person and machine-inbox conversations show neither). Two indexed reads:
    the machine's presence row and its live directory entries."""
    if peer.kind != "agent":
        return None
    now = now or identity.now()
    machine = peer.agent
    row = db.get(AgentPresence, machine.id)
    if machine.revoked_at is not None or row is None or now - row.seen_at >= OFFLINE_AFTER:
        return HeaderPresence(None, "Offline", "Its machine hasn't reported in the last 2 minutes.")
    entries = [e for e in presence.live_machine_agents(db, [machine.id]).get(machine.id, []) if e.name == peer.name]
    entry = next((e for e in entries if not e.ambiguous), None)
    if entry is None:
        return HeaderPresence(None, "Offline", "Its machine doesn't list this agent at the moment.")
    chip = {"instant": "Instant", "next-turn": "Next turn"}.get(entry.reachability or "")
    if entry.status == "idle":
        return HeaderPresence(chip, "Ready", "The session was idle at its last report.")
    if entry.status in ("working", "blocked"):
        return HeaderPresence(chip, "Busy", "The session was working at its last report.")
    return HeaderPresence(None, "Offline", "The session wasn't running at its last report.")


# The sender warning (§19.6) ------------------------------------------------------------------------

@dataclass
class Warning:
    handle: str
    kind: str  # never, not_connected or not_picking_up
    since: datetime | None = None


def machine_warning(db: Session, machine: Agent, now: datetime | None = None) -> Warning | None:
    """Two indexed reads: the machine's newest credential use, and its oldest unacknowledged message
    (``ix_messages_recipient_pending``, migration 0009)."""
    if machine.revoked_at is not None:
        return None
    now = now or identity.now()
    last_used = db.scalar(select(func.max(AgentCredential.last_used_at)).where(AgentCredential.agent_id == machine.id))
    if last_used is None:
        return Warning(machine.handle, "never")
    if now - last_used > WARNING_AFTER:
        return Warning(machine.handle, "not_connected", last_used)
    oldest = db.scalar(
        select(Message.created_at).where(Message.recipient_agent_id == machine.id, Message.acked_at.is_(None))
        .order_by(Message.seq).limit(1))
    if oldest is not None and now - oldest > WARNING_AFTER:
        return Warning(machine.handle, "not_picking_up", oldest)
    return None


def endpoint_warning(db: Session, ep: messaging.Endpoint | None) -> Warning | None:
    """A machine or agent recipient only, never a person."""
    if ep is None or ep.kind == "person":
        return None
    return machine_warning(db, ep.agent)


def recipient_warning(db: Session, team_ids: list[uuid.UUID], text: str) -> Warning | None:
    """The warning for a recipient typed on the compose page (``handle`` or ``handle/agent``), in whichever of
    the viewer's teams has it. A person (``@email``) or anything unknown has none."""
    from raincli_server import routing
    from raincli_server.messaging_errors import MessagingError
    from raincli_server.web.queries import endpoint_from_text

    text = (text or "").strip()
    if not text or text.startswith("@") or len(text) > 140:
        return None
    try:
        ref = routing.parse_endpoint(endpoint_from_text(text))
    except MessagingError:
        return None
    for team_id in team_ids:
        try:
            return endpoint_warning(db, routing.resolve(db, team_id, ref))
        except MessagingError:
            continue
    return None


def owner_warnings(db: Session, user: User, team_ids: list[uuid.UUID]) -> list[Warning]:
    """The same notices for the machines the viewer owns, for their Inbox (§19.6)."""
    if not team_ids:
        return []
    machines = db.scalars(select(Agent).where(Agent.owner_user_id == user.id, Agent.team_id.in_(team_ids),
                                              Agent.revoked_at.is_(None)).order_by(Agent.handle))
    return [w for w in (machine_warning(db, m) for m in machines) if w is not None]
