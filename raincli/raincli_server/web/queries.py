"""Read queries for the web app, plus the send adapter.

Every query is scoped to the viewing user: their memberships and the agents they
own. Anything outside that scope is reported as "not found" by the callers.

Sending and attachment access go through ``raincli_server.messaging`` (the
agent API's service layer), so the browser follows exactly the same protocol
rules; ``send_as`` only maps its errors onto text for the page.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from raincli_server import identity, messaging, presentation, security
from raincli_server.models import (
    Agent,
    AgentCredential,
    AgentPresence,
    Attachment,
    Conversation,
    DeliveryEvent,
    Invitation,
    Membership,
    Message,
    Team,
    User,
)

CONNECTED_WINDOW = timedelta(minutes=5)
CONVERSATION_PAGE = 200


def parse_uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


# Agents ---------------------------------------------------------------------

@dataclass
class AgentRow:
    agent: Agent
    team: Team
    owner: User
    credential_prefix: str | None
    last_used_at: datetime | None
    credentials_active: int
    presence: AgentPresence | None = None
    # Live sessions the machine's runtime published (protocol §14.2): inbox first, never keys.
    sessions: list = field(default_factory=list)
    owner_shown: str = ""  # §18.1 item 7: the owner as the team sees them (owners.displays)

    @property
    def runtime_status(self) -> str:
        from raincli_server.presence import TTL_SECONDS
        if not self.active:
            return "offline"
        if self.presence is None:
            return "unknown"
        if identity.now() - self.presence.seen_at >= timedelta(seconds=TTL_SECONDS):
            return "offline"
        return self.presence.status

    @property
    def client(self) -> AgentPresence | None:
        """The machine's last client report (version and update state), or None if never reported."""
        if not self.active or self.presence is None or self.presence.client_version is None:
            return None
        return self.presence

    @property
    def active(self) -> bool:
        return self.agent.revoked_at is None

    @property
    def connection(self) -> str:
        """connected / idle / never / revoked, from the credentials' ``last_used_at``."""
        if not self.active:
            return "revoked"
        if self.last_used_at is None:
            return "never"
        return "connected" if identity.now() - self.last_used_at <= CONNECTED_WINDOW else "idle"


def _agent_rows(db: Session, where) -> list[AgentRow]:
    live = and_(AgentCredential.agent_id == Agent.id, AgentCredential.revoked_at.is_(None))
    last_used = select(func.max(AgentCredential.last_used_at)).where(AgentCredential.agent_id == Agent.id)
    prefix = (
        select(AgentCredential.prefix).where(live)
        .order_by(AgentCredential.created_at.desc()).limit(1)
    )
    active = select(func.count()).select_from(AgentCredential).where(live)
    rows = db.execute(
        select(
            Agent, Team, User, prefix.scalar_subquery(), last_used.scalar_subquery(), active.scalar_subquery(), AgentPresence,
        )
        .join(Team, Team.id == Agent.team_id).join(User, User.id == Agent.owner_user_id)
        .outerjoin(AgentPresence, AgentPresence.agent_id == Agent.id)
        .where(where).order_by(Agent.revoked_at.is_not(None), Team.name, Agent.handle)
    )
    out = [AgentRow(*row) for row in rows]
    from raincli_server.presence import live_machine_agents
    live = live_machine_agents(db, [r.agent.id for r in out if r.active])
    for r in out:
        r.sessions = live.get(r.agent.id, [])
    _show_owners(db, out)
    return out


def _show_owners(db: Session, rows) -> None:
    """Set ``owner_shown`` on machine rows: the owner's display name, told apart within the team (§18.1)."""
    from raincli_server import owners

    shown: dict = {}
    for r in rows:
        if r.team.id not in shown:
            shown[r.team.id] = owners.displays(db, r.team.id)
        if r.owner.id not in shown[r.team.id]:  # an owner who has left the team
            shown[r.team.id] = owners.displays(db, r.team.id, [r.owner])
        r.owner_shown = shown[r.team.id][r.owner.id]


