"""Durable per-message queue: one JSON file per message, written atomically."""

import contextlib
import fcntl
import json
import os
import time
import uuid

from ..errors import ConfigError, RainError
from ..fsutil import atomic_write_json, ensure_private_dir

# Local states. "held" carries a hold_reason; "submitting" is only ever seen
# on disk if the process died mid-submission, and becomes submission_uncertain.
ATTACHMENT_PENDING = "attachment_pending"  # stored, attachments not yet durable; unacked
RECEIVED = "received"
HELD = "held"
SUBMITTING = "submitting"
SUBMITTED = "submitted"
UNCERTAIN = "submission_uncertain"
REJECTED = "rejected"
DISMISSED = "dismissed"
STATES = (ATTACHMENT_PENDING, RECEIVED, HELD, SUBMITTING, SUBMITTED, UNCERTAIN, REJECTED, DISMISSED)
PENDING = (RECEIVED, HELD)

# Escalation states (protocol section 10). Escalations are local only: the
# server never sees them.
ESC_PENDING = "pending"
ESC_DONE = "done"
ESC_STATES = (ESC_PENDING, SUBMITTING, SUBMITTED, UNCERTAIN, ESC_DONE, DISMISSED)


class QueueError(RainError):
    pass


class ConnectorBusy(RainError):
    pass


def normalize_id(value):
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise QueueError(f"not a valid message id: {str(value)[:60]!r}") from None


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Queue:
    def __init__(self, state_dir):
        # Resolved once (section 12.4): a symlinked ancestor such as a stowed
        # ~/.local is trusted; only what we create below it must be link-free.
        self.state_dir = os.path.realpath(os.path.abspath(state_dir))
        self.messages_dir = os.path.join(self.state_dir, "messages")
        self.escalations_dir = os.path.join(self.state_dir, "escalations")
        ensure_private_dir(self.state_dir)
        ensure_private_dir(self.messages_dir)
        ensure_private_dir(self.escalations_dir)
        self._run_lock_fd = None

    # -- locking ---------------------------------------------------------

    @contextlib.contextmanager
    def lock(self):
        """Short exclusive lock around read-modify-write of queue files."""
        fd = os.open(os.path.join(self.state_dir, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def acquire_run_lock(self):
        """Held for the whole life of ``connector run``: one runner per queue."""
        fd = os.open(os.path.join(self.state_dir, "run.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ConnectorBusy(f"another connector is already running on {self.state_dir}") from None
        self._run_lock_fd = fd

    def release_run_lock(self):
        if self._run_lock_fd is not None:
            fcntl.flock(self._run_lock_fd, fcntl.LOCK_UN)
            os.close(self._run_lock_fd)
            self._run_lock_fd = None

    # -- records ---------------------------------------------------------

    def path(self, message_id):
        return os.path.join(self.messages_dir, normalize_id(message_id) + ".json")

    def exists(self, message_id):
        return os.path.exists(self.path(message_id))

    def load(self, message_id):
        try:
            with open(self.path(message_id), encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None

    def get(self, message_id):
        record = self.load(message_id)
        if record is None:
            raise QueueError(f"message {normalize_id(message_id)} is not in the local queue")
        return record

    def save(self, record):
        """Atomic, fsynced write. Returns only once the record is durable."""
        record["updated_at"] = now_iso()
        atomic_write_json(self.path(record["id"]), record)

    def all(self):
        records = []
        for name in os.listdir(self.messages_dir):
            if name.endswith(".json") and not name.startswith("."):
                with open(os.path.join(self.messages_dir, name), encoding="utf-8") as fh:
                    records.append(json.load(fh))
        records.sort(key=lambda r: (r.get("seq") or 0, r["id"]))
        return records

    # -- escalations -----------------------------------------------------

    def escalation_path(self, esc_id):
        return os.path.join(self.escalations_dir, normalize_id(esc_id) + ".json")

    def load_escalation(self, esc_id):
        try:
            with open(self.escalation_path(esc_id), encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None

    def save_escalation(self, record):
        record["updated_at"] = now_iso()
        atomic_write_json(self.escalation_path(record["id"]), record)

    def escalations(self):
        records = []
        for name in os.listdir(self.escalations_dir):
            if name.endswith(".json") and not name.startswith("."):
                with open(os.path.join(self.escalations_dir, name), encoding="utf-8") as fh:
                    records.append(json.load(fh))
        records.sort(key=lambda r: (r.get("created_at", ""), r["id"]))
        return records

    @staticmethod
    def transition(record, state, reason=None, detail=""):
        if state not in STATES and state not in ESC_STATES:
            raise ValueError(state)
        record["state"] = state
        record["hold_reason"] = reason if state in (HELD, ESC_PENDING) else None
        if state not in (HELD, ESC_PENDING):
            record["hold_detail"] = ""
        record["detail"] = detail
        record.setdefault("history", []).append(
            {"at": now_iso(), "state": state, "reason": record["hold_reason"], "detail": detail})

    # -- cursor and policy ----------------------------------------------

    def _read_json(self, name, default):
        try:
            with open(os.path.join(self.state_dir, name), encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return default
        except ValueError:
            raise ConfigError(f"{os.path.join(self.state_dir, name)} is corrupt") from None

    def cursor(self):
        return int(self._read_json("cursor.json", {"after": 0}).get("after", 0))

    def set_cursor(self, after):
        atomic_write_json(os.path.join(self.state_dir, "cursor.json"), {"after": int(after)})

    MAX_SKIPPED = 50

    def record_skipped(self, message, problem):
        """Keep a bounded log of malformed server messages that were skipped."""
        data = self._read_json("skipped.json", {"count": 0, "recent": []})
        seq = message.get("seq") if isinstance(message, dict) else None
        mid = message.get("id") if isinstance(message, dict) else None
        data["count"] = int(data.get("count", 0)) + 1
        data["recent"] = (data.get("recent", []) + [{
            "at": now_iso(), "problem": problem,
            "seq": seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
            "id": str(mid)[:80] if mid is not None else None}])[-self.MAX_SKIPPED:]
        atomic_write_json(os.path.join(self.state_dir, "skipped.json"), data)

    def skipped(self):
        return self._read_json("skipped.json", {"count": 0, "recent": []})

    def trusted(self):
        return list(self._read_json("policy.json", {"trusted_senders": []}).get("trusted_senders", []))

    def add_trusted(self, handle):
        trusted = self.trusted()
        if handle not in trusted:
            trusted.append(handle)
            atomic_write_json(os.path.join(self.state_dir, "policy.json"),
                              {"trusted_senders": sorted(trusted)})
        return trusted
