"""Messaging service layer (protocol §1-§3), shared by the agent API and the web UI.

Every function takes an open SQLAlchemy Session and does not commit; callers own
the transaction. Failures raise :class:`MessagingError`, which carries the HTTP
status and protocol error code so the API can render it directly.

Concurrency is enforced by PostgreSQL, not by the process:

* ``send_message`` takes ``SELECT ... FOR NO KEY UPDATE`` on the recipient agent
  row before anything else. All sends to one recipient are therefore serialized,
  which makes the ``inbox_full`` capacity check exact and means a recipient's
  ``seq`` values commit in increasing order, so an inbox cursor never skips a
  message that commits late. ``NO KEY UPDATE`` does not conflict with the
  ``KEY SHARE`` locks that foreign keys take, so A->B and B->A sends cannot
  deadlock.
* The message insert is ``INSERT ... ON CONFLICT (id) DO NOTHING``; a lost race
  re-reads the winner and answers ``created: false`` or ``id_conflict``.
* The default conversation for a pair is an upsert against the partial unique
  index ``uq_conversations_default_pair``.
* Ack is a conditional ``UPDATE ... WHERE acked_at IS NULL``.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, object_session

from raincli_server import routing, security
from raincli_server.models import (EVENT_STATES, MESSAGE_KINDS, Agent, Attachment, Conversation, DeliveryEvent,
                                   Membership, Message, User)
from raincli_server.routing import Endpoint

EVENT_DETAIL_MAX = 500
INBOX_LIMIT_MAX = 500
CONVERSATIONS_LIMIT_MAX = 200
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


from raincli_server.messaging_errors import MessagingError  # noqa: E402  (re-exported)


def _invalid(message: str) -> MessagingError:
    return MessagingError(400, "invalid", message)


def _not_found() -> MessagingError:
    return MessagingError(404, "not_found", "message not found")


# Serialization ---------------------------------------------------------------

def iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _handle(session: Session, agent_id: uuid.UUID) -> str:
    agent = session.get(Agent, agent_id)
    return agent.handle if agent is not None else ""


def sender_endpoint(session: Session, msg: Message) -> Endpoint:
    if msg.sender_user_id is not None:
        return Endpoint("person", user=session.get(User, msg.sender_user_id))
    agent = session.get(Agent, msg.sender_agent_id)
    # from_agent is a hint for replies (§16.1); the sender is the machine.
    return Endpoint("agent", agent=agent, name=msg.sender_agent_name) if msg.sender_agent_name else \
        Endpoint("machine", agent=agent)


def recipient_endpoint(session: Session, msg: Message) -> Endpoint:
    if msg.recipient_user_id is not None:
        return Endpoint("person", user=session.get(User, msg.recipient_user_id))
    agent = session.get(Agent, msg.recipient_agent_id)
    return Endpoint("agent", agent=agent, name=msg.recipient_agent_name) if msg.recipient_agent_name else \
        Endpoint("machine", agent=agent)


_ATTACHMENT_META = (Attachment.id, Attachment.message_id, Attachment.filename, Attachment.media_type,
                    Attachment.size, Attachment.sha256)


def _attachment_meta(session: Session, message_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict]]:
    """Attachment metadata (never content) for several messages in one query, in send order."""
    out: dict[uuid.UUID, list[dict]] = {mid: [] for mid in message_ids}
    if not message_ids:
        return out
    rows = session.execute(
        select(*_ATTACHMENT_META).where(Attachment.message_id.in_(message_ids))
        .order_by(Attachment.message_id, Attachment.position)
    )
    for aid, mid, filename, media_type, size, sha in rows:
        out[mid].append({"id": str(aid), "filename": filename, "media_type": media_type,
                         "size": size, "sha256": sha})
    return out


def attachments_json(message: Message) -> list[dict]:
    """``[{id, filename, media_type, size, sha256}]`` for ``message`` in send order. No content."""
    session = object_session(message)
    if session is None:
        raise ValueError("message is not attached to a session")
    return _attachment_meta(session, [message.id])[message.id]


def hold_reason(session: Session, msg: Message) -> str | None:
    """The hold a sender sees: derived for an unacked ``stored`` message (§16.12 C8), otherwise the
    latest ``held`` event's detail when the message is held."""
    if msg.delivery_state == "stored" and msg.acked_at is None:
        recipient = session.get(Agent, msg.recipient_agent_id) if msg.recipient_agent_id else None
        return routing.derived_hold(session, recipient, msg.recipient_agent_name, msg.sender_user_id is not None)
    if msg.delivery_state == "held":
        return session.scalar(
            select(DeliveryEvent.detail).where(DeliveryEvent.message_id == msg.id, DeliveryEvent.state == "held")
            .order_by(DeliveryEvent.id.desc()).limit(1))
    return None


