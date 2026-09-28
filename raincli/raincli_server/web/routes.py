"""Web routes: public pages, sign-in, invitations and the authenticated app (/app)."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from raincli_server import identity, security
from raincli_server.config import Settings
from raincli_server.models import Agent, Team
from raincli_server.web import auth, queries
from raincli_server.web.middleware import WebSecurityMiddleware

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

STATE_LABELS = {
    "stored": ("Stored", "Committed on the server; not yet picked up by the recipient."),
    "received": ("Received", "The recipient's client stored it locally and acknowledged it."),
    "held": ("Held", "The recipient's connector is holding it (approval, busy, blocked or offline)."),
    "submitted": ("Submitted", "Handed to the recipient's agent session. That does not mean it was acted on."),
    "submission_uncertain": ("Uncertain", "A hand-off was interrupted; it may or may not have reached the session."),
    "rejected": ("Rejected", "The recipient's operator declined it."),
    "replied": ("Replied", "The recipient sent a reply."),
}
CONNECTION_LABELS = {
    "connected": ("Connected recently", "Used its credential in the last 5 minutes."),
    "idle": ("Idle", "Has connected before, but not in the last 5 minutes."),
    "never": ("Never connected", "No client has used this agent's credential yet."),
    "revoked": ("Revoked", "This agent and all of its credentials are revoked."),
}
NOTICES = {
    "signed-in": "Signed in.",
    "joined": "Welcome aboard. You have joined the team.",
    "sent": "Message stored on the server.",
    "duplicate": "That message was already stored; nothing was sent twice.",
    "revoked": "Agent revoked. Its credentials stopped working immediately.",
    "invite-revoked": "Invitation revoked. The link no longer works.",
    "member-removed": "Member removed. Their agents in this team were revoked and they were signed out.",
}
_NEXT_RE = re.compile(r"^/app(/[A-Za-z0-9/_.-]*)?$")


class WebError(Exception):
    def __init__(self, status: int, title: str, message: str) -> None:
        self.status, self.title, self.message = status, title, message


class LoginRequired(Exception):
    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


def not_found() -> WebError:
    return WebError(404, "Not found", "That page does not exist, or you do not have access to it.")


# Helpers ----------------------------------------------------------------------

def get_db(request: Request) -> Iterator[Session]:
    db = request.app.state.sessionmaker()
    try:
        yield db
    finally:
        db.close()


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _url(request: Request, path: str) -> str:
    return _settings(request).root_path + path


def _route_path(request: Request) -> str:
    path, root = request.url.path, _settings(request).root_path
    if root and (path == root or path.startswith(root + "/")):
        path = path[len(root):] or "/"
    return path


def redirect(request: Request, path: str) -> RedirectResponse:
    return RedirectResponse(_url(request, path), status_code=303)


def fmt_ts(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def iso_ts(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _static_versions(root: Path) -> dict[str, str]:
    """sha256[:12] of every static file, computed once at startup, for cache-busting URLs."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        for path in sorted(root.rglob("*")) if path.is_file()
    }


STATIC_VERSIONS = _static_versions(HERE / "static")

templates.env.filters["ts"] = fmt_ts
templates.env.filters["iso"] = iso_ts
templates.env.filters["filesize"] = lambda n: f"{n} B" if n < 1024 else f"{n / 1024:.1f} KiB"
templates.env.globals["STATE_LABELS"] = STATE_LABELS
templates.env.globals["CONNECTION_LABELS"] = CONNECTION_LABELS


def render(
    request: Request, name: str, *, viewer: auth.Viewer | None = None, status: int = 200,
    csrf: str | None = None, pre_nonce: list[str] | None = None, **ctx,
) -> HTMLResponse:
    settings = _settings(request)
    root = settings.root_path
    context = {
        "url": lambda path: root + path,
        "static": lambda name: f"{root}/static/{name}?v={STATIC_VERSIONS.get(name, '0')}",
        "viewer": viewer,
        "csrf": csrf if csrf is not None else (viewer.csrf if viewer else ""),
        "path": _route_path(request),
        "notice": NOTICES.get(request.query_params.get("notice", "")),
        **ctx,
    }
    response = templates.TemplateResponse(request, name, context, status_code=status)
    for nonce in pre_nonce or []:
        auth.set_pre_csrf_cookie(response, settings, nonce)
    return response


def require_viewer(request: Request, db: Session) -> auth.Viewer:
    viewer = auth.load_viewer(db, request)
    if viewer is None:
        if request.method == "GET":
            raise LoginRequired(_route_path(request))
        raise WebError(403, "Signed out", "Your session has ended. Sign in and try again.")
    return viewer


