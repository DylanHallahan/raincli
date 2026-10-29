"""Group chats (protocol §22): who sees a group, who takes part, and the read-side summaries.

The helpers here never write. Posting, fan-out, withdrawal and the operator commands build on them.

- **Visible:** a group is visible to its current members only (a person, or a machine of a member in the group's
  team; an app session only in its own team, §21.13). Everyone else gets the same answer as for an unknown id.
- **Taking part (D10):** a member takes part while they have an inbox in the group's team on a machine that is
  active, has polled with ``groups=1`` (``groups_capable_at``) and whose current presence reports a client of at
  least ``GROUPS_CLIENT``. It is computed live, never stored.
- **Delivered to N of M:** M is the copies made at fan-out (``recipients_m``); N the copies not withdrawn whose state
  is received, submitted or replied. Only copy states count, never anyone's reads.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from raincli_server import presentation
from raincli_server.models import (Agent, AgentPresence, Conversation, GroupMember, GroupPost, Membership, Message,
                                   PersonInbox, Team, User)

GROUPS_CLIENT = (0, 6, 1)  # the first client that takes group copies (§22)
DELIVERED_STATES = ("received", "submitted", "replied")
MAX_MEMBERS = 20  # D8
NAME_MAX = 40


def client_takes_groups(version: str | None) -> bool:
    """Whether a reported client version is at least ``GROUPS_CLIENT``."""
    try:
        return version is not None and tuple(int(x) for x in version.split(".")[:3]) >= GROUPS_CLIENT
    except ValueError:
        return False


def machine_takes_groups(session: Session, machine: Agent | None) -> bool:
    """An active machine that polled with ``groups=1`` and whose current presence reports a groups client."""
    if machine is None or machine.revoked_at is not None or machine.groups_capable_at is None:
        return False
    presence = session.get(AgentPresence, machine.id)
    return client_takes_groups(presence.client_version if presence is not None else None)


def inbox_machine(session: Session, team_id: uuid.UUID, user_id: uuid.UUID) -> Agent | None:
    """The machine holding the person's inbox in the team, if any (active or not)."""
    row = session.get(PersonInbox, (user_id, team_id))
    return session.get(Agent, row.agent_id) if row is not None else None


