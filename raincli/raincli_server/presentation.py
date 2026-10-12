"""Presentation (protocol §21): the ONLY place endpoint labels, hold wording and teammate-facing errors are made.

Teammates see people, never machines or agents. Every API serializer, app-mode page, website page and error goes
through this module:

- ``person_label``: "Jon" for what a person wrote in the app, "Jon · agent" for what Jon's machine sent; the same
  for every viewer (PR4);
- ``hold_text``/``held_line`` (pages) and ``api_hold_code`` (the API): hold reasons with the person's name and
  no agent or session words;
- ``error_text``: the teammate-facing error messages (``address_people``, ``ambiguous_person``, ``inbox_invalid``);
- ``resolve_person_text``: the one parser for typed recipients;
- ``conversation_peer``: who a conversation is with, as the viewer sees it;
- ``person_presence``: a person's presence and speed, from their inbox in that team (PR13).

Own machines are shown only on the owner's own pages (This computer, the website Machines page, owner warnings,
``/me`` and the inbox setting); those pages do not use this module's labels.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from raincli_server.models import (Agent, AgentPresence, Conversation, MachineAgent, Membership, PersonInbox, Team,
                                   User)
from raincli_server.routing import Endpoint

TTL_SECONDS = 120  # presence freshness, as presence.TTL_SECONDS

# Event details a client may report (§21 C5.5): fixed codes only, never free text.
EVENT_CODES = frozenset({
    "busy", "blocked", "offline", "next_turn", "target_ambiguous", "sender_blocked", "approval_required",
    "delivered", "expired_offline", "too_large_for_hook", "too_large_for_command_line", "client_update_needed",
    "no_inbox",
})

# Raw hold codes to the coarse groups teammates see (§21 C5.5, C13 M3).
_GROUPS = {
    "no_inbox": "no_inbox",
    "busy": "busy", "blocked": "busy",
    "offline": "offline", "expired_offline": "offline",
    "client_update_needed": "update",
    "approval_required": "approval", "sender_blocked": "approval",
}
_API_CODES = {"no_inbox": "no_inbox", "busy": "inbox_busy", "offline": "inbox_offline", "update": "update_needed",
              "approval": "approval", "failed": "inbox_failed"}
_API_REPLY_CODES = {"busy": "agent_busy", "offline": "agent_offline"}
_TEXTS = {
    "no_inbox": "waiting for {name} to pick an inbox",
    "busy": "{name}'s inbox is busy",
    "offline": "{name}'s inbox is offline",
    "update": "{name}'s RainCLI needs an update",
    "approval": "waiting for {name}'s approval",
    "failed": "{name}'s inbox couldn't take it",
}
_REPLY_TEXTS = {"busy": "{name}'s agent is busy", "offline": "{name}'s agent is offline"}  # C13 L5

ADDRESS_PEOPLE = "Address people: send to a teammate's name or email"
_ERRORS = {
    "address_people": ADDRESS_PEOPLE,
    "address_people_mapped": "Address people: send to {name} ({email})",
    "ambiguous_person": "More than one teammate is called {text}: use their email",
    "inbox_invalid": "{machine} can't be your inbox: pick one of your own agents that can receive messages",
    "invalid": "recipient must be a teammate's name or email",
}


def display_name(user: User) -> str:
    """A person as people see them: their display name, else their email."""
    return (user.display_name or "").strip() or user.email


def name_in_team(db: Session, team_id: uuid.UUID | None, user: User) -> str:
    """A person as their team sees them: their display name, with a distinguishing suffix when another member's
    name reads alike (owners.displays, §18.1 item 7). The person is the address in v0.6, so look-alike names must
    never show as the same person."""
    if team_id is None:
        return display_name(user)
    from raincli_server import owners

    return owners.displays(db, team_id, [user]).get(user.id) or display_name(user)


def person_of(db: Session, endpoint: Endpoint) -> User | None:
    """The person an endpoint belongs to: the person, or the machine's owner."""
    if endpoint.kind == "person":
        return endpoint.user
    return db.get(User, endpoint.agent.owner_user_id) if endpoint.agent is not None else None


def person_label(db: Session, endpoint: Endpoint, *, sender: bool = False, team_id: uuid.UUID | None = None) -> str:
    """PR4: "<Name>" for a person, "<Name> · agent" for a machine (or an agent on it) that SENT a message
    (``sender=True``). A machine as a recipient is the person's inbox: "<Name>". The same for every viewer. Pass the
    ``team_id`` to tell look-alike names apart (``name_in_team``)."""
    user = person_of(db, endpoint)
    name = name_in_team(db, team_id, user) if user is not None else "a teammate"
    return f"{name} · agent" if sender and endpoint.kind != "person" else name


def _group(code: str | None) -> str | None:
    if not code:
        return None
    return _GROUPS.get(code, "failed")


def hold_text(code: str | None, name: str, route: str | None = None) -> str | None:
    """The hold reason a teammate sees, without the "held: " prefix (``held_line`` adds it): never an agent name or
    a session word beyond these. ``route='reply'`` speaks of the person's agent (C13 L5)."""
    group = _group(code)
    if group is None:
        return None
    template = (_REPLY_TEXTS.get(group) if route == "reply" else None) or _TEXTS[group]
    return template.format(name=name)