def require_csrf(request: Request, submitted: str, viewer: auth.Viewer | None) -> None:
    if not auth.check_csrf(request, submitted, viewer):
        raise WebError(403, "Form expired", "This form is missing a valid security token. Reload the page and try again.")


def app_viewer(request: Request, db: Session, csrf_token: str | None = None) -> auth.Viewer:
    """Authenticated viewer; for POSTs also enforces the per-session CSRF token."""
    viewer = require_viewer(request, db)
    if request.method != "GET":
        require_csrf(request, csrf_token or "", viewer)
    return viewer


def _team_ids(viewer: auth.Viewer) -> list[uuid.UUID]:
    return [team.id for team, _ in viewer.teams]


def _viewer_team(viewer: auth.Viewer, slug: str) -> tuple[Team, str]:
    for team, role in viewer.teams:
        if team.slug == slug:
            return team, role
    raise not_found()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


router = APIRouter(include_in_schema=False)


# Legacy service-worker kill switch ------------------------------------------------------

# The pre-cutover site may have registered a service worker that keeps serving old pages.
# Browsers re-fetch registered worker scripts, bypassing the HTTP cache, so answering at the
# usual paths replaces the old worker with this one, which clears caches and unregisters itself.
SW_KILL_SWITCH = """// RainCLI: retire any service worker left over from the previous site.
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (event) {
  event.waitUntil((async function () {
    var keys = await caches.keys();
    await Promise.all(keys.map(function (key) { return caches.delete(key); }));
    await self.registration.unregister();
    var windows = await self.clients.matchAll({ type: "window" });
    windows.forEach(function (client) { client.navigate(client.url); });
  })());
});
"""
SW_PATHS = ("/sw.js", "/service-worker.js", "/serviceworker.js", "/sw.min.js")


def service_worker_kill_switch() -> Response:
    return Response(SW_KILL_SWITCH, media_type="application/javascript", headers={
        "Cache-Control": "no-store",
        "Clear-Site-Data": '"cache", "storage"',
    })


for _sw_path in SW_PATHS:
    router.add_api_route(_sw_path, service_worker_kill_switch, methods=["GET"], include_in_schema=False)


# Public pages -------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    signed_in = auth.load_viewer(db, request) is not None  # only switches the nav link; no data shown
    return render(request, "home.html", signed_in=signed_in)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "", db: Session = Depends(get_db)):
    if auth.load_viewer(db, request) is not None:
        return redirect(request, "/app")
    nonce: list[str] = []
    token = auth.pre_csrf(request, nonce)
    return render(request, "login.html", csrf=token, pre_nonce=nonce, next=next if _NEXT_RE.match(next) else "")


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request, email: str = Form(""), password: str = Form(""), next: str = Form(""),
    csrf_token: str = Form(""), db: Session = Depends(get_db),
):
    require_csrf(request, csrf_token, None)
    limiter: auth.LoginLimiter = request.app.state.web_login_limiter
    ip = _client_ip(request)
    email = email.strip()[:254]
    next = next if _NEXT_RE.match(next) else ""

    def fail(status: int, message: str) -> HTMLResponse:
        return render(
            request, "login.html", status=status, csrf=csrf_token, error=message, email=email, next=next,
        )

    if limiter.blocked(ip, email):
        return fail(429, "Too many sign-in attempts. Wait a few minutes and try again.")
    user = identity.authenticate_user(db, email, password[:256]) if email and password else None
    if user is None:
        limiter.failure(ip, email)
        return fail(400, "That email and password combination is not correct.")
    limiter.success(ip, email)
    old = auth.load_viewer(db, request)
    response = redirect(request, next or "/app")
    if old is not None:
        auth.end_session(db, old, response, _settings(request))
    auth.start_session(db, user, response, _settings(request))
    db.commit()
    return response


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    viewer = app_viewer(request, db, csrf_token)
    response = redirect(request, "/")
    auth.end_session(db, viewer, response, _settings(request))
    db.commit()
    return response


# Invitations ----------------------------------------------------------------------

