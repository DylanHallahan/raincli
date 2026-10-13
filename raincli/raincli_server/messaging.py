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
* The default conversation for a pair in a team is an upsert against the partial
  unique index ``uq_conversations_default_pair`` (team, pair). A message's
  ``(conversation_id, team_id)`` references its conversation's ``(id, team_id)``.
* Ack is a conditional ``UPDATE ... WHERE acked_at IS NULL``.
* A send takes ``FOR KEY SHARE`` on its conversation row before it allocates the
  ``seq`` and holds it to commit; archive and unarchive take ``FOR UPDATE``. Sends
  never wait on each other there, and an archive's ``max(seq)`` never precedes an
  uncommitted lower ``seq`` (§17.3 A1).
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, object_session

from raincli_server import presentation, routing, security
from raincli_server.models import (EVENT_STATES, MESSAGE_KINDS, Agent, AgentPresence, Attachment, Conversation,
                                   DeliveryEvent, MachineAgent, Membership, Message, PersonInbox, User)
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


def view_state(session: Session, viewer: User | None, msg: Message) -> tuple[str, str | None, str | None]:
    """``(state, raw hold code, route)`` as ``viewer`` may see a message (§21 C13 H4). For a message to a person
    (``recipient_user_id``), anyone but that person sees no read receipt: held ``no_inbox`` while the person has
    no inbox in that team, otherwise ``received`` (``replied`` once they replied). The person sees their own read
    state. Otherwise today's state, with the derived or reported hold."""
    if msg.recipient_user_id is not None and (viewer is None or viewer.id != msg.recipient_user_id):
        if msg.delivery_state == "replied":
            return "replied", None, msg.route
        own = person_of_endpoint(session, sender_endpoint(session, msg)) == msg.recipient_user_id
        if not own and presentation.inbox_of(session, msg.team_id, msg.recipient_user_id) is None:
            return "held", "no_inbox", msg.route
        return "received", None, msg.route
    if msg.recipient_user_id is not None:  # the person's own view: their own read state
        return ("received" if msg.person_seen_at is not None and msg.delivery_state == "stored"
                else msg.delivery_state), None, msg.route
    hold = hold_reason(session, msg)
    state = "held" if msg.delivery_state == "stored" and hold else msg.delivery_state
    return state, hold, msg.route


def message_json(session: Session, msg: Message, attachments: list[dict] | None = None,
                 owners: dict | None = None, *, reader_agent: Agent | None = None,
                 reader_user: User | None = None) -> dict:
    """Protocol message JSON (M) for one reader, with attachment metadata (§21 C5.2, C13 M3).

    Teammates are people: ``from_endpoint``/``to_endpoint`` are ``{person, display_name}`` and ``from``/``to``
    are ``@email``, except that the reading machine's OWN endpoint keeps ``{machine, agent}`` and its handle, so
    a connector still sees its own target. ``from_agent`` is shown only to the sending machine's owner;
    ``from_owner`` is null; ``from_kind`` is ``app`` or ``agent``. ``hold_reason`` is the raw code for the
    recipient machine's owner and a coarse code for anyone else.
    """
    if attachments is None:
        attachments = _attachment_meta(session, [msg.id])[msg.id]
    sender, recipient = sender_endpoint(session, msg), recipient_endpoint(session, msg)
    viewer = reader_user if reader_user is not None else (
        session.get(User, reader_agent.owner_user_id) if reader_agent is not None else None)
    viewer_id = viewer.id if viewer is not None else None

    def shown(ep: Endpoint) -> tuple[str, dict]:
        if reader_agent is not None and ep.agent is not None and ep.agent.id == reader_agent.id:
            return ep.agent.handle, ep.json()
        person = session.get(User, person_of_endpoint(session, ep))
        return "@" + person.email, {"person": person.email,
                                    "display_name": presentation.name_in_team(session, msg.team_id, person)}

    from_text, from_json = shown(sender)
    to_text, to_json = shown(recipient)
    state, hold, route = view_state(session, viewer, msg)
    owns_recipient = recipient.agent is not None and recipient.agent.owner_user_id == viewer_id
    return {
        "id": str(msg.id),
        "conversation_id": str(msg.conversation_id),
        "in_reply_to": str(msg.in_reply_to) if msg.in_reply_to else None,
        "from": from_text,
        "to": to_text,
        "from_endpoint": from_json,
        "to_endpoint": to_json,
        "from_agent": msg.sender_agent_name if sender.agent is not None and sender.agent.owner_user_id == viewer_id
        else None,
        "from_owner": None,
        "from_kind": "app" if sender.kind == "person" else "agent",
        "kind": msg.kind,
        "from_same_owner": same_owner(sender, recipient),
        "body": msg.body,
        "created_at": iso(msg.created_at),
        "seq": msg.seq,
        # a person's own read state for them (person_seen_at); a machine's pickup for the machine's owner
        "acked_at": iso(msg.person_seen_at) if msg.recipient_user_id is not None and viewer_id == msg.recipient_user_id
        else iso(msg.acked_at) if msg.recipient_user_id is None and owns_recipient else None,
        "delivery_state": state,
        "hold_reason": hold if owns_recipient else presentation.api_hold_code(hold, route),
        # H4: for a message to a person, the time it changed would tell the sender when they read it.
        "delivery_updated_at": iso(msg.delivery_updated_at) if msg.recipient_user_id is None
        or viewer_id == msg.recipient_user_id or msg.delivery_state == "replied" else iso(msg.created_at),
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


def messages_json(session: Session, messages: list[Message], *, reader_agent: Agent | None = None,
                  reader_user: User | None = None) -> list[dict]:
    """``message_json`` for a page of messages, for one reader, with one attachment query."""
    meta = _attachment_meta(session, [m.id for m in messages])
    return [message_json(session, m, meta[m.id], reader_agent=reader_agent, reader_user=reader_user)
            for m in messages]


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


def machine_can_see(session: Session, msg: Message, agent: Agent) -> bool:
    """§21 C13 M1: a machine sees the messages it is in, and its owner's messages in its team (the owner's own
    data)."""
    if msg.team_id != agent.team_id:
        return False
    if _is_participant(msg, agent):
        return True
    owner = session.get(User, agent.owner_user_id)
    return owner is not None and person_can_see(session, msg, owner)


def get_visible_message(session: Session, agent: Agent, message_id: uuid.UUID | str) -> Message:
    """The message if ``agent`` may see it (``machine_can_see``); otherwise ``404`` (no existence leak)."""
    mid = parse_uuid(message_id, "message id", not_found=True)
    msg = _load_message(session, mid)
    if msg is None or not machine_can_see(session, msg, agent):
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
    """Messages the recipient has not taken yet: a machine's unacked ones; for a person, the ones they have neither
    seen (person_seen_at, §21 H2) nor had picked up by a machine."""
    if recipient.kind == "person":
        where = and_(Message.recipient_user_id == recipient.user.id, Message.person_seen_at.is_(None))
    else:
        where = Message.recipient_agent_id == recipient.agent.id
    return session.scalar(
        select(func.count()).select_from(Message).where(where, Message.acked_at.is_(None))) or 0


def _endpoint_columns(side: str, ep: Endpoint) -> dict:
    """Conversation columns for side ``a`` or ``b``."""
    agent_col = {"a": "agent_a_id", "b": "agent_b_id"}[side]
    return {agent_col: ep.machine_id, f"{side}_agent_name": ep.name if ep.kind == "agent" else None,
            f"{side}_user_id": ep.user.id if ep.kind == "person" else None, f"{side}_key": ep.key}


def _default_conversation(session: Session, team_id: uuid.UUID, x: Endpoint, y: Endpoint) -> Conversation:
    a, b = sorted((x, y), key=lambda ep: ep.key)
    where = (Conversation.team_id == team_id, Conversation.a_key == a.key, Conversation.b_key == b.key,
             Conversation.is_default.is_(True))  # §18.1: a pair's default thread is per team
    # A plain read first: an existing conversation is found without waiting on any row lock (an archive in
    # progress holds one; §17.3 A1: the send path never contends with it). Insert only the first time.
    existing = session.scalar(select(Conversation).where(*where))
    if existing is not None:
        return existing
    session.execute(
        pg_insert(Conversation)
        .values(id=uuid.uuid4(), team_id=team_id, is_default=True, **_endpoint_columns("a", a),
                **_endpoint_columns("b", b))
        .on_conflict_do_nothing(index_elements=["team_id", "a_key", "b_key"], index_where=Conversation.is_default)
    )
    return session.scalar(select(Conversation).where(*where))


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
    quotas=None,
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
                in_reply_to=in_reply_to, max_pending=max_pending, attachments=attachments, kind=kind,
                quotas=quotas)


