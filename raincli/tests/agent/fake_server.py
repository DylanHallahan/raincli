"""In-process fake of the RainCLI agent API (protocol section 3) on 127.0.0.1.

It implements the semantics the client and connector depend on: bearer auth,
idempotent send, recipient-only ack, events only after ack, long-poll inbox,
capacity limits, replied state, and fault injection (503/429, dropped
connections before or after processing, and redirects).

It follows the v0.6 server (§21): teammates are people. Every machine has an owner person (by default
``<handle>@example.com``, named "<Handle> Example"); a message names a person and goes to that person's inbox
(their first machine's endpoint unless picked with PUT /me/inbox; with no machine, it waits for the person);
machine and agent forms are refused (``address_people``); replies are routed from ``in_reply_to``; and message
JSON is rendered per reader as ``messaging.message_json`` renders it.
"""

import base64
import binascii
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


from raincli_agent import attachments as att
from raincli_agent.text import body_problem

SEND_KEYS = {"id", "to", "body", "conversation_id", "in_reply_to", "from", "sender", "attachments",
             "from_agent", "kind"}
EVENT_STATES = {"held", "submitted", "submission_uncertain", "rejected"}
# presentation.EVENT_CODES (§21 C5.5): the only event details the server accepts.
EVENT_CODES = frozenset({
    "busy", "blocked", "offline", "next_turn", "target_ambiguous", "sender_blocked", "approval_required",
    "expired_offline", "too_large_for_hook", "too_large_for_command_line", "client_update_needed", "no_inbox",
    "owner_unreachable", "delivered", "submission_uncertain", "rejected", "declined", "target_mismatch",
    "agent_not_ready", "herdr_timeout", "herdr_failed", "handover_failed", "handover_missing",
    "claimed_without_receipt", "next_turn_emitted",
})
# presentation's teammate-facing texts.
ADDRESS_PEOPLE = "Address people: send to a teammate's name or email"
INVALID_RECIPIENT = "recipient must be a teammate's name or email"
INBOX_INVALID = "{machine} can't be your inbox: pick one of your own agents that can receive messages"
_HOLD_GROUPS = {"no_inbox": "no_inbox", "busy": "busy", "blocked": "busy", "offline": "offline",
                "expired_offline": "offline", "client_update_needed": "update", "approval_required": "approval",
                "sender_blocked": "approval"}
_API_CODES = {"no_inbox": "no_inbox", "busy": "inbox_busy", "offline": "inbox_offline", "update": "update_needed",
              "approval": "approval", "failed": "inbox_failed"}
_API_REPLY_CODES = {"busy": "agent_busy", "offline": "agent_offline"}


def api_hold_code(code, route=None):
    """presentation.api_hold_code (C13 M3): the coarse hold_reason for anyone but the recipient machine's owner."""
    if not code:
        return None
    group = _HOLD_GROUPS.get(code, "failed")
    return (_API_REPLY_CODES.get(group) if route == "reply" else None) or _API_CODES[group]


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ApiFail(Exception):
    def __init__(self, status, code, message="", headers=None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message or code, headers or {}
        self.top = {}  # reply fields beside "error", as MessagingError.extra (e.g. "people" for ambiguous_person)


AGENT_KEYS = {"key", "name", "type", "status", "role", "reachability", "source"}
CLIENT_KEYS = {"version", "update_mode", "update_state", "error"}


def presence_problem(body):
    """Protocol 14.1 / 14.7 validation: all-or-nothing, unknown keys rejected at every level."""
    if not isinstance(body, dict) or set(body) - {"status", "agents", "client"} or "status" not in body:
        return "keys"
    if body["status"] not in {"ready", "busy", "blocked", "unknown", "offline"}:
        return "status"
    agents = body.get("agents", [])
    if not isinstance(agents, list) or len(agents) > 100:
        return "agents"
    keys, inboxes = set(), 0
    for a in agents:
        if not isinstance(a, dict) or not AGENT_KEYS <= set(a) or set(a) - AGENT_KEYS - {"ambiguous"}:
            return "agent keys"
        if not isinstance(a["key"], str) or not re.fullmatch(r"[a-z0-9]{8,64}", a["key"]) or a["key"] in keys:
            return "agent key"
        keys.add(a["key"])
        name = a["name"]
        if (not isinstance(name, str) or not 1 <= len(name) <= 64 or not name.strip()
                or re.search("[\x00-\x1f\x7f-\x9f\u2028\u2029]", name)
                or re.search(r"rc[ai]_[A-Za-z0-9_-]{20,}", name)):  # token-shaped: a real leak
            return "agent name"
        if a["type"] not in {"claude", "codex", "gemini", "cursor", "opencode", "other"}:
            return "agent type"
        if a["status"] not in {"working", "idle", "blocked", "offline", "unknown"}:
            return "agent status"
        if a["source"] not in {"herdr", "hook", "scan"} or (a["source"] == "scan" and a["status"] != "unknown"):
            return "agent source"
        # §16.2 / migration 0006: reachability on any agent; an inbox must be deliverable;
        # "ambiguous" only as true, with "listed".
        if a["role"] not in (None, "inbox") or a["reachability"] not in (None, "instant", "next-turn", "listed"):
            return "agent role"
        if "ambiguous" in a and (a["ambiguous"] is not True or a["reachability"] != "listed"):
            return "ambiguous"
        if a["role"] == "inbox":
            inboxes += 1
            if a["reachability"] not in ("instant", "next-turn"):
                return "reachability"
    if inboxes > 1:
        return "inboxes"
    client = body.get("client")
    if client is not None:
        if not isinstance(client, dict) or set(client) - CLIENT_KEYS or not {"version", "update_mode", "update_state"} <= set(client):
            return "client keys"
        if not re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}", str(client["version"])):
            return "client version"
        if client["update_mode"] not in ("automatic", "manual"):
            return "update mode"
        if client["update_state"] not in ("current", "updating", "failed", "rolled_back"):
            return "update state"
        error = client.get("error")
        if error is not None and not (isinstance(error, str) and re.fullmatch(r"[a-z0-9_.:-]{1,64}", error)):
            return "client error"
    return None