def _invite_page(request: Request, db: Session, token: str, *, status: int = 200, **ctx) -> HTMLResponse:
    found = queries.peek_invitation(db, token)
    if found is None:
        return render(request, "invite_invalid.html", status=404, viewer=auth.load_viewer(db, request))
    inv, team = found
    viewer = auth.load_viewer(db, request)
    nonce: list[str] = []
    csrf = viewer.csrf if viewer else auth.pre_csrf(request, nonce)
    already = viewer is not None and viewer.role_in(team.id) is not None
    return render(
        request, "invite.html", viewer=viewer, csrf=csrf, pre_nonce=nonce, status=status,
        invitation=inv, team=team, token=token, already_member=already, **ctx,
    )


@router.get("/invite/{token}", response_class=HTMLResponse)
def invite_page(request: Request, token: str, db: Session = Depends(get_db)):
    return _invite_page(request, db, token)


@router.post("/invite/{token}", response_class=HTMLResponse)
def invite_accept(
    request: Request, token: str, csrf_token: str = Form(""), email: str = Form(""),
    display_name: str = Form(""), password: str = Form(""), password_confirm: str = Form(""),
    db: Session = Depends(get_db),
):
    viewer = auth.load_viewer(db, request)
    require_csrf(request, csrf_token, viewer)
    if queries.peek_invitation(db, token) is None:
        return render(request, "invite_invalid.html", status=404, viewer=viewer)
    form = {"email": email.strip()[:254], "display_name": display_name[:80]}
    if viewer is None and password != password_confirm:
        return _invite_page(request, db, token, status=400, error="The two passwords do not match.", form=form)
    try:
        if viewer is not None:
            identity.accept_invitation(db, token, user=viewer.user)
        else:
            user, _ = identity.accept_invitation(
                db, token, email=email, display_name=display_name, password=password,
            )
    except identity.IdentityError as exc:
        db.rollback()
        message = str(exc)
        if "already exists" in message:
            message = "An account with that email already exists. Sign in first, then open this invitation link again."
        return _invite_page(request, db, token, status=400, error=message[:1].upper() + message[1:] + ".", form=form)
    response = redirect(request, "/app?notice=joined")
    if viewer is None:
        auth.start_session(db, user, response, _settings(request))
    db.commit()
    return response


# App: inbox and conversations -----------------------------------------------------------

@router.get("/app", response_class=HTMLResponse)
def inbox(request: Request, agent: str = "", db: Session = Depends(get_db)):
    viewer = app_viewer(request, db)
    team_ids = _team_ids(viewer)
    mine = queries.my_agents(db, viewer.user, team_ids)
    selected = queries.parse_uuid(agent)
    if selected is not None and selected not in {r.agent.id for r in mine}:
        raise not_found()
    rows = queries.list_conversations(db, viewer.user, team_ids, only_agent=selected)
    return render(
        request, "app/inbox.html", viewer=viewer, conversations=rows, agents=mine, selected=selected,
        pending=queries.pending_for_user(db, viewer.user, team_ids),
    )


@router.get("/app/conversations/{conversation_id}", response_class=HTMLResponse)
def conversation(request: Request, conversation_id: str, reply_to: str = "", db: Session = Depends(get_db)):
    viewer = app_viewer(request, db)
    view = queries.get_conversation(db, viewer.user, _team_ids(viewer), conversation_id)
    if view is None:
        raise not_found()
    return _conversation_page(request, viewer, view, reply_to=reply_to)


def _conversation_page(
    request: Request, viewer: auth.Viewer, view: queries.ConversationView, *, reply_to: str = "",
    status: int = 200, **ctx,
) -> HTMLResponse:
    parent = None
    rid = queries.parse_uuid(reply_to)
    if rid is not None:
        parent = next((m for m in view.messages if m.id == rid), None)
    active_mine = [a for a in view.mine if a.revoked_at is None]
    if parent is not None:  # a reply is sent by the parent's other participant
        active_mine = [a for a in active_mine if a.id in (parent.sender_agent_id, parent.recipient_agent_id)]
    return render(
        request, "app/conversation.html", viewer=viewer, status=status, view=view, parent=parent,
        senders=active_mine, can_send=bool(active_mine) and view.peer.revoked_at is None,
        message_id=str(uuid.uuid4()), **ctx,
    )


@router.get("/app/compose", response_class=HTMLResponse)
def compose(request: Request, from_agent: str = "", to: str = "", db: Session = Depends(get_db)):
    viewer = app_viewer(request, db)
    return _compose_page(request, db, viewer, form={"from_agent": from_agent, "to": to[:32], "body": ""})


def _compose_page(request, db, viewer, *, form, status: int = 200, error: str | None = None) -> HTMLResponse:
    team_ids = _team_ids(viewer)
    senders = [r for r in queries.my_agents(db, viewer.user, team_ids) if r.active]
    return render(
        request, "app/compose.html", viewer=viewer, status=status, error=error, form=form, senders=senders,
        handles=queries.active_handles(db, team_ids), message_id=str(uuid.uuid4()),
    )


