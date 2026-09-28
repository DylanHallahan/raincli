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
from sqlalchemy.orm import Session, aliased, object_session

from raincli_server import security
from raincli_server.models import EVENT_STATES, Agent, Attachment, Conversation, DeliveryEvent, Message

EVENT_DETAIL_MAX = 500
INBOX_LIMIT_MAX = 500
CONVERSATIONS_LIMIT_MAX = 200
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MessagingError(Exception):
    """A protocol-level failure: ``status`` and ``code`` follow protocol §3."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


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


def message_json(session: Session, msg: Message, attachments: list[dict] | None = None) -> dict:
    """Protocol message JSON (M), including attachment metadata."""
    if attachments is None:
        attachments = _attachment_meta(session, [msg.id])[msg.id]
    return {
        "id": str(msg.id),
        "conversation_id": str(msg.conversation_id),
        "in_reply_to": str(msg.in_reply_to) if msg.in_reply_to else None,
        "from": _handle(session, msg.sender_agent_id),
        "to": _handle(session, msg.recipient_agent_id),
        "body": msg.body,
        "created_at": iso(msg.created_at),
        "seq": msg.seq,
        "acked_at": iso(msg.acked_at),
        "delivery_state": msg.delivery_state,
        "delivery_updated_at": iso(msg.delivery_updated_at),
        "attachments": attachments,
    }


def messages_json(session: Session, messages: list[Message]) -> list[dict]:
    """``message_json`` for a page of messages with one attachment query."""
    meta = _attachment_meta(session, [m.id for m in messages])
    return [message_json(session, m, meta[m.id]) for m in messages]


# Lookups ---------------------------------------------------------------------

def _load_message(session: Session, message_id: uuid.UUID, *, lock: bool = False) -> Message | None:
    stmt = select(Message).where(Message.id == message_id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    return session.scalar(stmt)


def _is_participant(msg: Message, agent: Agent) -> bool:
    return agent.id in (msg.sender_agent_id, msg.recipient_agent_id)


def get_visible_message(session: Session, agent: Agent, message_id: uuid.UUID | str) -> Message:
    """The message if ``agent`` is its sender or recipient; otherwise ``404`` (no existence leak)."""
    mid = parse_uuid(message_id, "message id", not_found=True)
    msg = _load_message(session, mid)
    if msg is None or msg.team_id != agent.team_id or not _is_participant(msg, agent):
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


def _pending_count(session: Session, recipient_id: uuid.UUID) -> int:
    return session.scalar(
        select(func.count()).select_from(Message)
        .where(Message.recipient_agent_id == recipient_id, Message.acked_at.is_(None))
    ) or 0


def _default_conversation(session: Session, team_id: uuid.UUID, x: uuid.UUID, y: uuid.UUID) -> Conversation:
    a, b = sorted((x, y))
    session.execute(
        pg_insert(Conversation)
        .values(id=uuid.uuid4(), team_id=team_id, agent_a_id=a, agent_b_id=b, is_default=True)
        .on_conflict_do_nothing(index_elements=["agent_a_id", "agent_b_id"], index_where=Conversation.is_default)
    )
    return session.scalar(
        select(Conversation).where(
            Conversation.agent_a_id == a, Conversation.agent_b_id == b, Conversation.is_default.is_(True))
    )


def _same_payload(existing: Message, sender: Agent, recipient: Agent, body: str,
                  in_reply_to: uuid.UUID | None, conversation_id: uuid.UUID | None,
                  attachment_keys: list[tuple[str, str]]) -> bool:
    session = object_session(existing)
    stored = [
        (a["filename"], a["sha256"]) for a in _attachment_meta(session, [existing.id])[existing.id]
    ] if session is not None else []
    return (
        stored == attachment_keys
        and existing.sender_agent_id == sender.id
        and existing.recipient_agent_id == recipient.id
        and existing.body == body
        and existing.in_reply_to == in_reply_to
        # An omitted conversation_id means "the server's choice", which the first send made.
        and (conversation_id is None or existing.conversation_id == conversation_id)
    )


def _idempotent_result(existing: Message, *args) -> tuple[Message, bool]:
    if _same_payload(existing, *args):
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


# Send --------------------------------------------------------------------------

def send_message(
    session: Session,
    sender_agent: Agent,
    *,
    id: uuid.UUID | str,  # noqa: A002 - protocol field name
    to_handle: str,
    body: str,
    conversation_id: uuid.UUID | str | None = None,
    in_reply_to: uuid.UUID | str | None = None,
    max_pending: int,
    attachments: list[tuple[str, bytes]] | None = None,
) -> tuple[Message, bool]:
    """Store a message from ``sender_agent``. Returns ``(message, created)``.

    ``created`` is False for an idempotent retry (same id and payload, including the
    ordered attachment (filename, sha256) list). ``attachments`` are ``(filename, bytes)``
    pairs; they are stored in the same transaction as the message (protocol §8).
    """
    message_id = parse_uuid(id, "id")
    reply_to = parse_uuid(in_reply_to, "in_reply_to") if in_reply_to is not None else None
    conv_id = parse_uuid(conversation_id, "conversation_id") if conversation_id is not None else None
    if not security.valid_message_body(body):
        raise _invalid("body must be 1-16000 characters of plain text")
    files = validate_attachments(attachments)
    if sender_agent.revoked_at is not None:
        raise MessagingError(403, "forbidden", "sender agent is revoked")

    # Same response for unknown, other-team, revoked and self: never reveal other teams' handles.
    bad_recipient = _invalid("recipient must be another active agent in your team")
    if not security.valid_handle(to_handle):
        raise bad_recipient
    # Lock the recipient row first: serializes sends to this recipient (capacity + cursor order).
    recipient = session.scalar(
        select(Agent).where(Agent.team_id == sender_agent.team_id, Agent.handle == to_handle)
        .with_for_update(key_share=True).execution_options(populate_existing=True)
    )
    if recipient is None or recipient.id == sender_agent.id:
        raise bad_recipient
    payload = (sender_agent, recipient, body, reply_to, conv_id, [(n, h) for n, _, h in files])

    # Idempotency is decided before any other state or capacity check.
    existing = _load_message(session, message_id)
    if existing is not None:
        return _idempotent_result(existing, *payload)
    if recipient.revoked_at is not None:
        raise bad_recipient

    parent: Message | None = None
    if reply_to is not None:
        parent = _load_message(session, reply_to)
        if parent is None or parent.team_id != sender_agent.team_id or not _is_participant(parent, sender_agent):
            raise MessagingError(404, "not_found", "in_reply_to message not found")
        other = parent.recipient_agent_id if parent.sender_agent_id == sender_agent.id else parent.sender_agent_id
        if other != recipient.id:
            raise _invalid("a reply must be addressed to the other participant of the parent message")
        if conv_id is not None and conv_id != parent.conversation_id:
            raise _invalid("conversation_id does not match the parent message")
        conversation_id_final = parent.conversation_id
    elif conv_id is not None:
        conv = session.get(Conversation, conv_id)
        if conv is None or {conv.agent_a_id, conv.agent_b_id} != {sender_agent.id, recipient.id}:
            raise _invalid("conversation_id is not a conversation between you and the recipient")
        conversation_id_final = conv.id
    else:
        conversation_id_final = _default_conversation(
            session, sender_agent.team_id, sender_agent.id, recipient.id).id

    if _pending_count(session, recipient.id) >= max_pending:
        raise MessagingError(429, "inbox_full", "the recipient has too many unacknowledged messages")

    inserted = session.scalar(
        pg_insert(Message)
        .values(
            id=message_id, team_id=sender_agent.team_id, conversation_id=conversation_id_final,
            in_reply_to=reply_to, sender_agent_id=sender_agent.id, recipient_agent_id=recipient.id,
            body=body, delivery_state="stored",
        )
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(Message.id)
    )
    msg = _load_message(session, message_id)
    if inserted is None:  # lost a race with a concurrent send of the same id (other recipient)
        return _idempotent_result(msg, *payload)
    for position, (name, data, sha) in enumerate(files):
        session.add(Attachment(message_id=message_id, position=position, filename=name,
                               media_type=security.ATTACHMENT_MEDIA_TYPE, size=len(data), sha256=sha,
                               content=data))
    session.flush()

    if parent is not None and parent.recipient_agent_id == sender_agent.id:
        _set_state(session, parent.id, "replied", sender_agent.id, detail=None)
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


def inbox(session: Session, agent: Agent, *, after: int = 0, limit: int = 100,
          include_acked: bool = False) -> tuple[list[Message], int]:
    """Messages to ``agent`` with ``seq > after``, ascending. Returns ``(messages, cursor)``."""
    stmt = (
        select(Message).where(Message.recipient_agent_id == agent.id, Message.seq > after)
        .order_by(Message.seq).limit(_clamp(limit, 1, INBOX_LIMIT_MAX))
        .execution_options(populate_existing=True)
    )
    if not include_acked:
        stmt = stmt.where(Message.acked_at.is_(None))
    messages = list(session.scalars(stmt))
    return messages, (messages[-1].seq if messages else after)


def list_conversations(session: Session, agent: Agent, *, limit: int = 50) -> list[dict]:
    """``[{id, peer, last_seq, last_at, unacked}]`` newest first, for conversations ``agent`` is in."""
    peer = aliased(Agent)
    stats = (
        select(
            Message.conversation_id.label("cid"),
            func.max(Message.seq).label("last_seq"),
            func.max(Message.created_at).label("last_at"),
            func.count().filter(
                and_(Message.recipient_agent_id == agent.id, Message.acked_at.is_(None))
            ).label("unacked"),
        )
        .where(or_(Message.sender_agent_id == agent.id, Message.recipient_agent_id == agent.id))
        .group_by(Message.conversation_id)
        .subquery()
    )
    peer_id = func.coalesce(
        func.nullif(Conversation.agent_a_id, agent.id), Conversation.agent_b_id)
    rows = session.execute(
        select(Conversation.id, peer.handle, stats.c.last_seq, stats.c.last_at, stats.c.unacked)
        .join(stats, stats.c.cid == Conversation.id)
        .join(peer, peer.id == peer_id)
        .where(or_(Conversation.agent_a_id == agent.id, Conversation.agent_b_id == agent.id))
        .order_by(stats.c.last_seq.desc())
        .limit(_clamp(limit, 1, CONVERSATIONS_LIMIT_MAX))
    )
    return [
        {"id": str(cid), "peer": handle, "last_seq": last_seq, "last_at": iso(last_at), "unacked": unacked}
        for cid, handle, last_seq, last_at, unacked in rows
    ]


def get_conversation(session: Session, agent: Agent, conversation_id: uuid.UUID | str) -> Conversation:
    cid = parse_uuid(conversation_id, "conversation id", not_found=True)
    conv = session.get(Conversation, cid)
    if conv is None or agent.id not in (conv.agent_a_id, conv.agent_b_id):
        raise MessagingError(404, "not_found", "conversation not found")
    return conv


def conversation_messages(session: Session, agent: Agent, conversation_id: uuid.UUID | str, *,
                          after: int = 0, limit: int = 100) -> tuple[list[Message], int]:
    """Messages of a conversation ``agent`` participates in (404 otherwise). Returns ``(messages, cursor)``."""
    conv = get_conversation(session, agent, conversation_id)
    messages = list(session.scalars(
        select(Message).where(Message.conversation_id == conv.id, Message.seq > after)
        .order_by(Message.seq).limit(_clamp(limit, 1, INBOX_LIMIT_MAX))
        .execution_options(populate_existing=True)
    ))
    return messages, (messages[-1].seq if messages else after)


def delivery_events(session: Session, agent: Agent, message_id: uuid.UUID | str) -> list[DeliveryEvent]:
    """Event history of a message visible to ``agent`` (for the web conversation view)."""
    msg = get_visible_message(session, agent, message_id)
    return list(session.scalars(
        select(DeliveryEvent).where(DeliveryEvent.message_id == msg.id).order_by(DeliveryEvent.id)))


@dataclass(frozen=True)
class SendRequest:
    """Validated agent-API send body (protocol §3 "Send")."""

    id: str
    to: str
    body: str
    conversation_id: str | None
    in_reply_to: str | None
    attachments: list[tuple[str, bytes]]

    ALLOWED = frozenset({"id", "to", "body", "conversation_id", "in_reply_to", "attachments", "from", "sender"})

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
        if not isinstance(to, str):
            raise _invalid("to must be a handle")
        if not isinstance(body, str):
            raise _invalid("body must be a string")
        conv, reply = data.get("conversation_id"), data.get("in_reply_to")
        for name, value in (("conversation_id", conv), ("in_reply_to", reply)):
            if value is not None and not isinstance(value, str):
                raise _invalid(f"{name} must be a UUID string or null")
        return cls(id=msg_id, to=to, body=body, conversation_id=conv, in_reply_to=reply,
                   attachments=_decode_attachments(data.get("attachments")))


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
