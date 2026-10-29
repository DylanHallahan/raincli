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

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
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


# The capability gate (§22, G2 with H1) ------------------------------------------------------------------------

def _copies(where) -> object:
    """Unacked, undelivered copies matching ``where``: the ones a withdrawal or a move acts on."""
    return and_(Message.route == "group", Message.acked_at.is_(None), Message.withdrawn_at.is_(None), where)


def withdraw(session: Session, where) -> int:
    """Withdraw the matching unacked copies: never returned by a poll, never redelivered, never delivered."""
    from raincli_server import identity

    result = session.execute(update(Message).where(_copies(where)).values(withdrawn_at=identity.now(),
                                                                           redeliver=False)
                             .execution_options(synchronize_session=False))
    return result.rowcount or 0


def withdraw_copies_of(session: Session, user_id: uuid.UUID, team_id: uuid.UUID,
                       group_id: uuid.UUID | None = None) -> int:
    """``_group_copies_of(user, team)`` withdrawn (G12 M1): the person's unacked copies at any of their machines in
    the team, of one group or of all."""
    mine = select(Agent.id).where(Agent.owner_user_id == user_id, Agent.team_id == team_id)
    where = and_(Message.team_id == team_id, Message.recipient_agent_id.in_(mine))
    if group_id is not None:
        where = and_(where, Message.conversation_id == group_id)
    return withdraw(session, where)


def move_copies_of(session: Session, user: User, team_id: uuid.UUID, target) -> int:
    """An inbox change: the person's unacked copies move to the new inbox (redelivered below the cursor) when it
    takes group copies; otherwise they are withdrawn. ``target`` is the new inbox endpoint, or None."""
    mine = select(Agent.id).where(Agent.owner_user_id == user.id, Agent.team_id == team_id)
    where = and_(Message.team_id == team_id, Message.recipient_agent_id.in_(mine))
    if target is None or target.kind == "person" or not machine_takes_groups(session, target.agent):
        return withdraw(session, where)
    values = dict(recipient_agent_id=target.agent.id, recipient_agent_name=target.name if target.kind == "agent"
                  else None, redeliver=True)
    result = session.execute(update(Message).where(_copies(where), Message.recipient_agent_id != target.agent.id)
                             .values(**values).execution_options(synchronize_session=False))
    # Copies already at that machine stay (a change of agent name on the same machine renames them too).
    session.execute(update(Message).where(_copies(where), Message.recipient_agent_id == target.agent.id)
                    .values(recipient_agent_name=values["recipient_agent_name"])
                    .execution_options(synchronize_session=False))
    return result.rowcount or 0


def lose_capability(session: Session, machine: Agent) -> int:
    """H1: the machine no longer takes group copies (an older client, or a poll without ``groups=1``). Its unacked
    copies are withdrawn, and fan-out skips it until it qualifies again."""
    machine.groups_capable_at = None
    session.flush()
    return withdraw(session, Message.recipient_agent_id == machine.id)


def note_poll(session: Session, machine: Agent, groups_flag: bool) -> None:
    """A poll: ``groups=1`` on a groups client makes the machine capable; anything else ends it."""
    from raincli_server import identity

    presence = session.get(AgentPresence, machine.id)
    current = client_takes_groups(presence.client_version if presence is not None else None)
    if groups_flag and current:
        if machine.groups_capable_at is None:
            machine.groups_capable_at = identity.now()
            session.flush()
    elif machine.groups_capable_at is not None:
        lose_capability(session, machine)


def note_presence(session: Session, machine: Agent) -> None:
    """A presence report: a client below ``GROUPS_CLIENT`` ends the machine's capability (H1)."""
    if machine.groups_capable_at is not None and not machine_takes_groups(session, machine):
        lose_capability(session, machine)


# Mentions (§22, L5) ---------------------------------------------------------------------------------------------

def _candidates(session: Session, group: Conversation) -> dict[str, set[uuid.UUID]]:
    """Each member's email and names (lower case) to the members they could mean."""
    out: dict[str, set[uuid.UUID]] = {}
    for user in session.scalars(select(User).join(GroupMember, GroupMember.user_id == User.id)
                                .where(GroupMember.conversation_id == group.id)):
        for key in {user.email, presentation.display_name(user), presentation.name_in_team(session, group.team_id, user)}:
            out.setdefault(key.lower(), set()).add(user.id)
    return out


