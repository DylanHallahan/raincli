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
from raincli_server.models import Agent, Grant, Message, PersonSession, User


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
    kind: str = "individual"  # or "all_members" (A18: a team-wide grant)

    @property
    def email(self) -> str:
        return self.user.email

    def json(self) -> dict:
        return {"email": self.user.email, "display_name": self.name, "kind": self.kind}


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
    if not ps.delegated:  # A18: what this person may add, individual and team-wide; never added automatically
        added = {a.user.id for a in out.accounts}
        for _grant, user, kind in available_grants(session, ps.user_id, team_id):
            if user.id not in added:
                out.addable.append(Addable(user=user, name=presentation.name_in_team(session, team_id, user),
                                           kind=kind))
    return out


def _excluded(session: Session, person_id: uuid.UUID, team_id: uuid.UUID) -> bool:
    """A18.1: a team-wide grant never covers someone who is themselves the ACCOUNT of a live grant in that team."""
    return session.scalar(select(Grant.id).where(Grant.team_id == team_id, Grant.account_user_id == person_id,
                                                 Grant.revoked_at.is_(None)).limit(1)) is not None


def available_grants(session: Session, grantee_id: uuid.UUID, team_id: uuid.UUID) -> list[tuple[Grant, User, str]]:
    """``(grant, account, kind)`` for every account ``grantee_id`` may act as in the team, by email: their live
    individual grants (``identity.grants_of``) and the team-wide (all-members) grants that cover them (A18.1), one
    per account, the individual grant first (A19 E1). Accounts and the grantee are active members."""
    out: dict[uuid.UUID, tuple[Grant, User, str]] = {}
    for grant, user in identity.grants_of(session, grantee_id, team_id):  # D1: both kinds, when it returns both
        out.setdefault(user.id, (grant, user, "all_members" if getattr(grant, "all_members", False) else "individual"))
    if not _excluded(session, grantee_id, team_id) and identity._active_member(session, team_id, grantee_id):
        rows = session.execute(
            select(Grant, User).join(User, User.id == Grant.account_user_id)
            .where(Grant.team_id == team_id, Grant.all_members.is_(True), Grant.revoked_at.is_(None),
                   Grant.account_user_id != grantee_id)).all()
        for grant, user in rows:
            if user.id not in out and identity._active_member(session, team_id, user.id):
                out[user.id] = (grant, user, "all_members")
    return sorted(out.values(), key=lambda t: t[1].email)


def effective_kind(session: Session, ps: PersonSession | None) -> str | None:
    """The kind of a delegated session's effective grant (A19 E1): ``individual`` or ``all_members``, else None
    (an own session, or no covering grant). Uses ``identity.effective_grant`` when the server has it; otherwise the
    same order: the live individual grant, then a covering team-wide one."""
    if ps is None or not ps.delegated:
        return None
    resolver = getattr(identity, "effective_grant", None)
    if resolver is not None:
        grant = resolver(session, ps)
        return None if grant is None else ("all_members" if grant.all_members else "individual")
    team_id = session.get(Agent, ps.machine_agent_id).team_id
    if identity.live_grant(session, team_id, ps.acting_user_id, ps.user_id) is not None:
        return "individual"
    if _excluded(session, ps.acting_user_id, team_id):
        return None
    covering = session.scalar(select(Grant.id).where(
        Grant.team_id == team_id, Grant.account_user_id == ps.user_id, Grant.all_members.is_(True),
        Grant.revoked_at.is_(None)))
    return "all_members" if covering is not None else None


def self_replies_ok(session: Session, viewer) -> bool:
    """A18.5: under a team-wide effective grant, the account's escalations and "From your agents" are read-only."""
    if not getattr(viewer, "delegated", False):
        return True
    return effective_kind(session, getattr(viewer, "person_session", None)) != "all_members"
