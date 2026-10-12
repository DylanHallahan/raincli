"""The person session on this machine (protocol §16.3, §16.4, §16.10, §16.14 S3).

Three private files sit beside the agent config:

- ``person.json``: ``{"person_session": "rps_…"}``, or ``{"person_session_dpapi": …}`` on
  Windows (§15.3 rules). It is never put in argv, the environment or a log.
- ``app-install.json``: this install's random ``app_install_token`` (32 bytes), stored the
  same way. The app's window sends it only in its User-Agent (``RainCLIApp/<token>``) and
  the handoff request carries only its sha256. It is rotated on every sign-in, every
  sign-out (machine or person) and every install or reinstall of the app
  (``rotate_app_install_token``); a rotation ends handoff codes and app-mode web sessions
  bound to the old one.
- ``notifications/``: the runtime's notification feed (§16.10): ``queue.json`` holds
  ``{id, kind, at}`` entries for messages to the person and to their own machines (§21 PR6),
  each with the facts the app's toast rule needs (``from_kind``, an approval ``hold``,
  ``waiting``), kept for 7 days and at most 500 (§16.12 C14), written by the runtime only;
  ``taken.json`` is how far the app has read and ``settings.json`` the app's "Notify me about
  every message" choice, both written by the app only. No body or name is ever written there.

No function here puts a token in an error message or a return value other than the ones
named for it (``load_session``, ``app_install_token``).
"""
import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_mod
from .config import Secret, default_config_path, load_config
from .errors import ApiError, ConfigError, Unauthorized, UsageError
from .fsutil import atomic_write_json, ensure_private_dir, read_private_file, read_state_bytes

PERSON_FILE = "person.json"
INSTALL_FILE = "app-install.json"
DEVICE_FILE = "known-device.json"  # the server's recognised-device token for this install (protocol §18.1)
NOTIFY_DIR = "notifications"
PERSON_TOKEN_RE = re.compile(r"^rps_[A-Za-z0-9_-]{43}$")
INSTALL_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
DEVICE_TOKEN_RE = re.compile(r"rkd1_[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9]{1,12}\.[0-9]{1,9}\.[0-9a-f]{64}")
NOTIFY_KEEP_SECONDS = 7 * 86400
NOTIFY_KEEP_COUNT = 500
FEED_WAIT = 25
FEED_BACKOFF = (5, 300)


# -- paths -------------------------------------------------------------------------------

def _dir(agent_config):
    return Path(agent_config or default_config_path()).absolute().parent


def person_path(agent_config=None):
    return _dir(agent_config) / PERSON_FILE


def install_path(agent_config=None):
    return _dir(agent_config) / INSTALL_FILE


def notify_dir(agent_config=None):
    return _dir(agent_config) / NOTIFY_DIR


# -- one private secret per file -----------------------------------------------------------

def _write_secret(path, key, value, extra=None):
    ensure_private_dir(str(path.parent))
    if config_mod.protects_tokens():
        from .dpapi import protect_token
        data = {key + "_dpapi": protect_token(value)}
    else:
        data = {key: value}
    data.update(extra or {})
    atomic_write_json(str(path), data, mode=0o600)


def _read_secret(path, key, pattern, what, extra=()):
    """The stored value, or None when the file does not exist. A damaged or foreign
    file is a ``ConfigError`` that never quotes the file."""
    if not os.path.lexists(path):
        return None
    raw = read_private_file(str(path), what)
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ConfigError(f"{what} {path} is damaged; sign in again") from None
    if not isinstance(data, dict) or len(set(data) - set(extra)) != 1 or not ({key, key + "_dpapi"} & set(data)):
        raise ConfigError(f"{what} {path} is damaged; sign in again")
    if key + "_dpapi" in data:
        from .dpapi import unprotect_token
        value = unprotect_token(data[key + "_dpapi"])
    else:
        value = data[key]
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ConfigError(f"{what} {path} is damaged; sign in again")
    return value


def _remove(path):
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


# -- the person session -------------------------------------------------------------------------

def validate_session(token):
    if not isinstance(token, str) or not PERSON_TOKEN_RE.fullmatch(token):
        raise ConfigError("the server's person session is malformed")
    return Secret(token)


def save_session(agent_config, token):
    """Store a new person session; a new session always rotates the install token."""
    token = token if isinstance(token, Secret) else validate_session(token)
    validate_session(token.reveal())
    _write_secret(person_path(agent_config), "person_session", token.reveal())
    rotate_app_install_token(agent_config)
    return str(person_path(agent_config))


