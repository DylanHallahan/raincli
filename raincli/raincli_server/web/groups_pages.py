"""The app's read-only group pages (protocol §22 G6, D9): the thread, the member panel and "Delivered to N of M".

- **Read-only:** no composer, no trail, no ack. Nothing here writes, and nothing a page shows depends on anyone's
  reads (PR5): "Delivered to N of M" comes from copy states only (``groups.delivered_summary``'s rule, in one grouped
  query per page or poll).
- **People, never machines (PR4):** a post shows as "<Name> · agent", a mention as "@<Name>"; the stated agent name,
  the machine and its handle never reach a page.
- **App mode only:** the website has no group pages in v1 (L4); a person session sees its own team's groups (T1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from raincli_server import groups, presentation
from raincli_server.models import Conversation, GroupPost, Message, Team, User
from raincli_server.web import present

# The member panel's line (the brief's wording, D6): who changes a group's members. One constant, so it can become a
# server setting if other teams' operators differ.
GROUP_OPERATOR = "Dylan"
GROUP_PAGE = 200  # the newest posts a group page shows (as CONVERSATION_PAGE for a 1:1 thread)
LIVE_IN_VIEW = 200  # the viewer's own posts among the newest this many get their "Delivered" count on each poll


@dataclass
class PostLine:
    post: GroupPost
    author: str  # "Jon · agent" (presentation: the person behind the machine, look-alikes told apart)
    initials: str
    mentions: list[str]  # "Jon", "Priya": the mentioned people's team names, in stored order
    own: bool  # the viewer's own agent's post: it shows "Delivered to N of M"
    delivered: tuple[int, int] | None = None
    reply_to: str = ""  # the replied-to post's author ("Maya · agent"), when it is a reply
    reply_to_id: uuid.UUID | None = None
    reply_to_shown: bool = False  # whether the replied-to post is on the page (the line links to it)


@dataclass
class GroupView:
    group: Conversation
    team: Team
    members: list[groups.MemberRow]
    lines: list[PostLine] = field(default_factory=list)
    truncated: bool = False
    newest_seq: int = 0

    @property
    def members_n(self) -> int:
        return len(self.members)

    def initials(self, member: groups.MemberRow) -> str:
        return present.initials(member.name)


class _Names:
    """Team names per person, worked out once per request (through presentation, as thread.Names)."""

    def __init__(self, db: Session, team_id: uuid.UUID):
        self.db, self.team_id, self._cache = db, team_id, {}

    def of(self, user_id: uuid.UUID) -> str:
        if user_id not in self._cache:
            user = self.db.get(User, user_id)
            self._cache[user_id] = presentation.name_in_team(self.db, self.team_id, user) if user else "a teammate"
        return self._cache[user_id]


def delivered_counts(db: Session, post_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """N for each post: its copies not withdrawn whose state is received, submitted or replied (one query)."""
    if not post_ids:
        return {}
    return dict(db.execute(
        select(Message.post_id, func.count()).where(
            Message.post_id.in_(post_ids), Message.withdrawn_at.is_(None),
            Message.delivery_state.in_(groups.DELIVERED_STATES))
        .group_by(Message.post_id)).all())


def post_lines(db: Session, user: User, group: Conversation, posts: list[GroupPost],
               shown_ids: set[uuid.UUID] | None = None) -> list[PostLine]:
    """The lines for these posts, as the viewer sees them. ``shown_ids``: the posts on the page (a reply's "Reply
    to" line links to its post when it is there)."""
    names = _Names(db, group.team_id)
    own = [p.id for p in posts if p.author_user_id == user.id]
    counts = delivered_counts(db, own)
    parent_ids = {p.in_reply_to_post for p in posts if p.in_reply_to_post is not None}
    parents = {p.id: p for p in db.scalars(select(GroupPost).where(GroupPost.id.in_(parent_ids)))} \
        if parent_ids else {}
    shown = shown_ids if shown_ids is not None else {p.id for p in posts}
    lines = []
    for p in posts:
        name = names.of(p.author_user_id)
        parent = parents.get(p.in_reply_to_post) if p.in_reply_to_post is not None else None
        lines.append(PostLine(
            post=p, author=f"{name} · agent", initials=present.initials(name),
            mentions=[names.of(uid) for uid in (p.mentions or []) if uid != p.author_user_id],
            own=p.author_user_id == user.id,
            delivered=(counts.get(p.id, 0), p.recipients_m) if p.author_user_id == user.id else None,
            reply_to=f"{names.of(parent.author_user_id)} · agent" if parent is not None else "",
            reply_to_id=parent.id if parent is not None else None,
            reply_to_shown=parent is not None and parent.id in shown))
    return lines


def group_view(db: Session, user: User, scope: uuid.UUID | None, group_id: object) -> GroupView | None:
    """The group page, or None (answered like an unknown id) when the viewer isn't a current member (D7)."""
    group = groups.visible_group(db, group_id, user=user, scope=scope)
    if group is None:
        return None
    newest = list(db.scalars(
        select(GroupPost).where(GroupPost.conversation_id == group.id, GroupPost.team_id == group.team_id)
        .order_by(GroupPost.seq.desc()).limit(GROUP_PAGE + 1)))
    truncated = len(newest) > GROUP_PAGE
    posts = list(reversed(newest[:GROUP_PAGE]))
    view = GroupView(group=group, team=db.get(Team, group.team_id),
                     members=groups.members_with_taking_part(db, group), truncated=truncated,
                     newest_seq=posts[-1].seq if posts else 0)
    view.lines = post_lines(db, user, group, posts)
    return view


def own_delivered(db: Session, user: User, group: Conversation) -> list[tuple[uuid.UUID, int, int]]:
    """``(post id, N, M)`` for the viewer's own agent's posts among the group's newest LIVE_IN_VIEW (live.js keeps
    each post's "Delivered to N of M" current from these)."""
    newest = (select(GroupPost.id, GroupPost.author_user_id, GroupPost.recipients_m)
              .where(GroupPost.conversation_id == group.id, GroupPost.team_id == group.team_id)
              .order_by(GroupPost.seq.desc()).limit(LIVE_IN_VIEW).subquery())
    own = db.execute(select(newest.c.id, newest.c.recipients_m).where(newest.c.author_user_id == user.id)).all()
    counts = delivered_counts(db, [pid for pid, _ in own])
    return [(pid, counts.get(pid, 0), m) for pid, m in own]


def newest_activity(rows, group_rows) -> tuple[str, uuid.UUID] | None:
    """D12 (G12 L4): what "open latest" opens: the 1:1 conversation or the group with the newest activity, by team
    number (a person session covers one team, §21.13, so the numbers compare). ``rows``: the 1:1 list, newest first;
    ``group_rows``: ``groups.groups_of``, newest first."""
    conv = next((r for r in rows if r.last is not None), None)
    group = next((g for g in group_rows if g.latest_seq is not None), None)
    if group is not None and (conv is None or group.latest_seq > conv.last.seq):
        return "group", group.group.id
    if conv is not None:
        return "conversation", conv.conversation.id
    if rows:
        return "conversation", rows[0].conversation.id
    return None


def latest_time(row: groups.GroupRow) -> datetime | None:
    return row.latest_at or row.group.created_at


def delivered_text(n: int, m: int) -> str:
    """"Delivered to N of M" (the page and the live update use the same words)."""
    return f"Delivered to {n} of {m}"
