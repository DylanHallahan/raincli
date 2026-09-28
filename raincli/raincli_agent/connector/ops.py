"""Operator commands on the local queue: approve, reject, trust, resubmit,
dismiss, and the section 10 escalate / escalation-done."""

import hashlib
import uuid

from ..errors import EXIT_CONFLICT
from . import queue as q
from .config import HANDLE_RE


class StateConflict(q.QueueError):
    exit_code = EXIT_CONFLICT


def _require(record, allowed, action):
    if record["state"] not in allowed:
        kind = "escalation" if "message_id" in record else "message"
        raise StateConflict(f"cannot {action} {kind} {record['id']}: it is {record['state']}")


def approve(queue, message_id):
    with queue.lock():
        record = queue.get(message_id)
        _require(record, q.PENDING, "approve")
        if record.get("hold_reason") == "sender_blocked":
            raise StateConflict(f"cannot approve message {record['id']}: the sender is in blocked_senders")
        record["approved"] = True
        if record["state"] == q.HELD and record["hold_reason"] == "approval_required":
            q.Queue.transition(record, q.RECEIVED, detail="approved by operator")
        queue.save(record)
        return record


def reject(queue, message_id):
    with queue.lock():
        record = queue.get(message_id)
        _require(record, q.PENDING, "reject")
        q.Queue.transition(record, q.REJECTED, detail="declined by recipient operator")
        queue.save(record)
        return record


def trust(queue, handle):
    if not HANDLE_RE.match(handle or ""):
        raise q.QueueError(f"not a valid agent handle: {handle[:40]!r}")
    with queue.lock():
        return queue.add_trusted(handle)


def _escalation_or_none(queue, some_id):
    """Resolve an id that may name an escalation instead of a message."""
    esc = queue.load_escalation(some_id)
    if esc is not None and queue.load(some_id) is None:
        return esc
    return None


def resubmit(queue, message_id):
    """Explicitly allow one more submission of an uncertain message or escalation."""
    with queue.lock():
        esc = _escalation_or_none(queue, message_id)
        if esc is not None:
            _require(esc, (q.UNCERTAIN,), "resubmit")
            esc["resubmits"] = esc.get("resubmits", 0) + 1
            q.Queue.transition(esc, q.ESC_PENDING, detail="resubmit requested by operator")
            queue.save_escalation(esc)
            return esc
        record = queue.get(message_id)
        _require(record, (q.UNCERTAIN,), "resubmit")
        record["approved"] = True  # the operator chose to deliver it
        record["resubmits"] = record.get("resubmits", 0) + 1
        q.Queue.transition(record, q.RECEIVED, detail="resubmit requested by operator")
        queue.save(record)
        return record


def dismiss(queue, message_id):
    with queue.lock():
        esc = _escalation_or_none(queue, message_id)
        if esc is not None:
            _require(esc, (q.UNCERTAIN,), "dismiss")
            q.Queue.transition(esc, q.DISMISSED, detail="dismissed by operator")
            queue.save_escalation(esc)
            return esc
        record = queue.get(message_id)
        _require(record, (q.UNCERTAIN, q.ATTACHMENT_PENDING), "dismiss")
        q.Queue.transition(record, q.DISMISSED, detail="dismissed by operator")
        queue.save(record)
        return record


def escalation_id(message_id, body):
    """Default escalation id: uuid5 of MSG_ID + sha256(body), so a retried
    ``connector escalate`` never creates a duplicate."""
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, q.normalize_id(message_id) + digest))


def escalate(queue, config, message_id, body, esc_id=None):
    """Record a durable ``pending`` escalation. Returns ``(record, created)``."""
    if config.mode != "inbox":
        raise q.QueueError('connector escalate requires "mode": "inbox" in the connector config')
    if config.escalation is None:
        raise q.QueueError("connector escalate requires an \"escalation\" mapping in the connector config")
    with queue.lock():
        message = queue.get(message_id)  # must be in the local queue
        # Section 12.2: only messages the inbox agent could actually have seen.
        _require(message, (q.SUBMITTED, q.UNCERTAIN), "escalate")
        if esc_id:
            esc_id = q.normalize_id(esc_id)
            if queue.load(esc_id) is not None or queue.load_escalation(esc_id) is not None:
                raise StateConflict(f"--id {esc_id} already exists as a message or escalation id")
        else:
            esc_id = escalation_id(message["id"], body)
        existing = queue.load_escalation(esc_id)
        if existing is not None:
            if existing["message_id"] != message["id"] or existing["body"] != body:
                raise StateConflict(f"escalation {esc_id} already exists with different content")
            return existing, False
        record = {"version": 1, "id": esc_id, "message_id": message["id"], "sender": message["sender"],
                  "body": body, "attempts": 0, "created_at": q.now_iso(),
                  "notify_attempted_at": None, "notified_at": None}
        q.Queue.transition(record, q.ESC_PENDING, detail="escalation recorded")
        queue.save_escalation(record)
        return record, True


def escalation_done(queue, esc_id):
    with queue.lock():
        esc = queue.load_escalation(esc_id)
        if esc is None:
            raise q.QueueError(f"escalation {q.normalize_id(esc_id)} is not in the local queue")
        _require(esc, (q.ESC_PENDING, q.SUBMITTED, q.UNCERTAIN), "mark done")
        q.Queue.transition(esc, q.ESC_DONE, detail="resolved by operator")
        queue.save_escalation(esc)
        return esc