def my_agents(db: Session, user: User, team_ids: list[uuid.UUID]) -> list[AgentRow]:
    if not team_ids:
        return []
    return _agent_rows(db, and_(Agent.owner_user_id == user.id, Agent.team_id.in_(team_ids)))


def team_machines(db: Session, user: User, team_ids: list[uuid.UUID]) -> list[AgentRow]:
    """Every machine in the viewer's teams, the viewer's own first (protocol §14.9).

    The same team scope as ``GET /api/v1/agents``; managing a machine stays with its
    owner (or a team owner, through the team routes)."""
    if not team_ids:
        return []
    rows = _agent_rows(db, Agent.team_id.in_(team_ids))
    return sorted(rows, key=lambda r: r.agent.owner_user_id != user.id)  # stable: keeps the query order


def visible_machines(db: Session, user: User, teams: list[tuple[Team, str]]) -> list[AgentRow]:
    """§21 C5.8 (PR11): the website Machines page. A member sees only their own machines; a team owner also sees
    every machine of the teams they own (the template shows those without agents, sessions or presence)."""
    team_ids = [t.id for t, _ in teams]
    owned_teams = {t.id for t, role in teams if role == "owner"}
    return [r for r in team_machines(db, user, team_ids)
            if r.agent.owner_user_id == user.id or r.team.id in owned_teams]


def team_agents(db: Session, team: Team) -> list[AgentRow]:
    return _agent_rows(db, Agent.team_id == team.id)


def owned_agent(db: Session, user: User, team_ids: list[uuid.UUID], agent_id: object) -> Agent | None:
    aid = parse_uuid(agent_id)
    if aid is None or not team_ids:
        return None
    return db.scalar(
        select(Agent).where(Agent.id == aid, Agent.owner_user_id == user.id, Agent.team_id.in_(team_ids))
    )


def team_agent(db: Session, team: Team, agent_id: object) -> Agent | None:
    aid = parse_uuid(agent_id)
    if aid is None:
        return None
    return db.scalar(select(Agent).where(Agent.id == aid, Agent.team_id == team.id))


def active_handles(db: Session, team_ids: list[uuid.UUID]) -> list[tuple[str, str]]:
    """(team slug, handle) of every active agent in the viewer's teams, for recipient suggestions."""
    if not team_ids:
        return []
    rows = db.execute(
        select(Team.slug, Agent.handle).join(Team, Team.id == Agent.team_id)
        .where(Agent.team_id.in_(team_ids), Agent.revoked_at.is_(None)).order_by(Team.slug, Agent.handle)
    )
    return [(slug, handle) for slug, handle in rows]


# Teams ----------------------------------------------------------------------

def team_members(db: Session, team: Team) -> list[tuple[User, str, datetime]]:
    rows = db.execute(
        select(User, Membership.role, Membership.created_at).join(Membership, Membership.user_id == User.id)
        .where(Membership.team_id == team.id).order_by(Membership.role.desc(), User.display_name)
    )
    return [tuple(r) for r in rows]


def open_invitations(db: Session, team: Team) -> list[tuple[Invitation, User]]:
    rows = db.execute(
        select(Invitation, User).join(User, User.id == Invitation.invited_by)
        .where(
            Invitation.team_id == team.id, Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None),
            Invitation.expires_at > identity.now(),
        ).order_by(Invitation.created_at.desc())
    )
    return [tuple(r) for r in rows]


def peek_invitation(db: Session, token: str) -> tuple[Invitation, Team] | None:
    if not isinstance(token, str) or not token.startswith(security.INVITE_TOKEN_PREFIX) or len(token) > 200:
        return None
    row = db.execute(
        select(Invitation, Team).join(Team, Team.id == Invitation.team_id)
        .where(Invitation.token_hash == security.hash_token(token))
    ).first()
    if row is None:
        return None
    inv, team = row
    if inv.accepted_at or inv.revoked_at or inv.expires_at <= identity.now():
        return None
    return inv, team