class FakeState:
    def __init__(self):
        self.lock = threading.Condition()
        self.agents = {}  # id -> {handle, display_name, team, active}
        self.teams = {}
        self.tokens = {}  # token -> agent id
        self.messages = {}  # id -> internal record
        self.conversations = {}  # id -> {"people": frozenset of emails, "kind": "pair" | "self", "team": slug}
        self.team_ids = {}  # slug -> a uuid (conversation JSON's team_id)
        self.persons = {}  # email -> display name (§21: the people teammates address)
        self.inboxes = {}  # (email, team) -> {"machine": agent id, "agent": name, "set_at"} | None (no inbox)
        self.offline_agents = set()  # (agent id, name): a stated agent that is not live (replies fall back, C4)
        self.presence = {}
        self.directory = {}  # handle -> agents list (snapshot, protocol 14.1)
        self.clients = {}  # handle -> client block
        self.targets = {}  # team -> {"version", "allow_downgrade"} (reply target)
        self.presence_bodies = []  # every accepted presence body, in order
        self.events = []  # (message id, state, detail)
        self.seq = 0
        self.max_pending = 1000
        self.faults = []  # [method, path regex, action, remaining]
        self.redirect_to = None
        self.requests = []  # (method, path, headers)
        self.on_ack = None
        self.send_commits = 0
        self.tamper_download = {}  # attachment id -> bytes served instead of the stored ones
        self.missing_attachments = set()  # attachment ids whose row is "lost" (404)
        # Machine sign-in (protocol 15.1, 15.8 H2): accounts and the shared limiter.
        self.users = {}  # email -> {"password", "teams": [slug]}
        self.owners = {}  # agent id -> its owner person's email (every machine has one)
        self.delivered = set()  # agent ids with delivery history (they rotate only with previous_token)
        self.login_failures = {}  # email -> failures
        self.login_bodies = []  # every /app/login body received (tests check what was sent)
        self.devices_issued = 0
        self.inbox_queries = []  # (handle, routing=1?) for every inbox poll
        self.person_sessions = {}  # "rps_" token -> {"email", "machine": agent id} (§16.3)
        self.known_devices = {}  # known-device token -> email (§18.1 item 2)
        self.over_cap_retry_after = None  # set: every inbox poll is answered at once, empty, with Retry-After
        self.over_cap_polls = 0
        self.person_inbox = {}  # email -> [message JSON] seeded by the notification feed tests (/person/inbox)
        self.routing = {}  # agent id -> "all" | "inbox-only" (§16.5)
        self.archive_calls = []  # (handle, conversation id, archived?, archived_through_seq sent)
        self.conversation_queries = []  # the archived= parameter of each GET /conversations
        self.me_owner = True  # False: an older server, whose /me has no "owner" (§16.16)

    # -- setup -----------------------------------------------------------

    def add_agent(self, handle, team="alpha", display_name=None, owner=None, owner_name=None):
        """A machine owned by ``owner`` (default ``<handle>@example.com``, named "<Handle> Example")."""
        self.teams.setdefault(team, team.title())
        agent_id = str(uuid.uuid4())
        self.agents[agent_id] = {"id": agent_id, "handle": handle, "team": team,
                                 "display_name": display_name or handle.title(), "active": True}
        owner = owner or f"{handle}@example.com"
        self.owners[agent_id] = owner
        if owner_name is not None:
            self.persons[owner] = owner_name
        else:
            self.persons.setdefault(owner, f"{handle.title()} Example")
        token = "rca_" + secrets.token_urlsafe(32)
        self.tokens[token] = agent_id
        return token

    def set_owner(self, token, email, name=None):
        """Test helper: the machine behind ``token`` belongs to ``email``."""
        self.owners[self.tokens[token]] = email
        if name is not None:
            self.persons[email] = name
        else:
            self.persons.setdefault(email, email.split("@")[0].title())

    def add_user(self, email, password, teams=("alpha",)):
        for team in teams:
            self.teams.setdefault(team, team.title())
        self.users[email] = {"password": password, "teams": list(teams)}
        self.persons.setdefault(email, email.split("@")[0].title())

    def app_login(self, body):
        """15.1 as amended by 15.8: generic errors, team choice, name rules, rotation with proof."""
        if not isinstance(body, dict) or set(body) - {"email", "password", "machine_name", "team",
                                                      "previous_token", "replace", "person_session",
                                                      "person_only", "known_device"}:
            raise ApiFail(400, "invalid")
        user = self.users.get(body.get("email"))
        if self.login_failures.get(body.get("email"), 0) >= 5:
            raise ApiFail(429, "rate_limited", headers={"Retry-After": "900"})
        if user is None or body.get("password") != user["password"]:
            self.login_failures[body.get("email")] = self.login_failures.get(body.get("email"), 0) + 1
            raise ApiFail(401, "invalid_credentials")
        if body.get("person_only") is True:  # §16.3: a person session for the proven machine's owner
            machine = self.tokens.get(body.get("previous_token") or "")
            if "machine_name" in body:
                raise ApiFail(400, "invalid")
            if machine is None or not self.agents[machine]["active"]:  # §16.17 (1)
                raise ApiFail(409, "machine_credential_invalid")
            if self.owners.get(machine) != body["email"]:
                raise ApiFail(409, "not_machine_owner")
            return 200, {"person_session": self.new_person_session(body["email"], machine),
                         "known_device": self.new_known_device(body["email"])}
        teams = user["teams"]
        team = body.get("team")
        if team is None:
            if len(teams) != 1:
                fail = ApiFail(409, "team_choice_required")
                fail.extra = {"teams": [{"slug": t, "name": self.teams[t]} for t in teams]}
                raise fail
            team = teams[0]
        elif team not in teams:
            raise ApiFail(400, "invalid")
        name = body.get("machine_name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", name):
            raise ApiFail(400, "invalid")
        existing = self.agent_by_handle(team, name)
        rotated = False
        if existing is not None:
            mine = self.owners.get(existing["id"]) == body["email"] and existing["active"]
            if not mine:
                raise ApiFail(409, "name_taken")
            proof = self.tokens.get(body.get("previous_token") or "") == existing["id"]
            if not proof and not (body.get("replace") is True and existing["id"] not in self.delivered):
                raise ApiFail(409, "name_in_use")
            for token in [t for t, aid in self.tokens.items() if aid == existing["id"]]:
                del self.tokens[token]
            token = "rca_" + secrets.token_urlsafe(32)
            self.tokens[token] = existing["id"]
            rotated, status = True, 200
        else:
            token = self.add_agent(name, team=team, owner=body["email"])
            status = 201
        machine = self.tokens[token]
        for ps in [t for t, s in self.person_sessions.items() if s["machine"] == machine]:
            del self.person_sessions[ps]  # §16.12 C6: a rotation ends the machine's person sessions
        reply = {"api_url": "https://raincli.example", "token": token, "handle": name,
                 "team": {"slug": team, "name": self.teams[team]}, "rotated": rotated}
        if body.get("person_session") is True:
            reply["person_session"] = self.new_person_session(body["email"], machine)
        reply["known_device"] = self.new_known_device(body["email"])  # every success (§18.1 item 2)
        return status, reply

    def new_known_device(self, email):
        """The server's token shape, deterministic: rkd1_<user>.<device>.<issued>.<epoch>.<signature>."""
        self.devices_issued += 1
        token = f"rkd1_{self.devices_issued:032x}.{'d' * 32}.1700000000.0." + format(self.devices_issued, "064x")
        self.known_devices[token] = email
        return token

    def new_person_session(self, email, machine):
        token = "rps_" + secrets.token_urlsafe(32)
        self.person_sessions[token] = {"email": email, "machine": machine}
        return token

    def sign_out(self, caller):
        for ps in [t for t, s in self.person_sessions.items() if s["machine"] == caller["id"]]:
            del self.person_sessions[ps]
        caller["active"] = False
        for token in [t for t, aid in self.tokens.items() if aid == caller["id"]]:
            del self.tokens[token]
        return 200, {"signed_out": True}

    # -- people (§21: teammates are people; every machine has an owner person) ----------------

    def name_of(self, email):
        """A person as their team sees them: their display name, else their email."""
        return (self.persons.get(email) or "").strip() or email

    def set_person_name(self, email, display_name):
        self.persons[email] = display_name

    def owner_of(self, agent_id):
        return self.owners.get(agent_id)

    def members(self, team):
        """The team's people: the owners of its active machines and its signed-up users, sorted by name."""
        emails = {self.owners[a["id"]] for a in self.agents.values()
                  if a["team"] == team and a["active"] and a["id"] in self.owners}
        emails |= {e for e, u in self.users.items() if team in u["teams"]}
        return sorted(emails, key=lambda e: (self.name_of(e).lower(), e))

    def machines_of(self, email, team):
        return [a for a in self.agents.values() if a["team"] == team and a["active"] and self.owners.get(a["id"]) == email]

    def inbox_of(self, email, team):
        """The person's inbox in the team: ``{"machine": agent id, "agent": name or None, "set_at"}`` or None. Without
        a pick (PUT /me/inbox), it is the person's first machine in the team on the inbox-mode path (agent None)."""
        if (email, team) in self.inboxes:
            row = self.inboxes[(email, team)]
            if row is not None and not self.agents[row["machine"]]["active"]:
                return None
            return row
        machines = self.machines_of(email, team)
        return {"machine": machines[0]["id"], "agent": None, "set_at": None} if machines else None

    def set_inbox(self, email, team, handle, agent=None):
        """Test helper: pick ``agent`` on ``handle`` as the person's inbox (None: the machine's inbox-mode path);
        ``handle=None``: no inbox (messages to the person wait in their app)."""
        if handle is None:
            self.inboxes[(email, team)] = None
        else:
            self.inboxes[(email, team)] = {"machine": self.agent_by_handle(team, handle)["id"], "agent": agent,
                                           "set_at": now()}

    def inbox_agent_name(self, agent_id):
        """The machine's own inbox agent (the directory's ``role: inbox`` entry), if it published one."""
        handle = self.agents[agent_id]["handle"]
        return next((e["name"] for e in self.directory.get(handle, []) if e.get("role") == "inbox"), None)

    def inbox_target(self, email, team):
        """C3: ``(recipient machine id, agent name, recipient person, route)`` for a message to the person now: the
        inbox-mode path (the machine), the named path (machine and agent), or the person (no inbox)."""
        row = self.inbox_of(email, team)
        if row is None:
            return None, None, email, "person"
        machine = row["machine"]
        if row["agent"] is None or self.routing.get(machine) == "inbox-only" \
                or self.inbox_agent_name(machine) == row["agent"]:
            return machine, None, None, "inbox"
        return machine, row["agent"], None, "inbox"

    def live(self, agent_id, name):
        """C13 M4, simplified: a stated agent is live unless marked offline (``offline_agents``)."""
        a = self.agents[agent_id]
        return a["active"] and self.routing.get(agent_id) != "inbox-only" and (agent_id, name) not in self.offline_agents

    def set_offline(self, handle, name, team="alpha"):
        self.offline_agents.add((self.agent_by_handle(team, handle)["id"], name))

    def refuse_machine(self, team, handle, me):
        """``address_people``: names the handle's person only for a teammate's active machine (C2.2, C13 L3)."""
        handle = handle.strip() if isinstance(handle, str) else ""
        machine = self.agent_by_handle(team, handle) if handle else None
        owner = self.owners.get(machine["id"]) if machine is not None and machine["active"] else None
        if owner is None or owner not in self.members(team) or owner == me:
            return ApiFail(400, "address_people", ADDRESS_PEOPLE)
        return ApiFail(400, "address_people", f"Address people: send to {self.name_of(owner)} ({owner})")

    def resolve_text(self, team, text, me):
        """presentation.resolve_person_text: an email (``@`` optional) or a unique display name."""
        if not isinstance(text, str) or not text.strip() or len(text) > 254:
            raise ApiFail(400, "invalid", INVALID_RECIPIENT)
        value = text.strip()
        email = value[1:] if value.startswith("@") else value
        if "@" in email:
            found = next((e for e in self.members(team) if e.lower() == email.lower()), None)
            if found is None:
                raise ApiFail(400, "address_people", ADDRESS_PEOPLE)
            return found
        named = sorted(e for e in self.members(team) if self.name_of(e).strip().lower() == value.lower())
        if len(named) == 1:
            return named[0]
        if named:
            fail = ApiFail(400, "ambiguous_person",
                           f"More than one teammate is called {self.name_of(named[0])}: use their email")
            fail.top = {"people": [{"name": f"{self.name_of(e)} ({e})", "email": e} for e in named]}
            raise fail
        raise self.refuse_machine(team, value.split("/", 1)[0], me)

    def resolve_recipient(self, team, to, me):
        """messaging.resolve_recipient: the one person ``to`` names; machine and agent forms are refused."""
        if isinstance(to, dict):
            if set(to) == {"person"} and isinstance(to["person"], str):
                text = to["person"]
            elif set(to) == {"person_name"} and isinstance(to["person_name"], str):
                text = to["person_name"]
            elif "machine" in to and set(to) <= {"machine", "agent"} and isinstance(to["machine"], str):
                raise self.refuse_machine(team, to["machine"], me)
            else:
                raise ApiFail(400, "invalid", INVALID_RECIPIENT)
        elif isinstance(to, str):
            text = to
        else:
            raise ApiFail(400, "invalid", INVALID_RECIPIENT)
        return self.resolve_text(team, text, me)

    def person_named(self, team, to, me):
        """C2.5: the person a reply's ``to`` names, tolerating an old client's machine form (its owner)."""
        if isinstance(to, dict) and "machine" in to:
            handle = to.get("machine")
        elif isinstance(to, str) and "@" not in to:
            handle = to.split("/", 1)[0].strip()
        else:
            handle = None
        if handle:
            machine = self.agent_by_handle(team, handle)
            if machine is not None and machine["active"]:
                return self.owners.get(machine["id"])
        try:
            return self.resolve_recipient(team, to, me)
        except ApiFail:
            return None

    def conversation_for(self, team, people, kind="pair"):
        people = frozenset(people)
        for cid, c in self.conversations.items():
            if c["team"] == team and c["people"] == people and c["kind"] == kind:
                return cid
        cid = str(uuid.uuid4())
        self.conversations[cid] = {"people": people, "kind": kind, "team": team}
        return cid

    def person_message(self, recipient_handle, email, display_name, body, agent=None, team="alpha"):
        """A message from a person (the website or app) to a machine or one of its agents, as the server stores
        one it routed to that machine (the person's inbox there)."""
        recipient = self.agent_by_handle(team, recipient_handle)
        self.persons.setdefault(email, display_name)
        self.seq += 1
        ts = now()
        mid = str(uuid.uuid4())
        owner = self.owners.get(recipient["id"])
        conv = self.conversation_for(team, {email, owner}, "self" if owner == email else "pair")
        self.messages[mid] = {"id": mid, "team": team, "conversation_id": conv, "in_reply_to": None,
                              "sender": None, "person": {"email": email, "display_name": display_name},
                              "agent": agent, "from_agent": None, "kind": "message", "recipient": recipient["id"],
                              "recipient_person": None, "route": "inbox", "redeliver": False, "body": body,
                              "created_at": ts, "seq": self.seq, "acked_at": None, "delivery_state": "stored",
                              "delivery_updated_at": ts, "hold": None, "attachments": []}
        with self.lock:
            self.lock.notify_all()
        return mid

    def fail(self, method, path_regex, action, times=1):
        self.faults.append([method, re.compile(path_regex), action, times])

    def agent_by_handle(self, team, handle):
        for a in self.agents.values():
            if a["team"] == team and a["handle"] == handle:
                return a
        return None

    # -- rendering (messaging.message_json, per reader) -------------------------------------

    def sender_person(self, m):
        return m["person"]["email"] if m.get("person") else self.owners.get(m["sender"])

    def recipient_person(self, m):
        return m["recipient_person"] or self.owners.get(m["recipient"])

    def view_state(self, viewer, m):
        """messaging.view_state: no read receipt for a message to a person, except for that person (C13 H4)."""
        rp = m["recipient_person"]
        if rp is not None and viewer != rp:
            if m["delivery_state"] == "replied":
                return "replied", None
            own = self.sender_person(m) == rp
            if not own and self.inbox_of(rp, m["team"]) is None:
                return "held", "no_inbox"
            return "received", None
        return m["delivery_state"], (m.get("hold") if m["delivery_state"] == "held" else None)

    def render(self, m, reader=None, reader_email=None):
        """The message as ``reader`` (a machine record) or ``reader_email`` (a person) sees it. Without either, as
        its recipient sees it."""
        if reader is None and reader_email is None:
            if m["recipient"] is not None:
                reader = self.agents[m["recipient"]]
            else:
                reader_email = m["recipient_person"]
        viewer = reader_email if reader is None else self.owners.get(reader["id"])

        def person(email, display=None):
            return "@" + email, {"person": email, "display_name": display or self.name_of(email)}

        def machine(agent_id, name):
            handle = self.agents[agent_id]["handle"]
            return handle, {"machine": handle, **({"agent": name} if name else {})}

        if m.get("person"):
            from_text, from_json = person(m["person"]["email"], m["person"].get("display_name"))
        elif reader is not None and reader["id"] == m["sender"]:
            from_text, from_json = machine(m["sender"], m.get("from_agent"))
        else:
            from_text, from_json = person(self.owners[m["sender"]])
        if m["recipient_person"] is not None:
            to_text, to_json = person(m["recipient_person"])
        elif reader is not None and reader["id"] == m["recipient"]:
            to_text, to_json = machine(m["recipient"], m.get("agent"))
        else:
            to_text, to_json = person(self.owners[m["recipient"]])
        state, hold = self.view_state(viewer, m)
        owns_recipient = m["recipient"] is not None and self.owners.get(m["recipient"]) == viewer
        sees_read = m["recipient_person"] is None or viewer == m["recipient_person"] or owns_recipient
        return {"id": m["id"], "conversation_id": m["conversation_id"], "in_reply_to": m["in_reply_to"],
                "from": from_text, "to": to_text, "from_endpoint": from_json, "to_endpoint": to_json,
                "from_agent": m.get("from_agent") if m["sender"] is not None and self.owners.get(m["sender"]) == viewer
                else None,
                "from_owner": None,
                "from_kind": "app" if m.get("person") else "agent",
                "kind": m.get("kind", "message"),
                "from_same_owner": self.same_owner(m),
                "body": m["body"], "created_at": m["created_at"], "seq": m["seq"],
                "acked_at": m["acked_at"] if sees_read else None,
                "delivery_state": state,
                "hold_reason": hold if owns_recipient else api_hold_code(hold, m.get("route")),
                "delivery_updated_at": m["delivery_updated_at"] if (m["recipient_person"] is None
                                                                    or viewer == m["recipient_person"]
                                                                    or m["delivery_state"] == "replied")
                else m["created_at"],
                "attachments": [{k: a[k] for k in ("id", "filename", "media_type", "size", "sha256")}
                                for a in m["attachments"]]}

    def same_owner(self, m):
        """§16.16 (2), as the server decides it: a message to a machine from a machine, or a person, with that
        machine's owner."""
        if m["recipient"] is None:
            return False
        owner = self.owners.get(m["recipient"])
        return owner is not None and self.sender_person(m) == owner

    def can_see(self, email, team, m):
        """§16.6 / C13 M1: a person (and so each of their machines) sees the messages they are a side of."""
        return m["team"] == team and email is not None and email in (self.sender_person(m), self.recipient_person(m))

    def visible(self, caller, message_id):
        m = self.messages.get(message_id)
        if m is None or not (caller["id"] in (m["sender"], m["recipient"])
                             or self.can_see(self.owners.get(caller["id"]), caller["team"], m)):
            raise ApiFail(404, "not_found")
        return m

    # -- endpoints -------------------------------------------------------

    def reply_target(self, caller, me, parent):
        """messaging._reply_target (C4, C13 M4, H2): ``(machine id, agent name, person, route, other person)``."""
        team = parent["team"]
        xs, xr = self.sender_person(parent), self.recipient_person(parent)
        if xs == xr == me:  # H2: your machine writing to you, or answering you
            return None, None, me, "self", me
        if xs == me:  # a follow-up of your own message: its recipient person, now
            other = xr
            if other not in self.members(team):
                raise ApiFail(400, "invalid", INVALID_RECIPIENT)
            return (*self.inbox_target(other, team), other)
        other = xs
        if other not in self.members(team):
            raise ApiFail(400, "invalid", INVALID_RECIPIENT)
        sender = parent["sender"]
        if sender is not None and self.agents[sender]["active"]:
            if not parent.get("from_agent"):
                return sender, None, None, "reply", other
            if self.live(sender, parent["from_agent"]):
                return sender, parent["from_agent"], None, "reply", other
        return (*self.inbox_target(other, team), other)

    def send(self, caller, body):
        if not isinstance(body, dict) or set(body) - SEND_KEYS:
            raise ApiFail(400, "invalid", "unknown fields")
        for k in ("from", "sender"):
            if k in body and body[k] != caller["handle"]:
                raise ApiFail(403, "forbidden", "sender spoofing")
        try:
            mid = str(uuid.UUID(str(body.get("id"))))
        except ValueError:
            raise ApiFail(400, "invalid", "bad id") from None
        to, in_reply_to = body.get("to"), body.get("in_reply_to")
        if not isinstance(to, (str, dict)) and not (to is None and in_reply_to is not None):
            raise ApiFail(400, "invalid", 'to must be a person: an email, a name or {"person": email} '
                                          '(a reply may omit it)')
        text = body.get("body")
        if body_problem(text):
            raise ApiFail(400, "invalid", body_problem(text))
        kind = body.get("kind", "message")
        if kind not in ("message", "escalation"):
            raise ApiFail(400, "invalid", "kind must be one of: message, escalation")
        from_agent = body.get("from_agent")
        if from_agent is not None and (not isinstance(from_agent, str) or not 1 <= len(from_agent) <= 64
                                       or from_agent != from_agent.strip()
                                       or re.search("[\x00-\x1f\x7f-\x9f]", from_agent)):
            raise ApiFail(400, "invalid", "from_agent must be a directory name")
        team, me = caller["team"], self.owners.get(caller["id"])
        files = self.decode_attachments(body.get("attachments"))
        existing = self.messages.get(mid)
        if existing is not None:  # idempotency is decided before anything else
            same = (existing["sender"] == caller["id"] and existing["body"] == text
                    and existing.get("kind", "message") == kind and existing["in_reply_to"] == in_reply_to
                    and existing.get("from_agent") == from_agent
                    and body.get("conversation_id") in (None, existing["conversation_id"])
                    and [(a["filename"], a["sha256"]) for a in existing["attachments"]]
                    == [(f["filename"], f["sha256"]) for f in files])
            if same and to is not None and in_reply_to is None:
                same = self.person_named(team, to, me) == self.recipient_person(existing)
            if same:
                return 200, {"message": self.render(existing, caller), "created": False}
            raise ApiFail(409, "id_conflict")
        parent = None
        if in_reply_to:
            parent = self.messages.get(in_reply_to)
            if parent is None or not self.can_see(me, team, parent):
                raise ApiFail(404, "not_found", "in_reply_to message not found")
            machine, agent_name, person, route, other = self.reply_target(caller, me, parent)
            if to is not None and self.person_named(team, to, me) != other:
                raise ApiFail(400, "invalid", "a reply goes to the other person of the message it answers")
            conv = parent["conversation_id"]
            if body.get("conversation_id") and body["conversation_id"] != conv:
                raise ApiFail(400, "invalid", "conversation_id does not match the parent message")
        else:
            other = self.resolve_recipient(team, to, me)
            if other == me:  # H2 (i): a machine writing to its own owner
                machine, agent_name, person, route = None, None, me, "self"
                conv = self.conversation_for(team, {me}, "self")
            else:
                machine, agent_name, person, route = self.inbox_target(other, team)
                conv = self.conversation_for(team, {me, other})
            if body.get("conversation_id") and body["conversation_id"] != conv:
                raise ApiFail(400, "invalid", "conversation_id is not your conversation with that person")
        if kind == "escalation" and route != "self":
            raise ApiFail(400, "invalid", "an escalation goes only from a machine to its own owner (§16.8)")
        pending = sum(1 for m in self.messages.values() if m["acked_at"] is None
                      and ((machine is not None and m["recipient"] == machine)
                           or (machine is None and m["recipient_person"] == person)))
        if pending >= self.max_pending:
            raise ApiFail(429, "inbox_full")
        self.seq += 1
        ts = now()
        m = {"id": mid, "team": team, "conversation_id": conv, "in_reply_to": in_reply_to, "sender": caller["id"],
             "person": None, "agent": agent_name, "from_agent": from_agent, "kind": kind,
             "recipient": machine, "recipient_person": person, "route": route, "redeliver": False,
             "body": text, "created_at": ts, "seq": self.seq, "acked_at": None, "delivery_state": "stored",
             "delivery_updated_at": ts, "hold": None,
             "attachments": [dict(f, id=str(uuid.uuid4()), media_type="text/markdown") for f in files]}
        self.messages[mid] = m
        self.send_commits += 1
        if parent is not None and self.recipient_person(parent) == me:
            parent["delivery_state"], parent["delivery_updated_at"] = "replied", ts
        self.lock.notify_all()
        return 201, {"message": self.render(m, caller), "created": True}

    def to_person(self, email):
        """Test helper: the messages stored for the person themselves (no inbox, or their own machines writing to
        them), as that person reads them."""
        return [self.render(m, reader_email=email) for m in sorted(self.messages.values(), key=lambda m: m["seq"])
                if m["recipient_person"] == email]

    def decode_attachments(self, items):
        if items is None:
            return []
        if not isinstance(items, list) or len(items) > att.MAX_COUNT:
            raise ApiFail(400, "invalid", "attachments must be a list of at most 5")
        files, names, total = [], set(), 0
        for i, item in enumerate(items):
            if not isinstance(item, dict) or set(item) != {"filename", "content_b64", "sha256"}:
                raise ApiFail(400, "invalid", f"attachment {i}: bad fields")
            name = item["filename"]
            if not att.valid_name(name) or name.lower() in names:
                raise ApiFail(400, "invalid", f"attachment {i}: bad or duplicate name")
            names.add(name.lower())
            try:
                data = base64.b64decode(item["content_b64"], validate=True)
            except (binascii.Error, ValueError, TypeError):
                raise ApiFail(400, "invalid", f"attachment {name}: bad base64") from None
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ApiFail(400, "invalid", f"attachment {name}: sha256 mismatch")
            if att.content_problem(data):
                raise ApiFail(400, "invalid", f"attachment {name}: {att.content_problem(data)}")
            total += len(data)
            if total > att.MAX_TOTAL:
                raise ApiFail(400, "invalid", "attachments too large in total")
            files.append({"filename": name, "sha256": item["sha256"], "size": len(data), "content": data})
        return files

    def download(self, caller, mid, aid):
        m = self.visible(caller, mid)
        a = next((a for a in m["attachments"] if a["id"] == aid), None)
        if a is None or aid in self.missing_attachments:
            raise ApiFail(404, "not_found")
        data = self.tamper_download.get(aid, a["content"])
        return "raw", data, {"Content-Type": "text/markdown; charset=utf-8",
                             "Content-Disposition": f'attachment; filename="{a["filename"]}"',
                             "X-Content-Type-Options": "nosniff", "X-RainCLI-SHA256": a["sha256"],
                             "Cache-Control": "no-store"}

    def inbox(self, caller, query):
        if self.over_cap_retry_after is not None:  # §18.1 item 4: answered at once, empty
            self.over_cap_polls += 1
            after = int(query.get("after", ["0"])[0])
            return 200, {"messages": [], "cursor": after}, {"Retry-After": str(self.over_cap_retry_after)}
        after = int(query.get("after", ["0"])[0])
        limit = min(int(query.get("limit", ["100"])[0]), 500)
        wait = min(float(query.get("wait", ["0"])[0]), 25)
        include_acked = query.get("include_acked", ["false"])[0] == "true"
        routing = query.get("routing", ["0"])[0] == "1"
        self.inbox_queries.append((caller["handle"], routing))

        def matches():
            # §16.2 capability gate: without routing=1, no named-agent or person-sent messages. C13 H1: a re-targeted
            # message comes again below the cursor until acked.
            return sorted((m for m in self.messages.values()
                           if m["recipient"] == caller["id"]
                           and (m["seq"] > after or (m["redeliver"] and m["acked_at"] is None))
                           and (routing or (not m.get("agent") and not m.get("person")))
                           and (include_acked or m["acked_at"] is None)),
                          key=lambda m: m["seq"])[:limit]

        deadline = time.monotonic() + wait
        found = matches()
        while not found and time.monotonic() < deadline:
            self.lock.wait(timeout=max(0.0, deadline - time.monotonic()))
            found = matches()
        return 200, {"messages": [self.render(m, caller) for m in found],
                     "cursor": max([after, *(m["seq"] for m in found)])}

    def ack(self, caller, mid):
        m = self.messages.get(mid)
        if m is not None and m["team"] == caller["team"] and m["recipient"] != caller["id"] \
                and m.get("route") in ("inbox", "person", "reply") \
                and self.recipient_person(m) == self.owners.get(caller["id"]):
            return 200, {"message": self.render(m, caller), "acked": False}  # C13 L1: re-targeted away from here
        m = self.visible(caller, mid)
        if m["recipient"] != caller["id"]:
            raise ApiFail(403, "forbidden", "only the recipient may ack")
        if self.on_ack:
            self.on_ack(mid)
        acked = m["acked_at"] is None
        if acked:
            m["acked_at"], m["redeliver"] = now(), False
            if m["delivery_state"] == "stored":
                m["delivery_state"] = "received"
            m["delivery_updated_at"] = m["acked_at"]
        return 200, {"message": self.render(m, caller), "acked": acked}

    def event(self, caller, mid, body):
        if not isinstance(body, dict) or set(body) - {"state", "detail"} or "state" not in body:
            raise ApiFail(400, "invalid")
        if body["state"] not in EVENT_STATES:
            raise ApiFail(400, "invalid", "state must be one of: " + ", ".join(sorted(EVENT_STATES)))
        detail = body.get("detail")
        if detail is not None:
            if not isinstance(detail, str) or len(detail) > 500:
                raise ApiFail(400, "invalid", "detail must be a string of at most 500 characters")
            if detail == "":
                detail = None
            elif detail not in EVENT_CODES:  # §21 C5.5: codes only, never free text
                raise ApiFail(400, "invalid", "detail must be one of the event codes")
        m = self.visible(caller, mid)
        if m["recipient"] != caller["id"]:
            raise ApiFail(403, "forbidden")
        if m["acked_at"] is None:
            raise ApiFail(409, "not_acked")
        self.events.append((mid, body["state"], body.get("detail", "")))
        if m["delivery_state"] != "replied":  # replied is sticky
            m["delivery_state"] = body["state"]
        m["hold"] = detail if body["state"] == "held" else None
        m["delivery_updated_at"] = now()
        return 200, {"message": self.render(m, caller)}

    def my_conversation(self, caller, cid):
        c = self.conversations.get(cid)
        if c is None or c["team"] != caller["team"] or self.owners.get(caller["id"]) not in c["people"]:
            return None
        return c

    def peer_json(self, viewer, c):
        if c["kind"] == "self":
            return {"peer": "From your agents", "peer_endpoint": {"person": viewer, "display_name": "From your agents"}}
        other = next(iter(c["people"] - {viewer}), viewer)
        return {"peer": self.name_of(other), "peer_endpoint": {"person": other, "display_name": self.name_of(other)}}

    def conversations_list(self, caller, query=None):
        """§17.2: ``archived=exclude`` (default), ``only`` or ``include``."""
        given = (query or {}).get("archived")
        self.conversation_queries.append(given[0] if given else None)
        archived = given[0] if given else "include"  # §17.3 A4: a machine sending none gets include
        if archived not in ("exclude", "only", "include"):
            raise ApiFail(400, "invalid")
        viewer = self.owners.get(caller["id"])
        out = []
        for cid in self.conversations:
            if self.my_conversation(caller, cid) is None:
                continue
            msgs = [m for m in self.messages.values() if m["conversation_id"] == cid]
            if not msgs:
                continue
            is_archived = self.is_archived(cid)
            if (archived == "exclude" and is_archived) or (archived == "only" and not is_archived):
                continue
            last = max(msgs, key=lambda m: m["seq"])
            out.append({"id": cid, **self.peer_json(viewer, self.conversations[cid]), "last_seq": last["seq"],
                        "last_at": last["created_at"],
                        "unacked": sum(1 for m in msgs if m["recipient"] == caller["id"] and not m["acked_at"]),
                        **self.archive_json(cid)})
        out.sort(key=lambda c: -c["last_seq"])
        return 200, {"conversations": out}

    def is_archived(self, cid):
        """§17.3 A1: archived only while no message is newer than archived_through_seq."""
        c = self.conversations[cid]
        newest = max((m["seq"] for m in self.messages.values() if m["conversation_id"] == cid), default=0)
        return bool(c.get("archived_at")) and newest <= (c.get("archived_through_seq") or 0)

    def archive_json(self, cid):
        c = self.conversations[cid]
        if not self.is_archived(cid):
            return {"archived": False, "archived_at": None, "archived_through_seq": None, "archived_by": None}
        by = c["archived_by"]
        return {"archived": True, "archived_at": c["archived_at"], "archived_through_seq": c["archived_through_seq"],
                "archived_by": {"person": by, "display_name": self.name_of(by)} if by else None}

    def archive(self, caller, cid, archived, body=None):
        """§17.2, §17.3 A1, C13 M1: a machine whose owner is a side archives or unarchives for both; idempotent; the
        archive records the seq the archiver saw, capped at the newest; others get 404."""
        c = self.my_conversation(caller, cid)
        if c is None:
            raise ApiFail(404, "not_found")
        newest = max((m["seq"] for m in self.messages.values() if m["conversation_id"] == cid), default=0)
        seen = (body or {}).get("archived_through_seq", newest)
        if archived and not self.is_archived(cid):
            c.update(archived_at=now(), archived_by=self.owners.get(caller["id"]),
                     archived_through_seq=min(int(seen), newest))
        elif not archived:
            c.update(archived_at=None, archived_by=None, archived_through_seq=None)
        self.archive_calls.append((caller["handle"], cid, archived, (body or {}).get("archived_through_seq")))
        return 200, {"conversation": {"id": cid, "team_id": self.team_id(c["team"]), **self.archive_json(cid),
                                      **self.peer_json(self.owners.get(caller["id"]), c)}}

    def team_id(self, team):
        return self.team_ids.setdefault(team, str(uuid.uuid4()))

    def conversation_messages(self, caller, cid, query):
        if self.my_conversation(caller, cid) is None:
            raise ApiFail(404, "not_found")
        after = int(query.get("after", ["0"])[0])
        limit = int(query.get("limit", ["100"])[0])
        msgs = sorted((m for m in self.messages.values()
                       if m["conversation_id"] == cid and m["seq"] > after), key=lambda m: m["seq"])[:limit]
        return 200, {"messages": [self.render(m, caller) for m in msgs],
                     "cursor": msgs[-1]["seq"] if msgs else after}

    # -- people and the inbox setting (§21 C5.3, C8) --------------------------------------------

    def person_presence(self, email, team):
        """presentation.person_presence, simplified: from the person's inbox only."""
        row = self.inbox_of(email, team)
        if row is None:
            return "no_inbox", None
        handle = self.agents[row["machine"]]["handle"]
        status = self.presence.get(handle)
        if status is None or status == "offline":
            return "offline", None
        machine, name, _, _ = self.inbox_target(email, team)
        entry = next((e for e in self.directory.get(handle, [])
                      if e["name"] == (row["agent"] or self.inbox_agent_name(machine)) and not e.get("ambiguous")),
                     None)
        live = entry is not None and entry["reachability"] in ("instant", "next-turn")
        words = {"idle": "ready", "working": "busy", "blocked": "busy", "ready": "ready", "busy": "busy"}
        if name is None:  # the inbox-mode path: the machine's own presence
            return words.get(status, "ready"), ("instant" if live and entry["reachability"] == "instant"
                                                else "next_turn")
        if live:
            return words.get(entry["status"], "ready"), ("instant" if entry["reachability"] == "instant"
                                                         else "next_turn")
        return "offline", None

    def people(self, caller):
        team = caller["team"]
        out = []
        for email in self.members(team):
            presence, speed = self.person_presence(email, team)
            out.append({"display_name": self.name_of(email), "email": email, "team": self.teams[team],
                        "presence": presence, "speed": speed})
        return 200, {"people": out}

    def inbox_json(self, row):
        if row is None:
            return None
        return {"machine": self.agents[row["machine"]]["handle"],
                "agent": row["agent"] or self.inbox_agent_name(row["machine"]) or "inbox", "set_at": row["set_at"]}

    def candidates(self, email, team):
        out = []
        for machine in sorted(self.machines_of(email, team), key=lambda a: a["handle"]):
            for e in sorted(self.directory.get(machine["handle"], []), key=lambda e: e["name"]):
                if e["name"].startswith("-") or not self.pickable(machine["id"], e["name"]):
                    continue
                out.append({"machine": machine["handle"], "agent": e["name"], "live": self.live(machine["id"], e["name"]),
                            "suggested": e.get("role") == "inbox"})
        return out

    def pickable(self, agent_id, name):
        """C1.1, simplified: the machine's inbox agent, or a deliverable (instant or next-turn), unambiguous one."""
        entry = next((e for e in self.directory.get(self.agents[agent_id]["handle"], []) if e["name"] == name), None)
        return entry is not None and (entry.get("role") == "inbox" or (
            entry["reachability"] in ("instant", "next-turn") and not entry.get("ambiguous")))

    def waiting(self, email, team):
        """C6.2: messages waiting for the person in their app, not sent by them, not answered."""
        out = []
        for m in self.messages.values():
            if m["team"] != team or m["recipient_person"] != email or m.get("route") != "person" \
                    or m["acked_at"] is not None or self.sender_person(m) == email:
                continue
            answered = any(a["conversation_id"] == m["conversation_id"] and a["seq"] > m["seq"]
                           and self.sender_person(a) == email for a in self.messages.values())
            if not answered:
                out.append(m)
        return out

    def my_inbox(self, caller):
        email, team = self.owners.get(caller["id"]), caller["team"]
        return 200, {"inbox": self.inbox_json(self.inbox_of(email, team)), "candidates": self.candidates(email, team),
                     "waiting": len(self.waiting(email, team))}

    def retarget(self, messages, target):
        machine, name, person, route = target
        for m in messages:
            m.update(recipient=machine, agent=name, recipient_person=person, route=route, redeliver=True)
        self.lock.notify_all()
        return len(messages)

    def at_inbox(self, team, row):
        """Messages routed to a person's inbox (route inbox) at that inbox, not answered."""
        return [m for m in self.messages.values() if m["team"] == team and m.get("route") == "inbox"
                and m["recipient"] == row["machine"] and m.get("agent") in (row["agent"], None)
                and m["delivery_state"] != "replied"]

    def set_my_inbox(self, caller, data):
        """C8.1, C13 L4: an agent on the CALLING machine, or ``{"none": true}``; idempotent."""
        email, team = self.owners.get(caller["id"]), caller["team"]
        previous = self.inbox_of(email, team)
        if data == {"none": True}:
            moved = 0
            if previous is not None:
                moved = self.retarget([m for m in self.at_inbox(team, previous) if m["acked_at"] is None],
                                      (None, None, email, "person"))
            self.inboxes[(email, team)] = None
            return 200, {"inbox": None, "moved": moved, "stayed": 0, "delivered_waiting": 0}
        if not isinstance(data, dict) or set(data) - {"machine", "agent", "deliver_waiting"} \
                or not isinstance(data.get("agent"), str) or not isinstance(data.get("deliver_waiting", True), bool):
            raise ApiFail(400, "invalid", 'body must be {"machine", "agent", "deliver_waiting"} or {"none": true}')
        if data.get("machine", caller["handle"]) != caller["handle"]:
            raise ApiFail(400, "inbox_invalid", INBOX_INVALID.format(machine=str(data.get("machine"))[:32]))
        name = data["agent"]
        if not name.strip() or name.startswith("-") or not self.pickable(caller["id"], name):
            raise ApiFail(400, "inbox_invalid", INBOX_INVALID.format(machine=caller["handle"]))
        row = {"machine": caller["id"], "agent": name, "set_at": now()}
        if previous is not None and previous.get("set_at") and (previous["machine"], previous["agent"]) == \
                (caller["id"], name):
            return 200, {"inbox": self.inbox_json(previous), "moved": 0, "stayed": 0, "delivered_waiting": 0}
        self.inboxes[(email, team)] = row
        target = self.inbox_target(email, team)
        moved = stayed = delivered = 0
        if previous is not None:
            old = self.at_inbox(team, previous)
            stayed = sum(1 for m in old if m["acked_at"] is not None)
            moved = self.retarget([m for m in old if m["acked_at"] is None], target)
        if data.get("deliver_waiting", True):
            delivered = self.retarget(self.waiting(email, team), target)
        return 200, {"inbox": self.inbox_json(row), "moved": moved, "stayed": stayed, "delivered_waiting": delivered}

    def person_inbox_page(self, email, query):
        """``/person/inbox``: the raw ``person_inbox`` entries a test seeded, and the messages stored for the person
        (with ``routed=1``, also those routed to their own machines that they did not send)."""
        after = int(query.get("after", ["0"])[0])
        routed = query.get("routed", ["0"])[0] == "1"
        include_acked = query.get("include_acked", ["false"])[0] == "true"
        mine = {aid for aid, e in self.owners.items() if e == email}
        page = [m for m in self.person_inbox.get(email, []) if m["seq"] > after]
        for m in self.messages.values():
            to_me = m["recipient_person"] == email or (
                routed and m["recipient"] in mine and self.sender_person(m) != email)
            if not to_me or not (m["seq"] > after or (m["redeliver"] and m["acked_at"] is None)):
                continue
            if m["acked_at"] is not None and m["recipient_person"] == email and not include_acked:
                continue
            page.append(self.render(m, reader_email=email))
        page.sort(key=lambda m: m["seq"])
        page = page[:min(int(query.get("limit", ["100"])[0]), 500)]
        return 200, {"messages": page, "cursor": max([after, *(m["seq"] for m in page)])}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: FakeState = None

    def log_message(self, *args):
        pass

    def _send(self, status, payload, headers=None):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _drop(self):
        self.close_connection = True
        try:
            self.connection.shutdown(2)
        except OSError:
            pass

    def _handle(self, method):
        st = self.state
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        with st.lock:
            st.requests.append((method, path, dict(self.headers)))
            action = None
            for fault in st.faults:
                if fault[0] == method and fault[1].search(path) and fault[3] > 0:
                    fault[3] -= 1
                    action = fault[2]
                    break
            redirect = st.redirect_to
        if redirect:
            self.send_response(307)
            self.send_header("Location", redirect + path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if action == "drop":
            return self._drop()
        if isinstance(action, tuple):  # (status, code, headers)
            return self._send(action[0], {"error": {"code": action[1], "message": action[1]}}, action[2])
        try:
            result = self._route(method, path, query, raw)
            if result[0] == "raw":
                if action == "drop_after":
                    return self._drop()
                data, headers = result[1], result[2]
                self.send_response(200)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            status, payload = result[0], result[1]
            extra_headers = result[2] if len(result) > 2 else None
        except ApiFail as exc:
            status, payload = exc.status, {"error": {"code": exc.code, "message": exc.message,
                                                     **getattr(exc, "extra", {})}, **exc.top}
            headers = exc.headers
        else:
            headers = extra_headers
        if action == "drop_after":  # committed, but the response is lost
            return self._drop()
        self._send(status, payload, headers)

    def _route(self, method, path, query, raw):
        st = self.state
        if not path.startswith("/api/v1/"):
            raise ApiFail(404, "not_found")
        route = path[len("/api/v1"):]
        if method == "GET" and route == "/health":
            return 200, {"ok": True, "db": "ok"}
        if method == "POST" and route == "/app/login":
            with st.lock:
                try:
                    body = json.loads(raw)
                except ValueError:
                    raise ApiFail(400, "invalid") from None
                st.login_bodies.append(body)
                return st.app_login(body)  # every success carries a known-device token (§18.1 item 2)
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer rps_") and (route.startswith("/person/") or route == "/app/handoff"):
            with st.lock:
                return self._person_route(method, route, query, raw, st.person_sessions.get(auth[7:]), auth[7:])
        with st.lock:
            agent_id = st.tokens.get(auth[7:]) if auth.startswith("Bearer ") else None
            if agent_id is None:
                raise ApiFail(401, "unauthorized")
            caller = st.agents[agent_id]
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                raise ApiFail(400, "invalid", "bad json") from None
            if method == "GET" and route == "/me":
                return 200, {"agent": {"handle": caller["handle"], "display_name": caller["display_name"],
                                       "team": {"slug": caller["team"], "name": st.teams[caller["team"]]}},
                             "credential": {"prefix": auth[7:15], "scopes": ["messages:read", "messages:send", "messages:ack"]},
                             "delivery_history": caller["id"] in st.delivered,
                             **({"owner": ({"email": st.owners[caller["id"]],
                                            "display_name": st.name_of(st.owners[caller["id"]])}
                                           if caller["id"] in st.owners else None)} if st.me_owner else {})}
            if method == "POST" and route == "/app/sign-out":
                return st.sign_out(caller)
            if method == "PUT" and route == "/presence":
                if presence_problem(body):
                    raise ApiFail(400, "invalid", presence_problem(body))
                st.presence[caller["handle"]] = body["status"]
                st.presence_bodies.append((caller["handle"], body))
                if "agents" in body:
                    st.directory[caller["handle"]] = body["agents"]
                if "client" in body:
                    st.clients[caller["handle"]] = body["client"]
                return 200, {"presence": {"status": body["status"], "expires_at": "test-expiry"},
                             "target": st.targets.get(caller["team"])}
            if method == "GET" and route == "/agents":  # C13 M2: the caller's OWN machines, the old shape
                def machine(handle):
                    c = st.clients.get(handle)
                    return None if c is None else {"client_version": c["version"], "update_mode": c["update_mode"],
                                                   "update_state": c["update_state"], "error": c.get("error"),
                                                   "seen_at": "now"}
                me = st.owners.get(caller["id"])
                return 200, {"agents": [{"handle": a["handle"], "display_name": a["display_name"],
                                         "owner": st.name_of(me),
                                         "active": a["active"], "machine": machine(a["handle"]),
                                         "agents": [{k: v for k, v in e.items() if k != "key"}
                                                    for e in st.directory.get(a["handle"], [])]}
                                        for a in sorted(st.agents.values(), key=lambda a: a["handle"])
                                        if a["team"] == caller["team"] and st.owners.get(a["id"]) == me]}
            if method == "GET" and route == "/people":
                return st.people(caller)
            if route == "/me/inbox" and method == "GET":
                return st.my_inbox(caller)
            if route == "/me/inbox" and method == "PUT":
                return st.set_my_inbox(caller, body)
            if route == "/routing" and method in ("GET", "PUT"):
                if method == "PUT":
                    if not isinstance(body, dict) or set(body) != {"routing"} or \
                            body["routing"] not in ("all", "inbox-only"):
                        raise ApiFail(400, "invalid")
                    st.routing[caller["id"]] = body["routing"]
                return 200, {"routing": st.routing.get(caller["id"], "all")}
            if method == "POST" and route == "/messages":
                return st.send(caller, body)
            if method == "GET" and route == "/inbox":
                return st.inbox(caller, query)
            if method == "GET" and route == "/conversations":
                return st.conversations_list(caller, query)
            m = re.fullmatch(r"/conversations/([0-9a-f-]{36})/(archive|unarchive)", route)
            if m and method == "POST":
                return st.archive(caller, m.group(1), m.group(2) == "archive", body)
            m = re.fullmatch(r"/messages/([0-9a-f-]{36})(/ack|/events)?", route)
            if m:
                mid, tail = m.group(1), m.group(2)
                if method == "GET" and not tail:
                    return 200, {"message": st.render(st.visible(caller, mid), caller)}
                if method == "POST" and tail == "/ack":
                    return st.ack(caller, mid)
                if method == "POST" and tail == "/events":
                    return st.event(caller, mid, body)
            m = re.fullmatch(r"/messages/([0-9a-f-]{36})/attachments/([0-9a-f-]{36})", route)
            if m and method == "GET":
                return st.download(caller, m.group(1), m.group(2))
            m = re.fullmatch(r"/conversations/([0-9a-f-]{36})/messages", route)
            if m and method == "GET":
                return st.conversation_messages(caller, m.group(1), query)
        raise ApiFail(404, "not_found")

    def _person_route(self, method, route, query, raw, session, token):
        """A small part of the person API (§16.4) for client tests; the real server is
        exercised by tests/e2e/test_person_cli.py."""
        st = self.state
        if session is None:
            raise ApiFail(401, "unauthorized")
        email = session["email"]
        if method == "GET" and route == "/person/me":
            user = st.users[email]
            return 200, {"user": {"display_name": st.name_of(email), "email": email},
                         "teams": [{"slug": t, "name": st.teams[t]} for t in user["teams"]],
                         "session": {"created_at": "t", "expires_at": "t"}}
        if method == "GET" and route == "/person/inbox":
            return st.person_inbox_page(email, query)
        if method == "POST" and route == "/person/sign-out":
            del st.person_sessions[token]
            return 200, {"signed_out": True}
        if method == "POST" and route == "/app/handoff":
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(body.get("app_install_hash"))):
                raise ApiFail(400, "invalid")
            return 200, {"code": "rhc_x", "expires_at": "t", "url": "https://raincli.example/app/handoff?code=rhc_x"}
        raise ApiFail(404, "not_found")

    def do_GET(self):
        self._handle("GET")

    def do_PUT(self):
        self._handle("PUT")

    def do_POST(self):
        self._handle("POST")


class FakeApi:
    def __init__(self):
        self.state = FakeState()
        handler = type("Handler", (_Handler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        with self.state.lock:
            self.state.lock.notify_all()
        self.server.shutdown()
        self.server.server_close()


class _RecorderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log = None

    def log_message(self, *args):
        pass

    def _any(self):
        self.log.append((self.command, self.path, dict(self.headers)))
        data = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _any


class Recorder:
    """A second origin that records every request (the redirect target)."""

    def __init__(self):
        self.requests = []
        handler = type("Rec", (_RecorderHandler,), {"log": self.requests})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