def resolve_mentions(session: Session, group: Conversation, explicit: object, body: str,
                     poster: uuid.UUID) -> list[uuid.UUID]:
    """The mentioned members: the explicit list (each an email, ``@email``, a name or ``@Name``; unknown or
    ambiguous is a 400), then ``@email`` and ``@Name`` in the body, longest match first (an ambiguous or unknown
    token stays text; ``@everyone`` is nothing special). In order of first appearance, without the poster."""
    from raincli_server.messaging import MessagingError

    cands = _candidates(session, group)
    out: list[uuid.UUID] = []

    def add(uid):
        if uid != poster and uid not in out:
            out.append(uid)
    if explicit is not None:
        if not isinstance(explicit, list) or len(explicit) > MAX_MEMBERS or \
                not all(isinstance(x, str) and 0 < len(x) <= 254 for x in explicit):
            raise MessagingError(400, "invalid", "mentions must be a list of people (a name or an email)")
        for text in explicit:
            key = text.strip().removeprefix("@").strip().lower()
            found = cands.get(key, set())
            if len(found) != 1:
                raise MessagingError(400, "invalid", f"{text.strip()[:80]} isn't one member of this group: mention "
                                                     "a member by their name or email")
            add(next(iter(found)))
    keys = sorted((k for k, v in cands.items() if len(v) == 1), key=len, reverse=True)
    lowered = body.lower()
    i = lowered.find("@")
    while i != -1:
        rest = lowered[i + 1:]
        for key in keys:
            end = len(key)
            if rest.startswith(key) and (end == len(rest) or not (rest[end].isalnum() or rest[end] in "_-")):
                add(next(iter(cands[key])))
                break
        i = lowered.find("@", i + 1)
    return out


# Posting (§22, G3 with M2, M6, L5, L6) -------------------------------------------------------------------------

GROUP_INBOX_ONLY = "Post from the machine that holds your inbox."


def _unknown():
    from raincli_server.messaging import MessagingError

    return MessagingError(404, "not_found", "group not found")


def parent_post(session: Session, group: Conversation, machine: Agent, ref: object) -> GroupPost | None:
    """What a reply answers: a copy this machine currently holds (not withdrawn), or a post, in this group."""
    if ref is None:
        return None
    rid = _parse(ref)
    if rid is None:
        raise _unknown()
    copy = session.get(Message, rid)
    if copy is not None and copy.route == "group":
        if copy.recipient_agent_id != machine.id or copy.withdrawn_at is not None or copy.conversation_id != group.id:
            raise _unknown()
        return session.get(GroupPost, copy.post_id)
    post = session.get(GroupPost, rid)
    if post is None or post.conversation_id != group.id:
        raise _unknown()
    return post


def post(session: Session, machine: Agent, group_ref: object, *, id: object, body: object,  # noqa: A002
         mentions: object = None, in_reply_to: object = None, from_agent: str | None = None,
         reply_copy: Message | None = None) -> tuple[GroupPost, bool]:
    """Post in a group as ``machine`` (and its stated agent), fanning out one copy per other member taking part.
    Returns ``(post, created)``. ``reply_copy``: the copy a reply through ``POST /messages`` answers."""
    from raincli_server import security
    from raincli_server.messaging import MessagingError, _inbox_target, _lock_inbox_row, _set_state, parse_uuid

    post_id = parse_uuid(id, "id")
    if not isinstance(body, str) or not security.valid_message_body(body):
        raise MessagingError(400, "invalid", "body must be 1-16000 characters of plain text")
    group = visible_group(session, group_ref, machine=machine)  # L6 (1): unknown, outside or not a member
    if group is None:
        raise _unknown()
    parent = session.get(GroupPost, reply_copy.post_id) if reply_copy is not None else \
        parent_post(session, group, machine, in_reply_to)
    explicit = mentions
    owner = machine.owner_user_id

    # One id namespace for posts and messages (M6, §21.13): a retry is the same post; anything else conflicts.
    existing = session.get(GroupPost, post_id)
    if existing is not None or session.get(Message, post_id) is not None:
        if existing is not None and existing.conversation_id == group.id and existing.author_agent_id == machine.id \
                and existing.author_agent_name == from_agent and existing.body == body \
                and existing.in_reply_to_post == (parent.id if parent else None) \
                and existing.mentions == resolve_mentions(session, group, explicit, body, owner):
            return existing, False
        raise MessagingError(409, "id_conflict", "a different message already uses this id")

    # M2 lock order: the group row, then the members' membership and inbox rows by user id, then the inserts.
    session.execute(select(Conversation.id).where(Conversation.id == group.id)
                    .with_for_update(read=True, key_share=True))
    members = sorted(session.scalars(select(GroupMember.user_id).where(GroupMember.conversation_id == group.id)))
    if owner not in members:
        raise _unknown()
    inboxes = {uid: _lock_inbox_row(session, uid, group.team_id, write=False) for uid in members}
    if inboxes[owner] is None or inboxes[owner].agent_id != machine.id:  # L6 (2)
        raise MessagingError(403, "group_inbox_only", GROUP_INBOX_ONLY)
    mentioned = resolve_mentions(session, group, explicit, body, owner)

    # The post's number first (the team counter, last of the locks). A concurrent post with the same id (a retry
    # racing the original) waits here for it, then is answered as a retry: the same post, or 409.
    inserted = session.scalar(pg_insert(GroupPost).values(
        id=post_id, team_id=group.team_id, conversation_id=group.id, author_agent_id=machine.id,
        author_agent_name=from_agent, author_user_id=owner, body=body, mentions=mentioned,
        in_reply_to_post=parent.id if parent else None).on_conflict_do_nothing(index_elements=["id"])
        .returning(GroupPost.id))
    if inserted is None:
        existing = session.scalars(select(GroupPost).where(GroupPost.id == post_id)
                                   .execution_options(populate_existing=True)).one()
        if existing.conversation_id == group.id and existing.author_agent_id == machine.id \
                and existing.author_agent_name == from_agent and existing.body == body \
                and existing.in_reply_to_post == (parent.id if parent else None) and existing.mentions == mentioned:
            return existing, False
        raise MessagingError(409, "id_conflict", "a different message already uses this id")
    created = session.scalars(select(GroupPost).where(GroupPost.id == post_id)
                              .execution_options(populate_existing=True)).one()
    copies = 0
    for uid in members:
        if uid == owner:
            continue
        person = session.get(User, uid)
        target, _route = _inbox_target(session, inboxes[uid], person)
        if target.kind == "person" or not machine_takes_groups(session, target.agent):
            continue  # not taking part (D10): no copy, ever, for this post
        session.add(Message(id=uuid.uuid4(), team_id=group.team_id, conversation_id=group.id, body=body,
                            kind="message", route="group", post_id=post_id, delivery_state="stored",
                            sender_agent_id=machine.id, sender_agent_name=from_agent,
                            recipient_agent_id=target.agent.id,
                            recipient_agent_name=target.name if target.kind == "agent" else None))
        copies += 1
    created.recipients_m = copies
    session.flush()
    if reply_copy is not None and reply_copy.delivery_state != "replied":
        _set_state(session, reply_copy.id, "replied", machine.id, detail=None)
    return created, True