def load_session(agent_config=None):
    """The person session as a ``Secret``, or None when this machine has none."""
    value = _read_secret(person_path(agent_config), "person_session", PERSON_TOKEN_RE, "person session file")
    return None if value is None else Secret(value)


def clear_session(agent_config=None):
    """Delete ``person.json`` and the notification feed, and rotate the install token
    (§16.14 S3: every sign-out). Returns whether a session was stored."""
    had = _remove(person_path(agent_config))
    folder = notify_dir(agent_config)
    for name in ("queue.json", "taken.json"):
        _remove(folder / name)
    rotate_app_install_token(agent_config)
    return had


def add_session(agent_config, email, password, *, api_url=None):
    """``raincli login --person``: a person session for this signed-in machine
    (``person_only`` with the machine credential as ``previous_token``; no rotation)."""
    from . import login
    config = load_config(agent_config or default_config_path())
    token = login.request_person_only(api_url or config.api_url, email, password, config.token,
                                      device_config=agent_config or config.path)
    save_session(agent_config or config.path, token)
    login.record_owner(agent_config or config.path, email)
    return {"config": str(person_path(agent_config or config.path))}


def owner_email(agent_config=None):
    """The machine owner's email (§16.8): recorded in the machine-mode runtime config at
    sign-in, else asked of this machine's person session. Raises ``ConfigError`` when
    neither is available."""
    from .login import read_runtime_json, runtime_config_path
    data = read_runtime_json(runtime_config_path(agent_config or default_config_path())) or {}
    if isinstance(data.get("owner_email"), str) and EMAIL_RE.fullmatch(data["owner_email"]):
        return data["owner_email"].lower()
    if load_session(agent_config) is None:
        raise ConfigError("the machine's owner is not known here: sign in again, or add a person session "
                          "with raincli login --person")
    return PersonClient.for_config(agent_config, max_attempts=1).me()["user"]["email"].lower()


# -- the recognised-device token (§18.1 item 2) -------------------------------------------------------
# It lets this install's sign-ins pass the server's per-account slowdown; it never signs anything in. It is
# kept across sign-out, and dropped on reinstall (the install it was stored under changed) or when asked.

def device_path(agent_config=None):
    return _dir(agent_config) / DEVICE_FILE


def load_known_device(agent_config=None):
    """The stored token, or None (none yet, another install's, or a damaged file, which is removed)."""
    path = device_path(agent_config)
    try:
        value = _read_secret(path, "known_device", DEVICE_TOKEN_RE, "device file", ("created_under",))
        if value is None:
            return None
        under = json.loads(read_private_file(str(path), "device file")).get("created_under")
    except (ConfigError, ValueError, OSError):
        _remove(path)
        return None
    if not _same_install(under):  # a reinstall (a new install_stamp); an update keeps it
        _remove(path)
        return None
    return value


def _same_install(under):
    """Whether a token stored under ``under`` (``[current, install_stamp]``, or None outside the app)
    still belongs to this install. Only a new ``install_stamp`` (a full install or reinstall) ends
    it; a pushed update changes ``current`` and keeps it (§18.1 item 2)."""
    installed = _installed_under()
    if under is None or installed is None:
        return under == installed
    return len(under) == 2 and under[1] == installed[1]


def save_known_device(agent_config, token):
    """Keep the token from a sign-in reply; anything that is not one is ignored."""
    if isinstance(token, str) and DEVICE_TOKEN_RE.fullmatch(token):
        _write_secret(device_path(agent_config), "known_device", token, {"created_under": _installed_under()})


def forget_known_device(agent_config=None):
    """"Forget this device": the next sign-in is treated as a new device. Returns whether one was stored."""
    return _remove(device_path(agent_config))


# -- the app install token (§16.14 S3) ------------------------------------------------------------

def _installed_under():
    """``[current, install_stamp]`` of the app install running this client, or None outside
    the app (§16.15: a token is rotated when either differs from what it was created under)."""
    from .runtime import winapp
    root = winapp.app_root()
    return None if root is None else list(winapp.install_identity(root))


def rotate_app_install_token(agent_config=None):
    """Replace the install token with a fresh one. Called on sign-in and sign-out, and by
    ``app_install_token`` when the app install changed (§16.15); also available as
    ``raincli app rotate-install-token``. Returns nothing, so no caller can log the token."""
    _write_secret(install_path(agent_config), "app_install_token", secrets.token_urlsafe(32),
                  {"created_under": _installed_under()})