# Conversations ----------------------------------------------------------------

@dataclass
class Side:
    """One conversation endpoint as the viewer sees it (§16.6)."""

    endpoint: messaging.Endpoint
    mine: bool  # the viewer is this person, or owns this machine

    @property
    def label(self) -> str:
        return self.endpoint.label()

    @property
    def display(self) -> str:
        """The person (§21 C5.1): "You" for the viewer's own person side, else their name. Never a handle."""
        if self.endpoint.kind == "person":
            return "You" if self.mine else presentation.display_name(self.endpoint.user)
        return self.user_name

    @property
    def user_name(self) -> str:
        user = self.endpoint.user if self.endpoint.kind == "person" else None
        if user is None and self.endpoint.agent is not None:
            from raincli_server.models import User as _User
            from sqlalchemy.orm import object_session

            db = object_session(self.endpoint.agent)
            user = db.get(_User, self.endpoint.agent.owner_user_id) if db is not None else None
        return presentation.display_name(user) if user is not None else "a teammate"

    @property
    def active(self) -> bool:
        if self.endpoint.kind == "person":
            return self.endpoint.user.is_active
        return self.endpoint.agent.revoked_at is None


@dataclass
class ConversationRow:
    conversation: Conversation
    team: Team
    me: Side
    peer: Side
    last: Message | None = None
    last_sender: str = ""
    count: int = 0
    unacked: int = 0
    archived: bool = False  # §17.2: the effective state (§17.3 A1)
    archived_by: str = ""  # "Archived by <endpoint>" (§17.3 A8)
    name: str = ""  # §21 C5.1: the other person, or "From your agents" / "Your agents" (presentation)
    last_sender_name: str = ""  # §21 C5.1: "Jon" or "Jon · agent"
    peer_user: User | None = None  # the other person (None for your own agents' conversations)
    kind: str = "pair"
    last_state: str = ""  # the last message's state as the viewer may see it (§21 C13 H4)


@dataclass
class MessageLine:
    message: Message
    sender: Side
    recipient: Side
    outgoing: bool
    hold_reason: str | None = None
    sender_name: str = ""  # §19.4, §19.7: the meta line's name
    recipient_name: str = ""
    trail: object = None  # thread.Trail, only for the viewer's own messages (§19.7)
    initials: str = ""  # §20.4 R8: from the person behind the sender (present.initials)
    state: str = ""  # §21 C13 H4: the delivery state this viewer may see
    hold_text: str = ""  # §21 C5.5: "held: …" in presentation's words, for the website

    @property
    def shown_state(self) -> str:
        state = self.state or self.message.delivery_state
        return "held" if state == "stored" and self.hold_reason else state


@dataclass
class ConversationView:
    conversation: Conversation
    team: Team
    me: Side
    peer: Side
    lines: list[MessageLine] = field(default_factory=list)
    events: dict[uuid.UUID, list[DeliveryEvent]] = field(default_factory=dict)
    attachments: dict[uuid.UUID, list] = field(default_factory=dict)
    truncated: bool = False
    archived: bool = False
    archived_by: str = ""
    newest_seq: int = 0  # the archive form sends it as archived_through_seq (§17.3 A1)
    name: str = ""  # §21 C5.1: the other person, or "From your agents" / "Your agents"
    address: str = ""  # §21 C7.5: the header's second line is the team only
    presence: object = None  # thread.HeaderPresence: the person's presence · speed (§21 C5.4), else None
    warning: object = None  # thread.Warning about the person's inbox (§21 C7.4), else None
    peer_user: User | None = None
    kind: str = "pair"

    @property
    def messages(self) -> list[Message]:
        return [line.message for line in self.lines]

    @property
    def can_send(self) -> bool:
        """A pair conversation with an active teammate. Your own agents' conversations take replies only (§21 C13
        H2), from a message's Reply link."""
        if self.kind != "pair":
            return False
        return self.peer_user is not None and self.peer_user.is_active


