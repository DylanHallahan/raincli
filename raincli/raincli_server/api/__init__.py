"""Agent API (/api/v1). Owned by the API builder; see docs/raincli-protocol.md §3.

The API is a sub-application mounted at ``/api/v1`` so its error envelope,
body-size limit and exception handlers never touch web routes. ``GET
/api/v1/health`` stays on the parent app (defined in ``app.py``) and is matched
before the mount.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
from collections import deque
from typing import Callable

from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message as ASGIMessage, Receive, Scope, Send

from raincli_server import identity, messaging, presence, security
from raincli_server.db import session_scope
from raincli_server.identity import AgentAuth
from raincli_server.messaging import MessagingError
from raincli_server.models import ROUTING_POLICIES, Agent
from raincli_server.web import auth as web_auth

log = logging.getLogger("raincli_server.api")

MAX_BODY_BYTES = 64 * 1024
MAX_SEND_BODY_BYTES = 2 * 1024 * 1024  # POST /messages carries base64 attachments (protocol §8)
# PUT /presence: a contract-maximal report (100 agents with 64 astral-plane names, which Python's
# default json.dumps escapes to 12 bytes each, plus the client block) is about 95 KB (protocol §14.1).
MAX_PRESENCE_BODY_BYTES = 128 * 1024
MAX_WAIT_SECONDS = 25
POLL_INTERVAL = 0.5
PREFIX = "/api/v1"
MAX_SEQ = 2**63 - 1

_STATUS_CODES = {
    400: "invalid", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
    409: "id_conflict", 413: "too_large", 429: "rate_limited", 503: "unavailable",
}


def error_response(status: int, code: str, message: str, headers: dict | None = None,
                   extra: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}, **(extra or {})},
                        status_code=status, headers=headers)


class ApiError(MessagingError):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None,
                 extra: dict | None = None):
        super().__init__(status, code, message, extra)  # extra: e.g. "teams" for team_choice_required
        self.headers = headers


# Middleware and limits ------------------------------------------------------------

class BodySizeLimit:
    """Reject request bodies over the limit with 413, by header or while streaming.

    ``POST .../messages`` gets ``send_limit``, ``PUT .../presence`` gets ``presence_limit``;
    every other API request gets ``limit``.
    """

    def __init__(self, app: ASGIApp, limit: int = MAX_BODY_BYTES, send_limit: int = MAX_SEND_BODY_BYTES,
                 presence_limit: int = MAX_PRESENCE_BODY_BYTES):
        self.app = app
        self.default_limit = limit
        self.send_limit = send_limit
        self.presence_limit = presence_limit

    def limit_for(self, scope: Scope) -> int:
        method, path = scope.get("method"), scope.get("path", "").rstrip("/")
        if method == "POST" and (path.endswith("/messages") or path.endswith("/person/send")):
            return self.send_limit
        if method == "PUT" and path.endswith("/presence"):
            return self.presence_limit
        return self.default_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope)
        too_large = error_response(413, "too_large", f"request body exceeds {limit} bytes")
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = limit + 1
                if declared > limit:
                    await too_large(scope, receive, send)
                    return
        received = 0
        started = False

        async def limited_receive() -> ASGIMessage:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: ASGIMessage) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await too_large(scope, receive, send)


class _BodyTooLarge(Exception):
    pass


class RateLimiter:
    """Sliding one-minute window per credential, in process memory (per app instance)."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic):
        self.per_minute = per_minute
        self.clock = clock
        self._hits: dict[object, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: object) -> int | None:
        """Record a hit; return seconds to wait if the key is over its limit, else None."""
        now = self.clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - 60:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return max(1, math.ceil(hits[0] + 60 - now))
            hits.append(now)
            if len(self._hits) > 10000:  # drop idle keys so memory stays bounded
                for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - 60]:
                    del self._hits[k]
            return None


# Registration ---------------------------------------------------------------------

def register(app: FastAPI) -> None:
    """Attach agent API routes, error handlers and middleware to ``app``."""
    api = build_api(app)
    app.mount(PREFIX, api)
    app.state.api = api


