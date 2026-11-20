"""Account switching (protocol §23): what one person's linked accounts look like in an app window.

- ``unread_total``: the account's own unread messages in a team (§21 H2, ``messaging.unread_for``), the one count
  the switcher (``/app/accounts``) and ``GET /person/me`` share (A14 Q7). It's that person's read state (AS12), and
  senders never see it (PR5).
- ``window_accounts``: the accounts an app window holds (``identity.linked_sessions``, the one definition of linked),
  each with its unread count, which one the window shows, and the granted accounts not yet added (own session only).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from raincli_server import identity, messaging, presentation
from raincli_server.models import Agent, Message, PersonSession, User


def unread_total(session: Session, user_id: uuid.UUID, team_id: uuid.UUID) -> int:
    """The person's unread messages in the team (their own read state)."""
    return session.scalar(select(func.count()).select_from(Message).where(
        Message.team_id == team_id, messaging.unread_for(user_id))) or 0


@dataclass(frozen=True)
class WindowAccount:
    user: User
    name: str  # the team's display name (look-alikes told apart)
    initials: str
    active: bool  # the account this window shows
    own: bool  # the machine owner's own session, else a delegated one (removable)
    unread: int

    @property
    def email(self) -> str:
        return self.user.email

    def json(self) -> dict:
        return {"email": self.user.email, "display_name": self.name, "initials": self.initials,
                "active": self.active, "kind": "own" if self.own else "granted", "unread": self.unread}


@dataclass(frozen=True)
class Addable:
    user: User
    name: str

    @property
    def email(self) -> str:
        return self.user.email

    def json(self) -> dict:
        return {"email": self.user.email, "display_name": self.name}


@dataclass
class WindowAccounts:
    accounts: list[WindowAccount] = field(default_factory=list)
    addable: list[Addable] = field(default_factory=list)  # granted, not added (offered to the own session only)

    @property
    def active(self) -> WindowAccount | None:
        return next((a for a in self.accounts if a.active), None)

    def json(self) -> dict:
        return {"accounts": [a.json() for a in self.accounts], "addable": [a.json() for a in self.addable]}


def window_accounts(session: Session, ps: PersonSession | None) -> WindowAccounts:
    """The window's accounts: ``ps``'s linked family (the own session first, then the delegated ones oldest first),
    each with its unread count in the machine's team, and, when ``ps`` is the own session, its live grants not yet
    added. Empty for no session or a family that isn't live."""
    from raincli_server.web.present import initials

    out = WindowAccounts()
    family = identity.linked_sessions(session, ps) if ps is not None else []
    if not family:
        return out
    machine = session.get(Agent, family[0].machine_agent_id)
    team_id = machine.team_id
    for member in family:
        user = session.get(User, member.user_id)
        name = presentation.name_in_team(session, team_id, user)
        out.accounts.append(WindowAccount(user=user, name=name, initials=initials(name), active=member.id == ps.id,
                                          own=not member.delegated, unread=unread_total(session, user.id, team_id)))
    if not ps.delegated:
        added = {a.user.id for a in out.accounts}
        for _grant, user in identity.grants_of(session, ps.user_id, team_id):
            if user.id not in added:
                out.addable.append(Addable(user=user, name=presentation.name_in_team(session, team_id, user)))
    return out