def _owned_ids(db: Session, user: User, team_ids: list[uuid.UUID]) -> dict[uuid.UUID, Agent]:
    if not team_ids:
        return {}
    agents = db.scalars(select(Agent).where(Agent.owner_user_id == user.id, Agent.team_id.in_(team_ids)))
    return {a.id: a for a in agents}


def _side(db: Session, conv: Conversation, which: str, user: User, owned: dict) -> Side:
    ep = messaging.conversation_endpoint(db, conv, which)
    mine = (ep.kind == "person" and ep.user.id == user.id) or (ep.kind != "person" and ep.agent.id in owned)
    return Side(ep, mine)


def _sides(db: Session, conv: Conversation, user: User, owned: dict) -> tuple[Side, Side]:
    """(me, peer): the viewer's own person endpoint first, else an owned machine side."""
    a, b = _side(db, conv, "a", user, owned), _side(db, conv, "b", user, owned)
    for me, peer in ((a, b), (b, a)):
        if me.mine and me.endpoint.kind == "person":
            return me, peer
    return (a, b) if a.mine else (b, a)


def viewer_state(db: Session, user: User, m: Message) -> str:
    """A message's delivery state as ``user`` may see it (§21 C13 H4), through ``messaging.view_state``; one call per
    conversation row (the list's last message), never per message."""
    state, hold, _ = messaging.view_state(db, user, m)
    return "held" if state == "stored" and hold else state


def _visible(user: User, owned: dict):
    """§21: the viewer's person conversations. A row merged into another (``merged_into``) is never listed or opened
    by its own id; its old URL redirects (``final_conversation_id``)."""
    ids = list(owned)
    clauses = [Conversation.a_user_id == user.id, Conversation.b_user_id == user.id]
    if ids:
        clauses += [Conversation.agent_a_id.in_(ids), Conversation.agent_b_id.in_(ids)]
    return and_(or_(*clauses), Conversation.merged_into.is_(None))


def final_conversation_id(db: Session, conversation_id: object) -> uuid.UUID | None:
    """§21 C1.6: an old conversation id resolved through ``merged_into`` to its end (``messaging``'s resolver)."""
    cid = parse_uuid(conversation_id) if not isinstance(conversation_id, uuid.UUID) else conversation_id
    return messaging.final_conversation_id(db, cid) if cid is not None else None


def _mine_as_recipient(user: User, owned: dict):
    clauses = [Message.recipient_user_id == user.id]
    if owned:
        clauses.append(Message.recipient_agent_id.in_(list(owned)))
    return or_(*clauses)


def _archived_by(db: Session, conv: Conversation, user: User, owned: dict) -> str:
    """"Archived by you" or by the person (§21 C13 M3: through presentation, never a handle)."""
    endpoint = messaging.endpoint_for_key(db, conv.archived_by_key)
    if endpoint is None:
        return ""
    person = presentation.person_of(db, endpoint)
    if person is not None and person.id == user.id:
        return "you"
    return presentation.display_name(person) if person is not None else "a teammate"