def app_install_token(agent_config=None):
    """This install's token (``secrets.token_urlsafe(32)``, 43 characters), created on first
    use and rotated first when the app's ``(current, install_stamp)`` changed (§16.15)."""
    path = install_path(agent_config)

    def read():
        value = _read_secret(path, "app_install_token", INSTALL_TOKEN_RE, "app install file", ("created_under",))
        if value is None:
            return None, None
        under = json.loads(read_private_file(str(path), "app install file")).get("created_under")
        return value, under
    value, under = read()
    installed = _installed_under()
    if value is None or under != installed:
        rotate_app_install_token(agent_config)
        value, _ = read()
    return value


def app_install_hash(token):
    """``sha256(token)`` as 64 lowercase hex characters: the handoff's ``app_install_hash``."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def app_install(agent_config=None):
    """``(token, sha256 hex)`` for the handoff request and the window's User-Agent."""
    token = app_install_token(agent_config)
    return token, app_install_hash(token)


# -- the person API (§16.4) -----------------------------------------------------------------------

class PersonClient:
    """``/api/v1/person/*`` with this machine's person session."""

    def __init__(self, api):
        self.api = api
        self.poll_backoff = None

    @classmethod
    def for_config(cls, agent_config=None, **kwargs):
        from .api import ApiClient
        config = load_config(agent_config or default_config_path())
        token = load_session(config.path)
        if token is None:
            raise ConfigError("this machine has no person session; run raincli login --person")
        return cls(ApiClient(config.api_url, token, **kwargs))

    def me(self):
        return self.api.request("GET", "/person/me")[1]

    def inbox(self, *, after=0, wait=0, include_acked=False, limit=100, routed=False):
        """``routed`` (§21 PR6, the app's toasts): also the messages routed to this person's own machines
        (their inbox, replies to their agents). A v0.5 server ignores it."""
        from .api import MAX_WAIT
        wait = max(0, min(int(wait), MAX_WAIT))
        query = {"after": int(after), "limit": int(limit), "wait": wait,
                 "include_acked": "true" if include_acked else "false"}
        if routed:
            query["routed"] = 1
        data = self.api.request("GET", "/person/inbox", query=query,
                                timeout=max(self.api.timeout, wait + 15))[1]
        self.poll_backoff = self.api.retry_after if not data["messages"] else None  # §18.1 item 4
        return data["messages"], data.get("cursor", after)

    def message(self, message_id):
        from .api import _path_id, checked_message
        mid = _path_id(message_id)
        return checked_message(self.api.request("GET", f"/person/messages/{mid}")[1]["message"], mid)

    def ack(self, message_id):
        from .api import _path_id
        data = self.api.request("POST", f"/person/messages/{_path_id(message_id)}/ack", body={})[1]
        return data["message"], bool(data.get("acked"))

    def conversations(self, limit=50, archived="exclude"):
        from .api import archive_query
        return self.api.request("GET", "/person/conversations",
                                query=archive_query(limit, archived))[1]["conversations"]

    def archive(self, conversation_id, archived=True, through_seq=None):
        """§17.2, §17.3 A1: archive (or unarchive) a conversation the person is an endpoint of."""
        from .api import _archive, _path_id
        return _archive(self.api.request, f"/person/conversations/{_path_id(conversation_id)}", archived, through_seq)

    def conversation(self, conversation_id, *, after=0, limit=100):
        from .api import _path_id
        return self.api.request("GET", f"/person/conversations/{_path_id(conversation_id)}",
                                query={"after": int(after), "limit": int(limit)})[1]

    def send(self, to, body, *, message_id=None, conversation_id=None, in_reply_to=None, attachments=None,
             team=None):
        """Returns ``(message, created)``. The id is fixed first, so a retry is idempotent."""
        payload = {"id": str(message_id or uuid.uuid4()), "to": to, "body": body}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if in_reply_to:
            payload["in_reply_to"] = in_reply_to
        if attachments:
            payload["attachments"] = attachments
        if team:
            payload["team"] = team
        data = self.api.request("POST", "/person/send", body=payload)[1]
        return data["message"], bool(data.get("created"))

    def download_attachment(self, message_id, ref):
        """``ref`` is an attachment id or its 1-based position. Returns ``(bytes, sha256 header)``."""
        from .api import _path_id
        ref = str(int(ref)) if str(ref).isdigit() else _path_id(ref)
        _, body, headers = self.api.request(
            "GET", f"/person/messages/{_path_id(message_id)}/attachments/{ref}", raw=True)
        return body, headers.get("X-RainCLI-SHA256")

    def sign_out(self):
        return self.api.request("POST", "/person/sign-out", body={})[1]


