"""Attachment storage quotas (protocol §18.1 item 5).

Two budgets, both configurable and each overridable per user or per team by the operator:

- per user per day (UTC): the bytes of attachments the user sent that day, from any machine or person
  session (default 50 MiB);
- per team over a rolling 30 days (default 5 GiB).

The check and the charge are one locked step inside the send transaction (transaction-scoped advisory locks,
team first, then user), so concurrent uploads cannot overshoot. Messages without attachments are never
refused. A refusal is ``413 quota_exceeded`` naming the quota and when it frees up.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from raincli_server.messaging_errors import MessagingError
from raincli_server.models import Agent, Attachment, Message, Team, User

MIB = 1024 * 1024
USER_DAILY_DEFAULT = 50 * MIB
TEAM_WINDOW_DEFAULT = 5 * 1024 * MIB
TEAM_WINDOW = timedelta(days=30)
_LOCK_CLASS = 0x5243  # advisory-lock namespace for these charges ("RC")


@dataclass(frozen=True)
class Quotas:
    user_daily: int = USER_DAILY_DEFAULT
    team_window: int = TEAM_WINDOW_DEFAULT

    @classmethod
    def from_settings(cls, settings) -> Quotas:
        return cls(settings.attachment_user_daily_bytes, settings.attachment_team_30d_bytes)


def _day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def user_usage(session: Session, user_id: uuid.UUID, now: datetime) -> int:
    """Bytes the user sent as attachments since midnight UTC, as a person or from any machine they own."""
    owned = select(Agent.id).where(Agent.owner_user_id == user_id)
    return session.scalar(
        select(func.coalesce(func.sum(Attachment.size), 0)).join(Message, Message.id == Attachment.message_id)
        .where(Attachment.created_at >= _day_start(now),
               or_(Message.sender_user_id == user_id, Message.sender_agent_id.in_(owned)))) or 0


def team_usage(session: Session, team_id: uuid.UUID, now: datetime) -> int:
    return session.scalar(
        select(func.coalesce(func.sum(Attachment.size), 0)).join(Message, Message.id == Attachment.message_id)
        .where(Message.team_id == team_id, Attachment.created_at > now - TEAM_WINDOW)) or 0


def _team_frees_at(session: Session, team_id: uuid.UUID, now: datetime, need: int) -> datetime | None:
    """When enough of the team's window ages out to free ``need`` bytes (None if it never can)."""
    running = (select(Attachment.created_at.label("at"),
                      func.sum(Attachment.size).over(order_by=(Attachment.created_at, Attachment.id)).label("cum"))
               .join(Message, Message.id == Attachment.message_id)
               .where(Message.team_id == team_id, Attachment.created_at > now - TEAM_WINDOW).subquery())
    at = session.scalar(select(running.c.at).where(running.c.cum >= need).order_by(running.c.at).limit(1))
    return None if at is None else at + TEAM_WINDOW


def user_limit(user: User, quotas: Quotas) -> int:
    return user.attachment_quota_bytes if user.attachment_quota_bytes is not None else quotas.user_daily


def team_limit(team: Team, quotas: Quotas) -> int:
    return team.attachment_quota_bytes if team.attachment_quota_bytes is not None else quotas.team_window


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _exceeded(which: str, message: str, resets_at: datetime | None) -> MessagingError:
    return MessagingError(413, "quota_exceeded", message,
                          {"quota": which, "resets_at": _iso(resets_at) if resets_at else None})


def charge(session: Session, user_id: uuid.UUID, team_id: uuid.UUID, size: int, quotas: Quotas,
           now: datetime | None = None) -> None:
    """Admit ``size`` attachment bytes from ``user_id`` in ``team_id`` or raise ``413 quota_exceeded``. The
    locks are held until the send commits, so the bytes being stored are counted for everyone after it."""
    if size <= 0:
        return
    for subject in (team_id, user_id):  # always team, then user: one order, no deadlock
        session.execute(select(func.pg_advisory_xact_lock(_LOCK_CLASS, func.hashtext(str(subject)))))
    now = now or datetime.now(timezone.utc)
    team = session.get(Team, team_id)
    user = session.get(User, user_id)
    used, limit = team_usage(session, team_id, now), team_limit(team, quotas)
    if used + size > limit:
        frees = _team_frees_at(session, team_id, now, used + size - limit) if size <= limit else None
        raise _exceeded("team_30_days", f"this team's attachment storage for the last 30 days ({limit} bytes) "
                        f"is used up; it frees up at {_iso(frees) if frees else 'no point (the files are larger)'}",
                        frees)
    used, limit = user_usage(session, user_id, now), user_limit(user, quotas)
    if used + size > limit:
        resets = _day_start(now) + timedelta(days=1)
        raise _exceeded("user_daily", f"your attachment allowance for today ({limit} bytes) is used up; it resets "
                        f"at {_iso(resets)}", resets)
