"""Person API (protocol §16.3, §16.4, §16.10, §16.12 C6, C9, C11).

``Authorization: Bearer rps_…`` is accepted only here (and on ``POST /app/handoff``); machine
credentials are refused on these routes, and person sessions are refused on the agent routes.
Every request re-checks the user, the session's lifetime and, per action, team membership.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import timedelta
from typing import Callable

from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from raincli_server import identity, messaging, security
from raincli_server.db import session_scope
from raincli_server.identity import PersonAuth
from raincli_server.models import HandoffCode, Team

HANDOFF_TTL = timedelta(seconds=60)
PERSON_SEND_FIELDS = frozenset({"id", "to", "body", "conversation_id", "in_reply_to", "attachments", "kind", "team"})


def register(api: FastAPI, *, sessionmaker: Callable, limiter, settings, ApiError, MAX_WAIT_SECONDS: float,
             POLL_INTERVAL: float, attachment_headers: Callable) -> None:

    def authenticate(session: Session, request: Request, scope: str | None, *, count: bool = True) -> PersonAuth:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        auth = identity.authenticate_person(session, token.strip() if scheme.lower() == "bearer" else None,
                                            touch=count)
        if auth is None:
            raise ApiError(401, "unauthorized", "missing, invalid, expired or revoked person session",
                           {"WWW-Authenticate": "Bearer"})
        if count:
            retry = limiter.check(("person", auth.person_session.id))
            if retry is not None:
                raise ApiError(429, "rate_limited", "too many requests for this session", {"Retry-After": str(retry)})
        if scope is not None and not auth.has_scope(scope):
            raise ApiError(403, "forbidden", f"session lacks the {scope} scope")
        return auth

    def run(request: Request, scope: str | None, work: Callable[[Session, PersonAuth], object], *, counted=True):
        def _sync():
            with session_scope(sessionmaker()) as session:
                return work(session, authenticate(session, request, scope, count=counted))
        return run_in_threadpool(_sync)

    async def json_body(request: Request, scope: str) -> object:
        await run(request, scope, lambda session, auth: None)  # authenticate before reading the body
        raw = await request.body()
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise ApiError(400, "invalid", "request body must be valid JSON") from None

    def teams_of(session: Session, auth: PersonAuth) -> list[Team]:
        return [team for team, _role in identity.teams_for_user(session, auth.user)]

    def choose_team(session: Session, auth: PersonAuth, slug: object) -> Team:
        """§16.12 C9: ``team`` is required when the user belongs to several teams."""
        teams = teams_of(session, auth)
        if slug is None:
            if len(teams) == 1:
                return teams[0]
            if not teams:
                raise ApiError(403, "forbidden", "you are not a member of any team")
            raise ApiError(400, "team_required", "choose one of your teams with \"team\"",
                           extra={"teams": [{"slug": t.slug, "name": t.name} for t in teams]})
        team = next((t for t in teams if isinstance(slug, str) and t.slug == slug), None)
        if team is None:
            raise ApiError(400, "invalid", "unknown team")
        return team

    def reply_team(session: Session, auth: PersonAuth, in_reply_to: object) -> Team:
        """The team of the parent message's conversation, when the person may see it and is still
        a member; otherwise the same 404 a reply to an unseen message gets."""
        parent = messaging.get_message_for_person(session, auth.user,
                                                  messaging.parse_uuid(in_reply_to, "in_reply_to"))
        team = next((t for t in teams_of(session, auth) if t.id == parent.team_id), None)
        if team is None:
            raise ApiError(404, "not_found", "in_reply_to message not found")
        return team

    def m(session: Session, msg) -> dict:
        return messaging.message_json(session, msg)

    # Routes ------------------------------------------------------------------------

    @api.get("/person/me")
    async def person_me(request: Request):
        def work(session, auth: PersonAuth):
            ps = auth.person_session
            return {"user": {"display_name": auth.user.display_name, "email": auth.user.email},
                    "teams": [{"slug": t.slug, "name": t.name} for t in teams_of(session, auth)],
                    "session": {"created_at": messaging.iso(ps.created_at),
                                "expires_at": messaging.iso(identity.person_session_expires_at(ps))}}
        return await run(request, "person:read", work)

    @api.get("/person/inbox")
    async def person_inbox(request: Request, after: int = Query(0, ge=0, le=2**63 - 1),
                           limit: int = Query(100, ge=1, le=2**63 - 1), wait: float = Query(0, ge=0),
                           include_acked: bool = Query(False)):
        wait = min(wait, MAX_WAIT_SECONDS)
        limit = min(limit, messaging.INBOX_LIMIT_MAX)
        deadline = time.monotonic() + wait
        first = True
        while True:
            def poll(count: bool = first):
                with session_scope(sessionmaker()) as session:
                    auth = authenticate(session, request, "person:read", count=count)
                    msgs, cursor = messaging.person_inbox(session, auth.user, after=after, limit=limit,
                                                          include_acked=include_acked)
                    return {"messages": messaging.messages_json(session, msgs), "cursor": cursor}
            result = await run_in_threadpool(poll)
            first = False
            remaining = deadline - time.monotonic()
            if result["messages"] or remaining <= 0 or await request.is_disconnected():
                return result
            await asyncio.sleep(min(POLL_INTERVAL, remaining))

    @api.post("/person/messages/{message_id}/ack")
    async def person_ack(request: Request, message_id: str):
        def work(session, auth: PersonAuth):
            msg, acked = messaging.person_ack(session, auth.user, message_id)
            return {"message": m(session, msg), "acked": acked}
        return await run(request, "person:read", work)

    @api.get("/person/messages/{message_id}")
    async def person_message(request: Request, message_id: str):
        def work(session, auth: PersonAuth):
            return {"message": m(session, messaging.get_message_for_person(session, auth.user, message_id))}
        return await run(request, "person:read", work)

    @api.get("/person/conversations")
    async def person_conversations(request: Request, limit: int = Query(50, ge=1, le=2**63 - 1)):
        def work(session, auth: PersonAuth):
            return {"conversations": messaging.list_person_conversations(session, auth.user, limit=limit)}
        return await run(request, "person:read", work)

    @api.get("/person/conversations/{conversation_id}")
    async def person_conversation(request: Request, conversation_id: str,
                                  after: int = Query(0, ge=0, le=2**63 - 1),
                                  limit: int = Query(100, ge=1, le=2**63 - 1)):
        def work(session, auth: PersonAuth):
            conv = messaging.get_person_conversation(session, auth.user, conversation_id)
            peer = messaging.conversation_endpoint(session, conv, "b" if conv.a_user_id == auth.user.id else "a")
            msgs, cursor = messaging.person_conversation_messages(session, auth.user, conv.id, after=after, limit=limit)
            return {"conversation": {"id": str(conv.id), "peer": peer.label(), "peer_endpoint": peer.json()},
                    "messages": messaging.messages_json(session, msgs), "cursor": cursor}
        return await run(request, "person:read", work)

    @api.post("/person/send")
    async def person_send(request: Request):
        data = await json_body(request, "person:send")

        def work(session, auth: PersonAuth):
            if not isinstance(data, dict):
                raise ApiError(400, "invalid", "request body must be a JSON object")
            unknown = set(data) - PERSON_SEND_FIELDS
            if unknown:
                raise ApiError(400, "invalid", f"unknown fields: {', '.join(sorted(unknown))}")
            if data.get("in_reply_to") is not None:  # §16.16 (3): the parent's team; "team" is ignored
                team = reply_team(session, auth, data["in_reply_to"])
            else:
                team = choose_team(session, auth, data.get("team"))
            req = messaging.SendRequest.parse({k: v for k, v in data.items() if k != "team"}, "")
            msg, created = messaging.send_as_person(
                session, auth.user, team.id, id=req.id, to=req.to, body=req.body,
                conversation_id=req.conversation_id, in_reply_to=req.in_reply_to,
                max_pending=settings.max_pending, attachments=req.attachments, kind=req.kind)
            return created, {"message": m(session, msg), "created": created}
        created, body = await run(request, "person:send", work, counted=False)
        return JSONResponse(body, status_code=201 if created else 200)

    @api.get("/person/messages/{message_id}/attachments/{ref}")
    async def person_attachment(request: Request, message_id: str, ref: str):
        """``ref`` is an attachment id or its 1-based position (``raincli me fetch --attachment N``)."""
        def work(session, auth: PersonAuth):
            msg = messaging.get_message_for_person(session, auth.user, message_id)
            if ref.isdigit() and len(ref) <= 2:
                metas = messaging.attachments_json(msg)
                index = int(ref) - 1
                if not 0 <= index < len(metas):
                    raise ApiError(404, "not_found", "attachment not found")
                ref_id = metas[index]["id"]
            else:
                ref_id = ref
            att = messaging.get_attachment_for_person(session, auth.user, msg.id, ref_id)
            return att.filename, att.sha256, bytes(att.content)
        filename, sha, content = await run(request, "person:read", work)
        return Response(content, media_type=None, headers=attachment_headers(filename, sha, len(content)))

    @api.post("/person/sign-out")
    async def person_sign_out(request: Request):
        def work(session, auth: PersonAuth):
            identity.revoke_person_sessions(session, session_id=auth.person_session.id)
            return {"signed_out": True}
        return await run(request, None, work)

    @api.post("/app/handoff")
    async def app_handoff(request: Request):
        """§16.10, §16.14 S3/S4: a single-use code, valid for 60 s, bound to this person session and to the
        app install (``{"app_install_hash": sha256(app_install_token)}``)."""
        body = await json_body(request, "person:read")
        if not isinstance(body, dict) or set(body) - {"app_install_hash"}:
            raise ApiError(400, "invalid", "request body must be {\"app_install_hash\": ...}")
        install_hash = body.get("app_install_hash")
        if not isinstance(install_hash, str) or not security.INSTALL_HASH_RE.match(install_hash):
            raise ApiError(400, "invalid", "app_install_hash must be 64 lowercase hex characters")

        def work(session, auth: PersonAuth):
            code = security.new_token(security.HANDOFF_CODE_PREFIX)
            expires = identity.now() + HANDOFF_TTL
            session.add(HandoffCode(id=uuid.uuid4(), code_hash=security.hash_token(code),
                                    person_session_id=auth.person_session.id, expires_at=expires,
                                    install_hash=install_hash))
            return {"code": code, "expires_at": messaging.iso(expires),
                    "url": f"{settings.public_url}{settings.root_path or ''}/app/handoff?code={code}"}
        return await run(request, "person:read", work)