@router.post("/app/conversations/{conversation_id}/send", response_class=HTMLResponse)
def send_message(
    request: Request, conversation_id: str, csrf_token: str = Form(""), message_id: str = Form(""),
    from_agent: str = Form(""), to: str = Form(""), body: str = Form(""), in_reply_to: str = Form(""),
    files: list[UploadFile] = File(default=[]), db: Session = Depends(get_db),
):
    """Send as one of the viewer's agents; ``conversation_id`` is "new" from the compose page."""
    viewer = app_viewer(request, db, csrf_token)
    team_ids = _team_ids(viewer)
    view = None
    if conversation_id != "new":
        view = queries.get_conversation(db, viewer.user, team_ids, conversation_id)
        if view is None:
            raise not_found()
    sender = queries.owned_agent(db, viewer.user, team_ids, from_agent)
    if sender is None:  # only the viewer's own agents can send; others are indistinguishable from absent
        raise not_found()
    mid = queries.parse_uuid(message_id) or uuid.uuid4()
    parent_id = queries.parse_uuid(in_reply_to) if in_reply_to else None
    body = body.replace("\r\n", "\n")
    try:
        attachments = _read_uploads(files)
        msg, created = queries.send_as(
            db, sender, id=mid, to_handle=to.strip(), body=body,
            conversation_id=view.conversation.id if view else None, in_reply_to=parent_id,
            max_pending=_settings(request).max_pending, attachments=attachments,
        )
    except queries.SendError as exc:
        db.rollback()
        status = 429 if exc.code == "inbox_full" else 409 if exc.code == "id_conflict" else 400
        if view is not None:
            view = queries.get_conversation(db, viewer.user, team_ids, conversation_id)
            return _conversation_page(
                request, viewer, view, reply_to=in_reply_to, status=status, error=exc.message, draft=body,
            )
        return _compose_page(
            request, db, viewer, status=status, error=exc.message,
            form={"from_agent": from_agent, "to": to[:32], "body": body},
        )
    db.commit()
    notice = "sent" if created else "duplicate"
    return redirect(request, f"/app/conversations/{msg.conversation_id}?notice={notice}#m-{msg.id}")


def _read_uploads(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    """(filename, exact bytes) for each chosen file; the empty part of an unused file input is skipped."""
    chosen = [f for f in files if f.filename]
    if len(chosen) > security.ATTACHMENT_MAX_COUNT:
        raise queries.SendError("invalid", f"Attach at most {security.ATTACHMENT_MAX_COUNT} files.")
    # Read one byte past the per-file limit so oversize files fail validation without buffering more.
    return [(f.filename, f.file.read(security.ATTACHMENT_MAX_BYTES + 1)) for f in chosen]


@router.get("/app/messages/{message_id}/attachments/{attachment_id}")
def download_attachment(request: Request, message_id: str, attachment_id: str, db: Session = Depends(get_db)):
    viewer = app_viewer(request, db)
    att = queries.attachment_for_user(db, viewer.user, _team_ids(viewer), message_id, attachment_id)
    if att is None:
        raise not_found()
    return Response(att.content, media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{att.filename}"',
        "X-Content-Type-Options": "nosniff",
        "X-RainCLI-SHA256": att.sha256,
        "Cache-Control": "no-store",
        "Content-Security-Policy": "sandbox; default-src 'none'",
    })


# App: agents -----------------------------------------------------------------

def _config_json(request: Request, token: str) -> str:
    settings = _settings(request)
    return json.dumps({"api_url": settings.public_url + settings.root_path, "token": token}, indent=2) + "\n"


def _agents_page(request, db, viewer, *, status: int = 200, **ctx) -> HTMLResponse:
    return render(
        request, "app/agents.html", viewer=viewer, status=status,
        agents=queries.my_agents(db, viewer.user, _team_ids(viewer)), **ctx,
    )


def _token_page(request: Request, viewer: auth.Viewer, agent: Agent, team: Team, token: str, rotated: bool):
    settings = _settings(request)
    return render(
        request, "app/agent_token.html", viewer=viewer, agent=agent, team=team, token=token, rotated=rotated,
        api_url=settings.public_url + settings.root_path, filename=f"raincli-{agent.handle}.json",
    )