def build_api(parent: FastAPI) -> FastAPI:
    settings = parent.state.settings
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    api.state.rate_limiter = RateLimiter(settings.rate_limit_per_min)
    # The very LoginLimiter the website uses: a lockout on either applies to both (protocol §15.8 H1).
    api.state.login_limiter = parent.state.web_login_limiter
    api.add_middleware(BodySizeLimit, limit=MAX_BODY_BYTES, send_limit=MAX_SEND_BODY_BYTES,
                       presence_limit=MAX_PRESENCE_BODY_BYTES)
    limiter: RateLimiter = api.state.rate_limiter

    def sessionmaker():
        return parent.state.sessionmaker

    # Error envelope --------------------------------------------------------------

    @api.exception_handler(MessagingError)
    async def _messaging_error(request: Request, exc: MessagingError):
        return error_response(exc.status, exc.code, exc.message, getattr(exc, "headers", None),
                              getattr(exc, "extra", None))

    @api.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        code = _STATUS_CODES.get(exc.status_code, "error")
        message = "not found" if exc.status_code == 404 else str(exc.detail)
        return error_response(exc.status_code, code, message, getattr(exc, "headers", None))

    @api.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        fields = sorted({".".join(str(p) for p in e.get("loc", ())[1:]) for e in exc.errors()})
        return error_response(400, "invalid", "invalid parameters: " + ", ".join(f for f in fields if f))

    @api.exception_handler(OperationalError)
    @api.exception_handler(InterfaceError)
    async def _db_error(request: Request, exc: Exception):
        return error_response(503, "unavailable", "the service is temporarily unavailable; retry later")

    # Auth ------------------------------------------------------------------------

    def authenticate(session: Session, request: Request, scope: str | None, *, count: bool = True) -> AgentAuth:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        auth = identity.authenticate_agent(session, token.strip() if scheme.lower() == "bearer" else None,
                                           touch=count)
        if auth is None:
            raise ApiError(401, "unauthorized", "missing, invalid or revoked credential",
                           {"WWW-Authenticate": "Bearer"})
        if count:
            retry = limiter.check(auth.credential.id)
            if retry is not None:
                raise ApiError(429, "rate_limited", "too many requests for this credential",
                               {"Retry-After": str(retry)})
        if scope is not None and not auth.has_scope(scope):
            raise ApiError(403, "forbidden", f"credential lacks the {scope} scope")
        return auth

    def run(request: Request, scope: str | None, work: Callable[[Session, AgentAuth], object], *,
            counted: bool = True):
        def _sync():
            with session_scope(sessionmaker()) as session:
                auth = authenticate(session, request, scope, count=counted)
                return work(session, auth)
        return run_in_threadpool(_sync)

    async def authed_json_body(request: Request, scope: str) -> object:
        """Authenticate, rate-limit and check scope *before* reading the body (protocol §11.4).

        The caller then runs its work with ``counted=False``, which re-checks the
        credential (a revocation in between still gives 401) without a second rate-limit hit.
        """
        await run(request, scope, lambda session, auth: None)
        raw = await request.body()
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise ApiError(400, "invalid", "request body must be valid JSON") from None

    def m(session: Session, msg) -> dict:
        return messaging.message_json(session, msg)

    # Endpoints -------------------------------------------------------------------

    @api.get("/me")
    async def me(request: Request):
        def work(session, auth: AgentAuth):
            return {
                "agent": {
                    "handle": auth.agent.handle, "display_name": auth.agent.display_name,
                    "team": {"slug": auth.team.slug, "name": auth.team.name},
                },
                "credential": {"prefix": auth.credential.prefix, "scopes": list(auth.credential.scopes)},
                # The H2 replace rule's history (inbox role published, or a message recipient):
                # migration asks for the connector config before choosing machine mode.
                "delivery_history": identity.has_delivery_history(session, auth.agent),
            }
        return await run(request, None, work)

    @api.get("/agents")
    async def agents(request: Request):
        def work(session, auth: AgentAuth):
            rows = session.scalars(select(Agent).where(Agent.team_id == auth.team.id).order_by(Agent.handle))
            return {"agents": presence.directory(session, list(rows))}
        return await run(request, "messages:read", work)

    @api.put("/presence")
    async def update_presence(request: Request):
        data = await authed_json_body(request, "messages:ack")
        return await run(request, "messages:ack",
                         lambda session, auth: presence.publish(session, auth.agent, data),
                         counted=False)

    @api.post("/messages")
    async def send(request: Request):
        data = await authed_json_body(request, "messages:send")

        def work(session, auth: AgentAuth):
            req = messaging.SendRequest.parse(data, auth.agent.handle)
            msg, created = messaging.send_message(
                session, auth.agent, id=req.id, to=req.to, body=req.body,
                conversation_id=req.conversation_id, in_reply_to=req.in_reply_to,
                max_pending=settings.max_pending, attachments=req.attachments,
                from_agent=req.from_agent, kind=req.kind,
            )
            return created, {"message": m(session, msg), "created": created}
        created, body = await run(request, "messages:send", work, counted=False)
        return JSONResponse(body, status_code=201 if created else 200)

    @api.get("/inbox")
    async def inbox(
        request: Request,
        after: int = Query(0, ge=0, le=MAX_SEQ),
        limit: int = Query(100, ge=1, le=MAX_SEQ),
        wait: float = Query(0, ge=0),
        include_acked: bool = Query(False),
        routing: int = Query(0, ge=0, le=1),
    ):
        wait = min(wait, MAX_WAIT_SECONDS)
        limit = min(limit, messaging.INBOX_LIMIT_MAX)
        deadline = time.monotonic() + wait
        first = True
        while True:
            def poll(count: bool = first):
                # Re-authenticate on every poll so revocation ends a long-poll immediately.
                with session_scope(sessionmaker()) as session:
                    auth = authenticate(session, request, "messages:read", count=count)
                    msgs, cursor = messaging.inbox(
                        session, auth.agent, after=after, limit=limit, include_acked=include_acked,
                        routing_capable=bool(routing))
                    return {"messages": messaging.messages_json(session, msgs), "cursor": cursor}
            result = await run_in_threadpool(poll)
            first = False
            remaining = deadline - time.monotonic()
            if result["messages"] or remaining <= 0 or await request.is_disconnected():
                return result
            await asyncio.sleep(min(POLL_INTERVAL, remaining))

    @api.put("/routing")
    async def set_routing(request: Request):
        """The machine's routing policy (§16.5): ``{"routing": "all" | "inbox-only"}``."""
        data = await authed_json_body(request, "messages:ack")

        def work(session, auth: AgentAuth):
            if not isinstance(data, dict) or set(data) != {"routing"} or data["routing"] not in ROUTING_POLICIES:
                raise ApiError(400, "invalid", 'body must be {"routing": "all" | "inbox-only"}')
            auth.agent.routing = data["routing"]
            return {"routing": auth.agent.routing}
        return await run(request, "messages:ack", work, counted=False)

    @api.get("/routing")
    async def get_routing(request: Request):
        return await run(request, None, lambda session, auth: {"routing": auth.agent.routing})

    @api.get("/messages/{message_id}")
    async def get_message(request: Request, message_id: str):
        def work(session, auth: AgentAuth):
            return {"message": m(session, messaging.get_visible_message(session, auth.agent, message_id))}
        return await run(request, "messages:read", work)

    @api.post("/messages/{message_id}/ack")
    async def ack(request: Request, message_id: str):
        def work(session, auth: AgentAuth):
            msg, acked = messaging.ack(session, auth.agent, message_id)
            return {"message": m(session, msg), "acked": acked}
        return await run(request, "messages:ack", work)

    @api.post("/messages/{message_id}/events")
    async def events(request: Request, message_id: str):
        data = await authed_json_body(request, "messages:ack")

        def work(session, auth: AgentAuth):
            if not isinstance(data, dict) or set(data) - {"state", "detail"} or "state" not in data:
                raise ApiError(400, "invalid", 'body must be {"state": ..., "detail": ...}')
            msg = messaging.record_event(session, auth.agent, message_id, data["state"], data.get("detail"))
            return {"message": m(session, msg)}
        return await run(request, "messages:ack", work, counted=False)

    @api.get("/conversations")
    async def conversations(request: Request, limit: int = Query(50, ge=1, le=MAX_SEQ)):
        def work(session, auth: AgentAuth):
            return {"conversations": messaging.list_conversations(session, auth.agent, limit=limit)}
        return await run(request, "messages:read", work)

    @api.get("/conversations/{conversation_id}/messages")
    async def conversation_messages(
        request: Request, conversation_id: str,
        after: int = Query(0, ge=0, le=MAX_SEQ), limit: int = Query(100, ge=1, le=MAX_SEQ),
    ):
        def work(session, auth: AgentAuth):
            msgs, cursor = messaging.conversation_messages(
                session, auth.agent, conversation_id, after=after, limit=limit)
            return {"messages": messaging.messages_json(session, msgs), "cursor": cursor}
        return await run(request, "messages:read", work)

    @api.get("/messages/{message_id}/attachments/{attachment_id}")
    async def download_attachment(request: Request, message_id: str, attachment_id: str):
        def work(session, auth: AgentAuth):
            att = messaging.get_attachment_for_agent(session, auth.agent, message_id, attachment_id)
            return att.filename, att.sha256, bytes(att.content)
        filename, sha, content = await run(request, "messages:read", work)
        return Response(content, media_type=None, headers=attachment_headers(filename, sha, len(content)))

    # Machine sign-in (protocol §15.1) -----------------------------------------------

    @api.post("/app/login")
    async def app_login(request: Request):
        raw = await request.body()
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise ApiError(400, "invalid", "request body must be valid JSON") from None
        ip = web_auth.client_ip(request)
        status, body = await run_in_threadpool(
            sign_in, sessionmaker(), api.state.login_limiter, data, ip, settings.public_url)
        return JSONResponse(body, status_code=status)

    from raincli_server.api import person as person_api

    person_api.register(api, sessionmaker=sessionmaker, limiter=limiter, settings=settings, ApiError=ApiError,
                        MAX_WAIT_SECONDS=MAX_WAIT_SECONDS, POLL_INTERVAL=POLL_INTERVAL,
                        attachment_headers=attachment_headers)

    @api.post("/app/sign-out")
    async def app_sign_out(request: Request):
        def work(session, auth: AgentAuth):
            identity.revoke_agent(session, auth.agent)
            log.info("app sign-out: machine %s in team %s revoked", auth.agent.handle, auth.team.slug)
            return {"signed_out": True}
        return await run(request, None, work)

    return api