def list_conversations(db: Session, user: User, team_ids: list[uuid.UUID],
                       archived: str = "exclude") -> list[ConversationRow]:
    """The person's conversations and those of machines they own, newest first (§16.6). ``archived``:
    ``exclude`` (the main list), ``only`` (the Archived view) or ``include`` (§17.2)."""
    if not team_ids:
        return []
    owned = _owned_ids(db, user, team_ids)
    convs = db.execute(
        select(Conversation, Team).join(Team, Team.id == Conversation.team_id)
        .where(Conversation.team_id.in_(team_ids), _visible(user, owned))
    ).all()
    if not convs:
        return []
    conv_ids = [c.id for c, _ in convs]
    stats = {  # §18.1: keyed by conversation and team, so only a conversation's own team's messages count
        (cid, tid): (last_seq, count, unacked)
        for cid, tid, last_seq, count, unacked in db.execute(
            select(Message.conversation_id, Message.team_id, func.max(Message.seq), func.count(),
                   func.count().filter(and_(Message.acked_at.is_(None), _mine_as_recipient(user, owned))))
            .where(Message.conversation_id.in_(conv_ids)).group_by(Message.conversation_id, Message.team_id)
        )
    }
    last_seqs = [st[0] for st in stats.values() if st[0] is not None]
    lasts = {m.conversation_id: m for m in db.scalars(select(Message).where(Message.seq.in_(last_seqs)))} \
        if last_seqs else {}
    from raincli_server.web import thread

    names = thread.Names(db)
    rows = []
    for conv, team in convs:
        me, peer = _sides(db, conv, user, owned)
        last_seq, count, unacked = stats.get((conv.id, conv.team_id), (None, 0, 0))
        is_archived = messaging.is_archived(conv, last_seq)
        if (archived == "exclude" and is_archived) or (archived == "only" and not is_archived):
            continue
        last = lasts.get(conv.id)
        sender_name = names.sender(messaging.sender_endpoint(db, last)) if last else ""
        sender = "You" if last is not None and last.sender_user_id == user.id else sender_name
        who = presentation.conversation_peer(db, user, conv)
        rows.append(ConversationRow(conversation=conv, team=team, me=me, peer=peer, last=last,
                                    last_sender=sender, count=count, unacked=unacked, archived=is_archived,
                                    archived_by=_archived_by(db, conv, user, owned) if is_archived else "",
                                    name=who.label, last_sender_name=sender_name, peer_user=who.user,
                                    kind=who.kind, last_state=viewer_state(db, user, last) if last else ""))
    rows.sort(key=lambda r: (r.last.seq if r.last else 0), reverse=True)
    return rows


def conversation_head(db: Session, user: User, team_ids: list[uuid.UUID], conversation_id: object):
    """``(conversation, team, me, peer, owned)`` when the viewer may see it, else None. No messages are read."""
    cid = parse_uuid(conversation_id)
    if cid is None or not team_ids:
        return None
    cid = messaging.final_conversation_id(db, cid)  # migration 0010: a merged id means where it went
    owned = _owned_ids(db, user, team_ids)
    row = db.execute(
        select(Conversation, Team).join(Team, Team.id == Conversation.team_id)
        .where(Conversation.id == cid, Conversation.team_id.in_(team_ids), _visible(user, owned))
    ).first()
    if row is None:
        return None
    conv, team = row
    me, peer = _sides(db, conv, user, owned)
    return conv, team, me, peer, owned


