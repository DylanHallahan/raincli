"""Small app-mode presentation rules (protocol §20.3, §20.4, §21 C7.6): initials, the first-run step 1 (your inbox)
and the toast handoff target. Display only. Labels themselves come from ``raincli_server.presentation``.
"""

from __future__ import annotations

import re

from raincli_server.models import Agent, Message, User

_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")  # owners.displays' disambiguator, "Bob (bob)"
_NEXT = re.compile(r"/app/(people|agents|archived|compose|conversations/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


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
            from raincli_server import presentation

            user = self.db.get(User, uid) if uid else None
            self._names[uid] = presentation.display_name(user) if user else ""
        return self._names[uid]


def my_inbox(db, user: User, team) -> str | None:
    """§21 C7.6: the viewer's own inbox agent in that team (their own agent name, shown only to them), or None."""
    from raincli_server import presentation

    if team is None:
        return None
    inbox = presentation.inbox_of(db, team.id, user.id)
    return inbox.agent_name if inbox is not None else None


def handoff_next(value: str) -> str | None:
    """The page a handoff may continue to (§20.3): a toast's thread, exactly ``/app/conversations/<uuid>``, or one of
    the fixed pages the window opens directly (People, Agents, Archived, New message), so Inbox's latest-conversation step
    (§20.6 M1) never runs first. Anything else is Inbox; the handoff never redirects off the app's own pages."""
    return value if _NEXT.fullmatch(value or "") else None


P2_SINCE = (0, 5, 5)  # §20.7: the first app window that tells its page it is hidden while hidden


def opens_latest(viewer, user_agent: str | None) -> bool:
    """§20.7: Inbox opens the latest conversation (P2) only in an app window that keeps its page hidden while the
    window is hidden: one whose own User-Agent says RainCLIAppVersion 0.5.5 or later. An older window (a hidden start
    would read the latest conversation), a missing or unparsable version, or the website: no P2. The page request
    itself carries it, so a cold or logon launch decides at once, without waiting for a presence report. A spoofed
    version only changes the user's own window, which is acceptable."""
    from raincli_server.security import app_version

    session = getattr(viewer, "web_session", None)
    if session is None or not session.person_session_id:  # app mode only (a person session's window)
        return False
    version = app_version(user_agent)
    return version is not None and version >= P2_SINCE