def send_as_person(session: Session, user: User, team_id: uuid.UUID, *, id, to: object, body: str,  # noqa: A002
                   conversation_id=None, in_reply_to=None, max_pending: int,
                   attachments: list[tuple[str, bytes]] | None = None, kind: str = "message",
                   quotas=None) -> tuple[Message, bool]:
    """Store a message from a person (§16.3, §16.4). Only ``kind: message`` (§16.12 C11)."""
    if not user.is_active or session.get(Membership, (team_id, user.id)) is None:
        raise MessagingError(403, "forbidden", "you are not a member of that team")
    if kind != "message":
        raise _invalid("a person sends kind message only")
    return send(session, Endpoint("person", user=user), team_id, id=id, to=to, body=body,
                conversation_id=conversation_id, in_reply_to=in_reply_to, max_pending=max_pending,
                attachments=attachments, kind=kind, quotas=quotas)


def _sends_as(sender: Endpoint, msg: Message, side: str) -> bool:
    """Whether ``sender`` holds the ``side`` ("sender"/"recipient") of ``msg``: the same machine, or the
    same person."""
    if sender.kind == "person":
        return getattr(msg, f"{side}_user_id") == sender.user.id
    return getattr(msg, f"{side}_agent_id") == sender.agent.id


def send(session: Session, sender: Endpoint, team_id: uuid.UUID, *, id, to: object, body: str,  # noqa: A002
         conversation_id=None, in_reply_to=None, max_pending: int,
         attachments: list[tuple[str, bytes]] | None = None, kind: str = "message",
         quotas=None) -> tuple[Message, bool]:
    """The one send path for every sender (§21): teammates address people, never machines or agents.

    - A new message names a person (C2); it goes to that person's inbox in the team, or waits for them in their
      app (C3), in the pair conversation of the two people. A machine writing to its own owner (escalations,
      your agents writing to you) lands in the owner's ``self`` conversation (C13 H2).
    - A reply (``in_reply_to``) is routed from its parent, whatever ``to`` says (C4, C13 M4).
    ``quotas`` (a ``quotas.Quotas``) charges attachment bytes to the sending user and the team (§18.1 item 5)."""
    message_id = parse_uuid(id, "id")
    reply_to = parse_uuid(in_reply_to, "in_reply_to") if in_reply_to is not None else None
    conv_id = parse_uuid(conversation_id, "conversation_id") if conversation_id is not None else None
    if conv_id is not None:  # an id merged by migration 0010 means the conversation it went into
        conv_id = final_conversation_id(session, conv_id)
    if not security.valid_message_body(body):
        raise _invalid("body must be 1-16000 characters of plain text")
    if kind not in MESSAGE_KINDS:
        raise _invalid(f"kind must be one of: {', '.join(MESSAGE_KINDS)}")
    files = validate_attachments(attachments)
    me = sender_person(session, sender)
    if me is None or not me.is_active or session.get(Membership, (team_id, me.id)) is None:
        raise MessagingError(403, "forbidden", "you are not a member of that team")

    # Idempotency is decided before any other state or capacity check.
    existing = _load_message(session, message_id)
    if existing is not None:
        return _idempotent_v06(session, existing, sender, team_id, to, body, kind, reply_to, conv_id, files)

    parent: Message | None = None
    if reply_to is not None:
        parent = _load_message(session, reply_to)
        visible = parent is not None and parent.team_id == team_id and person_can_see(session, parent, me)
        if not visible:
            raise MessagingError(404, "not_found", "in_reply_to message not found")
        target, route, other = _reply_target(session, sender, me, parent)
        if to is not None and _person_named(session, team_id, to, me) != other.id:
            raise _invalid("a reply goes to the other person of the message it answers")
        parent_conv = final_conversation_id(session, parent.conversation_id)
        if conv_id is not None and conv_id != parent_conv:
            raise _invalid("conversation_id does not match the parent message")
        conversation = session.get(Conversation, parent_conv)
    else:
        if to is None:
            raise _invalid(presentation.error_text("invalid"))
        person = resolve_recipient(session, team_id, to, me)
        if person.id == me.id:
            if sender.kind == "person":  # §16.12 C7: never to yourself
                raise _invalid(presentation.error_text("invalid"))
            target, route = Endpoint("person", user=me), "self"  # H2 (i): your machine writes to you
            conversation = _self_conversation(session, team_id, me)
        else:
            target, route = inbox_endpoint(session, person, team_id)
            conversation = _pair_conversation(session, team_id, me, person)
        if conv_id is not None and conv_id != conversation.id:
            raise _invalid("conversation_id is not your conversation with that person")
    if kind == "escalation" and not (sender.kind != "person" and route == "self"):
        raise _invalid("an escalation goes only from a machine to its own owner (§16.8)")
    _lock_recipient(session, target)  # serializes sends to one recipient (capacity and cursor order)

    if _pending_count(session, target) >= max_pending:
        raise MessagingError(429, "inbox_full", "the recipient has too many unacknowledged messages")

    if files and quotas is not None:
        from raincli_server import quotas as quota_mod
        quota_mod.charge(session, me.id, team_id, sum(len(data) for _, data, _ in files), quotas)

    # §17.3 A1: KEY SHARE on the conversation before the seq is allocated (the insert below), held to commit.
    # Sends share it; an archive's FOR UPDATE waits for them. Nothing on the send path writes this row.
    session.execute(select(Conversation.id).where(Conversation.id == conversation.id)
                    .with_for_update(read=True, key_share=True))
    values = dict(
        id=message_id, team_id=team_id, conversation_id=conversation.id, in_reply_to=reply_to, body=body,
        delivery_state="stored", kind=kind, route=route,
        sender_agent_id=sender.machine_id, sender_agent_name=sender.name if sender.kind == "agent" else None,
        sender_user_id=sender.user.id if sender.kind == "person" else None,
        recipient_agent_id=target.machine_id,
        recipient_agent_name=target.name if target.kind == "agent" else None,
        recipient_user_id=target.user.id if target.kind == "person" else None,
    )
    inserted = session.scalar(
        pg_insert(Message).values(**values).on_conflict_do_nothing(index_elements=["id"]).returning(Message.id))
    msg = _load_message(session, message_id)
    if inserted is None:  # lost a race with a concurrent send of the same id
        return _idempotent_v06(session, msg, sender, team_id, to, body, kind, reply_to, conv_id, files)
    for position, (name, data, sha) in enumerate(files):
        session.add(Attachment(message_id=message_id, position=position, filename=name,
                               media_type=security.ATTACHMENT_MEDIA_TYPE, size=len(data), sha256=sha,
                               content=data))
    session.flush()

    if parent is not None and person_of_endpoint(session, recipient_endpoint(session, parent)) == me.id:
        _set_state(session, parent.id, "replied", sender.machine_id if sender.kind != "person" else None,
                   detail=None)
    return msg, True