def sign_out(agent_config=None, *, local_only=False):
    """``raincli me sign-out``: revoke this person session and delete it here. A session
    the server already refuses is cleaned up too."""
    server = "skipped"
    if not local_only and load_session(agent_config) is not None:
        try:
            PersonClient.for_config(agent_config, max_attempts=1).sign_out()
            server = "signed_out"
        except Unauthorized:
            server = "already_revoked"
    return {"server": server, "removed": clear_session(agent_config)}


# -- endpoints (§16.1 CLI forms) --------------------------------------------------------------------

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}$")


def parse_endpoint(text):
    """``handle``, ``handle/agent`` or ``@email`` -> the API form (§16.1)."""
    if not isinstance(text, str) or not text or text != text.strip():
        raise UsageError("an endpoint is a machine handle, handle/agent or @email")
    if text.startswith("@"):
        if not EMAIL_RE.fullmatch(text[1:]):
            raise UsageError("@email must be one email address")
        return {"person": text[1:]}
    if "/" in text:
        handle, agent = text.split("/", 1)
        if not handle or not agent:
            raise UsageError("handle/agent needs both a machine handle and an agent name")
        return {"machine": handle, "agent": agent}
    return text


def endpoint_label(endpoint):
    """The CLI form of an API endpoint object (``handle``, ``handle/agent``, ``@email``)."""
    if isinstance(endpoint, str):
        return endpoint
    if not isinstance(endpoint, dict):
        return "?"
    if "person" in endpoint:
        return "@" + str(endpoint["person"])
    if endpoint.get("agent"):
        return f"{endpoint.get('machine')}/{endpoint['agent']}"
    return str(endpoint.get("machine") or "?")


# -- the notification feed (§16.10, §16.12 C14, §21 PR6) ------------------------------------------------

NOTIFY_SETTINGS = "settings.json"
INBOX_OFFLINE_AFTER = 3600  # PR6: "your inbox offline for more than an hour"
INBOX_CHECK_EVERY = 60  # seconds between checks of the person's own presence (GET /people)
APPROVAL_HOLDS = ("approval_required", "sender_blocked", "approval")  # raw codes, and the coarse one (C13 M3)
FROM_KINDS = ("app", "agent")


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path, default):
    try:
        data = json.loads(read_state_bytes(str(path)))
    except FileNotFoundError:
        return default
    except (OSError, ValueError, UnicodeDecodeError):
        return default
    return data if isinstance(data, dict) else default


def _queue(agent_config):
    data = _read_json(notify_dir(agent_config) / "queue.json", {})
    entries = [e for e in data.get("entries", []) if isinstance(e, dict) and isinstance(e.get("seq"), int)]
    offline = data.get("inbox_offline") if isinstance(data.get("inbox_offline"), dict) else {}
    since = offline.get("since")
    return {"cursor": data.get("cursor", 0) if isinstance(data.get("cursor"), int) else 0,
            "session": data.get("session") if isinstance(data.get("session"), str) else None,
            "seq": data.get("seq", 0) if isinstance(data.get("seq"), int) else 0, "entries": entries,
            "inbox_offline": {"since": since if isinstance(since, (int, float)) and not isinstance(since, bool)
                              else None, "notified": offline.get("notified") is True}}


def _write_queue(agent_config, queue):
    ensure_private_dir(str(notify_dir(agent_config)))
    atomic_write_json(str(notify_dir(agent_config) / "queue.json"), queue)


def _prune(entries, now):
    keep = [e for e in entries if now - e.get("t", 0) < NOTIFY_KEEP_SECONDS]
    return keep[-NOTIFY_KEEP_COUNT:]


def _person_of(endpoint):
    return str(endpoint["person"]).lower() if isinstance(endpoint, dict) and isinstance(endpoint.get("person"), str) \
        else None