def post_json(session: Session, post_row: GroupPost) -> dict:
    """What both posting endpoints answer (M6): the post, never its copies."""
    group = session.get(Conversation, post_row.conversation_id)
    return {"id": str(post_row.id), "group": {"id": str(group.id), "name": group.name}, "seq": post_row.seq,
            "in_reply_to_post": str(post_row.in_reply_to_post) if post_row.in_reply_to_post else None,
            "mentions": [{"display_name": name, "email": user.email} for user, name in mention_names(session, post_row)],
            "recipients_m": post_row.recipients_m}


def group_json(session: Session, group: Conversation, viewer_id: uuid.UUID) -> dict:
    return {"id": str(group.id), "name": group.name, "members_n": members_n(session, group),
            "taking_part": taking_part(session, group.team_id, viewer_id)}


def copy_fields(session: Session, msg: Message, reader_user_id: uuid.UUID | None) -> dict:
    """The fields a group copy adds to message JSON for its recipient (§22)."""
    post_row = session.get(GroupPost, msg.post_id)
    group = session.get(Conversation, msg.conversation_id)
    return {"group": {"id": str(group.id), "name": group.name, "members_n": members_n(session, group)},
            "post_id": str(post_row.id),
            "in_reply_to_post": str(post_row.in_reply_to_post) if post_row.in_reply_to_post else None,
            "mentions": [{"display_name": name, "email": user.email} for user, name in mention_names(session, post_row)],
            "mentioned": reader_user_id is not None and reader_user_id in (post_row.mentions or [])}


# Membership changes (§22 G4, G8) ------------------------------------------------------------------------------

def lock_groups_of(session: Session, user_id: uuid.UUID, team_id: uuid.UUID) -> list[uuid.UUID]:
    """M2: lock the person's group rows in the team, by id, before any membership or inbox row."""
    ids = sorted(session.scalars(select(GroupMember.conversation_id).where(
        GroupMember.user_id == user_id, GroupMember.team_id == team_id)), key=str)
    for gid in ids:
        session.execute(select(Conversation.id).where(Conversation.id == gid).with_for_update())
    return ids


def leave_all(session: Session, user_id: uuid.UUID, team_id: uuid.UUID) -> int:
    """The person leaves every group in the team (a team removal, a disable): their member rows go and their
    unacked copies are withdrawn. The caller holds the group locks (``lock_groups_of``)."""
    session.execute(delete(GroupMember).where(GroupMember.user_id == user_id, GroupMember.team_id == team_id))
    withdrawn = withdraw_copies_of(session, user_id, team_id)
    session.flush()
    return withdrawn