@router.get("/app/agents", response_class=HTMLResponse)
def agents(request: Request, db: Session = Depends(get_db)):
    viewer = app_viewer(request, db)
    return _agents_page(request, db, viewer, form={})


@router.post("/app/agents", response_class=HTMLResponse)
def register_agent(
    request: Request, csrf_token: str = Form(""), team: str = Form(""), handle: str = Form(""),
    display_name: str = Form(""), db: Session = Depends(get_db),
):
    viewer = app_viewer(request, db, csrf_token)
    team_obj, _ = _viewer_team(viewer, team)
    handle = handle.strip()
    try:
        agent, token = identity.register_agent(db, team_obj, viewer.user, handle, display_name.strip() or None)
    except identity.IdentityError as exc:
        db.rollback()
        return _agents_page(
            request, db, viewer, status=400, error=str(exc),
            form={"team": team, "handle": handle[:32], "display_name": display_name[:80]},
        )
    db.commit()
    return _token_page(request, viewer, agent, team_obj, token, rotated=False)


@router.post("/app/agents/{agent_id}/rotate", response_class=HTMLResponse)
def rotate_agent(request: Request, agent_id: str, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    viewer = app_viewer(request, db, csrf_token)
    agent = queries.owned_agent(db, viewer.user, _team_ids(viewer), agent_id)
    if agent is None:
        raise not_found()
    try:
        token = identity.rotate_agent_credential(db, agent, viewer.user)
    except identity.IdentityError as exc:
        db.rollback()
        return _agents_page(request, db, viewer, status=400, error=str(exc), form={})
    db.commit()
    return _token_page(request, viewer, agent, db.get(Team, agent.team_id), token, rotated=True)


@router.post("/app/agents/{agent_id}/revoke")
def revoke_agent(request: Request, agent_id: str, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    viewer = app_viewer(request, db, csrf_token)
    agent = queries.owned_agent(db, viewer.user, _team_ids(viewer), agent_id)
    if agent is None:
        raise not_found()
    identity.revoke_agent(db, agent, viewer.user)
    db.commit()
    return redirect(request, "/app/agents?notice=revoked")


@router.post("/app/agents/{agent_id}/config")
def download_config(
    request: Request, agent_id: str, csrf_token: str = Form(""), token: str = Form(""),
    db: Session = Depends(get_db),
):
    """Re-serve the config for a token the page just showed; the server never stores raw tokens."""
    viewer = app_viewer(request, db, csrf_token)
    agent = queries.owned_agent(db, viewer.user, _team_ids(viewer), agent_id)
    auth_ = identity.authenticate_agent(db, token, touch=False) if agent is not None else None
    if auth_ is None or auth_.agent.id != agent.id:
        raise not_found()
    return Response(
        _config_json(request, token), media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="raincli-{agent.handle}.json"'},
    )


# App: team ---------------------------------------------------------------------

def _team_page(request, db, viewer, team: Team, role: str, *, status: int = 200, **ctx) -> HTMLResponse:
    is_owner = role == "owner"
    return render(
        request, "app/team.html", viewer=viewer, status=status, team=team, role=role, is_owner=is_owner,
        members=queries.team_members(db, team), team_agents=queries.team_agents(db, team),
        invitations=queries.open_invitations(db, team) if is_owner else [], **ctx,
    )


@router.get("/app/team")
def team_default(request: Request, db: Session = Depends(get_db)):
    viewer = app_viewer(request, db)
    if not viewer.teams:
        return render(request, "app/no_team.html", viewer=viewer)
    return redirect(request, f"/app/teams/{viewer.teams[0][0].slug}")


@router.get("/app/teams/{slug}", response_class=HTMLResponse)
def team_page(request: Request, slug: str, db: Session = Depends(get_db)):
    viewer = app_viewer(request, db)
    team, role = _viewer_team(viewer, slug)
    return _team_page(request, db, viewer, team, role)


@router.post("/app/teams/{slug}/invitations", response_class=HTMLResponse)
def create_invitation(
    request: Request, slug: str, csrf_token: str = Form(""), email: str = Form(""), role: str = Form("member"),
    db: Session = Depends(get_db),
):
    viewer = app_viewer(request, db, csrf_token)
    team, my_role = _viewer_team(viewer, slug)
    if my_role != "owner":
        raise WebError(403, "Owners only", "Only team owners can invite people.")
    try:
        _, token = identity.create_invitation(db, team, viewer.user, email.strip() or None, role)
    except identity.IdentityError as exc:
        db.rollback()
        return _team_page(request, db, viewer, team, my_role, status=400, error=str(exc),
                          form={"email": email[:254], "role": role})
    db.commit()
    settings = _settings(request)
    link = f"{settings.public_url}{settings.root_path}/invite/{quote(token)}"
    return _team_page(request, db, viewer, team, my_role, invite_link=link, invite_email=email.strip())


@router.post("/app/teams/{slug}/invitations/{invitation_id}/revoke")
def revoke_invitation(
    request: Request, slug: str, invitation_id: str, csrf_token: str = Form(""), db: Session = Depends(get_db),
):
    viewer = app_viewer(request, db, csrf_token)
    team, my_role = _viewer_team(viewer, slug)
    inv_id = queries.parse_uuid(invitation_id)
    inv = next((i for i, _ in queries.open_invitations(db, team) if i.id == inv_id), None) if my_role == "owner" else None
    if inv is None:
        raise not_found()
    identity.revoke_invitation(db, inv.id, viewer.user)
    db.commit()
    return redirect(request, f"/app/teams/{team.slug}?notice=invite-revoked")


def _removable_member(db: Session, viewer: auth.Viewer, slug: str, user_id: str):
    team, my_role = _viewer_team(viewer, slug)
    if my_role != "owner":
        raise WebError(403, "Owners only", "Only team owners can remove members.")
    uid = queries.parse_uuid(user_id)
    member = next((u for u, _, _ in queries.team_members(db, team) if u.id == uid), None)
    if member is None:
        raise not_found()
    return team, member


@router.get("/app/teams/{slug}/members/{user_id}/remove", response_class=HTMLResponse)
def remove_member_confirm(request: Request, slug: str, user_id: str, db: Session = Depends(get_db)):
    viewer = app_viewer(request, db)
    team, member = _removable_member(db, viewer, slug, user_id)
    owned = [r for r in queries.team_agents(db, team) if r.agent.owner_user_id == member.id and r.active]
    return render(request, "app/remove_member.html", viewer=viewer, team=team, member=member, agents=owned)


@router.post("/app/teams/{slug}/members/{user_id}/remove")
def remove_member(
    request: Request, slug: str, user_id: str, csrf_token: str = Form(""), db: Session = Depends(get_db),
):
    viewer = app_viewer(request, db, csrf_token)
    team, member = _removable_member(db, viewer, slug, user_id)
    try:
        identity.remove_member(db, team, member, viewer.user)
    except identity.IdentityError as exc:  # includes the last-owner refusal
        db.rollback()
        role = viewer.role_in(team.id) or "member"
        message = str(exc)
        return _team_page(request, db, viewer, team, role, status=400, error=message)
    db.commit()
    if member.id == viewer.user.id:  # removing yourself also ends your session
        response = redirect(request, "/")
        auth.end_session(db, None, response, _settings(request))
        return response
    return redirect(request, f"/app/teams/{team.slug}?notice=member-removed")


@router.post("/app/teams/{slug}/agents/{agent_id}/revoke")
def owner_revoke_agent(
    request: Request, slug: str, agent_id: str, csrf_token: str = Form(""), db: Session = Depends(get_db),
):
    viewer = app_viewer(request, db, csrf_token)
    team, my_role = _viewer_team(viewer, slug)
    agent = queries.team_agent(db, team, agent_id)
    if agent is None:
        raise not_found()
    try:
        identity.revoke_agent(db, agent, viewer.user)
    except identity.PermissionDenied:
        raise WebError(403, "Owners only", "Only team owners can revoke other members' agents.") from None
    db.commit()
    return redirect(request, f"/app/teams/{team.slug}?notice=revoked")


# Installation ---------------------------------------------------------------------

def install(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    app.state.web_login_limiter = auth.LoginLimiter()
    app.add_middleware(WebSecurityMiddleware, root_path=settings.root_path, cookie_secure=settings.cookie_secure)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def _error_page(request: Request, exc: WebError) -> HTMLResponse:
        db = request.app.state.sessionmaker()
        try:
            viewer = auth.load_viewer(db, request)
        finally:
            db.close()
        return render(request, "error.html", viewer=viewer, status=exc.status, code=exc.status,
                      title=exc.title, message=exc.message)

    @app.exception_handler(WebError)
    async def _web_error(request: Request, exc: WebError):
        return await run_in_threadpool(_error_page, request, exc)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        target = "/login"
        if _NEXT_RE.match(exc.next_path) and exc.next_path != "/app":
            target += "?next=" + quote(exc.next_path, safe="/")
        return redirect(request, target)

    app.include_router(router)