def message_lines(db: Session, user: User, owned: dict, messages: list[Message], names=None
                  ) -> tuple[list[MessageLine], dict[uuid.UUID, list[DeliveryEvent]]]:
    """Lines for ``messages`` (oldest first), with sender names and the viewer's delivery trails, and their
    events in one query."""
    from raincli_server.web import thread

    from raincli_server.web import present

    names = names or thread.Names(db)
    people = present.People(db)
    events: dict[uuid.UUID, list[DeliveryEvent]] = {}
    if messages:
        for ev in db.scalars(
            select(DeliveryEvent).where(DeliveryEvent.message_id.in_([m.id for m in messages]))
            .order_by(DeliveryEvent.id)
        ):
            events.setdefault(ev.message_id, []).append(ev)
    from raincli_server import routing

    derived: dict = {}

    def hold_of(m: Message) -> str | None:
        """messaging.hold_reason without a query per message (§19.1): the derived hold is the same for every
        stored message to one recipient, and a held message's reason is its latest held event, loaded above."""
        if m.delivery_state == "stored" and m.acked_at is None:
            key = (m.recipient_agent_id, m.recipient_agent_name, m.sender_user_id is not None)
            if key not in derived:
                agent = db.get(Agent, m.recipient_agent_id) if m.recipient_agent_id else None
                derived[key] = routing.derived_hold(db, agent, m.recipient_agent_name, m.sender_user_id is not None)
            return derived[key]
        if m.delivery_state == "held":
            held = [ev for ev in events.get(m.id, []) if ev.state == "held"]
            return held[-1].detail if held else None
        return None

    inboxes: dict = {}

    def has_inbox(team_id, user_id) -> bool:
        """Once per (team, person) per request: a miss isn't cached by the identity map (§19.1: no query per
        message)."""
        if (team_id, user_id) not in inboxes:
            inboxes[(team_id, user_id)] = presentation.inbox_of(db, team_id, user_id) is not None
        return inboxes[(team_id, user_id)]

    def person_of(m: Message) -> uuid.UUID | None:
        if m.sender_user_id is not None:
            return m.sender_user_id
        agent = db.get(Agent, m.sender_agent_id) if m.sender_agent_id else None
        return agent.owner_user_id if agent is not None else None

    def view(m: Message, recipient_ep) -> tuple[str, str | None, str | None]:
        """(state, raw hold code, route) as this viewer may see it: ``messaging.view_state``'s rules (§21 C13 H4,
        M3), applied here with per-request caches so a poll costs the same however many messages are in view
        (§19.1). tests/server/test_v06_web.py checks it against ``messaging.view_state`` message by message."""
        route = getattr(m, "route", None)
        if m.recipient_user_id is not None and m.recipient_user_id != user.id:
            if m.delivery_state == "replied":
                return "replied", None, route
            own = person_of(m) == m.recipient_user_id
            if not own and not has_inbox(m.team_id, m.recipient_user_id):
                return "held", "no_inbox", route
            return "received", None, route
        hold = hold_of(m)
        return ("held" if m.delivery_state == "stored" and hold else m.delivery_state), hold, route

    lines = []
    for m in messages:
        sender_ep, recipient_ep = messaging.sender_endpoint(db, m), messaging.recipient_endpoint(db, m)
        sender = Side(sender_ep, (m.sender_user_id == user.id) or (m.sender_agent_id in owned))
        recipient = Side(recipient_ep, (m.recipient_user_id == user.id) or (m.recipient_agent_id in owned))
        # The viewer's own message: one they sent as a person (to anyone, their own machines included), or one a
        # machine of theirs sent to someone else. A machine of theirs writing to them is incoming.
        outgoing = m.sender_user_id == user.id or (sender.mine and not recipient.mine)
        state, hold, route = view(m, recipient_ep)
        person = names.person(recipient_ep)
        lines.append(MessageLine(
            m, sender, recipient, outgoing=outgoing, hold_reason=hold,
            sender_name=names.sender(sender_ep), recipient_name=names.endpoint(recipient_ep),
            trail=thread.trail_for(m, recipient_ep, events.get(m.id, []), state, hold, person, route)
            if outgoing else None,
            initials=present.initials(people.of(m)), state=state,
            hold_text=(presentation.held_line(hold, person, route) or "") if outgoing and hold else ""))
    return lines, events