# Operator commands (§22 G8): raincli-admin only, never HTTP ----------------------------------------------------

class GroupRuleError(Exception):
    """A refused operator command, with a message for the operator."""


_BIDI = set("‎‏‪‫‬‭‮⁦⁧⁨⁩")


def clean_name(name: str) -> str:
    """L1: trimmed, NFC, 1-40 characters, with no control, line-break or bidi-override characters."""
    import unicodedata

    value = unicodedata.normalize("NFC", (name or "").strip())
    if not 1 <= len(value) <= NAME_MAX:
        raise GroupRuleError(f"a group name has 1 to {NAME_MAX} characters")
    if any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") or ch in _BIDI for ch in value):
        raise GroupRuleError("a group name can't contain control characters, line breaks or direction overrides")
    return value


def named_group(session: Session, team: Team, name: str, *, lock: bool = False) -> Conversation:
    value = clean_name(name)
    stmt = select(Conversation).where(Conversation.team_id == team.id, Conversation.kind == "group",
                                      func.lower(Conversation.name) == value.lower())
    group = session.scalars(stmt.with_for_update() if lock else stmt).first()
    if group is None:
        raise GroupRuleError(f'there is no group "{value}" in {team.slug}')
    return group


def _people(session: Session, team: Team, emails: list[str]) -> list[User]:
    from raincli_server import identity

    out = []
    for email in emails:
        user = identity.find_user_by_email(session, email)
        if user is None or not user.is_active or session.get(Membership, (team.id, user.id)) is None:
            raise GroupRuleError(f"{email} isn't an active member of {team.slug}")
        if user.id in {u.id for u in out}:
            raise GroupRuleError(f"{email} is named twice")
        out.append(user)
    return out


def create_group(session: Session, team: Team, name: str, emails: list[str]) -> Conversation:
    value = clean_name(name)
    if session.scalar(select(Conversation.id).where(Conversation.team_id == team.id, Conversation.kind == "group",
                                                    func.lower(Conversation.name) == value.lower())) is not None:
        raise GroupRuleError(f'{team.slug} already has a group "{value}" (names are unique, whatever the case)')
    people = _people(session, team, emails)
    if len(people) > MAX_MEMBERS:
        raise GroupRuleError(f"a group has at most {MAX_MEMBERS} members")
    gid, slug = uuid.uuid4(), team.slug
    group = Conversation(id=gid, team_id=team.id, kind="group", name=value, a_key=f"group:{gid}",
                         b_key=f"group:{gid}:", is_default=False)
    session.add(group)
    try:
        session.flush()
    except IntegrityError:  # another group-create with the same name won the race
        raise GroupRuleError(f'{slug} already has a group "{value}" (names are unique, whatever the case)') \
            from None
    for person in people:
        session.add(GroupMember(conversation_id=gid, user_id=person.id, team_id=team.id))
    session.flush()
    return group


def add_members(session: Session, team: Team, name: str, emails: list[str]) -> Conversation:
    group = named_group(session, team, name, lock=True)  # M2: the group row first
    people = _people(session, team, emails)
    for person in people:
        if is_member(session, group, person.id):
            raise GroupRuleError(f'{person.email} is already in "{group.name}"')
    if members_n(session, group) + len(people) > MAX_MEMBERS:
        raise GroupRuleError(f'"{group.name}" would have more than {MAX_MEMBERS} members (it has '
                             f"{members_n(session, group)})")
    for person in people:  # D11: they receive only posts made from now on
        session.add(GroupMember(conversation_id=group.id, user_id=person.id, team_id=team.id))
    session.flush()
    return group


def remove_members(session: Session, team: Team, name: str, emails: list[str]) -> tuple[Conversation, int]:
    """Members leave; their unacked copies of this group are withdrawn (G4). Returns the group and how many."""
    from raincli_server import identity

    group = named_group(session, team, name, lock=True)
    people = []
    for email in emails:
        user = identity.find_user_by_email(session, email)
        if user is None or not is_member(session, group, user.id):
            raise GroupRuleError(f'{email} isn\'t in "{group.name}"')
        if user.id in {u.id for u in people}:
            raise GroupRuleError(f"{email} is named twice")
        people.append(user)
    withdrawn = 0
    for person in people:
        session.execute(delete(GroupMember).where(GroupMember.conversation_id == group.id,
                                                  GroupMember.user_id == person.id))
        withdrawn += withdraw_copies_of(session, person.id, team.id, group.id)
    session.flush()
    return group, withdrawn