def message_facts(message, to_person=None):
    """What the app's toast rule (§21 PR6) needs from a person-API message, and nothing else (C14):

    - ``from_kind``: ``app`` (a teammate wrote it by hand) or ``agent``; None from a server before v0.6;
    - ``hold``: ``approval`` when it is held for the person's approval, else None;
    - ``waiting``: it waits in the person's app because they have no inbox (``no_inbox``). The person's own
      read of such a message shows no hold, so it is also recognised as addressed to the person themselves
      (``to_person``: the ids the poll without ``routed`` returned) and not sent by them or their machines:
      a new message keeps ``recipient_user_id`` only while the person has no inbox in that team (C3, C6)."""
    from_kind = message.get("from_kind") if message.get("from_kind") in FROM_KINDS else None
    hold = message.get("hold_reason")
    own = _person_of(message.get("from_endpoint")) is not None \
        and _person_of(message.get("from_endpoint")) == _person_of(message.get("to_endpoint"))
    waiting = hold == "no_inbox" or (to_person is not None and message.get("id") in to_person and not own)
    return {"from_kind": from_kind, "hold": "approval" if hold in APPROVAL_HOLDS else None, "waiting": bool(waiting)}


def append_notifications(agent_config, messages, cursor, session_id, now=None, to_person=None):
    """The runtime's side: append ``{id, kind, at}`` and the toast facts (``message_facts``) for each
    new message, and keep the cursor. Only the runtime writes ``queue.json``. Returns the number added."""
    now = time.time() if now is None else now
    queue = _queue(agent_config)
    known = {e.get("id") for e in queue["entries"]}
    added = 0
    for message in messages:
        mid = message.get("id") if isinstance(message, dict) else None
        if not isinstance(mid, str) or mid in known:
            continue
        try:
            mid = str(uuid.UUID(mid))
        except ValueError:
            continue
        kind = message.get("kind") if message.get("kind") in ("message", "escalation") else "message"
        queue["seq"] += 1
        queue["entries"].append({"seq": queue["seq"], "id": mid, "kind": kind, "at": _iso(now), "t": now,
                                 **message_facts(message, to_person)})
        known.add(mid)
        added += 1
    queue.update(cursor=int(cursor), session=session_id, entries=_prune(queue["entries"], now))
    _write_queue(agent_config, queue)
    return added


def record_inbox_presence(agent_config, presence, now=None):
    """PR6: one ``inbox_offline`` entry when the person's inbox has been ``offline`` for more than an hour,
    re-armed when it is back (any other presence, ``no_inbox`` included). ``presence`` None (not known)
    changes nothing. Returns whether an entry was added."""
    now = time.time() if now is None else now
    queue = _queue(agent_config)
    state = dict(queue["inbox_offline"])
    added = False
    if presence is None:
        return False
    if presence == "offline":
        state["since"] = now if state["since"] is None else state["since"]
        if not state["notified"] and now - state["since"] > INBOX_OFFLINE_AFTER:
            queue["seq"] += 1
            queue["entries"].append({"seq": queue["seq"], "id": str(uuid.uuid4()), "kind": "inbox_offline",
                                     "at": _iso(now), "t": now})
            state["notified"] = added = True
    else:
        state = {"since": None, "notified": False}
    if state != queue["inbox_offline"] or added:
        queue.update(inbox_offline=state, entries=_prune(queue["entries"], now))
        _write_queue(agent_config, queue)
    return added


def take_notifications(agent_config=None, now=None):
    """The app's side: new entries ``{id, kind, at, from_kind, hold, waiting}``, oldest first, each
    returned once. ``kind`` is ``message``, ``escalation`` or ``inbox_offline``; an entry written before
    v0.6 has ``from_kind`` None."""
    now = time.time() if now is None else now
    queue = _queue(agent_config)
    taken = _read_json(notify_dir(agent_config) / "taken.json", {}).get("seq", 0)
    taken = taken if isinstance(taken, int) else 0
    if taken > queue["seq"]:  # the queue was cleared and started again
        taken = 0
    fresh = [e for e in _prune(queue["entries"], now) if e["seq"] > taken]
    if fresh:
        ensure_private_dir(str(notify_dir(agent_config)))
        atomic_write_json(str(notify_dir(agent_config) / "taken.json"), {"seq": fresh[-1]["seq"]})
    return [{"id": e["id"], "kind": e["kind"], "at": e["at"],
             "from_kind": e.get("from_kind") if e.get("from_kind") in FROM_KINDS else None,
             "hold": "approval" if e.get("hold") == "approval" else None, "waiting": e.get("waiting") is True}
            for e in fresh]


def notify_all(agent_config=None):
    """The app's "Notify me about every message" setting (§21 PR6): off unless chosen."""
    return _read_json(notify_dir(agent_config) / NOTIFY_SETTINGS, {}).get("notify_all") is True


