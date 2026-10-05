"""The connector loop (protocol sections 5, 8 and 10).

Per message: store durably -> ack -> policy -> readiness -> submit, reporting a
delivery event to the server whenever the reportable state changes. In inbox
mode the loop also delivers pending escalations to the main session.
"""

import json
import os
import random
import re
import sys
import time

from .. import attachments as att
from ..config import standard_config_path
from .config import HANDLE_RE
from ..errors import ApiError, ConfigError, RainError, Unauthorized
from ..text import escape_line, escape_text
from . import queue as q
from ..runtime import sessions
from .herdr import READY_STATUSES, HerdrError, HerdrRejected, HerdrTimeout

# Protocol §11.1 framing, as replaced by §16.12 C17: one module builds every prompt.
from .framing import (ATTACHMENTS_LABEL, frame, quote, reply_command, wrap_escalation,  # noqa: F401
                      wrap_message)

INBOX_GUIDANCE = (
    "[Inbox for {handle}: answer, ask follow-ups and continue the conversation with the reply command. "
    "Share only from: {context}. No need to acknowledge receipt. "
    "Escalate what you can't handle: raincli connector escalate --config {config} {id} --body-file -]\n")

NOTIFY_TITLE = "RainCLI escalation"


def inbox_guidance(message_id, handle, shareable_context, config_path):
    """The section 10 guidance block. Paths are quoted; files are never read.
    The reply command itself is in the message header."""
    context = ", ".join(quote(p) for p in shareable_context) or "none configured"
    return INBOX_GUIDANCE.format(handle=escape_line(handle), id=message_id, context=context,
                                 config=quote(config_path))


def notification_body(sender, summary):
    first = " ".join(summary.split())[:80]
    return escape_line(f"{sender}: {first}")


def frame_record(record, identity, guidance="", agent_config=None, held_seconds=None):
    """The C17 frame for a stored message: sender endpoint, ``from_agent``, the
    target (a named agent, or the inbox) and this machine's handle."""
    message = record["message"]
    origin = message.get("from_endpoint") or {}
    person = origin if isinstance(origin, dict) and "person" in origin else None
    person = {"display_name": person.get("display_name"), "email": person.get("person")} if person else None
    team = message.get("team") or identity.get("team", "")
    return wrap_message(record["id"], record["sender"], team, message["body"],
                        record.get("attachments_local") or (), guidance, agent_config,
                        machine=identity.get("handle", ""), target=record.get("target"), person=person,
                        from_agent=message.get("from_agent"), held_seconds=held_seconds)


EMAIL_SENDER_RE = re.compile(r"^@[^@\s/\\]{1,64}@[^@\s/\\]{1,190}$")
EXPIRE_OFFLINE = 14 * 86400  # §16.12 C12: an agent message held offline this long is rejected


def message_target(message):
    """The named agent a message is addressed to (§16.1), None for the machine
    endpoint, or False when the name is not a valid agent name."""
    to = message.get("to_endpoint")
    if not isinstance(to, dict) or to.get("agent") is None:
        return None
    name = to["agent"]
    if (not isinstance(name, str) or not 1 <= len(name) <= 64
            or sessions.normalize_name(name, "") != name):
        return False
    return name


def message_problem(message):
    """Why a server message can't be queued, or None. Checked before any path use."""
    if not isinstance(message, dict):
        return "not an object"
    try:
        q.normalize_id(message.get("id"))
    except q.QueueError:
        return "bad id"
    sender = message.get("from")
    origin = message.get("from_endpoint")
    if isinstance(origin, dict) and "person" in origin:
        # §16.1: a person sender is "@email" (an email is never a handle or an option).
        if not (isinstance(sender, str) and EMAIL_SENDER_RE.fullmatch(sender)
                and sender[1:].lower() == str(origin.get("person", "")).lower()):
            return "bad sender"
    elif not isinstance(sender, str) or not HANDLE_RE.match(sender):
        return "bad sender"  # section 12.3: the handle grammar, never e.g. "-evil"
    if message_target(message) is False:
        return "bad target"
    if not isinstance(message.get("body"), str):
        return "bad body"
    seq = message.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return "bad seq"
    if message.get("attachments") is not None and not isinstance(message["attachments"], list):
        return "bad attachments"
    return None


def reportable(record):
    """The (state, detail) the server should show for this record, or None."""
    state = record["state"]
    if state == q.HELD:
        return (q.HELD, record["hold_reason"])
    if state in (q.SUBMITTED, q.UNCERTAIN, q.REJECTED):
        return (state, record.get("detail") or "")
    if state == q.HANDED_OVER or state == q.AGENT_HANDED_OVER:
        return (q.HELD, "next_turn")  # waiting for the hook session's next turn (14.4)
    if state == q.AGENT_HELD:
        return (q.HELD, record["hold_reason"])
    return None  # received, submitting and dismissed have no server event