_LOGIN_FIELDS = {"email", "password", "machine_name", "team", "previous_token", "replace", "person_session",
                 "person_only"}


def sign_in(factory, limiter: web_auth.LoginLimiter, data: object, ip: str, api_url: str) -> tuple[int, dict]:
    """``POST /api/v1/app/login`` (protocol §15.1, §15.8). Never stores, logs or echoes the password.

    Only a wrong password counts against the limiter. ``blocked`` is checked before scrypt runs, and
    once the password is right every outcome calls ``success()`` (§15.8 M2).
    """
    person_only = data.get("person_only", False) if isinstance(data, dict) else False
    required = {"email", "password"} | (set() if person_only is True else {"machine_name"})
    if not isinstance(data, dict) or set(data) - _LOGIN_FIELDS or not required <= set(data):
        raise ApiError(400, "invalid", 'body must be {"email", "password", "machine_name", "team"?, '
                                       '"previous_token"?, "replace"?, "person_session"?, "person_only"?}')
    email, password, machine_name, team_slug, previous_token = (
        data.get(k) for k in ("email", "password", "machine_name", "team", "previous_token"))
    replace, want_person = data.get("replace", False), data.get("person_session", False)
    if (not all(isinstance(v, str) for v in (email, password))
            or not all(v is None or isinstance(v, str) for v in (machine_name, team_slug, previous_token))
            or not all(isinstance(v, bool) for v in (replace, want_person, person_only))):
        raise ApiError(400, "invalid", "email, password, machine_name, team and previous_token must be strings, "
                                       "and replace, person_session and person_only booleans")
    if not person_only and machine_name is None:
        raise ApiError(400, "invalid", "machine_name is required")
    if person_only and (previous_token is None or replace):
        raise ApiError(400, "invalid", "person_only needs previous_token, and no replace")
    if machine_name is not None and not security.valid_handle(machine_name):
        raise ApiError(400, "invalid", "machine_name must match ^[a-z][a-z0-9-]{1,31}$")
    if team_slug is not None and not security.valid_slug(team_slug):
        raise ApiError(400, "invalid", "unknown team")
    email = email.strip()[:254]
    if limiter.blocked(ip, email):
        raise ApiError(429, "rate_limited", "too many sign-in attempts; wait a few minutes and try again")
    with session_scope(factory) as session:
        user = identity.authenticate_user(session, email, password) if email and password and len(password) <= 256 else None
        if user is None:
            limiter.failure(ip, email)
            outcome = ApiError(401, "invalid_credentials", "that email and password combination is not correct")
        elif person_only:
            limiter.success(ip, email)
            outcome = _person_only(session, user, previous_token)
        else:
            limiter.success(ip, email)
            outcome = _sign_in_machine(session, user, machine_name, team_slug, api_url,
                                       previous_token=previous_token, replace=replace, person_session=want_person)
    if isinstance(outcome, ApiError):
        raise outcome
    return outcome