def held_line(code: str | None, name: str, route: str | None = None) -> str | None:
    """"held: <reason>", as trails and conversation pages show it."""
    text = hold_text(code, name, route)
    return f"held: {text}" if text else None


def api_hold_code(code: str | None, route: str | None = None) -> str | None:
    """C13 M3: the coarse ``hold_reason`` for anyone but the recipient machine's owner."""
    group = _group(code)
    if group is None:
        return None
    return (_API_REPLY_CODES.get(group) if route == "reply" else None) or _API_CODES[group]


def error_text(code: str, **values) -> str:
    """Teammate-facing error messages. ``address_people`` with ``name`` and ``email`` maps a typed handle to its
    person (C2.2, the only place that mapping is shown); ``inbox_invalid`` names only the viewer's own machine."""
    if code == "address_people" and values.get("email"):
        return _ERRORS["address_people_mapped"].format(**values)
    return _ERRORS.get(code, _ERRORS["invalid"]).format(**values)


# Typed recipients ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PersonResolution:
    """What a typed recipient means in a team.

    ``kind``:
    - ``person``: ``user`` is the one active member it names;
    - ``ambiguous``: ``people`` are the members sharing that display name (``ambiguous_person``);
    - ``address_people``: a machine or agent form, or nothing matched; ``mapped`` is the person when the text was
      the handle of a teammate's machine (C2.2, C13 L3), else None (the generic text);
    - ``invalid``: empty or malformed.
    """

    kind: str
    user: User | None = None
    people: list[User] = field(default_factory=list)
    mapped: User | None = None

    @property
    def error(self) -> tuple[int, str, str, dict] | None:
        """``(status, code, message, extra)`` for a refusal, else None."""
        if self.kind == "person":
            return None
        if self.kind == "ambiguous":
            return 400, "ambiguous_person", error_text("ambiguous_person", text=display_name(self.people[0])), {
                "people": [{"name": f"{display_name(u)} ({u.email})", "email": u.email} for u in self.people]}
        if self.kind == "address_people":
            if self.mapped is not None:  # the email is shown: look-alike names are told apart
                return 400, "address_people", error_text("address_people", name=display_name(self.mapped),
                                                         email=self.mapped.email), {}
            return 400, "address_people", error_text("address_people"), {}
        return 400, "invalid", error_text("invalid"), {}


def _members(db: Session, team_id: uuid.UUID):
    return (select(User).join(Membership, Membership.user_id == User.id)
            .where(Membership.team_id == team_id, User.is_active.is_(True)))


def resolve_person_text(db: Session, team_id: uuid.UUID, text: object, *, sender: User | None = None
                        ) -> PersonResolution:
    """The one parser for a typed ``to`` (C2.1): ``@email``, ``email`` or a display name unique among the team's
    active members (case-insensitive, trimmed). A handle or ``handle/agent`` is refused as ``address_people``;
    the refusal names the handle's person only when it is a teammate's non-revoked machine whose owner is an
    active member (the sender's OWN handle gets the generic text, C13 L3). No other answer distinguishes a handle
    that exists from one that doesn't."""
    if not isinstance(text, str) or not text.strip() or len(text) > 254:
        return PersonResolution("invalid")
    value = text.strip()
    email = value[1:] if value.startswith("@") else value
    if "@" in email:
        user = db.scalars(_members(db, team_id).where(func.lower(User.email) == email.lower())).first()
        return PersonResolution("person", user=user) if user is not None else PersonResolution("address_people")
    named = list(db.scalars(_members(db, team_id).where(
        func.lower(func.trim(User.display_name)) == value.lower()).order_by(User.email)))
    if len(named) == 1:
        return PersonResolution("person", user=named[0])
    if len(named) > 1:
        return PersonResolution("ambiguous", people=named)
    return machine_refusal(db, team_id, value.split("/", 1)[0], sender=sender)


def machine_refusal(db: Session, team_id: uuid.UUID, handle: object, *, sender: User | None = None
                    ) -> PersonResolution:
    """The ``address_people`` refusal for a machine or agent form (``{machine}``, ``{machine, agent}``, a typed
    handle): it names the handle's person only for a teammate's non-revoked machine whose owner is an active
    member; your own handle, and any handle that doesn't exist, get the generic text (C2.2, C13 L3)."""
    handle = handle.strip() if isinstance(handle, str) else ""
    machine = db.scalars(select(Agent).where(Agent.team_id == team_id, Agent.handle == handle,
                                             Agent.revoked_at.is_(None))).first() if handle else None
    owner = db.scalars(_members(db, team_id).where(User.id == machine.owner_user_id)).first() \
        if machine is not None else None
    if owner is None or (sender is not None and owner.id == sender.id):
        return PersonResolution("address_people")
    return PersonResolution("address_people", mapped=owner)


# Conversations -------------------------------------------------------------------------------------------------

SELF_TITLE = "From your agents"
OWN_AGENTS_TITLE = "Your agents"


