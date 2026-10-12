"""What the app-mode thread shows beyond the messages themselves (protocol §19, as changed by §21).

v0.6 (§21 C5, C7): teammates see people, never machines or agents. Every label, hold reason and presence word here
comes from ``raincli_server.presentation``; this module only arranges them.

- **Names:** ``presentation.person_label``: "Jon" for what Jon wrote in the app, "Jon · agent" for what his machine
  sent (PR4). The same for every viewer, the owner included.
- **The delivery trail** (§19.7, §21 C7.5): only under the viewer's own messages: stored → received → submitted →
  replied for a message to a person's inbox, stored → received → replied for one delivered in the app; held,
  uncertain or rejected in place of the remaining steps, worded by ``presentation.hold_text`` (no agent or session
  words, C5.5). Raw event details are never shown.
- **Header presence** (§21 C5.4): the person's presence · speed, from their inbox in that team.
- **Warnings** (§21 C7.4): about a person's inbox, never a handle: their inbox hasn't connected, hasn't picked up
  messages, or they haven't picked an inbox yet. Display only. The owner's own machine notices stay owner-only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from raincli_server import identity, messaging, presentation
from raincli_server.models import Agent, AgentCredential, AgentPresence, DeliveryEvent, Message, Team, User

WARNING_AFTER = timedelta(hours=24)

AGENT_STEPS = ("stored", "received", "submitted", "replied")  # a message to a person's inbox (an agent)
INBOX_STEPS = ("stored", "received", "replied")  # a message delivered in the person's app (no inbox)
STEP_LABELS = {"stored": "stored", "received": "received", "submitted": "submitted", "replied": "replied"}
OUTCOMES = {"held": "held", "submission_uncertain": "uncertain", "rejected": "rejected"}


def uncertain_words(name: str) -> str:
    return f"it may not have reached {name}'s inbox"


def rejected_words(name: str) -> str:
    return f"{name}'s inbox declined it"


# Names (§21 C5.1) ----------------------------------------------------------------------------------

class Names:
    """Shown names, worked out once per endpoint per request, all through ``presentation.person_label``."""

    def __init__(self, db: Session):
        self.db = db
        self._cache: dict = {}

    def _label(self, ep: messaging.Endpoint, sender: bool) -> str:
        key = (ep.key, sender)
        if key not in self._cache:
            self._cache[key] = presentation.person_label(self.db, ep, sender=sender)
        return self._cache[key]

    def endpoint(self, ep: messaging.Endpoint) -> str:
        """A recipient or a conversation side: the person ("Jon"), whatever machine or agent it is."""
        return self._label(ep, False)

    def sender(self, ep: messaging.Endpoint) -> str:
        """The meta line's name: "Jon", or "Jon · agent" for what Jon's machine sent."""
        return self._label(ep, True)

    def person(self, ep: messaging.Endpoint) -> str:
        """The person's name alone, for hold reasons ("waiting for Sam to pick an inbox")."""
        user = presentation.person_of(self.db, ep)
        return presentation.display_name(user) if user is not None else "a teammate"


# The delivery trail (§19.7, §21 C7.5) --------------------------------------------------------------

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
    events: list[DeliveryEvent] = field(default_factory=list)  # states and times only are ever shown

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


