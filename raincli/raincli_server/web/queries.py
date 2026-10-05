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

from raincli_server import identity, messaging, security
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
    return out


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
        if self.endpoint.kind == "person":
            return "You" if self.mine else self.endpoint.user.display_name
        return self.endpoint.label()

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


@dataclass
class MessageLine:
    message: Message
    sender: Side
    recipient: Side
    outgoing: bool
    hold_reason: str | None = None

    @property
    def shown_state(self) -> str:
        return "held" if self.message.delivery_state == "stored" and self.hold_reason else self.message.delivery_state


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

    @property
    def messages(self) -> list[Message]:
        return [line.message for line in self.lines]

    @property
    def can_send(self) -> bool:
        return self.peer.active and not (self.peer.endpoint.kind == "person" and self.peer.mine)


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


def _visible(user: User, owned: dict):
    ids = list(owned)
    clauses = [Conversation.a_user_id == user.id, Conversation.b_user_id == user.id]
    if ids:
        clauses += [Conversation.agent_a_id.in_(ids), Conversation.agent_b_id.in_(ids)]
    return or_(*clauses)


def _mine_as_recipient(user: User, owned: dict):
    clauses = [Message.recipient_user_id == user.id]
    if owned:
        clauses.append(Message.recipient_agent_id.in_(list(owned)))
    return or_(*clauses)


def list_conversations(db: Session, user: User, team_ids: list[uuid.UUID]) -> list[ConversationRow]:
    """The person's conversations and those of machines they own, newest first (§16.6)."""
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
    stats = {
        cid: (last_seq, count, unacked)
        for cid, last_seq, count, unacked in db.execute(
            select(Message.conversation_id, func.max(Message.seq), func.count(),
                   func.count().filter(and_(Message.acked_at.is_(None), _mine_as_recipient(user, owned))))
            .where(Message.conversation_id.in_(conv_ids)).group_by(Message.conversation_id)
        )
    }
    last_seqs = [st[0] for st in stats.values() if st[0] is not None]
    lasts = {m.conversation_id: m for m in db.scalars(select(Message).where(Message.seq.in_(last_seqs)))} \
        if last_seqs else {}
    rows = []
    for conv, team in convs:
        me, peer = _sides(db, conv, user, owned)
        last_seq, count, unacked = stats.get(conv.id, (None, 0, 0))
        last = lasts.get(conv.id)
        sender = Side(messaging.sender_endpoint(db, last), False).display if last else ""
        if last is not None and last.sender_user_id == user.id:
            sender = "You"
        rows.append(ConversationRow(conversation=conv, team=team, me=me, peer=peer, last=last,
                                    last_sender=sender, count=count, unacked=unacked))
    rows.sort(key=lambda r: (r.last.seq if r.last else 0), reverse=True)
    return rows


def get_conversation(db: Session, user: User, team_ids: list[uuid.UUID], conversation_id: object
                     ) -> ConversationView | None:
    cid = parse_uuid(conversation_id)
    if cid is None or not team_ids:
        return None
    owned = _owned_ids(db, user, team_ids)
    row = db.execute(
        select(Conversation, Team).join(Team, Team.id == Conversation.team_id)
        .where(Conversation.id == cid, Conversation.team_id.in_(team_ids), _visible(user, owned))
    ).first()
    if row is None:
        return None
    conv, team = row
    me, peer = _sides(db, conv, user, owned)
    newest = list(db.scalars(
        select(Message).where(Message.conversation_id == conv.id)
        .order_by(Message.seq.desc()).limit(CONVERSATION_PAGE + 1).execution_options(populate_existing=True)
    ))
    truncated = len(newest) > CONVERSATION_PAGE
    messages = list(reversed(newest[:CONVERSATION_PAGE]))
    lines = []
    for m in messages:
        sender = Side(messaging.sender_endpoint(db, m), False)
        sender.mine = (m.sender_user_id == user.id) or (m.sender_agent_id in owned)
        recipient = Side(messaging.recipient_endpoint(db, m), False)
        recipient.mine = (m.recipient_user_id == user.id) or (m.recipient_agent_id in owned)
        lines.append(MessageLine(m, sender, recipient, outgoing=sender.mine and not recipient.mine,
                                 hold_reason=messaging.hold_reason(db, m)))
    events: dict[uuid.UUID, list[DeliveryEvent]] = {}
    if messages:
        for ev in db.scalars(
            select(DeliveryEvent).where(DeliveryEvent.message_id.in_([m.id for m in messages]))
            .order_by(DeliveryEvent.id)
        ):
            events.setdefault(ev.message_id, []).append(ev)
    return ConversationView(conversation=conv, team=team, me=me, peer=peer, lines=lines, events=events,
                            attachments=attachment_meta(db, [m.id for m in messages]), truncated=truncated)


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