def _person_only(session: Session, user, previous_token: str) -> tuple[int, dict] | ApiError:
    """§16.3: a person session for an already signed-in machine's owner. No rotation, no machine change."""
    auth = identity.authenticate_agent(session, previous_token, touch=False)
    if auth is None or auth.agent.owner_user_id != user.id:
        return ApiError(400, "invalid", "person_only needs the current credential of a machine you own")
    token = identity.issue_person_session(session, user, auth.agent)
    log.info("app sign-in: person session added for machine %s in team %s", auth.agent.handle, auth.team.slug)
    return 200, {"person_session": token}


def _sign_in_machine(session: Session, user, machine_name: str, team_slug: str | None, api_url: str, *,
                     previous_token: str | None, replace: bool,
                     person_session: bool = False) -> tuple[int, dict] | ApiError:
    """Choose the team, then create or rotate the machine. Errors are returned, so the caller still
    commits the password-hash upgrade that a successful password check may have made."""
    teams = [team for team, _role in identity.teams_for_user(session, user)]
    if team_slug is None:
        if not teams:
            return ApiError(400, "invalid", "this account is not a member of any team; accept an invitation first")
        if len(teams) > 1:
            return ApiError(409, "team_choice_required", "choose one of your teams",
                            extra={"teams": [{"slug": t.slug, "name": t.name} for t in teams]})
        team = teams[0]
    else:
        team = next((t for t in teams if t.slug == team_slug), None)
        if team is None:
            return ApiError(400, "invalid", "unknown team")
    try:
        agent, token, rotated = identity.sign_in_machine(session, team, user, machine_name,
                                                         previous_token=previous_token, replace=replace)
    except identity.NameTaken:
        return ApiError(409, "name_taken", "that machine name is taken in this team; choose another")
    except identity.MachineLimit:
        return ApiError(409, "machine_limit", f"you already have {identity.MACHINE_LIMIT} active machines in this "
                                              "team; revoke one on the website first")
    except identity.NameInUse:
        return ApiError(409, "name_in_use", "you already have a machine with that name")
    log.info("app sign-in: machine %s in team %s %s", agent.handle, team.slug, "rotated" if rotated else "created")
    body = {"api_url": api_url, "token": token, "handle": agent.handle,
            "team": {"slug": team.slug, "name": team.name}, "rotated": rotated}
    if person_session:  # §16.12 C15: only when asked
        body["person_session"] = identity.issue_person_session(session, user, agent)
    return (200 if rotated else 201), body


def attachment_headers(filename: str, sha256: str, size: int) -> dict[str, str]:
    """Download headers of protocol §8. ``filename`` already passed ``valid_attachment_name``."""
    return {
        "Content-Type": "text/markdown; charset=utf-8",
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
        "X-RainCLI-SHA256": sha256,
        "Content-Length": str(size),
        "Cache-Control": "no-store",
    }