# Person routing (§21) -------------------------------------------------------------------

def sender_person(session: Session, sender: Endpoint) -> User | None:
    """The person a sender is: the person, or the sending machine's owner."""
    return sender.user if sender.kind == "person" else session.get(User, sender.agent.owner_user_id)


def person_of_endpoint(session: Session, ep: Endpoint) -> uuid.UUID | None:
    if ep.kind == "person":
        return ep.user.id
    return ep.agent.owner_user_id if ep.agent is not None else None


def _refuse(resolution) -> MessagingError:
    status, code, message, extra = resolution.error
    return MessagingError(status, code, message, extra or None)


def resolve_recipient(session: Session, team_id: uuid.UUID, to: object, me: User) -> User:
    """C2: the one person ``to`` names in the team. Machine and agent forms are refused with ``address_people``
    (naming the handle's person only for a teammate's machine); an ambiguous name is ``ambiguous_person``."""
    if isinstance(to, dict):
        if set(to) == {"person"} and isinstance(to["person"], str):
            text = to["person"]
        elif set(to) == {"person_name"} and isinstance(to["person_name"], str):
            text = to["person_name"]
        elif "machine" in to and set(to) <= {"machine", "agent"} and isinstance(to["machine"], str):
            raise _refuse(presentation.machine_refusal(session, team_id, to["machine"], sender=me))
        else:
            raise _invalid(presentation.error_text("invalid"))
    elif isinstance(to, str):
        text = to
    else:
        raise _invalid(presentation.error_text("invalid"))
    resolution = presentation.resolve_person_text(session, team_id, text, sender=me)
    if resolution.kind != "person":
        raise _refuse(resolution)
    return resolution.user


def _person_named(session: Session, team_id: uuid.UUID, to: object, me: User) -> uuid.UUID | None:
    """C2.5: the person a reply's ``to`` names, tolerating an old client's machine form (that machine's owner)."""
    if isinstance(to, dict) and "machine" in to:
        handle = to.get("machine")
    elif isinstance(to, str) and "@" not in to:
        handle = to.split("/", 1)[0].strip()
    else:
        handle = None
    if handle:
        machine = session.scalar(select(Agent).where(Agent.team_id == team_id, Agent.handle == handle,
                                                     Agent.revoked_at.is_(None)))  # C13 L3
        if machine is not None:
            return machine.owner_user_id
    try:
        return resolve_recipient(session, team_id, to, me).id
    except MessagingError:
        return None


def _lock_inbox_row(session: Session, user_id: uuid.UUID, team_id: uuid.UUID, *, write: bool) -> PersonInbox | None:
    """C13 M5a: inbox writes hold FOR UPDATE on the person's membership row (the per-(user, team) lock row) and
    their inbox row; send resolution holds FOR KEY SHARE on the same rows before reading the inbox."""
    lock = dict(read=True, key_share=True) if not write else {}
    session.execute(select(Membership.user_id).where(Membership.team_id == team_id, Membership.user_id == user_id)
                    .with_for_update(**lock))
    return session.scalars(select(PersonInbox).where(PersonInbox.user_id == user_id, PersonInbox.team_id == team_id)
                           .with_for_update(**lock).execution_options(populate_existing=True)).first()


def _inbox_target(session: Session, row: PersonInbox | None, person: User) -> tuple[Endpoint, str]:
    """C3.1: the inbox-mode path (the machine) or the named path (machine and name); C3.2 without an inbox."""
    if row is None:
        return Endpoint("person", user=person), "person"
    machine = session.get(Agent, row.agent_id)
    if machine is None or machine.revoked_at is not None:
        return Endpoint("person", user=person), "person"
    if presentation.uses_inbox_mode(session, row):
        return Endpoint("machine", agent=machine), "inbox"
    return Endpoint("agent", agent=machine, name=row.agent_name), "inbox"


def inbox_endpoint(session: Session, person: User, team_id: uuid.UUID) -> tuple[Endpoint, str]:
    """Where a message to ``person`` goes now (C3), with the route stored on it."""
    return _inbox_target(session, _lock_inbox_row(session, person.id, team_id, write=False), person)


def live_agent(session: Session, machine: Agent, name: str) -> bool:
    """C13 M4: a LIVE deliverable entry now: the machine's presence and its directory row fresh within the presence
    TTL, instant or next-turn, not ambiguous, and exactly one. ``known_agents`` alone does not count."""
    from raincli_server import identity

    cutoff = identity.now() - timedelta(seconds=routing.LIVE_SECONDS)
    presence_row = session.get(AgentPresence, machine.id)
    if presence_row is None or presence_row.seen_at <= cutoff or machine.revoked_at is not None:
        return False
    live = routing.live_entries(session, machine.id, name)
    return len(live) == 1 and routing.deliverable(live[0]) and machine.routing != "inbox-only"