def taking_part(session: Session, team_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return machine_takes_groups(session, inbox_machine(session, team_id, user_id))


# Groups and visibility -----------------------------------------------------------------------------------------

def _parse(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(value) if isinstance(value, str) and len(value) <= 64 else None
    except ValueError:
        return None


def get_group(session: Session, group_id: object) -> Conversation | None:
    """The group with this id, whoever asks (callers check visibility)."""
    gid = _parse(group_id)
    conv = session.get(Conversation, gid) if gid is not None else None
    return conv if conv is not None and conv.kind == "group" else None


def is_member(session: Session, group: Conversation, user_id: uuid.UUID) -> bool:
    return session.get(GroupMember, (group.id, user_id)) is not None


def visible_group(session: Session, group_id: object, *, user: User | None = None, machine: Agent | None = None,
                  scope: uuid.UUID | None = None) -> Conversation | None:
    """The group if the viewer may see it, else None (answered like an unknown id).

    - ``user``: a person (a website session has ``scope`` None; an app session has its machine's team);
    - ``machine``: a machine credential, which sees its owner's groups in its own team.
    """
    group = get_group(session, group_id)
    if group is None:
        return None
    if machine is not None:
        if machine.revoked_at is not None or machine.team_id != group.team_id:
            return None
        member = machine.owner_user_id
    elif user is not None:
        if (scope is not None and scope != group.team_id) or not user.is_active:
            return None
        member = user.id
    else:
        return None
    if session.get(Membership, (group.team_id, member)) is None or not is_member(session, group, member):
        return None
    return group


@dataclass(frozen=True)
class MemberRow:
    user: User
    name: str  # the team's display name (look-alikes told apart, §18.1)
    taking_part: bool
    joined_at: datetime

    def json(self) -> dict:
        return {"display_name": self.name, "email": self.user.email, "taking_part": self.taking_part}


def members_with_taking_part(session: Session, group: Conversation) -> list[MemberRow]:
    """The group's members as people, with whether each takes part, by name. Never machines or agents."""
    rows = session.execute(
        select(User, GroupMember.joined_at).join(GroupMember, GroupMember.user_id == User.id)
        .where(GroupMember.conversation_id == group.id)).all()
    out = [MemberRow(user, presentation.name_in_team(session, group.team_id, user),
                     taking_part(session, group.team_id, user.id), joined) for user, joined in rows]
    return sorted(out, key=lambda r: (r.name.lower(), r.user.email))


def members_n(session: Session, group: Conversation) -> int:
    return session.scalar(select(func.count()).select_from(GroupMember)
                          .where(GroupMember.conversation_id == group.id)) or 0


def latest_post_seq(session: Session, group_id: uuid.UUID) -> int | None:
    """The newest post's team number in the group (its newest activity), or None."""
    return session.scalar(select(func.max(GroupPost.seq)).where(GroupPost.conversation_id == group_id))


@dataclass(frozen=True)
class GroupRow:
    group: Conversation
    team: Team
    members_n: int
    latest_seq: int | None  # the newest post's team number (D12 compares it with 1:1 seqs, §22)
    latest_at: datetime | None


def groups_of(session: Session, user: User, team_ids: list[uuid.UUID]) -> list[GroupRow]:
    """The person's groups in these teams, newest activity first (groups without posts last, by name)."""
    if not team_ids:
        return []
    counts = (select(GroupMember.conversation_id.label("cid"), func.count().label("n"))
              .group_by(GroupMember.conversation_id).subquery())
    latest = (select(GroupPost.conversation_id.label("cid"), func.max(GroupPost.seq).label("seq"),
                     func.max(GroupPost.created_at).label("at"))
              .group_by(GroupPost.conversation_id).subquery())
    rows = session.execute(
        select(Conversation, Team, counts.c.n, latest.c.seq, latest.c.at)
        .join(GroupMember, and_(GroupMember.conversation_id == Conversation.id, GroupMember.user_id == user.id))
        .join(Membership, and_(Membership.team_id == Conversation.team_id, Membership.user_id == user.id))
        .join(Team, Team.id == Conversation.team_id)
        .join(counts, counts.c.cid == Conversation.id)
        .outerjoin(latest, latest.c.cid == Conversation.id)
        .where(Conversation.kind == "group", Conversation.team_id.in_(team_ids))).all()
    out = [GroupRow(conv, team, n, seq, at) for conv, team, n, seq, at in rows]
    return sorted(out, key=lambda r: (r.latest_seq is None, -(r.latest_seq or 0), r.group.name.lower()))


# Posts -------------------------------------------------------------------------------------------------------------

def posts_of(session: Session, group: Conversation, *, after: int = 0, limit: int = 200) -> list[GroupPost]:
    """The group's posts with a team number above ``after``, oldest first (the thread order)."""
    return list(session.scalars(
        select(GroupPost).where(GroupPost.conversation_id == group.id, GroupPost.team_id == group.team_id,
                                GroupPost.seq > after)
        .order_by(GroupPost.seq).limit(max(1, min(limit, 500)))))


def delivered_summary(session: Session, post: GroupPost) -> tuple[int, int]:
    """``(N, M)`` for "Delivered to N of M": copies delivered (received, submitted or replied, not withdrawn) and
    the copies made at fan-out. From copy states only."""
    n = session.scalar(select(func.count()).select_from(Message).where(
        Message.post_id == post.id, Message.withdrawn_at.is_(None),
        Message.delivery_state.in_(DELIVERED_STATES))) or 0
    return n, post.recipients_m


def mention_names(session: Session, post: GroupPost) -> list[tuple[User, str]]:
    """The mentioned people in stored order, with their team display names."""
    out = []
    for uid in post.mentions or []:
        user = session.get(User, uid)
        if user is not None:
            out.append((user, presentation.name_in_team(session, post.team_id, user)))
    return out