def message_json(session: Session, msg: Message, attachments: list[dict] | None = None) -> dict:
    """Protocol message JSON (M), including attachment metadata and the §16 endpoints.

    ``from``/``to`` keep their v0.4 meaning for machine endpoints (the handle); a person is ``@email``.
    """
    if attachments is None:
        attachments = _attachment_meta(session, [msg.id])[msg.id]
    sender, recipient = sender_endpoint(session, msg), recipient_endpoint(session, msg)
    hold = hold_reason(session, msg)
    state = "held" if msg.delivery_state == "stored" and hold else msg.delivery_state
    return {
        "id": str(msg.id),
        "conversation_id": str(msg.conversation_id),
        "in_reply_to": str(msg.in_reply_to) if msg.in_reply_to else None,
        "from": sender.agent.handle if sender.agent else "@" + sender.user.email,
        "to": recipient.agent.handle if recipient.agent else "@" + recipient.user.email,
        "from_endpoint": sender.json(),
        "to_endpoint": recipient.json(),
        "from_agent": msg.sender_agent_name,
        "kind": msg.kind,
        "from_same_owner": same_owner(sender, recipient),
        "body": msg.body,
        "created_at": iso(msg.created_at),
        "seq": msg.seq,
        "acked_at": iso(msg.acked_at),
        "delivery_state": state,
        "hold_reason": hold,
        "delivery_updated_at": iso(msg.delivery_updated_at),
        "attachments": attachments,
    }


def same_owner(sender: Endpoint, recipient: Endpoint) -> bool:
    """§16.16 (2): a message to a machine (or one of its agents) from a machine, or a person,
    with that machine's owner. The client's default trust set trusts it."""
    if recipient.agent is None:
        return False
    owner = recipient.agent.owner_user_id
    if sender.kind == "person":
        return sender.user is not None and sender.user.id == owner
    return sender.agent is not None and sender.agent.owner_user_id == owner


def messages_json(session: Session, messages: list[Message]) -> list[dict]:
    """``message_json`` for a page of messages with one attachment query."""
    meta = _attachment_meta(session, [m.id for m in messages])
    return [message_json(session, m, meta[m.id]) for m in messages]


# Lookups and visibility (§16.6) --------------------------------------------------