def has_inbox_path(session: Session, machine: Agent) -> bool:
    """Whether a machine takes machine-endpoint messages: a reported inbox agent (``role = inbox``), or
    ``inbox-only`` routing. A machine-mode machine (the app, ``raincli login``) has neither."""
    if machine.routing == "inbox-only":
        return True
    return session.scalar(select(MachineAgent.agent_id).where(MachineAgent.agent_id == machine.id,
                                                              MachineAgent.role == "inbox").limit(1)) is not None


def _active_member(session: Session, team_id: uuid.UUID, user_id: uuid.UUID | None) -> User | None:
    user = session.get(User, user_id) if user_id is not None else None
    if user is None or not user.is_active or session.get(Membership, (team_id, user.id)) is None:
        return None
    return user


def _reply_target(session: Session, sender: Endpoint, me: User, parent: Message) -> tuple[Endpoint, str, User]:
    """C4 with C13 M4 and H2: ``(target, route, other person)`` for a reply to ``parent``, decided once, now."""
    team_id = parent.team_id
    x_sender, x_recipient = sender_endpoint(session, parent), recipient_endpoint(session, parent)
    xs, xr = person_of_endpoint(session, x_sender), person_of_endpoint(session, x_recipient)
    if xs == xr == me.id:
        # H2 (ii): you answering your own machine (or it answering you) stays between you and that machine.
        if sender.kind == "person":
            machine_side = x_sender if x_sender.kind != "person" else x_recipient
            if machine_side.kind == "person" or machine_side.agent.revoked_at is not None:
                raise _invalid(presentation.error_text("invalid"))
            if machine_side.kind == "agent" and not live_agent(session, machine_side.agent, machine_side.name):
                machine_side = Endpoint("machine", agent=machine_side.agent)
            return machine_side, "self", me
        return Endpoint("person", user=me), "self", me
    if xs == me.id:  # following up your own message: to its recipient person, now
        other = _active_member(session, team_id, xr)
        if other is None:
            raise _invalid(presentation.error_text("invalid"))
        target, route = inbox_endpoint(session, other, team_id)
        return target, route, other
    other = _active_member(session, team_id, xs)
    if other is None:  # the sender person left the team
        raise _invalid(presentation.error_text("invalid"))
    if x_sender.kind != "person" and x_sender.agent.revoked_at is None:
        if x_sender.kind == "machine":
            if has_inbox_path(session, x_sender.agent):  # a connector inbox receives machine-endpoint messages
                return Endpoint("machine", agent=x_sender.agent), "reply", other
            target, route = inbox_endpoint(session, other, team_id)  # machine mode: no inbox path (C13 M1 review)
            return target, route, other
        if live_agent(session, x_sender.agent, x_sender.name):
            return x_sender, "reply", other
    target, route = inbox_endpoint(session, other, team_id)
    return target, route, other


def _upsert_conversation(session: Session, team_id: uuid.UUID, *, kind: str, a: User, b: User) -> Conversation:
    if kind == "pair":
        lo, hi = sorted((a, b), key=lambda u: f"p:{u.id}")
        keys = (f"p:{lo.id}", f"p:{hi.id}")
        values = dict(a_user_id=lo.id, b_user_id=hi.id)
    else:
        keys = (f"p:{a.id}", f"self:{a.id}")
        values = dict(a_user_id=a.id, b_user_id=a.id)
    where = (Conversation.team_id == team_id, Conversation.a_key == keys[0], Conversation.b_key == keys[1],
             Conversation.is_default.is_(True))
    existing = session.scalar(select(Conversation).where(*where))
    if existing is not None:
        return existing
    session.execute(
        pg_insert(Conversation)
        .values(id=uuid.uuid4(), team_id=team_id, is_default=True, kind=kind, a_key=keys[0], b_key=keys[1], **values)
        .on_conflict_do_nothing(index_elements=["team_id", "a_key", "b_key"], index_where=Conversation.is_default))
    return session.scalar(select(Conversation).where(*where))


def _pair_conversation(session: Session, team_id: uuid.UUID, me: User, other: User) -> Conversation:
    return _upsert_conversation(session, team_id, kind="pair", a=me, b=other)


def _self_conversation(session: Session, team_id: uuid.UUID, me: User) -> Conversation:
    """H2: "From your agents", one per team and person, visible (never archived by the server)."""
    return _upsert_conversation(session, team_id, kind="self", a=me, b=me)


def _lock_recipient(session: Session, target: Endpoint) -> None:
    if target.kind == "person":
        session.execute(select(User.id).where(User.id == target.user.id).with_for_update(key_share=True))
    else:
        session.execute(select(Agent.id).where(Agent.id == target.agent.id).with_for_update(key_share=True))