# The directory and the recipient picker (§16.2, §16.9) ------------------------------------

@dataclass
class PickerAgent:
    name: str
    type: str
    status: str
    role: str | None
    reachability: str | None
    ambiguous: bool
    source: str

    @property
    def deliverable(self) -> bool:
        return self.reachability in ("instant", "next-turn") and not self.ambiguous

    @property
    def reason(self) -> str:
        return "ambiguous" if self.ambiguous else "listed only"


@dataclass
class PickerMachine:
    agent: Agent
    team: Team
    owner: User
    agents: list[PickerAgent]


def team_directory(db: Session, user: User, team_ids: list[uuid.UUID]
                   ) -> tuple[list[PickerMachine], list[tuple[Team, User]]]:
    """Active machines with their live agents, and the people of the viewer's teams."""
    from raincli_server.presence import live_machine_agents

    if not team_ids:
        return [], []
    rows = db.execute(
        select(Agent, Team, User).join(Team, Team.id == Agent.team_id).join(User, User.id == Agent.owner_user_id)
        .where(Agent.team_id.in_(team_ids), Agent.revoked_at.is_(None)).order_by(Team.name, Agent.handle)
    ).all()
    live = live_machine_agents(db, [a.id for a, _, _ in rows])
    machines = [PickerMachine(agent, team, owner, [
        PickerAgent(m.name, m.type, m.status, m.role, m.reachability, m.ambiguous, m.source)
        for m in live.get(agent.id, [])]) for agent, team, owner in rows]
    people = list(db.execute(
        select(Team, User).join(Membership, Membership.team_id == Team.id).join(User, User.id == Membership.user_id)
        .where(Team.id.in_(team_ids), User.is_active.is_(True), User.id != user.id)
        .order_by(Team.name, User.display_name)
    ).all())
    return machines, people


def endpoint_from_text(text: str) -> object:
    """The §16.1 CLI form to the API form: ``handle``, ``handle/agent`` or ``@email``."""
    text = text.strip()
    if text.startswith("@"):
        return {"person": text[1:]}
    if "/" in text:
        handle, name = text.split("/", 1)
        return {"machine": handle, "agent": name}
    return {"machine": text}


def endpoint_text(endpoint: messaging.Endpoint) -> str:
    return endpoint.label()


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
    "routing_inbox_only": "That machine accepts messages only at its inbox. Send to the machine instead.",
    "unknown_agent": "That agent isn't known on that machine. Check the name, or send to the machine.",
    "rate_limited": "You're sending too fast. Wait a moment and try again.",
}


def send_as_person(
    db: Session, user: User, team: Team, *, id: uuid.UUID, to: object, body: str,  # noqa: A002
    conversation_id: uuid.UUID | None, in_reply_to: uuid.UUID | None, max_pending: int,
    attachments: list[tuple[str, bytes]] | None = None,
) -> tuple[Message, bool]:
    """Send as the person (§16.9) through ``messaging.send_as_person``. Raises SendError."""
    try:
        return messaging.send_as_person(
            db, user, team.id, id=id, to=to, body=body, conversation_id=conversation_id,
            in_reply_to=in_reply_to, max_pending=max_pending, attachments=attachments or None,
        )
    except messaging.MessagingError as exc:
        if exc.code == "not_deliverable":
            reason = (exc.extra or {}).get("reason", "listed_only")
            text = ("That agent can't receive messages: its name is used by more than one agent on that machine."
                    if reason == "ambiguous" else
                    "That agent can't receive messages: it is listed but has no name RainCLI can deliver to.")
            raise SendError(exc.code, text) from exc
        # "invalid" messages from the service name the problem (and attachment) without leaking data.
        text = _FRIENDLY.get(exc.code) or (exc.message[:1].upper() + exc.message[1:300] + ".")
        raise SendError(exc.code, text) from exc
