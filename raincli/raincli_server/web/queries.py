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
class ConversationRow:
    conversation: Conversation
    team: Team
    mine: list[Agent]
    peer: Agent
    last: Message | None = None
    last_sender: str = ""
    count: int = 0
    unacked: int = 0

    @property
    def me(self) -> Agent:
        return self.mine[0]


@dataclass
class ConversationView:
    conversation: Conversation
    team: Team
    mine: list[Agent]
    peer: Agent
    agents: dict[uuid.UUID, Agent] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)
    events: dict[uuid.UUID, list[DeliveryEvent]] = field(default_factory=dict)
    attachments: dict[uuid.UUID, list] = field(default_factory=dict)
    truncated: bool = False


def _owned_ids(db: Session, user: User, team_ids: list[uuid.UUID]) -> dict[uuid.UUID, Agent]:
    if not team_ids:
        return {}
    agents = db.scalars(select(Agent).where(Agent.owner_user_id == user.id, Agent.team_id.in_(team_ids)))
    return {a.id: a for a in agents}


def list_conversations(
    db: Session, user: User, team_ids: list[uuid.UUID], only_agent: uuid.UUID | None = None,
) -> list[ConversationRow]:
    owned = _owned_ids(db, user, team_ids)
    ids = [only_agent] if only_agent in owned else list(owned)
    if not ids:
        return []
    convs = db.execute(
        select(Conversation, Team).join(Team, Team.id == Conversation.team_id)
        .where(Conversation.team_id.in_(team_ids), or_(Conversation.agent_a_id.in_(ids), Conversation.agent_b_id.in_(ids)))
    ).all()
    if not convs:
        return []
    conv_ids = [c.id for c, _ in convs]
    stats = {
        cid: (last_seq, count, unacked)
        for cid, last_seq, count, unacked in db.execute(
            select(
                Message.conversation_id, func.max(Message.seq), func.count(),
                func.count().filter(and_(Message.acked_at.is_(None), Message.recipient_agent_id.in_(list(owned)))),
            ).where(Message.conversation_id.in_(conv_ids)).group_by(Message.conversation_id)
        )
    }
    last_seqs = [s[0] for s in stats.values() if s[0] is not None]
    lasts = {m.conversation_id: m for m in db.scalars(select(Message).where(Message.seq.in_(last_seqs)))} if last_seqs else {}
    agent_ids = {c.agent_a_id for c, _ in convs} | {c.agent_b_id for c, _ in convs}
    agents = {a.id: a for a in db.scalars(select(Agent).where(Agent.id.in_(agent_ids)))}
    rows = []
    for conv, team in convs:
        a, b = agents[conv.agent_a_id], agents[conv.agent_b_id]
        mine = [x for x in (a, b) if x.id in owned]
        peer = b if a.id in owned else a
        if len(mine) == 2 and only_agent == b.id:
            mine, peer = [b, a], a
        last_seq, count, unacked = stats.get(conv.id, (None, 0, 0))
        last = lasts.get(conv.id)
        rows.append(ConversationRow(
            conversation=conv, team=team, mine=mine, peer=peer, last=last,
            last_sender=agents[last.sender_agent_id].handle if last else "", count=count, unacked=unacked,
        ))
    rows.sort(key=lambda r: (r.last.seq if r.last else 0), reverse=True)
    return rows


def get_conversation(
    db: Session, user: User, team_ids: list[uuid.UUID], conversation_id: object,
) -> ConversationView | None:
    cid = parse_uuid(conversation_id)
    if cid is None or not team_ids:
        return None
    row = db.execute(
        select(Conversation, Team).join(Team, Team.id == Conversation.team_id)
        .where(Conversation.id == cid, Conversation.team_id.in_(team_ids))
    ).first()
    if row is None:
        return None
    conv, team = row
    a, b = db.get(Agent, conv.agent_a_id), db.get(Agent, conv.agent_b_id)
    mine = [x for x in (a, b) if x.owner_user_id == user.id]
    if not mine:
        return None
    peer = b if a.owner_user_id == user.id else a
    newest = list(db.scalars(
        select(Message).where(Message.conversation_id == conv.id)
        .order_by(Message.seq.desc()).limit(CONVERSATION_PAGE + 1)
    ))
    truncated = len(newest) > CONVERSATION_PAGE
    messages = list(reversed(newest[:CONVERSATION_PAGE]))
    events: dict[uuid.UUID, list[DeliveryEvent]] = {}
    if messages:
        for ev in db.scalars(
            select(DeliveryEvent).where(DeliveryEvent.message_id.in_([m.id for m in messages]))
            .order_by(DeliveryEvent.id)
        ):
            events.setdefault(ev.message_id, []).append(ev)
    return ConversationView(
        conversation=conv, team=team, mine=mine, peer=peer, agents={a.id: a, b.id: b},
        messages=messages, events=events, attachments=attachment_meta(db, [m.id for m in messages]),
        truncated=truncated,
    )


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


def attachment_for_user(
    db: Session, user: User, team_ids: list[uuid.UUID], message_id: object, attachment_id: object,
) -> Attachment | None:
    """The attachment if it belongs to that message and the viewer owns one of its participants."""
    mid = parse_uuid(message_id)
    if mid is None or not team_ids:
        return None
    msg = db.scalar(select(Message).where(Message.id == mid, Message.team_id.in_(team_ids)))
    if msg is None:
        return None
    agent = db.scalar(select(Agent).where(
        Agent.id.in_([msg.sender_agent_id, msg.recipient_agent_id]), Agent.owner_user_id == user.id,
    ).limit(1))
    if agent is None:
        return None
    try:
        return messaging.get_attachment_for_agent(db, agent, mid, attachment_id)
    except messaging.MessagingError:
        return None


def pending_for_user(db: Session, user: User, team_ids: list[uuid.UUID]) -> int:
    owned = list(_owned_ids(db, user, team_ids))
    if not owned:
        return 0
    return db.scalar(
        select(func.count()).select_from(Message)
        .where(Message.recipient_agent_id.in_(owned), Message.acked_at.is_(None))
    ) or 0


# Sending --------------------------------------------------------------------

class SendError(Exception):
    """A send the protocol rejects. ``code`` follows §3; ``message`` is safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_FRIENDLY = {
    "not_found": "The message you are replying to is not visible to this agent.",
    "forbidden": "This agent is not allowed to send that message.",
    "id_conflict": "That message id was already used for a different message. Reload and try again.",
    "inbox_full": "The recipient's inbox is full: too many messages are waiting to be acknowledged.",
}


def send_as(
    db: Session, sender: Agent, *, id: uuid.UUID, to_handle: str, body: str,
    conversation_id: uuid.UUID | None, in_reply_to: uuid.UUID | None, max_pending: int,
    attachments: list[tuple[str, bytes]] | None = None,
) -> tuple[Message, bool]:
    """Send as ``sender`` (already checked to belong to the viewer) through ``messaging.send_message``.

    ``attachments`` is a list of (filename, exact bytes) as in protocol §8. Raises SendError.
    """
    try:
        return messaging.send_message(
            db, sender, id=id, to_handle=to_handle, body=body, conversation_id=conversation_id,
            in_reply_to=in_reply_to, max_pending=max_pending, attachments=attachments or None,
        )
    except messaging.MessagingError as exc:
        # "invalid" messages from the service name the problem (and attachment) without leaking data.
        text = _FRIENDLY.get(exc.code) or (exc.message[:1].upper() + exc.message[1:300] + ".")
        raise SendError(exc.code, text) from exc