def _load_message(session: Session, message_id: uuid.UUID, *, lock: bool = False) -> Message | None:
    stmt = select(Message).where(Message.id == message_id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    return session.scalar(stmt)


def _is_participant(msg: Message, agent: Agent) -> bool:
    return agent.id in (msg.sender_agent_id, msg.recipient_agent_id)


def owned_machine_ids(session: Session, user: User, team_id: uuid.UUID | None = None) -> set[uuid.UUID]:
    stmt = select(Agent.id).where(Agent.owner_user_id == user.id)
    if team_id is not None:
        stmt = stmt.where(Agent.team_id == team_id)
    return set(session.scalars(stmt))


def person_can_see(session: Session, msg: Message, user: User) -> bool:
    """A person sees messages where they are an endpoint, plus those to or from machines they own,
    while they are still a member of the message's team (§16.6)."""
    if session.get(Membership, (msg.team_id, user.id)) is None:
        return False
    if user.id in (msg.sender_user_id, msg.recipient_user_id):
        return True
    machines = {msg.sender_agent_id, msg.recipient_agent_id} - {None}
    return bool(machines & owned_machine_ids(session, user, msg.team_id))


def get_visible_message(session: Session, agent: Agent, message_id: uuid.UUID | str) -> Message:
    """The message if ``agent`` is its sender or recipient machine; otherwise ``404`` (no existence leak)."""
    mid = parse_uuid(message_id, "message id", not_found=True)
    msg = _load_message(session, mid)
    if msg is None or msg.team_id != agent.team_id or not _is_participant(msg, agent):
        raise _not_found()
    return msg


def get_message_for_person(session: Session, user: User, message_id: uuid.UUID | str) -> Message:
    mid = parse_uuid(message_id, "message id", not_found=True)
    msg = _load_message(session, mid)
    if msg is None or not person_can_see(session, msg, user):
        raise _not_found()
    return msg


def parse_uuid(value: object, what: str, *, not_found: bool = False) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        return uuid.UUID(value)
    except ValueError:
        if not_found:
            raise _not_found() from None
        raise _invalid(f"{what} must be a UUID") from None


def _pending_count(session: Session, recipient: Endpoint) -> int:
    where = (Message.recipient_user_id == recipient.user.id) if recipient.kind == "person" else \
        (Message.recipient_agent_id == recipient.agent.id)
    return session.scalar(
        select(func.count()).select_from(Message).where(where, Message.acked_at.is_(None))) or 0


def _endpoint_columns(side: str, ep: Endpoint) -> dict:
    """Conversation columns for side ``a`` or ``b``."""
    agent_col = {"a": "agent_a_id", "b": "agent_b_id"}[side]
    return {agent_col: ep.machine_id, f"{side}_agent_name": ep.name if ep.kind == "agent" else None,
            f"{side}_user_id": ep.user.id if ep.kind == "person" else None, f"{side}_key": ep.key}


def _default_conversation(session: Session, team_id: uuid.UUID, x: Endpoint, y: Endpoint) -> Conversation:
    a, b = sorted((x, y), key=lambda ep: ep.key)
    session.execute(
        pg_insert(Conversation)
        .values(id=uuid.uuid4(), team_id=team_id, is_default=True, **_endpoint_columns("a", a),
                **_endpoint_columns("b", b))
        .on_conflict_do_nothing(index_elements=["a_key", "b_key"], index_where=Conversation.is_default)
    )
    return session.scalar(
        select(Conversation).where(
            Conversation.a_key == a.key, Conversation.b_key == b.key, Conversation.is_default.is_(True))
    )


def _payload(sender: Endpoint, recipient: Endpoint, body: str, in_reply_to, conversation_id, kind: str,
             attachment_keys: list[tuple[str, str]]) -> tuple:
    return (sender.key, recipient.key, body, in_reply_to, conversation_id, kind, attachment_keys)


def _same_payload(session: Session, existing: Message, payload: tuple) -> bool:
    sender_key, recipient_key, body, in_reply_to, conversation_id, kind, attachment_keys = payload
    stored = [(a["filename"], a["sha256"]) for a in _attachment_meta(session, [existing.id])[existing.id]]
    return (
        stored == attachment_keys
        and sender_endpoint(session, existing).key == sender_key
        and recipient_endpoint(session, existing).key == recipient_key
        and existing.body == body
        and existing.kind == kind
        and existing.in_reply_to == in_reply_to
        # An omitted conversation_id means "the server's choice", which the first send made.
        and (conversation_id is None or existing.conversation_id == conversation_id)
    )


def _idempotent_result(session: Session, existing: Message, payload: tuple) -> tuple[Message, bool]:
    if _same_payload(session, existing, payload):
        return existing, False
    raise MessagingError(409, "id_conflict", "a different message already uses this id")


# Attachments -------------------------------------------------------------------

def validate_attachments(attachments: object) -> list[tuple[str, bytes, str]]:
    """Validate ``[(filename, bytes)]`` (protocol §8). Returns ``[(filename, bytes, sha256)]``."""
    if attachments is None:
        return []
    if not isinstance(attachments, (list, tuple)):
        raise _invalid("attachments must be a list")
    if len(attachments) > security.ATTACHMENT_MAX_COUNT:
        raise _invalid(f"at most {security.ATTACHMENT_MAX_COUNT} attachments per message")
    out, seen, total = [], set(), 0
    for i, item in enumerate(attachments):
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            raise _invalid(f"attachment {i + 1} must be (filename, bytes)")
        name, data = item
        label = f"attachment {i + 1}"
        if not security.valid_attachment_name(name):
            raise _invalid(f"{label}: filename must be a plain .md name (letters, digits, space . _ -)")
        label = f"attachment {name!r}"
        if name.lower() in seen:
            raise _invalid(f"{label}: duplicate filename (names are compared ignoring case)")
        seen.add(name.lower())
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise _invalid(f"{label}: content must be bytes")
        data = bytes(data)
        problem = security.attachment_content_problem(data)
        if problem:
            raise _invalid(f"{label}: {problem}")
        total += len(data)
        if total > security.ATTACHMENT_MAX_TOTAL:
            raise _invalid(f"attachments exceed {security.ATTACHMENT_MAX_TOTAL} bytes in total")
        out.append((name, data, security.sha256_hex(data)))
    return out


def get_attachment_for_agent(session: Session, agent: Agent, message_id: uuid.UUID | str,
                             attachment_id: uuid.UUID | str) -> Attachment:
    """The attachment (with content) if ``agent`` participates in its message; otherwise 404."""
    msg = get_visible_message(session, agent, message_id)
    aid = parse_uuid(attachment_id, "attachment id", not_found=True)
    att = session.scalar(select(Attachment).where(Attachment.id == aid, Attachment.message_id == msg.id))
    if att is None:
        raise MessagingError(404, "not_found", "attachment not found")
    return att


def get_attachment_for_person(session: Session, user: User, message_id: uuid.UUID | str,
                              attachment_id: uuid.UUID | str) -> Attachment:
    msg = get_message_for_person(session, user, message_id)
    aid = parse_uuid(attachment_id, "attachment id", not_found=True)
    att = session.scalar(select(Attachment).where(Attachment.id == aid, Attachment.message_id == msg.id))
    if att is None:
        raise MessagingError(404, "not_found", "attachment not found")
    return att


# Send --------------------------------------------------------------------------

def machine_endpoint(agent: Agent, from_agent: str | None = None) -> Endpoint:
    """A machine credential's sender endpoint: the machine, or one of its agents named by ``from_agent``."""
    return Endpoint("agent", agent=agent, name=from_agent) if from_agent else Endpoint("machine", agent=agent)


def send_message(
    session: Session,
    sender_agent: Agent,
    *,
    id: uuid.UUID | str,  # noqa: A002 - protocol field name
    to_handle: str | None = None,
    to: object = None,
    body: str,
    conversation_id: uuid.UUID | str | None = None,
    in_reply_to: uuid.UUID | str | None = None,
    max_pending: int,
    attachments: list[tuple[str, bytes]] | None = None,
    from_agent: str | None = None,
    kind: str = "message",
) -> tuple[Message, bool]:
    """Store a message from a machine credential (§3, §16.1). ``to`` is any §16.1 endpoint form;
    ``to_handle`` is the v0.4 machine-endpoint shorthand. Returns ``(message, created)``."""
    from raincli_server.presence import valid_agent_name

    if from_agent is not None and (not valid_agent_name(from_agent) or from_agent != from_agent.strip()):
        raise _invalid("from_agent must be a directory name: 1-64 characters, no control or format characters")
    if sender_agent.revoked_at is not None:
        raise MessagingError(403, "forbidden", "sender agent is revoked")
    return send(session, machine_endpoint(sender_agent, from_agent), sender_agent.team_id, id=id,
                to=to if to is not None else to_handle, body=body, conversation_id=conversation_id,
                in_reply_to=in_reply_to, max_pending=max_pending, attachments=attachments, kind=kind)


def send_as_person(session: Session, user: User, team_id: uuid.UUID, *, id, to: object, body: str,  # noqa: A002
                   conversation_id=None, in_reply_to=None, max_pending: int,
                   attachments: list[tuple[str, bytes]] | None = None, kind: str = "message") -> tuple[Message, bool]:
    """Store a message from a person (§16.3, §16.4). Only ``kind: message`` (§16.12 C11)."""
    if not user.is_active or session.get(Membership, (team_id, user.id)) is None:
        raise MessagingError(403, "forbidden", "you are not a member of that team")
    if kind != "message":
        raise _invalid("a person sends kind message only")
    return send(session, Endpoint("person", user=user), team_id, id=id, to=to, body=body,
                conversation_id=conversation_id, in_reply_to=in_reply_to, max_pending=max_pending,
                attachments=attachments, kind=kind)


def _sends_as(sender: Endpoint, msg: Message, side: str) -> bool:
    """Whether ``sender`` holds the ``side`` ("sender"/"recipient") of ``msg``: the same machine, or the
    same person."""
    if sender.kind == "person":
        return getattr(msg, f"{side}_user_id") == sender.user.id
    return getattr(msg, f"{side}_agent_id") == sender.agent.id


def send(session: Session, sender: Endpoint, team_id: uuid.UUID, *, id, to: object, body: str,  # noqa: A002
         conversation_id=None, in_reply_to=None, max_pending: int,
         attachments: list[tuple[str, bytes]] | None = None, kind: str = "message") -> tuple[Message, bool]:
    """The one send path for every sender and endpoint kind (§16.1, §16.2, §16.4, §16.12 C7, C10, C11)."""
    message_id = parse_uuid(id, "id")
    reply_to = parse_uuid(in_reply_to, "in_reply_to") if in_reply_to is not None else None
    conv_id = parse_uuid(conversation_id, "conversation_id") if conversation_id is not None else None
    if not security.valid_message_body(body):
        raise _invalid("body must be 1-16000 characters of plain text")
    if kind not in MESSAGE_KINDS:
        raise _invalid(f"kind must be one of: {', '.join(MESSAGE_KINDS)}")
    files = validate_attachments(attachments)
    if to is None:
        raise _invalid(routing.BAD_RECIPIENT)

    # Lock the recipient first: serializes sends to it (capacity and cursor order).
    recipient = routing.resolve(session, team_id, routing.parse_endpoint(to), lock=True)
    if recipient.key == sender.key or (recipient.kind == "machine" and sender.kind != "person"
                                       and recipient.agent.id == sender.agent.id):
        # §16.12 C7: never to the sender's own endpoint, nor a machine to its own inbox.
        raise _invalid(routing.BAD_RECIPIENT)
    if kind == "escalation" and not (sender.kind != "person" and recipient.kind == "person"
                                     and recipient.user.id == sender.agent.owner_user_id):
        raise _invalid("an escalation goes only from a machine to its own owner (§16.8)")
    payload = _payload(sender, recipient, body, reply_to, conv_id, kind, [(n, h) for n, _, h in files])

    # Idempotency is decided before any other state or capacity check.
    existing = _load_message(session, message_id)
    if existing is not None:
        return _idempotent_result(session, existing, payload)

    if recipient.kind == "agent":
        routing.check_agent_route(session, recipient)

    parent: Message | None = None
    conversation: Conversation | None = None
    if reply_to is not None:
        parent = _load_message(session, reply_to)
        visible = parent is not None and parent.team_id == team_id and (
            person_can_see(session, parent, sender.user) if sender.kind == "person"
            else _is_participant(parent, sender.agent))
        if not visible:
            raise MessagingError(404, "not_found", "in_reply_to message not found")
        # The other participant, seen from the replier (§16.4 replies, C10).
        other = recipient_endpoint(session, parent) if _sends_as(sender, parent, "sender") else \
            sender_endpoint(session, parent)
        fallback = other.kind == "agent" and recipient.kind == "machine" and recipient.agent.id == other.agent.id
        if recipient.key != other.key and not fallback:
            raise _invalid("a reply must be addressed to the other participant of the parent message")
        if conv_id is not None and conv_id != parent.conversation_id:
            raise _invalid("conversation_id does not match the parent message")
        candidate = session.get(Conversation, parent.conversation_id)
        if candidate is not None and {candidate.a_key, candidate.b_key} == {sender.key, recipient.key}:
            conversation = candidate
    elif conv_id is not None:
        candidate = session.get(Conversation, conv_id)
        if candidate is None or candidate.team_id != team_id or \
                {candidate.a_key, candidate.b_key} != {sender.key, recipient.key}:
            raise _invalid("conversation_id is not a conversation between you and the recipient")
        conversation = candidate
    if conversation is None:
        conversation = _default_conversation(session, team_id, sender, recipient)

    if _pending_count(session, recipient) >= max_pending:
        raise MessagingError(429, "inbox_full", "the recipient has too many unacknowledged messages")

    values = dict(
        id=message_id, team_id=team_id, conversation_id=conversation.id, in_reply_to=reply_to, body=body,
        delivery_state="stored", kind=kind,
        sender_agent_id=sender.machine_id, sender_agent_name=sender.name if sender.kind == "agent" else None,
        sender_user_id=sender.user.id if sender.kind == "person" else None,
        recipient_agent_id=recipient.machine_id,
        recipient_agent_name=recipient.name if recipient.kind == "agent" else None,
        recipient_user_id=recipient.user.id if recipient.kind == "person" else None,
    )
    inserted = session.scalar(
        pg_insert(Message).values(**values).on_conflict_do_nothing(index_elements=["id"]).returning(Message.id))
    msg = _load_message(session, message_id)
    if inserted is None:  # lost a race with a concurrent send of the same id
        return _idempotent_result(session, msg, payload)
    for position, (name, data, sha) in enumerate(files):
        session.add(Attachment(message_id=message_id, position=position, filename=name,
                               media_type=security.ATTACHMENT_MEDIA_TYPE, size=len(data), sha256=sha,
                               content=data))
    session.flush()

    if parent is not None and (_sends_as(sender, parent, "recipient") or (
            sender.kind == "person" and parent.recipient_agent_id is not None
            and parent.recipient_agent_id in owned_machine_ids(session, sender.user, team_id))):
        _set_state(session, parent.id, "replied", sender.machine_id if sender.kind != "person" else None,
                   detail=None)
    return msg, True


def _set_state(session: Session, message_id: uuid.UUID, state: str, reporter: uuid.UUID | None,
               detail: str | None) -> None:
    """Append an event and show it, except that ``replied`` is sticky (protocol §11.2)."""
    session.execute(
        update(Message).where(Message.id == message_id)
        .values(
            delivery_state=case((Message.delivery_state == "replied", "replied"), else_=state),
            delivery_updated_at=func.now(),
        )
    )
    session.add(DeliveryEvent(message_id=message_id, state=state, detail=detail, reported_by=reporter))
    session.flush()


# Recipient actions ------------------------------------------------------------------

def _recipient_message(session: Session, agent: Agent, message_id: uuid.UUID | str) -> Message:
    msg = get_visible_message(session, agent, message_id)
    if msg.recipient_agent_id != agent.id:
        raise MessagingError(403, "forbidden", "only the recipient can do this")
    return msg


def ack(session: Session, agent: Agent, message_id: uuid.UUID | str) -> tuple[Message, bool]:
    """Recipient acknowledges durable local receipt. Idempotent: ``acked`` is False on a repeat."""
    msg = _recipient_message(session, agent, message_id)
    updated = session.execute(
        update(Message)
        .where(Message.id == msg.id, Message.acked_at.is_(None))
        .values(
            acked_at=func.now(),
            # A reply sent before the ack keeps "replied"; otherwise the message is now "received".
            delivery_state=func.coalesce(
                func.nullif(Message.delivery_state, "stored"), "received"),
            delivery_updated_at=func.now(),
        )
        .returning(Message.id)
    ).scalar()
    acked = updated is not None
    if acked:
        session.add(DeliveryEvent(message_id=msg.id, state="received", reported_by=agent.id))
        session.flush()
    return _load_message(session, msg.id), acked


def record_event(session: Session, agent: Agent, message_id: uuid.UUID | str, state: object,
                 detail: object = None) -> Message:
    """Recipient reports a connector state after acking (protocol §2)."""
    if state not in EVENT_STATES:
        raise _invalid(f"state must be one of: {', '.join(EVENT_STATES)}")
    if detail is not None:
        if not isinstance(detail, str) or len(detail) > EVENT_DETAIL_MAX:
            raise _invalid("detail must be a string of at most 500 characters")
        if detail == "":
            detail = None
        elif not security.valid_message_body(detail):
            raise _invalid("detail must be plain text")
    msg = _recipient_message(session, agent, message_id)
    msg = _load_message(session, msg.id, lock=True)  # serializes events for this message
    if msg.acked_at is None:
        raise MessagingError(409, "not_acked", "acknowledge the message before reporting events")
    latest = session.execute(
        select(DeliveryEvent.state, DeliveryEvent.detail).where(DeliveryEvent.message_id == msg.id)
        .order_by(DeliveryEvent.id.desc()).limit(1)
    ).first()
    if latest is not None and tuple(latest) == (state, detail):
        return msg  # an exact repeat of the latest event (e.g. a client retry) is a no-op (§11.3)
    _set_state(session, msg.id, state, agent.id, detail)
    return _load_message(session, msg.id)


# Reads -----------------------------------------------------------------------

def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


ROUTING_CLIENT = (0, 5, 0)


def reports_routing_client(session: Session, agent: Agent) -> bool:
    """§16.14 S1: a machine becomes routing-capable only when its last presence
    reported a client of v0.5.0 or later (so no other tool on an old install can)."""
    from raincli_server.models import AgentPresence
    row = session.get(AgentPresence, agent.id)
    version = row.client_version if row is not None else None
    try:
        return version is not None and tuple(int(x) for x in version.split(".")) >= ROUTING_CLIENT
    except ValueError:
        return False


def inbox(session: Session, agent: Agent, *, after: int = 0, limit: int = 100,
          include_acked: bool = False, routing_capable: bool = False) -> tuple[list[Message], int]:
    """Messages to ``agent`` with ``seq > after``, ascending. Returns ``(messages, cursor)``.

    §16.2 capability gate: without ``routing_capable`` (``?routing=1``), messages to a named agent and
    messages from a person are not returned (a v0.4 connector would deliver them to its inbox, or skip
    them as malformed). With it, the machine is recorded as routing-capable.
    """
    if routing_capable and agent.routing_capable_at is None and reports_routing_client(session, agent):
        agent.routing_capable_at = func.now()
        session.flush()
    stmt = (
        select(Message).where(Message.recipient_agent_id == agent.id, Message.seq > after)
        .order_by(Message.seq).limit(_clamp(limit, 1, INBOX_LIMIT_MAX))
        .execution_options(populate_existing=True)
    )
    if not routing_capable:
        stmt = stmt.where(Message.recipient_agent_name.is_(None), Message.sender_user_id.is_(None))
    if not include_acked:
        stmt = stmt.where(Message.acked_at.is_(None))
    messages = list(session.scalars(stmt))
    return messages, (messages[-1].seq if messages else after)


def person_inbox(session: Session, user: User, *, after: int = 0, limit: int = 100,
                 include_acked: bool = False) -> tuple[list[Message], int]:
    """Messages addressed to the person in teams they still belong to, ``seq > after`` ascending."""
    teams = select(Membership.team_id).where(Membership.user_id == user.id)
    stmt = (
        select(Message).where(Message.recipient_user_id == user.id, Message.seq > after,
                              Message.team_id.in_(teams))
        .order_by(Message.seq).limit(_clamp(limit, 1, INBOX_LIMIT_MAX))
        .execution_options(populate_existing=True)
    )
    if not include_acked:
        stmt = stmt.where(Message.acked_at.is_(None))
    messages = list(session.scalars(stmt))
    return messages, (messages[-1].seq if messages else after)


def person_ack(session: Session, user: User, message_id: uuid.UUID | str) -> tuple[Message, bool]:
    """A person marks a message addressed to them ``received`` (§16.4). Idempotent."""
    msg = get_message_for_person(session, user, message_id)
    if msg.recipient_user_id != user.id:
        raise MessagingError(403, "forbidden", "only the recipient can do this")
    updated = session.execute(
        update(Message).where(Message.id == msg.id, Message.acked_at.is_(None))
        .values(acked_at=func.now(), delivery_state=func.coalesce(func.nullif(Message.delivery_state, "stored"),
                                                                  "received"),
                delivery_updated_at=func.now())
        .returning(Message.id)
    ).scalar()
    if updated is not None:
        session.add(DeliveryEvent(message_id=msg.id, state="received", reported_by=None))
        session.flush()
    return _load_message(session, msg.id), updated is not None


def conversation_endpoint(session: Session, conv: Conversation, side: str) -> Endpoint:
    user_id = getattr(conv, f"{side}_user_id")
    if user_id is not None:
        return Endpoint("person", user=session.get(User, user_id))
    agent = session.get(Agent, conv.agent_a_id if side == "a" else conv.agent_b_id)
    name = getattr(conv, f"{side}_agent_name")
    return Endpoint("agent", agent=agent, name=name) if name else Endpoint("machine", agent=agent)


def _conversation_rows(session: Session, where, mine, unacked_where, limit: int) -> list[dict]:
    """``[{id, peer, peer_endpoint, last_seq, last_at, unacked}]`` newest first. ``mine(conv)`` returns
    the viewer's side (``"a"`` or ``"b"``)."""
    stats = (
        select(Message.conversation_id.label("cid"), func.max(Message.seq).label("last_seq"),
               func.max(Message.created_at).label("last_at"),
               func.count().filter(and_(unacked_where, Message.acked_at.is_(None))).label("unacked"))
        .group_by(Message.conversation_id).subquery()
    )
    rows = session.execute(
        select(Conversation, stats.c.last_seq, stats.c.last_at, stats.c.unacked)
        .join(stats, stats.c.cid == Conversation.id).where(where)
        .order_by(stats.c.last_seq.desc()).limit(_clamp(limit, 1, CONVERSATIONS_LIMIT_MAX))
    )
    out = []
    for conv, last_seq, last_at, unacked in rows:
        peer = conversation_endpoint(session, conv, "b" if mine(conv) == "a" else "a")
        out.append({"id": str(conv.id), "peer": peer.label(), "peer_endpoint": peer.json(), "last_seq": last_seq,
                    "last_at": iso(last_at), "unacked": unacked})
    return out


def list_conversations(session: Session, agent: Agent, *, limit: int = 50) -> list[dict]:
    """Conversations the machine is in (as a machine or as one of its agents), newest first."""
    return _conversation_rows(
        session, or_(Conversation.agent_a_id == agent.id, Conversation.agent_b_id == agent.id),
        lambda conv: "a" if conv.agent_a_id == agent.id else "b",
        Message.recipient_agent_id == agent.id, limit)


def list_person_conversations(session: Session, user: User, *, limit: int = 50) -> list[dict]:
    """The person's conversations: those with the person as an endpoint, in teams they belong to (§16.4)."""
    teams = select(Membership.team_id).where(Membership.user_id == user.id)
    return _conversation_rows(
        session, and_(or_(Conversation.a_user_id == user.id, Conversation.b_user_id == user.id),
                      Conversation.team_id.in_(teams)),
        lambda conv: "a" if conv.a_user_id == user.id else "b",
        Message.recipient_user_id == user.id, limit)


def get_conversation(session: Session, agent: Agent, conversation_id: uuid.UUID | str) -> Conversation:
    cid = parse_uuid(conversation_id, "conversation id", not_found=True)
    conv = session.get(Conversation, cid)
    if conv is None or agent.id not in (conv.agent_a_id, conv.agent_b_id):
        raise MessagingError(404, "not_found", "conversation not found")
    return conv


def get_person_conversation(session: Session, user: User, conversation_id: uuid.UUID | str) -> Conversation:
    cid = parse_uuid(conversation_id, "conversation id", not_found=True)
    conv = session.get(Conversation, cid)
    if conv is None or user.id not in (conv.a_user_id, conv.b_user_id) or \
            session.get(Membership, (conv.team_id, user.id)) is None:
        raise MessagingError(404, "not_found", "conversation not found")
    return conv


def _messages_of(session: Session, conv: Conversation, after: int, limit: int) -> tuple[list[Message], int]:
    messages = list(session.scalars(
        select(Message).where(Message.conversation_id == conv.id, Message.seq > after)
        .order_by(Message.seq).limit(_clamp(limit, 1, INBOX_LIMIT_MAX))
        .execution_options(populate_existing=True)
    ))
    return messages, (messages[-1].seq if messages else after)


def conversation_messages(session: Session, agent: Agent, conversation_id: uuid.UUID | str, *,
                          after: int = 0, limit: int = 100) -> tuple[list[Message], int]:
    """Messages of a conversation ``agent`` participates in (404 otherwise). Returns ``(messages, cursor)``."""
    return _messages_of(session, get_conversation(session, agent, conversation_id), after, limit)


def person_conversation_messages(session: Session, user: User, conversation_id: uuid.UUID | str, *,
                                 after: int = 0, limit: int = 100) -> tuple[list[Message], int]:
    return _messages_of(session, get_person_conversation(session, user, conversation_id), after, limit)


def delivery_events(session: Session, agent: Agent, message_id: uuid.UUID | str) -> list[DeliveryEvent]:
    """Event history of a message visible to ``agent`` (for the web conversation view)."""
    msg = get_visible_message(session, agent, message_id)
    return list(session.scalars(
        select(DeliveryEvent).where(DeliveryEvent.message_id == msg.id).order_by(DeliveryEvent.id)))


@dataclass(frozen=True)
class SendRequest:
    """Validated agent-API send body (protocol §3 "Send")."""

    id: str
    to: object
    body: str
    conversation_id: str | None
    in_reply_to: str | None
    attachments: list[tuple[str, bytes]]
    from_agent: str | None = None
    kind: str = "message"

    ALLOWED = frozenset({"id", "to", "body", "conversation_id", "in_reply_to", "attachments", "from", "sender",
                         "from_agent", "kind"})

    @classmethod
    def parse(cls, data: object, caller_handle: str) -> "SendRequest":
        if not isinstance(data, dict):
            raise _invalid("request body must be a JSON object")
        unknown = set(data) - cls.ALLOWED
        if unknown:
            raise _invalid(f"unknown fields: {', '.join(sorted(unknown))}")
        for key in ("from", "sender"):
            if key in data and data[key] != caller_handle:
                raise MessagingError(403, "forbidden", "the sender is taken from your credential")
        msg_id, to, body = data.get("id"), data.get("to"), data.get("body")
        if not isinstance(msg_id, str):
            raise _invalid("id must be a uuid4 string")
        try:
            if uuid.UUID(msg_id).version != 4:
                raise ValueError
        except ValueError:
            raise _invalid("id must be a uuid4 string") from None
        if not isinstance(to, (str, dict)):
            raise _invalid('to must be a handle or an endpoint object (§16.1)')
        from_agent, kind = data.get("from_agent"), data.get("kind", "message")
        if from_agent is not None and not isinstance(from_agent, str):
            raise _invalid("from_agent must be a string")
        if not isinstance(kind, str):
            raise _invalid("kind must be a string")
        if not isinstance(body, str):
            raise _invalid("body must be a string")
        conv, reply = data.get("conversation_id"), data.get("in_reply_to")
        for name, value in (("conversation_id", conv), ("in_reply_to", reply)):
            if value is not None and not isinstance(value, str):
                raise _invalid(f"{name} must be a UUID string or null")
        return cls(id=msg_id, to=to, body=body, conversation_id=conv, in_reply_to=reply,
                   attachments=_decode_attachments(data.get("attachments")), from_agent=from_agent, kind=kind)


def _decode_attachments(value: object) -> list[tuple[str, bytes]]:
    """Strictly decode API attachments ``[{filename, content_b64, sha256}]`` and verify each sha256."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise _invalid("attachments must be a list")
    if len(value) > security.ATTACHMENT_MAX_COUNT:
        raise _invalid(f"at most {security.ATTACHMENT_MAX_COUNT} attachments per message")
    out = []
    for i, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"filename", "content_b64", "sha256"}:
            raise _invalid(f"attachment {i + 1} must be {{filename, content_b64, sha256}}")
        name, b64, sha = item["filename"], item["content_b64"], item["sha256"]
        label = f"attachment {name!r}" if isinstance(name, str) and len(name) <= 100 else f"attachment {i + 1}"
        if not isinstance(b64, str) or len(b64) > 4 * (security.ATTACHMENT_MAX_BYTES // 3 + 1):
            raise _invalid(f"{label}: content_b64 must be base64 of at most {security.ATTACHMENT_MAX_BYTES} bytes")
        if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
            raise _invalid(f"{label}: sha256 must be 64 lowercase hex characters")
        try:
            data = base64.b64decode(b64.encode("ascii"), validate=True)
        except (binascii.Error, ValueError, UnicodeEncodeError):
            raise _invalid(f"{label}: content_b64 is not valid standard base64") from None
        if security.sha256_hex(data) != sha:
            raise _invalid(f"{label}: sha256 does not match the decoded content")
        out.append((name, data))
    return out