def set_notify_all(agent_config, value):
    """Save the setting (the app's side; a bool only)."""
    if not isinstance(value, bool):
        raise UsageError("notify_all is true or false")
    ensure_private_dir(str(notify_dir(agent_config)))
    atomic_write_json(str(notify_dir(agent_config) / NOTIFY_SETTINGS), {"notify_all": value})
    return value


class NotificationFeed:
    """The runtime's long-poll of ``/person/inbox?routed=1`` (§16.10, §21 PR6). It runs only while
    this machine has a person session, re-reads ``person.json`` after every failure, and never acks:
    viewing a message is what acks it. Every ``INBOX_CHECK_EVERY`` seconds it also reads the
    person's own presence (``GET /people`` with the machine credential, this machine's team) for the
    "inbox offline for more than an hour" toast."""

    def __init__(self, agent_config, *, client_factory=None, machine_factory=None, sleep=None, now=time.time,
                 log=None):
        self.agent_config = agent_config
        self.client_factory = client_factory or (lambda: PersonClient.for_config(agent_config, max_attempts=1))
        self.machine_factory = machine_factory or self._machine_client
        self.stop = threading.Event()
        self.sleep = sleep or self.stop.wait
        self.now = now
        self.log = log or (lambda text: None)
        self.failures = 0
        self.backoff = None
        self._checked = None
        self._email = None
        self._check_error = None

    def _machine_client(self):
        from .api import ApiClient
        config = load_config(self.agent_config or default_config_path())
        return ApiClient(config.api_url, config.token, timeout=15, max_attempts=1)

    def session_id(self):
        token = load_session(self.agent_config)
        return None if token is None else hashlib.sha256(token.reveal().encode()).hexdigest()[:16]

    def poll_once(self, wait=FEED_WAIT):
        """One long-poll. Returns the number of entries added, or None without a session."""
        session = self.session_id()
        if session is None:
            return None
        queue = _queue(self.agent_config)
        cursor = queue["cursor"] if queue["session"] == session else 0
        client = self.client_factory()
        messages, new_cursor = client.inbox(after=cursor, wait=wait, routed=True)
        self.backoff = getattr(client, "poll_backoff", None)
        to_person = None
        if messages:  # which of them are addressed to the person themselves (no inbox): one quick read
            direct, _ = client.inbox(after=cursor, wait=0)
            to_person = {m.get("id") for m in direct if isinstance(m, dict)}
        return append_notifications(self.agent_config, messages, new_cursor, session, self.now(), to_person)

    def check_inbox(self):
        """At most every ``INBOX_CHECK_EVERY`` seconds: the person's own presence from ``GET /people``
        (``ready``, ``busy``, ``offline`` or ``no_inbox``, C5.4) into ``record_inbox_presence``. Returns
        whether a toast entry was added, or None when it is not time yet."""
        now = self.now()
        if self._checked is not None and now - self._checked < INBOX_CHECK_EVERY:
            return None
        self._checked = now
        if self._email is None:
            self._email = owner_email(self.agent_config)
        people = self.machine_factory().request("GET", "/people")[1].get("people") or []
        mine = next((p for p in people if isinstance(p, dict) and isinstance(p.get("email"), str)
                     and p["email"].lower() == self._email), None)
        return record_inbox_presence(self.agent_config, mine.get("presence") if mine else None, now)

    def _check_inbox(self):
        try:
            self.check_inbox()
            self._check_error = None
        except Exception as exc:  # noqa: BLE001 - an old server (no /people) or offline: no offline toast
            line = f"notification feed: inbox check {type(exc).__name__}" + (
                f" {exc.code}" if isinstance(exc, ApiError) else "")
            if line != self._check_error:  # once per change, not every minute
                self.log(line)
            self._check_error = line

    def run(self):
        while not self.stop.is_set():
            try:
                added = self.poll_once()
                self.failures = 0
                if added is None:
                    self.sleep(FEED_BACKOFF[1] // 10)
                    continue
                self._check_inbox()
                if self.backoff:
                    self.sleep(self.backoff)  # an empty answer with Retry-After (§18.1 item 4)
            except (ApiError, ConfigError, OSError) as exc:
                self.failures += 1
                self.log(f"notification feed: {type(exc).__name__}"
                         + (f" {exc.code}" if isinstance(exc, ApiError) else ""))
                self.sleep(min(FEED_BACKOFF[1], FEED_BACKOFF[0] * 2 ** min(self.failures, 6)))

    def start(self):
        thread = threading.Thread(target=self.run, name="raincli-notification-feed", daemon=True)
        thread.start()
        return thread
