"""In-process fake of the RainCLI agent API (protocol section 3) on 127.0.0.1.

It implements the semantics the client and connector depend on: bearer auth,
idempotent send, recipient-only ack, events only after ack, long-poll inbox,
capacity limits, replied state, and fault injection (503/429, dropped
connections before or after processing, and redirects).
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


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ApiFail(Exception):
    def __init__(self, status, code, message="", headers=None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message or code, headers or {}


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
        self.conversations = {}  # id -> {"pair": frozenset, "team": slug}
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
        self.owners = {}  # agent id -> email
        self.delivered = set()  # agent ids with delivery history (they rotate only with previous_token)
        self.login_failures = {}  # email -> failures
        self.login_bodies = []  # every /app/login body received (tests check what was sent)
        self.inbox_queries = []  # (handle, routing=1?) for every inbox poll

    # -- setup -----------------------------------------------------------

    def add_agent(self, handle, team="alpha", display_name=None):
        self.teams.setdefault(team, team.title())
        agent_id = str(uuid.uuid4())
        self.agents[agent_id] = {"id": agent_id, "handle": handle, "team": team,
                                 "display_name": display_name or handle.title(), "active": True}
        token = "rca_" + secrets.token_urlsafe(32)
        self.tokens[token] = agent_id
        return token

    def add_user(self, email, password, teams=("alpha",)):
        for team in teams:
            self.teams.setdefault(team, team.title())
        self.users[email] = {"password": password, "teams": list(teams)}

    def app_login(self, body):
        """15.1 as amended by 15.8: generic errors, team choice, name rules, rotation with proof."""
        if not isinstance(body, dict) or set(body) - {"email", "password", "machine_name", "team",
                                                      "previous_token", "replace"}:
            raise ApiFail(400, "invalid")
        user = self.users.get(body.get("email"))
        if self.login_failures.get(body.get("email"), 0) >= 5:
            raise ApiFail(429, "rate_limited", headers={"Retry-After": "900"})
        if user is None or body.get("password") != user["password"]:
            self.login_failures[body.get("email")] = self.login_failures.get(body.get("email"), 0) + 1
            raise ApiFail(401, "invalid_credentials")
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
            token = self.add_agent(name, team=team)
            self.owners[self.tokens[token]] = body["email"]
            status = 201
        return status, {"api_url": "https://raincli.example", "token": token, "handle": name,
                        "team": {"slug": team, "name": self.teams[team]}, "rotated": rotated}

    def sign_out(self, caller):
        caller["active"] = False
        for token in [t for t, aid in self.tokens.items() if aid == caller["id"]]:
            del self.tokens[token]
        return 200, {"signed_out": True}

    def person_message(self, recipient_handle, email, display_name, body, agent=None, team="alpha"):
        """A message from a person (the website or app) to a machine or one of its agents."""
        recipient = self.agent_by_handle(team, recipient_handle)
        self.seq += 1
        ts = now()
        mid = str(uuid.uuid4())
        conv = str(uuid.uuid4())
        self.conversations[conv] = {"pair": frozenset((recipient["id"],)), "team": team}
        self.messages[mid] = {"id": mid, "conversation_id": conv, "in_reply_to": None, "sender": recipient["id"],
                              "person": {"email": email, "display_name": display_name}, "agent": agent,
                              "from_agent": None, "kind": "message", "recipient": recipient["id"], "body": body,
                              "created_at": ts, "seq": self.seq, "acked_at": None, "delivery_state": "stored",
                              "delivery_updated_at": ts, "attachments": []}
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

    def render(self, m):
        person = m.get("person")  # {"email", "display_name"} for a person sender (§16.1)
        sender = "@" + person["email"] if person else self.agents[m["sender"]]["handle"]
        to_handle = self.agents[m["recipient"]]["handle"]
        to_endpoint = {"machine": to_handle, **({"agent": m["agent"]} if m.get("agent") else {})}
        from_endpoint = ({"person": person["email"], "display_name": person["display_name"]} if person
                         else {"machine": sender})
        return {"id": m["id"], "conversation_id": m["conversation_id"], "in_reply_to": m["in_reply_to"],
                "from": sender, "to": to_handle, "from_endpoint": from_endpoint, "to_endpoint": to_endpoint,
                "from_agent": m.get("from_agent"), "kind": m.get("kind", "message"), "hold_reason": None,
                "body": m["body"], "created_at": m["created_at"], "seq": m["seq"],
                "acked_at": m["acked_at"], "delivery_state": m["delivery_state"],
                "delivery_updated_at": m["delivery_updated_at"],
                "attachments": [{k: a[k] for k in ("id", "filename", "media_type", "size", "sha256")}
                                for a in m["attachments"]]}

    def visible(self, caller, message_id):
        m = self.messages.get(message_id)
        if m is None or caller["id"] not in (m["sender"], m["recipient"]):
            raise ApiFail(404, "not_found")
        return m

    # -- endpoints -------------------------------------------------------

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
        text = body.get("body")
        if body_problem(text):
            raise ApiFail(400, "invalid", body_problem(text))
        to, agent_name = body.get("to"), None
        if isinstance(to, dict):  # §16.1 endpoint objects (machine, or machine and agent)
            if set(to) - {"machine", "agent"} or "machine" not in to:
                raise ApiFail(400, "invalid", "unsupported endpoint in the fake")
            to, agent_name = to["machine"], to.get("agent")
        recipient = self.agent_by_handle(caller["team"], to)
        if recipient is None or not recipient["active"] or (recipient["id"] == caller["id"] and not agent_name):
            raise ApiFail(400, "invalid", "unknown recipient")
        pair = frozenset((caller["id"], recipient["id"]))
        in_reply_to = body.get("in_reply_to")
        if in_reply_to:
            parent = self.visible(caller, in_reply_to)
            other = parent["recipient"] if parent["sender"] == caller["id"] else parent["sender"]
            if other != recipient["id"]:
                raise ApiFail(400, "invalid", "reply recipient mismatch")
            conv = parent["conversation_id"]
            if body.get("conversation_id") and body["conversation_id"] != conv:
                raise ApiFail(400, "invalid", "conversation mismatch")
        elif body.get("conversation_id"):
            conv = body["conversation_id"]
            if self.conversations.get(conv, {}).get("pair") != pair:
                raise ApiFail(400, "invalid", "bad conversation")
        else:
            conv = next((cid for cid, c in self.conversations.items() if c["pair"] == pair), None)
        files = self.decode_attachments(body.get("attachments"))
        existing = self.messages.get(mid)
        if existing is not None:
            same = (existing["sender"] == caller["id"] and existing["recipient"] == recipient["id"]
                    and existing["body"] == text and existing["in_reply_to"] == in_reply_to
                    and (conv is None or existing["conversation_id"] == conv)
                    and [(a["filename"], a["sha256"]) for a in existing["attachments"]]
                    == [(f["filename"], f["sha256"]) for f in files])
            if same:
                return 200, {"message": self.render(existing), "created": False}
            raise ApiFail(409, "id_conflict")
        pending = sum(1 for m in self.messages.values()
                      if m["recipient"] == recipient["id"] and m["acked_at"] is None)
        if pending >= self.max_pending:
            raise ApiFail(429, "inbox_full")
        if conv is None:
            conv = str(uuid.uuid4())
            self.conversations[conv] = {"pair": pair, "team": caller["team"]}
        self.seq += 1
        ts = now()
        m = {"id": mid, "conversation_id": conv, "in_reply_to": in_reply_to, "sender": caller["id"],
             "agent": agent_name, "from_agent": body.get("from_agent"), "kind": body.get("kind", "message"),
             "recipient": recipient["id"], "body": text, "created_at": ts, "seq": self.seq,
             "acked_at": None, "delivery_state": "stored", "delivery_updated_at": ts,
             "attachments": [dict(f, id=str(uuid.uuid4()), media_type="text/markdown") for f in files]}
        self.messages[mid] = m
        self.send_commits += 1
        if in_reply_to:
            parent = self.messages[in_reply_to]
            if parent["recipient"] == caller["id"]:
                parent["delivery_state"], parent["delivery_updated_at"] = "replied", ts
        self.lock.notify_all()
        return 201, {"message": self.render(m), "created": True}

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
        after = int(query.get("after", ["0"])[0])
        limit = min(int(query.get("limit", ["100"])[0]), 500)
        wait = min(float(query.get("wait", ["0"])[0]), 25)
        include_acked = query.get("include_acked", ["false"])[0] == "true"
        routing = query.get("routing", ["0"])[0] == "1"
        self.inbox_queries.append((caller["handle"], routing))

        def matches():
            # §16.2 capability gate: without routing=1, no named-agent or person-sent messages.
            return sorted((m for m in self.messages.values()
                           if m["recipient"] == caller["id"] and m["seq"] > after
                           and (routing or (not m.get("agent") and not m.get("person")))
                           and (include_acked or m["acked_at"] is None)),
                          key=lambda m: m["seq"])[:limit]

        deadline = time.monotonic() + wait
        found = matches()
        while not found and time.monotonic() < deadline:
            self.lock.wait(timeout=max(0.0, deadline - time.monotonic()))
            found = matches()
        return 200, {"messages": [self.render(m) for m in found],
                     "cursor": found[-1]["seq"] if found else after}

    def ack(self, caller, mid):
        m = self.visible(caller, mid)
        if m["recipient"] != caller["id"]:
            raise ApiFail(403, "forbidden", "only the recipient may ack")
        if self.on_ack:
            self.on_ack(mid)
        acked = m["acked_at"] is None
        if acked:
            m["acked_at"] = now()
            m["delivery_state"], m["delivery_updated_at"] = "received", m["acked_at"]
        return 200, {"message": self.render(m), "acked": acked}

    def event(self, caller, mid, body):
        m = self.visible(caller, mid)
        if m["recipient"] != caller["id"]:
            raise ApiFail(403, "forbidden")
        if m["acked_at"] is None:
            raise ApiFail(409, "not_acked")
        if not isinstance(body, dict) or body.get("state") not in EVENT_STATES \
                or len(body.get("detail", "")) > 500:
            raise ApiFail(400, "invalid")
        self.events.append((mid, body["state"], body.get("detail", "")))
        m["delivery_state"], m["delivery_updated_at"] = body["state"], now()
        return 200, {"message": self.render(m)}

    def conversations_list(self, caller):
        out = []
        for cid, c in self.conversations.items():
            if caller["id"] not in c["pair"]:
                continue
            msgs = [m for m in self.messages.values() if m["conversation_id"] == cid]
            peer = next(iter(c["pair"] - {caller["id"]}))
            last = max(msgs, key=lambda m: m["seq"])
            out.append({"id": cid, "peer": self.agents[peer]["handle"], "last_seq": last["seq"],
                        "last_at": last["created_at"],
                        "unacked": sum(1 for m in msgs if m["recipient"] == caller["id"] and not m["acked_at"])})
        return 200, {"conversations": out}

    def conversation_messages(self, caller, cid, query):
        c = self.conversations.get(cid)
        if c is None or caller["id"] not in c["pair"]:
            raise ApiFail(404, "not_found")
        after = int(query.get("after", ["0"])[0])
        limit = int(query.get("limit", ["100"])[0])
        msgs = sorted((m for m in self.messages.values()
                       if m["conversation_id"] == cid and m["seq"] > after), key=lambda m: m["seq"])[:limit]
        return 200, {"messages": [self.render(m) for m in msgs], "cursor": msgs[-1]["seq"] if msgs else after}


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
            status, payload = result
        except ApiFail as exc:
            status, payload = exc.status, {"error": {"code": exc.code, "message": exc.message,
                                                     **getattr(exc, "extra", {})}}
            headers = exc.headers
        else:
            headers = None
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
                return st.app_login(body)
        auth = self.headers.get("Authorization", "")
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
                             "delivery_history": caller["id"] in st.delivered}
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
            if method == "GET" and route == "/agents":
                def machine(handle):
                    c = st.clients.get(handle)
                    return None if c is None else {"client_version": c["version"], "update_mode": c["update_mode"],
                                                   "update_state": c["update_state"], "error": c.get("error"),
                                                   "seen_at": "now"}
                return 200, {"agents": [{"handle": a["handle"], "display_name": a["display_name"],
                                         "active": a["active"], "machine": machine(a["handle"]),
                                         "agents": [{k: v for k, v in e.items() if k != "key"}
                                                    for e in st.directory.get(a["handle"], [])]}
                                        for a in st.agents.values() if a["team"] == caller["team"]]}
            if method == "POST" and route == "/messages":
                return st.send(caller, body)
            if method == "GET" and route == "/inbox":
                return st.inbox(caller, query)
            if method == "GET" and route == "/conversations":
                return st.conversations_list(caller)
            m = re.fullmatch(r"/messages/([0-9a-f-]{36})(/ack|/events)?", route)
            if m:
                mid, tail = m.group(1), m.group(2)
                if method == "GET" and not tail:
                    return 200, {"message": st.render(st.visible(caller, mid))}
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