def _idempotent_v06(session: Session, existing: Message, sender: Endpoint, team_id: uuid.UUID, to: object,
                    body: str, kind: str, reply_to, conv_id, files) -> tuple[Message, bool]:
    """A retried send is the same message when its sender, body, kind, parent, attachments and conversation match,
    and ``to`` names the same person (or, for a message stored before §21, the same endpoint)."""
    stored = [(a["filename"], a["sha256"]) for a in _attachment_meta(session, [existing.id])[existing.id]]
    conv_ok = conv_id is None or conv_id in (existing.conversation_id, existing.moved_from_conversation_id,
                                            existing.premerge_conversation_id)
    same = (stored == [(n, h) for n, _, h in files] and sender_endpoint(session, existing).key == sender.key
            and existing.body == body and existing.kind == kind and existing.in_reply_to == reply_to and conv_ok
            and existing.team_id == team_id)
    if same and to is not None and reply_to is None:
        recipient = recipient_endpoint(session, existing)
        me = sender_person(session, sender)
        named = _person_named(session, team_id, to, me)
        if named is None or named != person_of_endpoint(session, recipient):
            try:
                same = routing.resolve(session, team_id, routing.parse_endpoint(to)).key == recipient.key
            except MessagingError:
                same = False
    if same:
        return existing, False
    raise MessagingError(409, "id_conflict", "a different message already uses this id")


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
    """Recipient acknowledges durable local receipt. Idempotent: ``acked`` is False on a repeat. A machine that
    was the recipient before a re-target (its owner's message, now elsewhere) gets a ``200`` no-op, so an old
    client drops it from its queue (§21 C13 L1)."""
    mid = parse_uuid(message_id, "message id", not_found=True)
    msg = _load_message(session, mid)
    if msg is not None and msg.team_id == agent.team_id and msg.recipient_agent_id != agent.id \
            and msg.route in ("inbox", "person", "reply") \
            and person_of_endpoint(session, recipient_endpoint(session, msg)) == agent.owner_user_id:
        return msg, False
    msg = _recipient_message(session, agent, message_id)
    updated = session.execute(
        update(Message)
        .where(Message.id == msg.id, Message.acked_at.is_(None))
        .values(
            acked_at=func.now(), redeliver=False,
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
        elif detail not in presentation.EVENT_CODES:  # §21 C5.5: codes only, never free text
            raise _invalid("detail must be one of the event codes")
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
    # §21 C13 H1: a re-targeted message comes again below the cursor until acked; the cursor never goes back.
    stmt = (
        select(Message).where(Message.recipient_agent_id == agent.id,
                              or_(Message.seq > after, and_(Message.redeliver.is_(True), Message.acked_at.is_(None))))
        .order_by(Message.seq).limit(_clamp(limit, 1, INBOX_LIMIT_MAX))
        .execution_options(populate_existing=True)
    )
    if not routing_capable:
        stmt = stmt.where(Message.recipient_agent_name.is_(None), Message.sender_user_id.is_(None))
    if not include_acked:
        stmt = stmt.where(Message.acked_at.is_(None))
    messages = list(session.scalars(stmt))
    return messages, max([after, *(m.seq for m in messages)])


def person_inbox(session: Session, user: User, *, after: int = 0, limit: int = 100,
                 include_acked: bool = False, routed: bool = False) -> tuple[list[Message], int]:
    """Messages addressed to the person in teams they still belong to, ``seq > after`` ascending. With ``routed``
    (§21 PR6: the app's toasts), also the messages routed to the person's own machines (their inbox, replies to
    their agents), except those the person or their own machines sent; never acked by this read."""
    teams = select(Membership.team_id).where(Membership.user_id == user.id)
    mine = select(Agent.id).where(Agent.owner_user_id == user.id)
    to_me = Message.recipient_user_id == user.id
    if routed:
        to_me = or_(to_me, and_(Message.recipient_agent_id.in_(mine),
                                or_(Message.sender_user_id.is_(None), Message.sender_user_id != user.id),
                                or_(Message.sender_agent_id.is_(None), Message.sender_agent_id.not_in(mine))))
    stmt = (
        select(Message).where(to_me, Message.team_id.in_(teams),
                              or_(Message.seq > after, and_(Message.redeliver.is_(True), Message.person_seen_at.is_(None),
                                                           Message.acked_at.is_(None))))
        .order_by(Message.seq).limit(_clamp(limit, 1, INBOX_LIMIT_MAX))
        .execution_options(populate_existing=True)
    )
    if not include_acked:  # unread by the person; a machine's ack of a routed message is not the person reading it
        stmt = stmt.where(or_(Message.person_seen_at.is_(None), Message.recipient_user_id.is_(None)))
    messages = list(session.scalars(stmt))
    return messages, max([after, *(m.seq for m in messages)])


def person_ack(session: Session, user: User, message_id: uuid.UUID | str) -> tuple[Message, bool]:
    """The person has seen a message to them (§16.4, §21 H2): only their own read state, ``person_seen_at``, used
    in their own views and unread counts. Nothing a sender can fetch changes (no event, no state, no ``acked_at``:
    that stays the machine's pickup, so a message read in the app is still delivered to an inbox picked later).
    Idempotent. A message routed to one of the person's machines may be marked seen too."""
    msg = get_message_for_person(session, user, message_id)
    if person_of_endpoint(session, recipient_endpoint(session, msg)) != user.id or \
            person_of_endpoint(session, sender_endpoint(session, msg)) == user.id and msg.recipient_user_id is None:
        raise MessagingError(403, "forbidden", "only the recipient can do this")
    updated = session.execute(
        update(Message).where(Message.id == msg.id, Message.person_seen_at.is_(None))
        .values(person_seen_at=func.now()).returning(Message.id)).scalar()
    session.flush()
    return _load_message(session, msg.id), updated is not None


def is_unread_for(session: Session, user_id: uuid.UUID, msg: Message, owned: set | None = None) -> bool:
    """``unread_for`` for one loaded message (templates). ``owned``: the user's machine ids, if already known."""
    if msg.person_seen_at is not None:
        return False
    if msg.recipient_user_id is not None:
        return msg.recipient_user_id == user_id  # to them as a person, from anyone (their own agents' escalations too)
    owned = owned if owned is not None else set(session.scalars(select(Agent.id).where(Agent.owner_user_id == user_id)))
    from_user = msg.sender_user_id == user_id or msg.sender_agent_id in owned
    return bool(msg.recipient_agent_id in owned and not from_user)


def unread_for(user_id: uuid.UUID):
    """A SQL condition: a message ``user_id`` has not seen yet (their unread counts and lists, §21 H2), without
    ``person_seen_at``: to them as a person (from anyone, their own agents' escalations and reports included), or
    routed to one of their machines and not sent by them or their machines."""
    mine = select(Agent.id).where(Agent.owner_user_id == user_id)
    routed_to_me = and_(Message.recipient_user_id.is_(None), Message.recipient_agent_id.in_(mine),
                        or_(Message.sender_user_id.is_(None), Message.sender_user_id != user_id),
                        or_(Message.sender_agent_id.is_(None), Message.sender_agent_id.not_in(mine)))
    return and_(or_(Message.recipient_user_id == user_id, routed_to_me), Message.person_seen_at.is_(None))


def conversation_endpoint(session: Session, conv: Conversation, side: str) -> Endpoint:
    user_id = getattr(conv, f"{side}_user_id")
    if user_id is not None:
        return Endpoint("person", user=session.get(User, user_id))
    agent = session.get(Agent, conv.agent_a_id if side == "a" else conv.agent_b_id)
    name = getattr(conv, f"{side}_agent_name")
    return Endpoint("agent", agent=agent, name=name) if name else Endpoint("machine", agent=agent)


def _conversation_rows(session: Session, where, mine, unacked_where, limit: int,
                       archived: str = "include", *, viewer_id: uuid.UUID | None = None) -> list[dict]:
    """``[{id, peer, peer_endpoint, last_seq, last_at, unacked, archived, archived_at, archived_through_seq,
    archived_by}]`` newest first. ``mine(conv)`` returns the viewer's side (``"a"`` or ``"b"``). ``archived``
    filters by the effective archive state (§17.3 A1)."""
    stats = (
        select(Message.conversation_id.label("cid"), Message.team_id.label("tid"),
               func.max(Message.seq).label("last_seq"), func.max(Message.created_at).label("last_at"),
               func.count().filter(and_(unacked_where, Message.acked_at.is_(None))).label("unacked"))
        .group_by(Message.conversation_id, Message.team_id).subquery()
    )
    rows = session.execute(
        select(Conversation, stats.c.last_seq, stats.c.last_at, stats.c.unacked)
        # §18.1: only the conversation's own team's messages count, in every summary.
        .join(stats, and_(stats.c.cid == Conversation.id, stats.c.tid == Conversation.team_id)).where(where, archive_filter(archived, stats.c.last_seq))
        .order_by(stats.c.last_seq.desc()).limit(_clamp(limit, 1, CONVERSATIONS_LIMIT_MAX))
    )
    out = []
    viewer = session.get(User, viewer_id) if viewer_id is not None else None
    for conv, last_seq, last_at, unacked in rows:
        out.append({"id": str(conv.id), **peer_json(session, conv, viewer), "last_seq": last_seq,
                    "last_at": iso(last_at), "unacked": unacked, **archive_json(session, conv, last_seq)})
    return out


def peer_json(session: Session, conv: Conversation, viewer: User | None) -> dict:
    """``peer`` and ``peer_endpoint`` as the viewer sees them: the other person, by name (§21 C13 M3); "From your
    agents" and "Your agents" for a person's own conversations."""
    peer = presentation.conversation_peer(session, viewer, conv) if viewer is not None else None
    if peer is None:
        return {"peer": "a teammate", "peer_endpoint": None}
    if peer.user is None:
        return {"peer": peer.label, "peer_endpoint": {"person": viewer.email, "display_name": peer.label}}
    return {"peer": peer.label, "peer_endpoint": {"person": peer.user.email, "display_name": peer.label}}


def _machine_side(conv: Conversation, agent: Agent) -> str | None:
    """The side a machine reads a conversation from: its own endpoint, or its owner's (§21 C13 M1)."""
    if agent.id == conv.agent_a_id or (conv.agent_a_id is None and conv.a_user_id == agent.owner_user_id):
        return "a"
    if agent.id == conv.agent_b_id or (conv.agent_b_id is None and conv.b_user_id == agent.owner_user_id):
        return "b"
    return None


def list_conversations(session: Session, agent: Agent, *, limit: int = 50, archived: str = "include") -> list[dict]:
    """Conversations the machine is in, or its owner is in (a pair, self or own_agents conversation in its team),
    newest first. A machine credential sending no ``archived`` gets ``include`` (§17.3 A4)."""
    return _conversation_rows(
        session, and_(Conversation.team_id == agent.team_id, Conversation.merged_into.is_(None),
                      or_(Conversation.agent_a_id == agent.id, Conversation.agent_b_id == agent.id,
                          Conversation.a_user_id == agent.owner_user_id,
                          Conversation.b_user_id == agent.owner_user_id)),
        lambda conv: _machine_side(conv, agent) or "a",
        Message.recipient_agent_id == agent.id, limit, archived, viewer_id=agent.owner_user_id)


def list_person_conversations(session: Session, user: User, *, limit: int = 50,
                              archived: str = "exclude") -> list[dict]:
    """The person's conversations: those with the person as an endpoint, in teams they belong to (§16.4)."""
    teams = select(Membership.team_id).where(Membership.user_id == user.id)
    return _conversation_rows(
        session, and_(or_(Conversation.a_user_id == user.id, Conversation.b_user_id == user.id),
                      Conversation.team_id.in_(teams), Conversation.merged_into.is_(None)),
        lambda conv: "a" if conv.a_user_id == user.id else "b",
        unread_for(user.id), limit, archived, viewer_id=user.id)


# Archived conversations (§17.2) ----------------------------------------------------

ARCHIVED_FILTERS = ("exclude", "include", "only")


def archive_filter(archived: str, last_seq):
    """``exclude`` (the main list), ``include`` or ``only`` (the Archived view), by the effective state:
    archived while ``archived_at`` is set and no message has a seq above ``archived_through_seq``."""
    if archived not in ARCHIVED_FILTERS:
        raise _invalid('archived must be "exclude", "include" or "only"')
    is_archived = and_(Conversation.archived_at.is_not(None), last_seq <= Conversation.archived_through_seq)
    if archived == "exclude":
        return ~is_archived
    if archived == "only":
        return is_archived
    return true()


def _max_seq(session: Session, conv: Conversation) -> int:
    return session.scalar(select(func.coalesce(func.max(Message.seq), 0))
                          .where(Message.conversation_id == conv.id, Message.team_id == conv.team_id))


def is_archived(conv: Conversation, last_seq: int | None) -> bool:
    return conv.archived_at is not None and (last_seq or 0) <= (conv.archived_through_seq or 0)


def endpoint_for_key(session: Session, key: str | None) -> Endpoint | None:
    """The endpoint behind a canonical key (``m:<id>``, ``a:<id>:<name>``, ``p:<id>``), or None."""
    if not key:
        return None
    kind, _, rest = key.partition(":")
    try:
        if kind == "p":
            user = session.get(User, uuid.UUID(rest))
            return Endpoint("person", user=user) if user is not None else None
        machine_id, _, name = rest.partition(":")
        agent = session.get(Agent, uuid.UUID(machine_id))
    except ValueError:
        return None
    if agent is None:
        return None
    return Endpoint("agent", agent=agent, name=name) if kind == "a" and name else Endpoint("machine", agent=agent)


def archive_json(session: Session, conv: Conversation, last_seq: int | None = None) -> dict:
    """The effective archive state for conversation JSON (§17.3 A1, A5): ``archived``, and while archived
    ``archived_at``, ``archived_through_seq`` and ``archived_by`` (the endpoint, shown like
    ``from_endpoint``); all null once a newer message arrived or after unarchiving."""
    if last_seq is None:
        last_seq = _max_seq(session, conv)
    if not is_archived(conv, last_seq):
        return {"archived": False, "archived_at": None, "archived_through_seq": None, "archived_by": None}
    by = endpoint_for_key(session, conv.archived_by_key)
    who = session.get(User, person_of_endpoint(session, by)) if by is not None else None
    return {"archived": True, "archived_at": iso(conv.archived_at), "archived_through_seq": conv.archived_through_seq,
            "archived_by": {"person": who.email, "display_name": presentation.name_in_team(session, conv.team_id, who)}
            if who is not None else None}


MERGE_CHAIN_LIMIT = 16  # merged_into hops followed before giving up (a chain is one hop after migration 0010)


def final_conversation_id(session: Session, cid: uuid.UUID) -> uuid.UUID:
    """The conversation an id now means: ``merged_into`` followed to its end (migration 0010). An id that was never
    merged, or that does not exist, is returned as it is."""
    seen = {cid}
    for _ in range(MERGE_CHAIN_LIMIT):
        nxt = session.scalar(select(Conversation.merged_into).where(Conversation.id == cid))
        if nxt is None or nxt in seen:
            return cid
        seen.add(nxt)
        cid = nxt
    return cid


resolve_conversation_id = final_conversation_id


def _locked_conversation(session: Session, conversation_id: uuid.UUID | str) -> Conversation | None:
    cid = final_conversation_id(session, parse_uuid(conversation_id, "conversation id", not_found=True))
    # FOR UPDATE (§17.3 A1): a send holds KEY SHARE on this row from before it allocates its seq until it
    # commits, so an archive waits for sends in flight and its max(seq) never precedes an uncommitted lower
    # seq. Sends never write this row and never wait on each other here.
    return session.scalars(select(Conversation).where(Conversation.id == cid).with_for_update()
                           .execution_options(populate_existing=True)).first()


def _through_seq(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid("archived_through_seq must be a non-negative integer")
    return value


def _set_archived(session: Session, conv: Conversation, actor_key: str, archived: bool,
                  through_seq: int | None = None) -> Conversation:
    """§17.3 A1. Archive: record the newest seq the archiver saw (``through_seq``, capped at the current
    newest message; the newest when not given). Idempotent: an effectively archived conversation is
    returned unchanged. Unarchive clears all three fields."""
    through_seq = _through_seq(through_seq)  # a bad value is refused even when nothing would change
    newest = _max_seq(session, conv)
    if archived:
        if is_archived(conv, newest):
            return conv
        through = newest if through_seq is None else min(through_seq, newest)
        conv.archived_at, conv.archived_by_key, conv.archived_through_seq = (
            datetime.now(timezone.utc), actor_key, through)
    else:
        conv.archived_at = conv.archived_by_key = conv.archived_through_seq = None
    session.flush()
    return conv


def archive_as_machine(session: Session, agent: Agent, conversation_id: uuid.UUID | str,
                       archived: bool, through_seq: int | None = None) -> Conversation:
    """A machine credential whose machine, or whose owner, is a side (§21 C13 M1). Anything else is ``404``,
    with no hint that the conversation exists."""
    conv = _locked_conversation(session, conversation_id)
    side = _machine_side(conv, agent) if conv is not None and conv.team_id == agent.team_id else None
    if side is None:
        raise MessagingError(404, "not_found", "conversation not found")
    return _set_archived(session, conv, getattr(conv, f"{side}_key"), archived, through_seq)


def can_archive_as_person(session: Session, user: User, conv: Conversation) -> bool:
    """A member of the conversation's team who is an endpoint, or who owns a machine endpoint."""
    if session.get(Membership, (conv.team_id, user.id)) is None:
        return False
    if user.id in (conv.a_user_id, conv.b_user_id):
        return True
    owned = owned_machine_ids(session, user, conv.team_id)
    return bool({conv.agent_a_id, conv.agent_b_id} & owned)


def archive_as_person(session: Session, user: User, conversation_id: uuid.UUID | str,
                      archived: bool, through_seq: int | None = None) -> Conversation:
    conv = _locked_conversation(session, conversation_id)
    if conv is None or not can_archive_as_person(session, user, conv):
        raise MessagingError(404, "not_found", "conversation not found")
    return _set_archived(session, conv, f"p:{user.id}", archived, through_seq)


def conversation_json(session: Session, conv: Conversation, viewer: User | None = None) -> dict:
    """A conversation's identity and archive state (§17.2), and its peer as ``viewer`` sees it (§21 C13 M3)."""
    out = {"id": str(conv.id), "team_id": str(conv.team_id), **archive_json(session, conv)}
    if viewer is not None:
        out.update(peer_json(session, conv, viewer))
    return out


def get_conversation(session: Session, agent: Agent, conversation_id: uuid.UUID | str) -> Conversation:
    cid = final_conversation_id(session, parse_uuid(conversation_id, "conversation id", not_found=True))
    conv = session.get(Conversation, cid)
    if conv is None or conv.team_id != agent.team_id or _machine_side(conv, agent) is None:
        raise MessagingError(404, "not_found", "conversation not found")
    return conv


def get_person_conversation(session: Session, user: User, conversation_id: uuid.UUID | str) -> Conversation:
    cid = final_conversation_id(session, parse_uuid(conversation_id, "conversation id", not_found=True))
    conv = session.get(Conversation, cid)
    if conv is None or user.id not in (conv.a_user_id, conv.b_user_id) or \
            session.get(Membership, (conv.team_id, user.id)) is None:
        raise MessagingError(404, "not_found", "conversation not found")
    return conv


def _messages_of(session: Session, conv: Conversation, after: int, limit: int) -> tuple[list[Message], int]:
    messages = list(session.scalars(
        select(Message).where(Message.conversation_id == conv.id, Message.team_id == conv.team_id,
                              Message.seq > after)
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
        if not isinstance(to, (str, dict)) and not (to is None and data.get("in_reply_to") is not None):
            raise _invalid("to must be a person: an email, a name or {\"person\": email} (a reply may omit it)")
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


# The inbox setting (§21 C6, C8; C13 H1, H3, M5) ------------------------------------------------------------------

def _inbox_invalid(machine: Agent | None) -> MessagingError:
    return MessagingError(400, "inbox_invalid", presentation.error_text(
        "inbox_invalid", machine=machine.handle if machine is not None else "That machine"))


def _accepted_inbox_name(session: Session, machine: Agent, name: str) -> bool:
    """C1.1: a name ``check_agent_route`` accepts on that machine now, or the machine's current inbox agent."""
    role = session.scalar(select(MachineAgent.name).where(MachineAgent.agent_id == machine.id,
                                                          MachineAgent.role == "inbox"))
    if role == name:
        return True
    try:
        routing.check_agent_route(session, Endpoint("agent", agent=machine, name=name))
    except MessagingError:
        return False
    return True


def _previous_inbox_messages(session: Session, team_id: uuid.UUID, row: PersonInbox):
    """Messages routed to a person's inbox (C13 M5c: ``route='inbox'``) at that inbox's machine and name, or at the
    machine alone (the inbox-mode path)."""
    return and_(Message.team_id == team_id, Message.route == "inbox", Message.recipient_agent_id == row.agent_id,
                or_(Message.recipient_agent_name == row.agent_name, Message.recipient_agent_name.is_(None)),
                Message.delivery_state != "replied")  # answered already: nothing to deliver again


def waiting_messages(session: Session, user: User, team_id: uuid.UUID):
    """C6.2 with C13 H2 and M5b: messages waiting for ``user`` in their app (``route='person'``, unacked), not
    sent by them, and not answered (no later message from them to that sender person in the same conversation)."""
    own_machines = select(Agent.id).where(Agent.owner_user_id == user.id)
    not_sent_by_user = and_(or_(Message.sender_user_id.is_(None), Message.sender_user_id != user.id),
                            or_(Message.sender_agent_id.is_(None), Message.sender_agent_id.not_in(own_machines)))
    waiting = Message.__table__.alias("waiting")
    answer = Message.__table__.alias("answer")
    answered = select(answer.c.id).where(
        answer.c.conversation_id == waiting.c.conversation_id, answer.c.seq > waiting.c.seq,
        or_(answer.c.sender_user_id == user.id,
            answer.c.sender_agent_id.in_(select(Agent.id).where(Agent.owner_user_id == user.id)))).exists()
    rows = select(waiting.c.id).where(
        waiting.c.team_id == team_id, waiting.c.recipient_user_id == user.id, waiting.c.route == "person",
        waiting.c.acked_at.is_(None), ~answered)
    return and_(Message.id.in_(rows), not_sent_by_user)


def waiting_count(session: Session, user: User, team_id: uuid.UUID) -> int:
    return session.scalar(select(func.count()).select_from(Message).where(waiting_messages(session, user, team_id))) or 0


def _retarget(session: Session, where, target: Endpoint, route: str) -> int:
    """Move unacked messages to ``target`` (C6), to be redelivered below the poll cursor (C13 H1)."""
    values = dict(recipient_agent_id=target.machine_id,
                  recipient_agent_name=target.name if target.kind == "agent" else None,
                  recipient_user_id=target.user.id if target.kind == "person" else None,
                  route=route, redeliver=True)
    result = session.execute(update(Message).where(where, Message.acked_at.is_(None)).values(**values)
                             .execution_options(synchronize_session=False))
    return result.rowcount or 0


def inbox_json(session: Session, row: PersonInbox | None) -> dict | None:
    if row is None:
        return None
    machine = session.get(Agent, row.agent_id)
    return {"machine": machine.handle if machine is not None else None, "agent": row.agent_name,
            "set_at": iso(row.set_at)}


def set_person_inbox(session: Session, user: User, team_id: uuid.UUID, machine_id: uuid.UUID, agent_name: str, *,
                     deliver_waiting: bool = True, via: str, check_live: bool = True) -> dict:
    """Pick ``(machine, agent_name)`` as ``user``'s inbox in the team (C6, C8). Unacked messages at the previous
    inbox move to the new one; the waiting messages are delivered too when ``deliver_waiting``. Idempotent: the
    same inbox again moves nothing. Returns ``{inbox, moved, stayed, delivered_waiting}``; the caller commits."""
    from raincli_server import identity
    from raincli_server.presence import valid_agent_name

    machine = session.get(Agent, machine_id) if machine_id is not None else None
    if session.get(Membership, (team_id, user.id)) is None or machine is None or machine.team_id != team_id \
            or machine.owner_user_id != user.id or machine.revoked_at is not None:
        raise _inbox_invalid(machine if machine is not None and machine.owner_user_id == user.id else None)
    if not isinstance(agent_name, str) or not valid_agent_name(agent_name) or agent_name.startswith("-") \
            or (check_live and not _accepted_inbox_name(session, machine, agent_name)):  # operator: check_live=False
        raise _inbox_invalid(machine)
    row = _lock_inbox_row(session, user.id, team_id, write=True)
    if row is not None and (row.agent_id, row.agent_name) == (machine.id, agent_name):
        return {"inbox": inbox_json(session, row), "moved": 0, "stayed": 0, "delivered_waiting": 0}
    previous = None
    if row is None:
        row = PersonInbox(user_id=user.id, team_id=team_id, agent_id=machine.id, agent_name=agent_name, set_via=via)
        session.add(row)
    else:
        previous = PersonInbox(user_id=user.id, team_id=team_id, agent_id=row.agent_id, agent_name=row.agent_name,
                               set_via=row.set_via)
        row.agent_id, row.agent_name, row.set_via, row.set_at = machine.id, agent_name, via, identity.now()
    session.flush()
    target, route = _inbox_target(session, row, user)
    moved = stayed = delivered = 0
    if previous is not None:
        where = _previous_inbox_messages(session, team_id, previous)
        stayed = session.scalar(select(func.count()).select_from(Message).where(  # picked up, still in progress
            where, Message.acked_at.is_not(None), Message.delivery_state.in_(("received", "held")))) or 0
        moved = _retarget(session, where, target, route)
    if deliver_waiting:
        delivered = _retarget(session, waiting_messages(session, user, team_id), target, route)
    session.flush()
    return {"inbox": inbox_json(session, row), "moved": moved, "stayed": stayed, "delivered_waiting": delivered}


def clear_person_inbox(session: Session, user: User, team_id: uuid.UUID, *, via: str | None = None) -> dict:
    """C6.4: no inbox. Unacked messages at the old inbox go back to waiting for the person in their app."""
    row = _lock_inbox_row(session, user.id, team_id, write=True)
    if row is None:
        return {"inbox": None, "moved": 0, "stayed": 0, "delivered_waiting": 0}
    moved = _retarget(session, _previous_inbox_messages(session, team_id, row), Endpoint("person", user=user),
                      "person")
    session.delete(row)
    session.flush()
    return {"inbox": None, "moved": moved, "stayed": 0, "delivered_waiting": 0}


def clear_inboxes_on(session: Session, machine: Agent) -> None:
    """C13 M5d: a machine revoked (or its owner removed from the team) is nobody's inbox any more; their unacked
    messages there go back to waiting, in the same transaction."""
    for row in list(session.scalars(select(PersonInbox).where(PersonInbox.agent_id == machine.id))):
        user = session.get(User, row.user_id)
        if user is not None:
            clear_person_inbox(session, user, row.team_id)


def inbox_candidates(session: Session, user: User, team_id: uuid.UUID) -> list[dict]:
    """The agents ``user`` may pick in the team (INV §3.4 filtered to C1.1): ``[{machine, agent, live, suggested}]``,
    where ``suggested`` is a machine's own inbox agent."""
    from raincli_server import identity
    from raincli_server.models import KnownAgent

    cutoff = identity.now() - timedelta(days=routing.KNOWN_DAYS)
    out, seen = [], set()
    machines = session.scalars(select(Agent).where(Agent.team_id == team_id, Agent.owner_user_id == user.id,
                                                   Agent.revoked_at.is_(None)).order_by(Agent.handle))
    for machine in machines:
        names = [r.name for r in session.scalars(select(MachineAgent).where(MachineAgent.agent_id == machine.id))]
        names += [k.name for k in session.scalars(select(KnownAgent).where(
            KnownAgent.agent_id == machine.id, KnownAgent.last_seen_at > cutoff))]
        role = session.scalar(select(MachineAgent.name).where(MachineAgent.agent_id == machine.id,
                                                              MachineAgent.role == "inbox"))
        for name in sorted(set(names)):
            if name.startswith("-") or (machine.id, name) in seen or not _accepted_inbox_name(session, machine, name):
                continue
            seen.add((machine.id, name))
            out.append({"machine": machine.handle, "agent": name, "live": live_agent(session, machine, name),
                        "suggested": name == role})
    return out
