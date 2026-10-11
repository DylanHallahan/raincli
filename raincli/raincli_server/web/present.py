"""Small app-mode presentation rules (protocol §20.3, §20.4): initials, the "via" line, the first-run step 1 and
the toast handoff target. Display only: nothing here changes delivery, framing (C17) or what agents receive.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta

from sqlalchemy import select

from raincli_server import identity
from raincli_server.models import Agent, AgentCredential, AgentPresence, Conversation, Message, PersonSession, User

CONNECTED_WITHIN = timedelta(hours=24)  # §20.4: step 1 shows done after a connection in the last 24 h
_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")  # owners.displays' disambiguator, "Bob (bob)"
_NEXT = re.compile(r"/app/(agents|archived|compose|conversations/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def initials(name: str | None) -> str:
    """Two letters from a person's display name (§20.4 R8, §20.6 L8): one word gives its first two letters
    (Jon → JO), more give the first and last initials (Priya Shah → PS), a one-letter name repeats it (J → JJ),
    and a name with no letters shows "?". A disambiguating "(…)" suffix is ignored."""
    words = [[c for c in w if c.isalpha()] for w in _SUFFIX.sub("", name or "").split()]
    words = [w for w in words if w]
    if not words:
        return "?"
    letters = (words[0] * 2)[:2] if len(words) == 1 else [words[0][0], words[-1][0]]
    return "".join(letters).upper()


class People:
    """The display name of the person behind a sender, once per user per request: the person, or a machine's
    owner. Agents and machine inboxes use their owner's initials (§20.4)."""

    def __init__(self, db):
        self.db = db
        self._names: dict = {}

    def of(self, message: Message) -> str:
        uid = message.sender_user_id
        if uid is None:
            agent = self.db.get(Agent, message.sender_agent_id)
            uid = agent.owner_user_id if agent else None
        if uid not in self._names:
            user = self.db.get(User, uid) if uid else None
            self._names[uid] = user.display_name if user else ""
        return self._names[uid]


def shown_via(db, message: Message) -> str | None:
    """P1 (§20.4, §20.6 L7): the stated agent name to show as "via …", or None. Hidden only when it equals,
    byte for byte, the agent name of the sender's side of the conversation (the side whose machine sent it);
    shown when they differ, or when that side is the machine's inbox (no agent name)."""
    stated = message.sender_agent_name
    if not stated:
        return None
    conv = db.get(Conversation, message.conversation_id)
    if conv is not None:
        for agent_id, name in ((conv.agent_a_id, conv.a_agent_name), (conv.agent_b_id, conv.b_agent_name)):
            if agent_id is not None and agent_id == message.sender_agent_id and name is not None and name == stated:
                return None
    return stated


def connected_machine(db, user: User, team_ids: list[uuid.UUID], now=None) -> str | None:
    """The handle of the viewer's machine that connected most recently within 24 h (first-run step 1), or None."""
    if not team_ids:
        return None
    now = now or identity.now()
    row = db.execute(
        select(Agent.handle, AgentCredential.last_used_at).join(AgentCredential, AgentCredential.agent_id == Agent.id)
        .where(Agent.owner_user_id == user.id, Agent.team_id.in_(team_ids), Agent.revoked_at.is_(None),
               AgentCredential.revoked_at.is_(None), AgentCredential.last_used_at > now - CONNECTED_WITHIN)
        .order_by(AgentCredential.last_used_at.desc()).limit(1)
    ).first()
    return row[0] if row else None


def handoff_next(value: str) -> str | None:
    """The page a handoff may continue to (§20.3): a toast's thread, exactly ``/app/conversations/<uuid>``, or one of
    the fixed pages the window opens directly (Agents, Archived, New message), so Inbox's latest-conversation step
    (§20.6 M1) never runs first. Anything else is Inbox; the handoff never redirects off the app's own pages."""
    return value if _NEXT.fullmatch(value or "") else None


P2_SINCE = (0, 5, 5)  # §20.7: the first client whose hidden window tells its page it is hidden


def opens_latest(db, viewer) -> bool:
    """§20.7: Inbox opens the latest conversation (P2) only in an app whose window keeps its page hidden while the
    window is hidden: the person session's machine reports client 0.5.5 or later. Older clients (a hidden start
    would read the latest conversation), a missing or unparsable report, or the website: no P2."""
    from raincli_server.presence import version_tuple

    session = getattr(viewer, "web_session", None)
    if session is None or not session.person_session_id:
        return False
    person = db.get(PersonSession, session.person_session_id)
    row = db.get(AgentPresence, person.machine_agent_id) if person is not None else None
    try:
        return row is not None and version_tuple(row.client_version or "") >= P2_SINCE
    except ValueError:
        return False