def get_conversation(db: Session, user: User, team_ids: list[uuid.UUID], conversation_id: object
                     ) -> ConversationView | None:
    from raincli_server.web import thread

    head = conversation_head(db, user, team_ids, conversation_id)
    if head is None:
        return None
    conv, team, me, peer, owned = head
    newest = list(db.scalars(
        select(Message).where(Message.conversation_id == conv.id, Message.team_id == conv.team_id)
        .order_by(Message.seq.desc()).limit(CONVERSATION_PAGE + 1).execution_options(populate_existing=True)
    ))
    truncated = len(newest) > CONVERSATION_PAGE
    messages = list(reversed(newest[:CONVERSATION_PAGE]))
    names = thread.Names(db)
    lines, events = message_lines(db, user, owned, messages, names)
    newest_seq = newest[0].seq if newest else 0
    is_archived = messaging.is_archived(conv, newest_seq)
    who = presentation.conversation_peer(db, user, conv)
    return ConversationView(conversation=conv, team=team, me=me, peer=peer, lines=lines, events=events,
                            attachments=attachment_meta(db, [m.id for m in messages]), truncated=truncated,
                            archived=is_archived, newest_seq=newest_seq,
                            archived_by=_archived_by(db, conv, user, owned) if is_archived else "",
                            name=who.label, address=team.name, peer_user=who.user, kind=who.kind,
                            presence=thread.header_presence(db, team.id, who.user),
                            warning=thread.person_warning(db, team.id, who.user))


def ack_viewed(db: Session, user: User, view: ConversationView) -> int:
    """Viewing a thread acks the messages addressed to the person (§16.4)."""
    acked = 0
    for line in view.lines:
        m = line.message
        if m.recipient_user_id == user.id and m.acked_at is None:
            messaging.person_ack(db, user, m.id)
            acked += 1
    return acked


@dataclass(frozen=True)
class AttachmentMeta:
    id: uuid.UUID
    message_id: uuid.UUID
    filename: str
    size: int
    sha256: str


def attachment_meta(db: Session, message_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[AttachmentMeta]]:
    """Attachment metadata per message, in send order. Never loads the content."""
    out: dict[uuid.UUID, list[AttachmentMeta]] = {}
    if not message_ids:
        return out
    rows = db.execute(
        select(Attachment.id, Attachment.message_id, Attachment.filename, Attachment.size, Attachment.sha256)
        .where(Attachment.message_id.in_(message_ids)).order_by(Attachment.message_id, Attachment.position)
    )
    for row in rows:
        out.setdefault(row.message_id, []).append(AttachmentMeta(*row))
    return out


def attachment_for_user(db: Session, user: User, team_ids: list[uuid.UUID], message_id: object,
                        attachment_id: object) -> Attachment | None:
    """The attachment if the person may see its message (§16.6)."""
    mid = parse_uuid(message_id)
    if mid is None or not team_ids:
        return None
    try:
        return messaging.get_attachment_for_person(db, user, mid, attachment_id)
    except messaging.MessagingError:
        return None


def pending_for_user(db: Session, user: User, team_ids: list[uuid.UUID]) -> int:
    """Unacknowledged messages to the person or to machines they own."""
    if not team_ids:
        return 0
    owned = _owned_ids(db, user, team_ids)
    return db.scalar(
        select(func.count()).select_from(Message)
        .where(Message.team_id.in_(team_ids), _mine_as_recipient(user, owned), Message.acked_at.is_(None))
    ) or 0


# People (§21 C5.3, C7.1, C7.2) ---------------------------------------------------------------

@dataclass
class PersonRow:
    """A teammate as the viewer sees them: name, email, team, and presence · speed from their inbox. Never a
    machine or an agent."""

    user: User
    team: Team
    presence: object  # presentation.PersonPresence
    last_seq: int = 0  # the newest message between the viewer and them in that team (recent first)
    shown: str = ""  # §18.1 item 7: the name told apart from look-alikes in the team (owners.displays)

    @property
    def name(self) -> str:
        return self.shown or presentation.display_name(self.user)

    @property
    def id(self) -> str:
        """§21 C7.2: the picker's opaque id, ``p.<team>.<user>``."""
        return f"p.{self.team.id}.{self.user.id}"