def trail_for(message: Message, recipient: messaging.Endpoint, events: list[DeliveryEvent], state: str,
              hold_code: str | None, name: str, route: str | None = None) -> Trail:
    """``state`` and ``hold_code`` are what the viewer may see (§21 C13 H4: for a message delivered in a person's
    app, never their read state); ``name`` is the recipient person's name for the hold wording."""
    steps = AGENT_STEPS if recipient.kind != "person" else INBOX_STEPS
    shown = "held" if state == "stored" and hold_code else state
    first = {}
    for ev in events:
        first.setdefault(ev.state, ev.created_at)
    if recipient.kind == "person":
        # §21 C13 H4: delivered in their app when it was sent; their read time (acked_at, delivery_updated_at) is
        # never shown to anyone else.
        received_at = message.created_at if state == "received" else None
    else:
        received_at = first.get("received") or message.acked_at
    times = {"stored": message.created_at, "received": received_at,
             "submitted": first.get("submitted"), "replied": first.get("replied")}
    if shown == "replied":
        reached_upto = len(steps)
    elif shown == "submitted":
        reached_upto = steps.index("submitted" if "submitted" in steps else "received") + 1
    elif shown == "received" or (recipient.kind != "person" and message.acked_at is not None):
        reached_upto = steps.index("received") + 1  # picked up; held, uncertain and rejected happen after this
    else:
        reached_upto = 1  # stored, including a hold the server derives before it is picked up
    outcome = OUTCOMES.get(shown)
    trail_steps = [Step(n, i < reached_upto, times.get(n) if i < reached_upto else None) for i, n in enumerate(steps)]
    if outcome:
        trail_steps = trail_steps[:reached_upto]  # the outcome replaces the remaining steps
    reason = ""
    if outcome == "held":
        reason = presentation.hold_text(hold_code or "failed", name, route) or ""
    elif outcome == "uncertain":
        reason = uncertain_words(name)
    elif outcome == "rejected":
        reason = rejected_words(name)
    return Trail(trail_steps, outcome, reason, events)


# Header presence (§21 C5.4) ------------------------------------------------------------------------

@dataclass
class HeaderPresence:
    chip: str | None  # "Instant" or "Next turn"
    word: str | None  # Ready, Busy, Offline or "No inbox yet"
    tooltip: str = ""

    @property
    def text(self) -> str:
        return " · ".join(p for p in (self.word, self.chip) if p)


def header_presence(db: Session, team_id: uuid.UUID, person: User | None, now: datetime | None = None
                    ) -> HeaderPresence | None:
    """The person's presence · speed in that team, from their inbox (``presentation.person_presence``). None for
    a conversation that isn't with a teammate (your own agents)."""
    if person is None:
        return None
    p = presentation.person_presence(db, team_id, person, now)
    if p.presence == "no_inbox":
        return HeaderPresence(None, "No inbox yet")
    if p.presence == "offline" or p.speed is None:
        return HeaderPresence(None, "Offline")
    return HeaderPresence("Instant" if p.speed == "instant" else "Next turn", p.presence.capitalize())


# Warnings (§21 C7.4) -------------------------------------------------------------------------------

@dataclass
class Warning:
    name: str  # the person, never a handle
    kind: str  # no_inbox, never, not_connected, not_picking_up or old_client (W1)
    since: datetime | None = None
    email: str = ""


def _never_reported_client(db: Session, machine: Agent) -> bool:
    """W1: the machine has never reported a client version (a pre-0.5 RainCLI that doesn't confirm delivery)."""
    report = db.get(AgentPresence, machine.id)
    return report is None or not report.client_version


def _most_recent_machine(db: Session, team_id: uuid.UUID, user_id: uuid.UUID) -> Agent | None:
    """The person's non-revoked machine in the team with the newest credential use."""
    return db.scalars(
        select(Agent).join(AgentCredential, AgentCredential.agent_id == Agent.id, isouter=True)
        .where(Agent.team_id == team_id, Agent.owner_user_id == user_id, Agent.revoked_at.is_(None))
        .group_by(Agent.id).order_by(func.max(AgentCredential.last_used_at).desc().nulls_last()).limit(1)).first()


def _oldest_unacked(db: Session, machine: Agent) -> datetime | None:
    return db.scalar(select(Message.created_at).where(Message.recipient_agent_id == machine.id,
                                                      Message.acked_at.is_(None)).order_by(Message.seq).limit(1))


def _last_contact(db: Session, machine: Agent) -> datetime | None:
    """The machine's newest sign of life: a credential use or a presence report."""
    used = db.scalar(select(func.max(AgentCredential.last_used_at)).where(AgentCredential.agent_id == machine.id))
    report = db.get(AgentPresence, machine.id)
    seen = report.seen_at if report is not None else None
    return max((t for t in (used, seen) if t is not None), default=None)