@dataclass(frozen=True)
class Peer:
    """Who a conversation is with, for one viewer: ``user`` (None for a self or own_agents conversation) and the
    ``label`` to show."""

    user: User | None
    label: str
    kind: str


def _side_person(db: Session, conv: Conversation, side: str) -> uuid.UUID | None:
    user_id = getattr(conv, f"{side}_user_id")
    if user_id is not None:
        return user_id
    agent = db.get(Agent, getattr(conv, f"agent_{side}_id"))
    return agent.owner_user_id if agent is not None else None


def conversation_peer(db: Session, viewer: User, conv: Conversation) -> Peer:
    """The other person of a pair conversation as ``viewer`` sees it, by name only. A ``self`` conversation is
    "From your agents" and an ``own_agents`` one "Your agents" (both private to their person)."""
    if conv.kind == "self":
        return Peer(None, SELF_TITLE, "self")
    if conv.kind == "own_agents":
        return Peer(None, OWN_AGENTS_TITLE, "own_agents")
    a, b = _side_person(db, conv, "a"), _side_person(db, conv, "b")
    other = b if a == viewer.id else a
    user = db.get(User, other) if other is not None else None
    return Peer(user, name_in_team(db, conv.team_id, user) if user is not None else "a teammate", "pair")


# Presence and speed --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PersonPresence:
    """PR13: ``presence`` is ``ready``, ``busy``, ``offline`` or ``no_inbox``; ``speed`` is ``instant``,
    ``next_turn`` or None (offline, no inbox)."""

    presence: str
    speed: str | None

    @property
    def text(self) -> str:
        """"Ready · Instant", "Busy · Next turn", "Offline" or "No inbox yet"."""
        if self.presence == "no_inbox":
            return "No inbox yet"
        if self.presence == "offline" or self.speed is None:
            return "Offline"
        return f"{self.presence.capitalize()} · {'Instant' if self.speed == 'instant' else 'Next turn'}"


def inbox_of(db: Session, team_id: uuid.UUID, user_id: uuid.UUID) -> PersonInbox | None:
    """A person's inbox in a team, if they picked one."""
    return db.get(PersonInbox, (user_id, team_id))


def uses_inbox_mode(db: Session, inbox: PersonInbox) -> bool:
    """C3.1(a): the machine's own inbox agent is the picked one, or the machine accepts only its inbox; messages
    then go to the machine endpoint (today's inbox path)."""
    machine = db.get(Agent, inbox.agent_id)
    if machine is None:
        return False
    if machine.routing == "inbox-only":
        return True
    role = db.scalars(select(MachineAgent.name).where(MachineAgent.agent_id == machine.id,
                                                      MachineAgent.role == "inbox")).first()
    return role == inbox.agent_name


_WORDS = {"idle": "ready", "working": "busy", "blocked": "busy", "ready": "ready", "busy": "busy"}


def person_presence(db: Session, team_id: uuid.UUID, user: User, now: datetime | None = None) -> PersonPresence:
    """A person's presence and speed in a team (C5.4), from their inbox only: the picked agent's directory row
    (or, on the inbox-mode path, the machine's own presence), fresh within the presence TTL."""
    from raincli_server import identity

    inbox = inbox_of(db, team_id, user.id)
    if inbox is None:
        return PersonPresence("no_inbox", None)
    now = now or identity.now()
    fresh = now - timedelta(seconds=TTL_SECONDS)
    machine = db.get(AgentPresence, inbox.agent_id)
    if machine is None or machine.seen_at <= fresh or machine.status == "offline":
        return PersonPresence("offline", None)
    row = db.scalars(select(MachineAgent).where(MachineAgent.agent_id == inbox.agent_id,
                                                MachineAgent.name == inbox.agent_name,
                                                MachineAgent.ambiguous.is_(False))).first()
    live = row is not None and row.seen_at > fresh and row.reachability in ("instant", "next-turn")
    if uses_inbox_mode(db, inbox):  # the inbox-mode path: the machine's own presence (C5.4)
        speed = ("instant" if row.reachability == "instant" else "next_turn") if live else "next_turn"
        return PersonPresence(_WORDS.get(machine.status, "ready"), speed)
    if live:
        return PersonPresence(_WORDS.get(row.status, "ready"), "instant" if row.reachability == "instant" else "next_turn")
    return PersonPresence("offline", None)


def team_name(db: Session, team_id: uuid.UUID) -> str:
    team = db.get(Team, team_id)
    return team.name if team is not None else ""


def people_json(db: Session, team_id: uuid.UUID) -> list[dict]:
    """§21 C5.3: the team's active members as people, with presence and speed (PR13); never machines or agents."""
    from raincli_server import owners

    team = team_name(db, team_id)
    shown = owners.displays(db, team_id)
    out = []
    for user in db.scalars(_members(db, team_id).order_by(func.lower(User.display_name), User.email)):
        state = person_presence(db, team_id, user)
        out.append({"display_name": shown.get(user.id) or display_name(user), "email": user.email, "team": team,
                    "presence": state.presence, "speed": state.speed})
    return out