def people_directory(db: Session, user: User, team_ids: list[uuid.UUID], now: datetime | None = None
                     ) -> list[PersonRow]:
    """The viewer's teammates across their teams (active members, the viewer excluded), one row per (team,
    person), alphabetical, each with their recency with the viewer."""
    if not team_ids:
        return []
    rows = db.execute(
        select(Team, User).join(Membership, Membership.team_id == Team.id).join(User, User.id == Membership.user_id)
        .where(Team.id.in_(team_ids), User.is_active.is_(True), User.id != user.id)
        .order_by(func.lower(User.display_name), User.email, Team.name)
    ).all()
    recent: dict = {}
    for team_id, a, b, last in db.execute(
        select(Conversation.team_id, Conversation.a_user_id, Conversation.b_user_id, func.max(Message.seq))
        .join(Message, and_(Message.conversation_id == Conversation.id, Message.team_id == Conversation.team_id))
        .where(Conversation.team_id.in_(team_ids), Conversation.kind == "pair",
               or_(Conversation.a_user_id == user.id, Conversation.b_user_id == user.id))
        .group_by(Conversation.team_id, Conversation.a_user_id, Conversation.b_user_id)
    ):
        other = b if a == user.id else a
        recent[(team_id, other)] = max(last or 0, recent.get((team_id, other), 0))
    from raincli_server import owners

    # §18.1 item 7 for people: the person is the address now, so names that read alike in a team (a Cyrillic "Воb"
    # beside "Bob") are told apart on People and in the picker.
    shown = {t.id: owners.displays(db, t.id) for t in {t for t, _ in rows}}
    return [PersonRow(u, t, presentation.person_presence(db, t.id, u, now), recent.get((t.id, u.id), 0),
                      shown[t.id].get(u.id, ""))
            for t, u in rows]


def person_by_id(db: Session, user: User, team_ids: list[uuid.UUID], value: str) -> tuple[Team, User] | None:
    """A picker id ``p.<team>.<user>`` re-resolved and checked again (§20.6 L1): a current team of the viewer and an
    active member, never the viewer. Never trusted as given."""
    parts = (value or "").split(".")
    if len(parts) != 3 or parts[0] != "p":
        return None
    tid, uid = parse_uuid(parts[1]), parse_uuid(parts[2])
    if tid is None or uid is None or tid not in team_ids or uid == user.id:
        return None
    row = db.execute(
        select(Team, User).join(Membership, Membership.team_id == Team.id).join(User, User.id == Membership.user_id)
        .where(Team.id == tid, User.id == uid, User.is_active.is_(True))
    ).first()
    return tuple(row) if row else None


# Sending --------------------------------------------------------------------

class SendError(Exception):
    """A send the protocol rejects. ``code`` follows §3; ``message`` is safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_FRIENDLY = {
    "not_found": "The message you are replying to is not visible to you.",
    "forbidden": "You can't send that message.",
    "id_conflict": "That message id was already used for a different message. Reload and try again.",
    "inbox_full": "The recipient's inbox is full: too many messages are waiting to be acknowledged.",
    "rate_limited": "You're sending too fast. Wait a moment and try again.",
}


def send_as_person(
    db: Session, user: User, team: Team, *, id: uuid.UUID, to: object, body: str,  # noqa: A002
    conversation_id: uuid.UUID | None, in_reply_to: uuid.UUID | None, max_pending: int,
    attachments: list[tuple[str, bytes]] | None = None, quotas=None,
) -> tuple[Message, bool]:
    """Send as the person (§16.9) through ``messaging.send_as_person``. Raises SendError."""
    try:
        return messaging.send_as_person(
            db, user, team.id, id=id, to=to, body=body, conversation_id=conversation_id,
            in_reply_to=in_reply_to, max_pending=max_pending, attachments=attachments or None, quotas=quotas,
        )
    except messaging.MessagingError as exc:
        if exc.code in ("address_people", "ambiguous_person"):
            raise SendError(exc.code, exc.message) from exc  # presentation's own words (§21 C2)
        # "invalid" messages from the service name the problem (and attachment) without leaking data.
        text = _FRIENDLY.get(exc.code) or (exc.message[:1].upper() + exc.message[1:300] + ".")
        raise SendError(exc.code, text) from exc