def person_warning(db: Session, team_id: uuid.UUID, person: User | None, now: datetime | None = None
                   ) -> Warning | None:
    """One warning at a time about a person's inbox in a team: none picked; its machine never connected or hasn't
    for a day; or it hasn't picked up a message for a day. It names only the person and a date."""
    if person is None or not person.is_active:
        return None
    name = presentation.display_name(person)
    inbox = presentation.inbox_of(db, team_id, person.id)
    machine = db.get(Agent, inbox.agent_id) if inbox is not None else None
    if machine is None or machine.revoked_at is not None:
        # W1: with no inbox, the machine concerned is their most recently seen one in the team; if it runs a client
        # that never confirms delivery and has messages waiting, say so; otherwise "no inbox yet".
        recent = _most_recent_machine(db, team_id, person.id)
        if recent is not None and _never_reported_client(db, recent):
            oldest = _oldest_unacked(db, recent)
            if oldest is not None and (now or identity.now()) - oldest > WARNING_AFTER:
                return Warning(name, "old_client", oldest, email=person.email)
        return Warning(name, "no_inbox", email=person.email)
    now = now or identity.now()
    last = _last_contact(db, machine)
    if last is None:
        return Warning(name, "never", email=person.email)
    if now - last > WARNING_AFTER:
        return Warning(name, "not_connected", last, email=person.email)
    oldest = db.scalar(
        select(Message.created_at).where(Message.recipient_agent_id == machine.id, Message.acked_at.is_(None))
        .order_by(Message.seq).limit(1))
    if oldest is not None and now - oldest > WARNING_AFTER:
        kind = "old_client" if _never_reported_client(db, machine) else "not_picking_up"  # W1
        return Warning(name, kind, oldest, email=person.email)
    return None


def warning_for_text(db: Session, team_ids: list[uuid.UUID], text: str, viewer: User,
                     now: datetime | None = None) -> Warning | None:
    """The compose page's warning for a typed person (an email or a unique name), in the viewer's teams. Anything
    else (a handle, nobody, the viewer) has none, the same for every input: it never confirms a handle exists."""
    text = (text or "").strip()
    if not text or len(text) > 254:
        return None
    for team_id in team_ids:
        found = presentation.resolve_person_text(db, team_id, text, sender=viewer)
        if found.kind == "person" and found.user is not None and found.user.id != viewer.id:
            return person_warning(db, team_id, found.user, now)
    return None


# The owner's own machines (§19.6, owner-only) ------------------------------------------------------

@dataclass
class MachineWarning:
    handle: str  # the owner's own machine: shown only to them
    kind: str  # never, not_connected, not_picking_up or old_client (W1)
    since: datetime | None = None


def machine_warning(db: Session, machine: Agent, now: datetime | None = None) -> MachineWarning | None:
    """Two indexed reads: the machine's newest credential use, and its oldest unacknowledged message
    (``ix_messages_recipient_pending``, migration 0009)."""
    if machine.revoked_at is not None:
        return None
    now = now or identity.now()
    last_used = db.scalar(select(func.max(AgentCredential.last_used_at)).where(AgentCredential.agent_id == machine.id))
    if last_used is None:
        return MachineWarning(machine.handle, "never")
    if now - last_used > WARNING_AFTER:
        return MachineWarning(machine.handle, "not_connected", last_used)
    oldest = db.scalar(
        select(Message.created_at).where(Message.recipient_agent_id == machine.id, Message.acked_at.is_(None))
        .order_by(Message.seq).limit(1))
    if oldest is not None and now - oldest > WARNING_AFTER:
        kind = "old_client" if _never_reported_client(db, machine) else "not_picking_up"  # W1
        return MachineWarning(machine.handle, kind, oldest)
    return None


def owner_warnings(db: Session, user: User, team_ids: list[uuid.UUID], now: datetime | None = None
                   ) -> list[MachineWarning]:
    """The same notices for the machines the viewer owns, for their Inbox (§19.6): their own handles only."""
    if not team_ids:
        return []
    machines = db.scalars(select(Agent).where(Agent.owner_user_id == user.id, Agent.team_id.in_(team_ids),
                                              Agent.revoked_at.is_(None)).order_by(Agent.handle))
    return [w for w in (machine_warning(db, m, now) for m in machines) if w is not None]


def team_label(db: Session, team: Team | None) -> str:
    return team.name if team is not None else ""