class Connector:
    def __init__(self, config, api, herdr, queue, *, identity=None, log=None,
                 sleep=time.sleep, clock=time.time, agent_config_path=None, sessions_state=None):
        self.config = config
        # The runtime state directory holding hook sessions (next-turn inbox only).
        self.sessions_state = sessions_state
        # The identity the session must reply with; None when it is the default path.
        path = agent_config_path or config.agent_config
        self.prompt_agent_config = (os.path.abspath(path) if path and
                                    os.path.abspath(path) != standard_config_path() else None)
        self.api = api
        self.herdr = herdr
        self.queue = queue
        self.identity = identity  # {"handle": ..., "team": ...}
        self._log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self._sleep = sleep
        self._clock = clock
        # Repeated agent_not_ready (Herdr's pane is not running the agent): back off (review H4).
        self._not_ready_count, self._not_ready_until = 0, 0.0
        self.started = False
        self.stop_requested = lambda: False  # set by a supervisor (runtime mode)

    def log(self, text):
        self._log("raincli connector: " + escape_line(text))

    # -- lifecycle -------------------------------------------------------

    def start(self):
        if self.config.inbox_hook and not self.sessions_state:
            raise ConfigError("a hook-session inbox is delivered by `raincli runtime run`, which owns the sessions")
        if self.identity is None:
            me = self.api.me()
            self.identity = {"handle": me["agent"]["handle"], "team": me["agent"]["team"]["slug"]}
        self.recover()
        self.started = True

    def recover(self):
        """A record still ``submitting`` on disk means we died mid-submission."""
        with self.queue.lock():
            for record in self.queue.all():
                if record.get("handover_key") and not self.sessions_state:
                    continue  # settled once a runtime that owns the sessions runs this queue
                if record["state"] == q.AGENT_SUBMITTING and record.get("handover_key"):
                    self._recover_handover(record)
                elif record["state"] == q.AGENT_SUBMITTING:
                    q.Queue.transition(record, q.UNCERTAIN,
                                       detail="connector restarted during submission; not resubmitted")
                    self.queue.save(record)
                    self.log(f"message {record['id']} is submission_uncertain (restart during submission)")
                elif record["state"] == q.SUBMITTING and record.get("handover_key"):
                    self._recover_handover(record)
                elif record["state"] == q.HANDED_OVER:
                    target = self._inbox_target()[0] if self.config.inbox_hook else None
                    self._reconcile_handover(record, target)
                elif record["state"] == q.SUBMITTING:
                    q.Queue.transition(record, q.UNCERTAIN,
                                       detail="connector restarted during submission; not resubmitted")
                    self.queue.save(record)
                    self.log(f"message {record['id']} is submission_uncertain (restart during submission)")
            for esc in self.queue.escalations():
                if esc["state"] == q.SUBMITTING:
                    q.Queue.transition(esc, q.UNCERTAIN,
                                       detail="connector restarted during submission; not resubmitted")
                    self.queue.save_escalation(esc)
                    self.log(f"escalation {esc['id']} is submission_uncertain (restart during submission)")

    def trusted(self):
        return set(self.config.trusted_senders) | set(self.queue.trusted())

    def policy_hold(self, record, trusted):
        """The policy hold reason for a message, or None if it may be delivered."""
        sender = record["sender"]
        if sender in self.config.blocked_senders:
            return "sender_blocked"  # whatever the trust mode, and even if approved
        if self.config.trust_mode == "team":
            return None  # the server guarantees every sender is in our team
        if sender in trusted or record["approved"]:
            return None
        return "approval_required"

    # -- one iteration ---------------------------------------------------

    def run_once(self, wait=0):
        if not self.started:
            self.start()
        self.retry_acks()
        self.poll(wait)
        self.process()
        self.process_named()
        self.process_escalations()
        self.sync_events()

    def poll(self, wait=0):
        after = self.queue.cursor()
        while True:
            messages, cursor = self.api.inbox(after=after, wait=wait, limit=100)
            for message in messages:
                problem = message_problem(message)
                if problem:
                    # Skip and log it (bounded) rather than stalling the queue for good.
                    self.log(f"skipping malformed message from server: {problem}")
                    self.queue.record_skipped(message, problem)
                    continue
                self.ingest(message)
            if cursor is not None and int(cursor) > after:
                # Only advance past messages that are durably stored locally.
                self.queue.set_cursor(cursor)
                after = int(cursor)
            if len(messages) < 100:
                return
            wait = 0

    def ingest(self, message):
        """Store the message durably, then (and only then) ack it."""
        message_id = q.normalize_id(message["id"])
        with self.queue.lock():
            record = self.queue.load(message_id)
            target = message_target(message)
            if record is None and target is None and not self.config.has_inbox:
                # A machine with no inbox mapping (machine mode, §16.7): messages to the
                # machine endpoint stay stored on the server, unacknowledged.
                return
            if record is None:
                record = {"version": 1, "id": message_id, "seq": message.get("seq"),
                          "sender": message.get("from", ""), "message": message,
                          "approved": False, "acked": False, "reported": None,
                          "attempts": 0, "received_at": q.now_iso()}
                record["attachments_local"] = []
                record["attachment_attempts"] = 0
                record["attachment_retry_at"] = 0
                if target:
                    # §16.12 C1: only agent_* states until it settles; attachments are a flag.
                    record["target"] = target
                    record["attachments_pending"] = bool(message.get("attachments"))
                    q.Queue.transition(record, q.AGENT_RECEIVED)
                else:
                    q.Queue.transition(record, q.ATTACHMENT_PENDING if message.get("attachments") else q.RECEIVED)
                self.queue.save(record)  # fsynced before the ack below
                self.log(f"stored message {message_id} from {record['sender']}")
            if not record["acked"] and self._ensure_local(record):
                self._ack(record)

    def _ensure_local(self, record):
        """True once the message and all its attachments are durable locally."""
        if record["state"] == q.DISMISSED:
            return False
        if record.get("target"):
            if not record.get("attachments_pending"):
                return True
        elif record["state"] != q.ATTACHMENT_PENDING:
            return True
        if self._clock() < record.get("attachment_retry_at", 0):
            return False
        try:
            record["attachments_local"] = self._fetch_attachments(record)
        except (RainError, OSError) as exc:
            n = record.get("attachment_attempts", 0) + 1
            record["attachment_attempts"] = n
            record["attachment_retry_at"] = self._clock() + min(300.0, 2.0 ** n) * random.uniform(0.5, 1.0)
            record["detail"] = f"attachment fetch failed: {exc}"[:500]
            self.queue.save(record)
            self.log(f"message {record['id']} stays unacked ({record['detail']})")
            return False
        if record.get("target"):
            record["attachments_pending"] = False
            record["detail"] = "attachments stored"
        else:
            q.Queue.transition(record, q.RECEIVED, detail="attachments stored")
        self.queue.save(record)
        return True

    def _fetch_attachments(self, record):
        """Download, verify and fsync every attachment into state_dir/attachments/<mid>/."""
        items = record["message"].get("attachments") or []
        if len(items) > att.MAX_COUNT:
            raise att.AttachmentError("too many attachments")
        names = [att.check_metadata(meta) for meta in items]
        if len({n.lower() for n in names}) != len(names):
            raise att.AttachmentError("duplicate attachment names")
        # state_dir is realpath'd once (trusted base); attachments/<mid>/ are ours.
        directory = att.prepare_dir(self.queue.state_dir, "attachments", record["id"])
        local = []
        for meta in items:
            target = os.path.join(directory, meta["filename"])
            if not att.existing_matches(target, meta["sha256"]):
                data, header_sha = self.api.download_attachment(record["id"], meta["id"])
                att.verify_download(meta, data, header_sha)
                att.write_exclusive(directory, meta["filename"], data, meta["sha256"])
            local.append({"filename": meta["filename"], "path": target,
                          "size": meta["size"], "sha256": meta["sha256"]})
        return local

    def _ack(self, record):
        try:
            self.api.ack(record["id"])
        except Unauthorized:
            raise
        except ApiError as exc:
            self.log(f"ack of {record['id']} failed, will retry: {exc}")
            return
        record["acked"] = True
        self.queue.save(record)

    def retry_acks(self):
        with self.queue.lock():
            for record in self.queue.all():
                if not record["acked"] and self._ensure_local(record):
                    self._ack(record)

    def readiness(self, target=None):
        """Return a hold reason, or None when the target is ready for a prompt.

        ``target`` is the inbox mapping (the config) or the escalation mapping;
        both carry herdr_agent, expect_pane_id and expect_cwd."""
        cfg = target or self.config
        name = cfg.herdr_agent
        try:
            info = self.herdr.get_agent(name)
        except HerdrError as exc:
            self.log(f"herdr agent get failed: {exc}")
            return "offline", f"herdr agent get {name} failed: {exc}"[:300]
        if info is None:
            return "offline", f"herdr agent {name} not found"
        if cfg.expect_pane_id and info.pane_id != cfg.expect_pane_id:
            return "target_mismatch", f"pane {info.pane_id or '?'} != expected {cfg.expect_pane_id}"
        if cfg.expect_cwd and os.path.normpath(info.cwd or "/nonexistent") != os.path.normpath(cfg.expect_cwd):
            return "target_mismatch", f"cwd {info.cwd or '?'} != expected {cfg.expect_cwd}"
        if info.status in READY_STATUSES:
            return None, ""
        detail = f"herdr reports {name} status {info.status}"
        if info.status == "blocked":
            return "blocked", detail
        return "busy", detail  # working, unknown or anything unexpected

    def process(self):
        """Apply policy and readiness; submit at most one message per iteration.

        The queue lock is released while ``herdr.prompt`` runs (R2-L7), so
        operator commands and ``connector escalate`` never wait on a prompt. The
        durable ``submitting`` record and run.lock guard the state meanwhile."""
        if not self.config.has_inbox:
            return  # machine mode: only named agents are delivered (§16.7)
        if self.config.inbox_hook:
            return self.process_next_turn()
        chosen = None
        with self.queue.lock():
            trusted = self.trusted()
            ready_reason, checked, submitted = None, False, False
            for record in self.queue.all():
                if not record["acked"] or record["state"] not in q.PENDING:
                    continue
                if self._policy_held(record, trusted):
                    continue
                if submitted:
                    self._hold(record, "busy", "another message was submitted this iteration")
                    continue
                if not checked:
                    (ready_reason, ready_detail), checked = self.readiness(), True
                    if not ready_reason and self._clock() < self._not_ready_until:
                        ready_reason = "offline"
                        ready_detail = (f"herdr reported agent_not_ready {self._not_ready_count} times; "
                                        "retrying after a backoff")
                if ready_reason:
                    self._hold(record, ready_reason, ready_detail)
                    continue
                if self.stop_requested():
                    break  # never start a submission once asked to stop; it stays queued
                fits = getattr(self.herdr, "command_line_fits", None)
                if fits is not None and not fits(self.config.herdr_agent, self._prompt_text(record)):
                    # Held, never truncated: Windows caps a command line (checked before "submitting").
                    self._hold(record, "too_large_for_command_line",
                               "the framed prompt would exceed the command-line bound; it is never truncated")
                    continue
                chosen = (record["id"], self._begin_submit(record))
                submitted = True
        if chosen is None:
            return
        outcome = self._prompt(self.config.herdr_agent, chosen[1])  # no lock held
        self._note_not_ready(outcome)
        with self.queue.lock():
            record = self.queue.get(chosen[0])
            self._finish(record, outcome, self.config.herdr_agent, q.HELD)
            self.queue.save(record)
            self.log(f"message {record['id']} is {record['state']}")

    def _policy_held(self, record, trusted):
        hold = self.policy_hold(record, trusted)
        if hold:
            detail = (f"sender {record['sender']} is in blocked_senders" if hold == "sender_blocked"
                      else f"sender {record['sender']} is not trusted (trust_mode list)")
            self._hold(record, hold, detail)
        return bool(hold)

    # -- next-turn inbox (protocol 14.4, 14.7) ------------------------------

    def _inbox_target(self):
        """(key of the single live mapped hook session or None, hold reason, detail)."""
        kind, name = self.config.inbox_hook
        live = sessions.live_sessions(self.sessions_state, kind, name, now=self._clock())
        if len(live) == 1:
            return live[0]["key"], None, ""
        if not live:
            return None, "offline", f"no live {kind} hook session named {name}"
        return None, "target_ambiguous", f"{len(live)} live {kind} hook sessions are named {name}"

    def _recover_handover(self, record):
        """Died between marking ``submitting`` and recording ``handed_over``."""
        key, mid = record["handover_key"], record["id"]
        if sessions.handover_state(self.sessions_state, key, mid) == "missing":
            # No file was ever written, so nothing can have reached the session.
            q.Queue.transition(record, q.received_state(record),
                               detail="restart before handover; nothing was handed over")
            record.pop("handover_key", None)
        else:
            q.Queue.transition(record, q.AGENT_HANDED_OVER if record.get("target") else q.HANDED_OVER,
                               detail="handed over (recovered after restart)")
        self.queue.save(record)
        self.log(f"message {mid} is {record['state']} (restart during handover)")

    def _reconcile_handover(self, record, target_key, reason="offline", detail=""):
        """Settle a handed-over message from the files. Never re-emits anything.

        A claim without a receipt becomes uncertain only CLAIM_GRACE seconds after
        this connector first saw it, so a hook that is still emitting (it exits
        within about 2 s) is never overtaken, whatever the file timestamps say."""
        key, mid = record["handover_key"], record["id"]
        state = sessions.handover_state(self.sessions_state, key, mid)
        if state == "submitted":
            label = record.get("target") or self.config.target_label
            q.Queue.transition(record, q.SUBMITTED, detail=f"emitted to {label} (next turn)")
        elif state == "missing":
            q.Queue.transition(record, q.UNCERTAIN, detail="the handover file is missing; not re-emitted")
        elif state == "claimed":
            seen = record.get("claim_seen_at")
            if seen is None:
                record["claim_seen_at"] = self._clock()
                self.queue.save(record)
                return False
            if self._clock() - seen < sessions.CLAIM_GRACE:
                return False
            q.Queue.transition(record, q.UNCERTAIN, detail="claimed by the hook without a receipt; not re-emitted")
        else:  # pending
            if record.pop("claim_seen_at", None) is not None:
                # Put back by a hook: a later claim starts a fresh grace (review 2, O7).
                self.queue.save(record)
            if key == target_key:
                return False  # still pending for the live session
            # The session ended, its process exited, or it is no longer the only
            # one: take the file back unless the hook claims it first (the rename
            # arbitrates).
            if not sessions.reclaim(self.sessions_state, key, mid):
                return False
            q.Queue.transition(record, q.held_state(record), reason=reason)
            record["hold_detail"] = detail or "the session ended before its next turn; reclaimed"
            self._held_since(record, reason)
        sessions.settle(self.sessions_state, key, mid)
        record.pop("claim_seen_at", None)
        if record["state"] in q.HOLD_STATES:
            record.pop("handover_key", None)
        self.queue.save(record)
        self.log(f"message {mid} is {record['state']}")
        return True

    def _undo_handover(self, record, key, exc):
        """The handover file could not be written (for example an unsafe inbox
        directory). Hold the message unless the file could already be emitted."""
        mid = record["id"]
        claimed = any(os.path.lexists(sessions._inbox_file(self.sessions_state, key, mid, x))
                      for x in (".claimed", ".receipt"))
        if not claimed and (sessions.reclaim(self.sessions_state, key, mid)
                            or not os.path.lexists(sessions._inbox_file(self.sessions_state, key, mid))):
            record.pop("handover_key", None)
            record["handover_retry_at"] = self._clock() + 30
            self._hold(record, "offline", f"cannot hand over to the session: {type(exc).__name__}")
        else:
            q.Queue.transition(record, q.UNCERTAIN, detail=f"handover failed after writing: {type(exc).__name__}")
            self.queue.save(record)
        self.log(f"message {mid} is {record['state']} (handover failed: {type(exc).__name__})")

    def process_next_turn(self):
        """Hand queued messages to the single live mapped hook session.

        Nothing interrupts the session: each message is written as a framed file
        that the session's hook emits at its next start or prompt."""
        with self.queue.lock():
            target_key, reason, detail = self._inbox_target()
            for record in self.queue.all():
                if record["state"] == q.HANDED_OVER:
                    self._reconcile_handover(record, target_key, reason=reason or "offline", detail=detail)
            trusted = self.trusted()
            for record in self.queue.all():
                if not record["acked"] or record["state"] not in q.PENDING:
                    continue
                if self._policy_held(record, trusted):
                    continue
                if target_key is None:
                    self._hold(record, reason, detail)
                    continue
                if self.stop_requested():
                    break
                if self._clock() < record.get("handover_retry_at", 0):
                    continue  # a recent handover failure: retry later without new history
                try:
                    sessions.inbox_dir(self.sessions_state, target_key, create=True)
                except (OSError, ConfigError) as exc:
                    # Checked before "submitting": a bad inbox directory only holds.
                    self._hold(record, "offline", f"cannot hand over to the session: {type(exc).__name__}")
                    continue
                text = self._prompt_text(record)
                kind = self.config.inbox_hook[0]
                if not sessions.fits_one_turn(text, kind):
                    cap = sessions.char_cap(kind)
                    bound = f"{cap} characters" if cap else f"{sessions.CLAIM_CAP_BYTES} bytes"
                    self._hold(record, "too_large_for_hook", f"framed message exceeds the per-turn bound ({bound})")
                    continue
                record["attempts"] = record.get("attempts", 0) + 1
                record["handover_key"] = target_key
                q.Queue.transition(record, q.SUBMITTING)
                self.queue.save(record)
                try:
                    sessions.hand_over(self.sessions_state, target_key, record["id"], text)
                except (OSError, ConfigError) as exc:
                    self._undo_handover(record, target_key, exc)
                    continue
                q.Queue.transition(record, q.HANDED_OVER,
                                   detail=f"handed to {self.config.target_label}; delivered at its next turn")
                self.queue.save(record)
                self.log(f"message {record['id']} is handed_over")

    # -- named agents (protocol §16.7, §16.12 C1, C12) -------------------------

    def _named_candidates(self, name, herdr_agents, hook_records):
        """(Herdr agents, live hook sessions) carrying exactly this name."""
        herdr = [a for a in herdr_agents if a.get("name") == name]
        hooks = sessions.live_sessions(self.sessions_state, None, name, now=self._clock(),
                                       records=hook_records) if self.sessions_state else []
        return herdr, hooks

    def _reconcile_named(self, record, hook_records):
        """A named-session handover: settled from the files; reclaimed and held when no
        single live session of that name is left (it is handed over again, with its
        held age, once one is)."""
        live = sessions.live_sessions(self.sessions_state, None, record["target"], now=self._clock(),
                                      records=hook_records)
        if len(live) == 1:
            return self._reconcile_handover(record, record["handover_key"])
        reason = "target_ambiguous" if live else "offline"
        return self._reconcile_handover(record, None, reason=reason,
                                        detail=f"no single live session named {record['target']}")

    def process_named(self):
        """Deliver messages addressed to a named agent on this machine (§16.7).

        The target is the Herdr agent or the live hook session with exactly that
        name; none is ``offline``, several (or one of each) ``target_ambiguous``.
        There is never a fallback and no inbox pin applies. At most one Herdr
        prompt per iteration; a next-turn handover goes to the name's box."""
        chosen = None
        with self.queue.lock():
            records = [r for r in self.queue.all() if r.get("target")]
            if not records:
                return
            hook_records = (sessions.read_sessions(self.sessions_state, now=self._clock(), drop=False)
                            if self.sessions_state else [])
            for record in records:
                if record["state"] == q.AGENT_HANDED_OVER:
                    self._reconcile_named(record, hook_records)
            trusted = self.trusted()
            herdr_agents, herdr_ok, herdr_error = None, True, ""
            prompted = False
            for record in records:
                if not record["acked"] or record["state"] not in q.AGENT_PENDING:
                    continue
                if record.get("attachments_pending"):
                    continue
                if (record.get("hold_reason") == "offline" and record.get("offline_since") is not None
                        and self._clock() - record["offline_since"] >= EXPIRE_OFFLINE):
                    q.Queue.transition(record, q.REJECTED, detail="expired_offline")
                    self.queue.save(record)
                    self.log(f"message {record['id']} to {record['target']} expired after 14 days offline")
                    continue
                if self._policy_held(record, trusted):
                    continue
                if herdr_agents is None:
                    try:
                        herdr_agents = self.herdr.list_agents()
                    except HerdrError as exc:
                        herdr_agents, herdr_ok, herdr_error = [], False, str(exc)[:200]
                name = record["target"]
                herdr, hooks = self._named_candidates(name, herdr_agents, hook_records)
                if len(herdr) + len(hooks) > 1:
                    self._hold(record, "target_ambiguous",
                               f"{len(herdr)} Herdr agent(s) and {len(hooks)} hook session(s) are named {name}")
                    continue
                if not herdr and not hooks:
                    detail = f"no live agent named {name}" + ("" if herdr_ok else f" (herdr: {herdr_error})")
                    self._hold(record, "offline", detail)
                    continue
                if self.stop_requested():
                    break
                if hooks:
                    self._hand_over_named(record, hooks[0])
                    continue
                status = herdr[0].get("status") or "unknown"
                if status == "blocked":
                    self._hold(record, "blocked", f"herdr reports {name} status blocked")
                    continue
                if status not in READY_STATUSES:
                    self._hold(record, "busy", f"herdr reports {name} status {status}")
                    continue
                if prompted:
                    self._hold(record, "busy", "another message was submitted this iteration")
                    continue
                fits = getattr(self.herdr, "command_line_fits", None)
                if fits is not None and not fits(name, self._prompt_text(record)):
                    self._hold(record, "too_large_for_command_line",
                               "the framed prompt would exceed the command-line bound; it is never truncated")
                    continue
                chosen = (record["id"], name, self._begin_submit(record))
                prompted = True
        if chosen is None:
            return
        outcome = self._prompt(chosen[1], chosen[2])  # no lock held
        with self.queue.lock():
            record = self.queue.get(chosen[0])
            self._finish(record, outcome, chosen[1], q.AGENT_HELD)
            if record["state"] == q.AGENT_HELD:
                self._held_since(record, record["hold_reason"])
            else:
                for field in ("held_since", "offline_since"):
                    record.pop(field, None)
            self.queue.save(record)
            self.log(f"message {record['id']} to {chosen[1]} is {record['state']}")

    def _hand_over_named(self, record, session):
        """Write the framed message to the name's box for the session's next turn."""
        box = sessions.name_box(record["target"])
        if self._clock() < record.get("handover_retry_at", 0):
            return
        try:
            sessions.inbox_dir(self.sessions_state, box, create=True)
        except (OSError, ConfigError) as exc:
            self._hold(record, "offline", f"cannot hand over to the session: {type(exc).__name__}")
            return
        held = self._clock() - record["held_since"] if record.get("held_since") is not None else None
        text = self._prompt_text(record, held_seconds=held)
        if not sessions.fits_one_turn(text, session["type"]):
            cap = sessions.char_cap(session["type"])
            bound = f"{cap} characters" if cap else f"{sessions.CLAIM_CAP_BYTES} bytes"
            self._hold(record, "too_large_for_hook", f"framed message exceeds the per-turn bound ({bound})")
            return
        record["attempts"] = record.get("attempts", 0) + 1
        record["handover_key"] = box
        q.Queue.transition(record, q.AGENT_SUBMITTING)
        self.queue.save(record)
        try:
            sessions.hand_over(self.sessions_state, box, record["id"], text)
        except (OSError, ConfigError) as exc:
            self._undo_handover(record, box, exc)
            return
        q.Queue.transition(record, q.AGENT_HANDED_OVER,
                           detail=f"handed to the {session['type']} session {record['target']}; at its next turn")
        for field in ("held_since", "offline_since"):
            record.pop(field, None)
        self.queue.save(record)
        self.log(f"message {record['id']} is handed_over to {record['target']}")

    def _hold(self, record, reason, detail=""):
        state = q.held_state(record)
        if record["state"] == state and record["hold_reason"] == reason:
            if record.get("hold_detail") != detail:  # local detail only; no new event
                record["hold_detail"] = detail
                self.queue.save(record)
            return
        q.Queue.transition(record, state, reason=reason)
        record["hold_detail"] = detail
        self._held_since(record, reason)
        self.queue.save(record)
        self.log(f"holding message {record['id']}: {reason} ({detail})")

    def _held_since(self, record, reason):
        """Clock times for the held age shown on a handover (C12) and offline expiry."""
        record.setdefault("held_since", self._clock())
        if reason == "offline":
            record.setdefault("offline_since", self._clock())
        else:
            record.pop("offline_since", None)

    def _begin_submit(self, record):
        """Durably mark ``submitting`` (before any input can reach the session)
        and return the prompt text."""
        record["attempts"] = record.get("attempts", 0) + 1
        q.Queue.transition(record, q.submitting_state(record))
        self.queue.save(record)
        return self._prompt_text(record)

    def _prompt_text(self, record, held_seconds=None):
        """The section 11.1 prompt; identical for Herdr and next-turn delivery."""
        guidance = ""
        if self.config.mode == "inbox" and not record.get("target"):
            guidance = inbox_guidance(record["id"], self.identity["handle"],
                                      self.config.shareable_context, self.config.path)
        return frame_record(record, self.identity, guidance, self.prompt_agent_config, held_seconds)

    def _prompt(self, target_name, text):
        """Run the prompt; return None on success or the Exception. A
        BaseException (a crash) propagates and leaves ``submitting`` on disk."""
        try:
            self.herdr.prompt(target_name, text, self.config.prompt_timeout)
        except Exception as exc:  # noqa: BLE001 - classified in _finish
            return exc
        return None

    @staticmethod
    def _finish(record, outcome, target_name, hold_state):
        if outcome is None:
            q.Queue.transition(record, q.SUBMITTED, detail=f"handed to herdr agent {target_name}")
        elif isinstance(outcome, HerdrRejected):
            # Herdr refused before sending any input: safe to keep holding.
            q.Queue.transition(record, hold_state, reason=outcome.reason)
            record["hold_detail"] = str(outcome)[:300]
        elif isinstance(outcome, HerdrTimeout):
            q.Queue.transition(record, q.UNCERTAIN, detail="herdr prompt timed out")
        else:  # any other failure leaves delivery unknown
            q.Queue.transition(record, q.UNCERTAIN, detail=f"herdr prompt failed: {outcome}"[:500])

    def process_escalations(self):
        """Deliver pending escalations to the main session (section 10).

        One visible notification per escalation, readiness and pins as in
        section 5, at most one submission per iteration, never resubmitted
        automatically."""
        target = self.config.escalation
        chosen = None
        with self.queue.lock():
            pending = [e for e in self.queue.escalations() if e["state"] == q.ESC_PENDING]
            if not pending:
                return
            if target is None:
                for esc in pending:
                    self._hold_escalation(esc, "not_configured", "no escalation mapping in the config")
                return
            ready_reason, ready_detail, checked = None, "", False
            for esc in pending:
                if target.notify and not esc.get("notify_attempted_at"):
                    esc["notify_attempted_at"] = q.now_iso()
                    try:
                        self.herdr.notify(NOTIFY_TITLE, notification_body(esc["sender"], esc["body"]))
                        esc["notified_at"] = esc["notify_attempted_at"]
                    except Exception as exc:  # noqa: BLE001 - never blocks delivery
                        esc["notify_error"] = str(exc)[:200]
                        self.log(f"notification for escalation {esc['id']} failed: {exc}")
                    self.queue.save_escalation(esc)
                if checked:
                    self._hold_escalation(esc, ready_reason or "busy", ready_detail)
                    continue
                (ready_reason, ready_detail), checked = self.readiness(target), True
                if ready_reason:
                    self._hold_escalation(esc, ready_reason, ready_detail)
                    continue
                if self.stop_requested():
                    break  # never start a submission once asked to stop; it stays pending
                fits = getattr(self.herdr, "command_line_fits", None)
                if fits is not None and not fits(target.herdr_agent, self._escalation_text(esc)):
                    self._hold_escalation(esc, "too_large_for_command_line",
                                          "the framed escalation would exceed the command-line bound; never truncated")
                    ready_reason, ready_detail, checked = None, "", False
                    continue
                chosen = (esc["id"], self._begin_escalation(esc))
                ready_reason, ready_detail = "busy", "another escalation was submitted this iteration"
        if chosen is None:
            return
        outcome = self._prompt(target.herdr_agent, chosen[1])  # no lock held (R2-L7)
        with self.queue.lock():
            esc = self.queue.load_escalation(chosen[0])
            self._finish(esc, outcome, target.herdr_agent, q.ESC_PENDING)
            self.queue.save_escalation(esc)
            self.log(f"escalation {esc['id']} is {esc['state']}")

    NOT_READY_FREE = 3  # agent_not_ready answers before backing off
    NOT_READY_MAX = 600

    def _note_not_ready(self, outcome):
        """Count consecutive agent_not_ready refusals; from the third, wait 30 s doubling
        to 10 min before prompting again (the pane is not running the agent)."""
        if isinstance(outcome, HerdrRejected) and getattr(outcome, "code", "") == "agent_not_ready":
            self._not_ready_count += 1
            if self._not_ready_count >= self.NOT_READY_FREE:
                delay = min(self.NOT_READY_MAX, 30 * 2 ** (self._not_ready_count - self.NOT_READY_FREE))
                self._not_ready_until = self._clock() + delay
                self.log(f"herdr agent_not_ready {self._not_ready_count} times; next prompt in {delay} s")
        elif outcome is None:
            self._not_ready_count, self._not_ready_until = 0, 0.0

    def _hold_escalation(self, esc, reason, detail=""):
        if esc.get("hold_reason") == reason:
            if esc.get("hold_detail") != detail:
                esc["hold_detail"] = detail
                self.queue.save_escalation(esc)
            return
        q.Queue.transition(esc, q.ESC_PENDING, reason=reason)
        esc["hold_detail"] = detail
        self.queue.save_escalation(esc)
        self.log(f"escalation {esc['id']} pending: {reason}")

    def _escalation_text(self, esc):
        return wrap_escalation(esc["id"], self.identity["handle"], esc["message_id"], esc["sender"],
                               esc["body"], self.prompt_agent_config, self.config.path)

    def _begin_escalation(self, esc):
        esc["attempts"] = esc.get("attempts", 0) + 1
        q.Queue.transition(esc, q.SUBMITTING)
        self.queue.save_escalation(esc)
        return self._escalation_text(esc)

    def sync_events(self):
        """Report the latest reportable state of every acked record, once per change."""
        with self.queue.lock():
            for record in self.queue.all():
                want = reportable(record)
                if not record["acked"] or want is None:
                    continue
                if record.get("reported") == list(want):
                    continue
                try:
                    self.api.event(record["id"], want[0], want[1])
                except Unauthorized:
                    raise
                except ApiError as exc:
                    self.log(f"event {want[0]} for {record['id']} not reported yet: {exc}")
                    continue
                record["reported"] = list(want)
                self.queue.save(record)

    # -- forever ---------------------------------------------------------

    def next_wait(self):
        pending = (any(r["state"] in q.PENDING for r in self.queue.all())
                   or any(e["state"] == q.ESC_PENDING for e in self.queue.escalations()))
        return 0 if pending else self.config.poll_wait

    def _pause(self, delay, stop_requested):
        """Sleep, returning early once a supervisor asks the connector to stop."""
        if stop_requested is None:
            self._sleep(delay)
            return
        deadline = self._clock() + delay
        while not stop_requested() and self._clock() < deadline:
            self._sleep(min(0.25, max(0.0, deadline - self._clock())))

    def run_forever(self, max_iterations=None, stop_requested=None, max_wait=None):
        """Loop until interrupted, or until ``stop_requested()`` is true.

        Once a stop is requested no new submission starts, while one already in
        progress completes. ``max_wait`` caps the long poll so a stop is noticed
        promptly."""
        failures, iterations = 0, 0
        if stop_requested is not None:
            self.stop_requested = stop_requested
        self.start()
        while max_iterations is None or iterations < max_iterations:
            if stop_requested is not None and stop_requested():
                return
            iterations += 1
            pending_wait = self.next_wait()
            if max_wait is not None:
                pending_wait = min(pending_wait, max_wait)
            try:
                self.run_once(wait=pending_wait)
                failures = 0
            except Unauthorized:
                raise
            except (ApiError, RainError) as exc:
                failures += 1
                delay = random.uniform(0, min(60.0, 2.0 ** failures))
                self.log(f"iteration failed ({exc}); retrying in {delay:.1f}s")
                self._pause(delay, stop_requested)
                continue
            if pending_wait == 0:
                self._pause(self.config.recheck_interval, stop_requested)
