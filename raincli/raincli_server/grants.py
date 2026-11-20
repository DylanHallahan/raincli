"""Grants (protocol §23 A2): the operator lets one person act as another person of the same team.

Only ``raincli-admin`` creates, revokes and lists grants (never HTTP, AS1 b). Output is people only (names and
emails), never machines, agents or message bodies. A refusal raises ``GrantRuleError`` (``raincli-admin`` exits 2).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from raincli_server import identity, presentation
from raincli_server.models import Conversation, Grant, Message, Team, User


class GrantRuleError(ValueError):
    pass


def _member(session: Session, team: Team, email: str, what: str) -> User:
    user = identity.find_user_by_email(session, email)
    if user is None or not user.is_active or identity.membership(session, team.id, user.id) is None:
        raise GrantRuleError(f"{email} ({what}) is not an active member of {team.slug}")
    return user


def add_grant(session: Session, team: Team, grantee_email: str, account_email: str,
              created_by: str = "operator", *, toasts: bool = True) -> Grant:
    """Both people active members of ``team``; never yourself; one live grant per pair."""
    grantee = _member(session, team, grantee_email, "grantee")
    account = _member(session, team, account_email, "account")
    if grantee.id == account.id:
        raise GrantRuleError("a person can't be granted their own account")
    if identity.live_grant(session, team.id, grantee.id, account.id) is not None:
        raise GrantRuleError(f"{grantee.email} may already act as {account.email} in {team.slug}")
    grant = Grant(id=uuid.uuid4(), team_id=team.id, grantee_user_id=grantee.id, account_user_id=account.id,
                  created_by=created_by, toasts=toasts)
    try:
        with session.begin_nested():  # a concurrent grant-add of the same pair is refused like a duplicate
            session.add(grant)
            session.flush()
    except IntegrityError:
        raise GrantRuleError(f"{grantee.email} may already act as {account.email} in {team.slug}") from None
    return grant


def add_team_grant(session: Session, team: Team, account_email: str, created_by: str = "operator", *,
                   toasts: bool = False) -> Grant:
    """A17: one team-wide grant: every active member of ``team`` but the account may act as it, now and later (never
    a shared account, A18.1). Toasts are off unless asked for (A18.4)."""
    account = _member(session, team, account_email, "account")
    if identity.live_team_grant(session, team.id, account.id) is not None:
        raise GrantRuleError(f"all members may already act as {account.email} in {team.slug}")
    grant = Grant(id=uuid.uuid4(), team_id=team.id, grantee_user_id=None, account_user_id=account.id,
                  all_members=True, created_by=created_by, toasts=toasts)
    try:
        with session.begin_nested():
            session.add(grant)
            session.flush()
    except IntegrityError:
        raise GrantRuleError(f"all members may already act as {account.email} in {team.slug}") from None
    return grant


def _end(session: Session, team: Team, grant: Grant) -> int:
    from raincli_server.models import PersonSession

    grant = session.scalar(select(Grant).where(Grant.id == grant.id).with_for_update())
    live = session.scalars(select(PersonSession.id).where(PersonSession.grant_id == grant.id,
                                                          PersonSession.revoked_at.is_(None))).all()
    identity.revoke_grants(session, grant.account_user_id, team.id, grant_id=grant.id)
    if not live:
        return 0
    # A18.2: a session another live grant still covers is re-pointed to it, not ended.
    return session.scalar(select(func.count()).select_from(PersonSession).where(
        PersonSession.id.in_(live), PersonSession.revoked_at.is_not(None))) or 0


def remove_team_grant(session: Session, team: Team, account_email: str) -> int:
    """A17: revoke the team-wide grant and end every member's session through it. Returns how many ended."""
    account = identity.find_user_by_email(session, account_email)
    grant = identity.live_team_grant(session, team.id, account.id) if account else None
    if grant is None:
        raise GrantRuleError(f"there is no team-wide grant to act as {account_email} in {team.slug}")
    return _end(session, team, grant)


def remove_grant(session: Session, team: Team, grantee_email: str, account_email: str) -> int:
    """Revoke the live grant and end every session issued through it (A7). Returns how many sessions ended."""
    grantee = identity.find_user_by_email(session, grantee_email)
    account = identity.find_user_by_email(session, account_email)
    grant = identity.live_grant(session, team.id, grantee.id, account.id) if grantee and account else None
    if grant is None:
        raise GrantRuleError(f"{grantee_email} has no grant to act as {account_email} in {team.slug}")
    return _end(session, team, grant)


@dataclass(frozen=True)
class GrantRow:
    grantee: str | None  # None for a team-wide grant (A17: all members)
    grantee_email: str | None
    account: str
    account_email: str
    created_at: datetime
    revoked_at: datetime | None
    toasts: bool


def list_grants(session: Session, team: Team) -> list[GrantRow]:
    """Every grant in the team, live and revoked, oldest first."""
    rows = session.scalars(select(Grant).where(Grant.team_id == team.id).order_by(Grant.created_at, Grant.id)).all()
    out = []
    for g in rows:
        grantee = session.get(User, g.grantee_user_id) if g.grantee_user_id is not None else None
        account = session.get(User, g.account_user_id)
        out.append(GrantRow(presentation.name_in_team(session, team.id, grantee) if grantee else None,
                            grantee.email if grantee else None,
                            presentation.name_in_team(session, team.id, account), account.email,
                            g.created_at, g.revoked_at, g.toasts))
    return out


@dataclass(frozen=True)
class ActionRow:
    at: datetime
    actor: str
    actor_email: str
    account: str
    account_email: str
    conversation_id: uuid.UUID
    with_label: str
    message_id: uuid.UUID
    via: str  # A19 D4: "individual" or "all_members", the grant in effect when it was sent


def delegated_actions(session: Session, team: Team, since: datetime | None = None) -> list[ActionRow]:
    """§23 A2 (L8): each SEND made through a grant (``messages.acting_user_id``), oldest first: who, as whom, when and
    in which conversation (by the other side's person label, as the account sees it). Reads and archives aren't
    audited (AS6 names sends). Never the body."""
    stmt = select(Message).where(Message.team_id == team.id, Message.acting_user_id.is_not(None))
    if since is not None:
        stmt = stmt.where(Message.created_at >= since)
    out = []
    for m in session.scalars(stmt.order_by(Message.created_at, Message.id)):
        actor, account = session.get(User, m.acting_user_id), session.get(User, m.sender_user_id)
        conv = session.get(Conversation, m.conversation_id)
        peer = presentation.conversation_peer(session, account, conv) if conv is not None else None
        out.append(ActionRow(m.created_at, presentation.name_in_team(session, team.id, actor), actor.email,
                             presentation.name_in_team(session, team.id, account), account.email,
                             m.conversation_id, peer.label if peer is not None else "a teammate", m.id,
                             identity.grant_kind(session.get(Grant, m.grant_id)) or "individual"))
    return out
