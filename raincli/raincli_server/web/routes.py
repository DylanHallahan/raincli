"""Web routes: public pages, sign-in, invitations and the authenticated app (/app)."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from raincli_server.markdown import render as render_markdown
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from sqlalchemy import and_, func, or_, select

from raincli_server import identity, messaging, presentation, quotas, security
from raincli_server.config import Settings
from raincli_server.models import Agent, HandoffCode, Message, PersonSession, Team, User
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
    "password-changed": "Password changed. Other browser sessions have been signed out. Agent credentials still work.",
    "joined": "Welcome aboard. You have joined the team.",
    "sent": "Message stored on the server.",
    "archived": "Conversation archived. It's under Archived, and a new message brings it back.",
    "unarchived": "Conversation moved back to your inbox.",
    "newer": "A new message arrived, so this conversation stays in your inbox. Archive it again to hide it.",
    "duplicate": "That message was already stored; nothing was sent twice.",
    "revoked": "Agent revoked. Its credentials stopped working immediately.",
    "invite-revoked": "Invitation revoked. The link no longer works.",
    "member-removed": "Member removed. Their agents in this team were revoked and they were signed out.",
    "app-signed-out": "Signed out of that app. Its person session stopped working immediately.",
    "inbox-set": "Your inbox is set. Your teammates' messages now go to it.",
    "inbox-invalid": "That agent can't be your inbox: pick one of your own agents that can receive messages.",
}


def _notice(request: Request) -> str | None:
    """The page's notice; after an inbox change, also how many messages stayed with the previous inbox (§21 C6.3)."""
    text = NOTICES.get(request.query_params.get("notice", ""))
    stayed = request.query_params.get("stayed", "")
    if text and request.query_params.get("notice") == "inbox-set" and stayed.isdigit() and 0 < int(stayed) < 10**6:
        n = int(stayed)
        text += f" {n} message{' is' if n == 1 else 's are'} with your previous inbox."
    return text
_NEXT_RE = re.compile(r"^/app(/[A-Za-z0-9/_.-]*)?$")


class WebError(Exception):
    def __init__(self, status: int, title: str, message: str, link: str | None = None) -> None:
        self.status, self.title, self.message, self.link = status, title, message, link


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

templates.env.filters["markdown"] = render_markdown  # the only value templates mark safe (§16.12 C13)
templates.env.filters["ts"] = fmt_ts
templates.env.filters["iso"] = iso_ts
templates.env.filters["filesize"] = lambda n: f"{n} B" if n < 1024 else f"{n / 1024:.1f} KiB"
templates.env.globals["STATE_LABELS"] = STATE_LABELS
# Public links. Release assets carry the version in their names, so the download goes to the latest
# release page rather than one installer file.
templates.env.globals["WINDOWS_DOWNLOAD_URL"] = "https://github.com/DylanHallahan/raincli/releases/latest"
templates.env.globals["SETUP_URL"] = "https://github.com/DylanHallahan/raincli/blob/main/SETUP.md"
templates.env.globals["HEADLESS_LOGIN_URL"] = (
    "https://github.com/DylanHallahan/raincli/blob/main/SETUP.md#headless-linux-raincli-login")
templates.env.globals["WINDOWS_GUIDE_URL"] = "https://github.com/DylanHallahan/raincli/blob/main/docs/windows-client.md"
# The homepage's links to the detail it leaves to the docs (pass 2).
templates.env.globals["PROTOCOL_STATES_URL"] = (
    "https://github.com/DylanHallahan/raincli/blob/main/docs/raincli-protocol.md#2-delivery-semantics")
templates.env.globals["DEPLOY_URL"] = "https://github.com/DylanHallahan/raincli/blob/main/docs/raincli-deploy.md"
templates.env.globals["SECURITY_URL"] = "https://github.com/DylanHallahan/raincli/blob/main/SECURITY.md"
templates.env.globals["CONNECTION_LABELS"] = CONNECTION_LABELS


def render(
    request: Request, name: str, *, viewer: auth.Viewer | None = None, status: int = 200,
    csrf: str | None = None, pre_nonce: list[str] | None = None, **ctx,
) -> HTMLResponse:
    settings = _settings(request)
    context = _context(request, viewer, csrf, ctx)
    response = templates.TemplateResponse(request, name, context, status_code=status)
    for nonce in pre_nonce or []:
        auth.set_pre_csrf_cookie(response, settings, nonce)
    return response


def _context(request: Request, viewer: auth.Viewer | None, csrf: str | None, ctx: dict) -> dict:
    settings = _settings(request)
    root = settings.root_path
    return {
        "url": lambda path: root + path,
        "static": lambda name: f"{root}/static/{name}?v={STATIC_VERSIONS.get(name, '0')}",
        # for link previews (og:image, twitter:image), which need an absolute URL
        "absolute_static": lambda name: f"{settings.public_url}{root}/static/{name}?v={STATIC_VERSIONS.get(name, '0')}",
        "viewer": viewer,
        "csrf": csrf if csrf is not None else (viewer.csrf if viewer else ""),
        "path": _route_path(request),
        "notice": _notice(request),
        "product_name": settings.product_name,
        "product_logo": settings.product_logo,
        "layout": "base_app.html" if viewer is not None and viewer.app_mode else "base.html",
        "website_url": settings.public_url + root,
        **ctx,
    }


def fragment(request: Request, name: str, viewer: auth.Viewer | None, **ctx) -> str:
    """A partial template rendered with the same context as a page (§19.1: the same templates as the page)."""
    return templates.env.get_template(name).render(_context(request, viewer, None, ctx))


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


def app_viewer(request: Request, db: Session, csrf_token: str | None = None, *,
               app_mode_ok: bool = False) -> auth.Viewer:
    """Authenticated viewer; for POSTs also enforces the per-session CSRF token.

    App-mode sessions (§16.12 C2) carry only the person scopes: they may use the inbox,
    conversations, compose and reply, archive and unarchive (§17.3 A5), attachments, the agent
    directory and picker, and ``/app/local/*``. Every other route refuses them with a link to the website.
    """
    viewer = require_viewer(request, db)
    if viewer.app_mode and not app_mode_ok:
        settings = _settings(request)
        raise WebError(403, "Open the website",
                       "This page isn't available in the app. Open it on the website in your browser.",
                       link=settings.public_url + settings.root_path + _route_path(request))
    if request.method != "GET":
        require_csrf(request, csrf_token or "", viewer)
    return viewer


def _team_ids(viewer: auth.Viewer) -> list[uuid.UUID]:
    return [team.id for team, _ in viewer.teams]


def _chosen_team_ids(viewer: auth.Viewer, slug: str) -> list[uuid.UUID]:
    """§21.13: the team a typed recipient or a warning is resolved in: the named one (none for a slug that isn't
    one of the viewer's teams, never widened), the only one, or none until a team is chosen."""
    if slug:
        return [t.id for t, _ in viewer.teams if t.slug == slug]
    return _team_ids(viewer) if len(viewer.teams) == 1 else []


def _viewer_team(viewer: auth.Viewer, slug: str) -> tuple[Team, str]:
    for team, role in viewer.teams:
        if team.slug == slug:
            return team, role
    raise not_found()


_client_ip = auth.client_ip


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

    device = auth.known_device_for(db, _settings(request), request.cookies.get(auth.KNOWN_DEVICE_COOKIE), email)
    attempt = limiter.attempt(ip, email, device)
    if attempt is None:
        return fail(429, "Too many sign-in attempts. Wait a few minutes and try again.")
    user = identity.authenticate_user(db, email, password) if email and password and len(password) <= 256 else None
    if user is None:
        return fail(400, "That email and password combination is not correct.")  # the attempt stays a failure
    limiter.success(attempt)
    old = auth.load_viewer(db, request)
    response = redirect(request, next or "/app")
    if old is not None:
        auth.end_session(db, old, response, _settings(request))
    auth.start_session(db, user, response, _settings(request))
    auth.set_known_device(response, _settings(request), user)
    db.commit()
    return response


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    viewer = app_viewer(request, db, csrf_token)
    response = redirect(request, "/")
    auth.end_session(db, viewer, response, _settings(request))
    db.commit()
    return response


# Account --------------------------------------------------------------------------

def _account_page(request: Request, db: Session, viewer: auth.Viewer, **ctx) -> HTMLResponse:
    return render(request, "app/account.html", viewer=viewer,
                  apps=identity.person_sessions_for(db, viewer.user), **ctx)


@router.get("/app/account", response_class=HTMLResponse)
def account(request: Request, db: Session = Depends(get_db)):
    return _account_page(request, db, app_viewer(request, db))


@router.post("/app/account/apps/{session_id}/revoke")
def revoke_app(request: Request, session_id: str, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    """The Signed-in apps list (§16.3, §16.12 C6): the owner signs one app out."""
    viewer = app_viewer(request, db, csrf_token)
    sid = queries.parse_uuid(session_id)
    if sid is None or sid not in {ps.id for ps, _ in identity.person_sessions_for(db, viewer.user)}:
        raise not_found()
    identity.revoke_person_sessions(db, session_id=sid)
    db.commit()
    return redirect(request, "/app/account?notice=app-signed-out#apps")


# The app handoff and local pages (protocol §16.10, §16.12 C2, C3) ------------------------------

_HANDOFF_ERROR = ("This sign-in link can't be used", "It has expired, was already used, or didn't come from the app. "
                  "Open the window again from the app.")


@router.get("/app/handoff", response_class=HTMLResponse)
def app_handoff(request: Request, code: str = "", db: Session = Depends(get_db)):
    """Consume a single-use handoff code and start an app-mode session in its own ``raincli_app`` cookie.

    Only a top-level navigation the user agent started itself is accepted (``Sec-Fetch-Site: none``,
    ``Sec-Fetch-Mode: navigate``), so another site can't sign a browser in (login CSRF), and only from the
    app install the code is bound to: its User-Agent carries ``RainCLIApp/<token>`` (§16.14 S3). Anything
    else consumes nothing and shows one generic error.
    """
    headers = request.headers
    if headers.get("sec-fetch-site") != "none" or headers.get("sec-fetch-mode") != "navigate":
        raise WebError(400, *_HANDOFF_ERROR)
    if not code.startswith(security.HANDOFF_CODE_PREFIX) or len(code) > 200:
        raise WebError(400, *_HANDOFF_ERROR)
    found = db.execute(
        select(HandoffCode, PersonSession).join(PersonSession, PersonSession.id == HandoffCode.person_session_id)
        .where(HandoffCode.code_hash == security.hash_token(code)).with_for_update(of=HandoffCode)
    ).first()
    if found is None:
        raise WebError(400, *_HANDOFF_ERROR)
    hc, ps = found
    if not security.same_install(hc.install_hash, headers.get("user-agent")):
        raise WebError(400, *_HANDOFF_ERROR)
    user = db.get(User, ps.user_id)
    machine = db.get(Agent, ps.machine_agent_id)
    valid = (hc.used_at is None and hc.expires_at > identity.now() and ps.revoked_at is None and user is not None
             and user.is_active and machine is not None and machine.revoked_at is None
             and identity.now() < identity.person_session_expires_at(ps))
    if not valid:
        raise WebError(400, *_HANDOFF_ERROR)
    existing = auth.app_session_user(db, request)
    if existing is not None and existing != user.id:  # never replace another user's app session
        raise WebError(400, *_HANDOFF_ERROR)
    hc.used_at = identity.now()
    from raincli_server.web import present

    # §20.3: a toast's thread may be named as ``next`` (exactly /app/conversations/<uuid>); anything else is Inbox.
    response = redirect(request, present.handoff_next(request.query_params.get("next", "")) or "/app/inbox")
    auth.start_app_session(db, ps, user, response, _settings(request), hc.install_hash)
    response.headers["Referrer-Policy"] = "no-referrer"
    db.commit()
    return response


@router.get("/app/local/{page}", response_class=HTMLResponse)
def app_local(request: Request, page: str, db: Session = Depends(get_db)):
    """A sentinel the Windows app intercepts to show its bundled page; a browser explains that."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", page):
        raise not_found()
    viewer = auth.load_viewer(db, request)
    return render(request, "app_mode/local.html", viewer=viewer, page=page.replace("-", " "))


@router.post("/app/account/password")
def account_password(
    request: Request, csrf_token: str = Form(""), current_password: str = Form(""),
    password: str = Form(""), password_confirm: str = Form(""), db: Session = Depends(get_db),
):
    viewer = app_viewer(request, db, csrf_token)
    limiter: auth.LoginLimiter = request.app.state.web_login_limiter
    ip, email = _client_ip(request), viewer.user.email

    def fail(status: int, message: str):
        return _account_page(request, db, viewer, status=status, error=message)

    if password != password_confirm:
        return fail(400, "The two new passwords do not match.")
    device = auth.known_device_id(_settings(request), request.cookies.get(auth.KNOWN_DEVICE_COOKIE), viewer.user)
    attempt = limiter.attempt(ip, email, device)
    if attempt is None:
        return fail(429, "Too many attempts. Wait a few minutes and try again.")
    try:
        user = identity.change_password(db, viewer.user.id, viewer.web_session.id,
                                        current_password, password)
    except identity.IdentityError as exc:
        db.rollback()  # the attempt stays a failure
        return fail(400, str(exc).capitalize() + ".")
    limiter.success(attempt)
    response = redirect(request, "/app/account?notice=password-changed")
    auth.start_session(db, user, response, _settings(request))
    auth.set_known_device(response, _settings(request), user)  # the new password revoked every other device
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

def _directory(db: Session, viewer: auth.Viewer) -> dict:
    """§21 C7.2: the people the viewer can message (the no-JavaScript <select> and the picker), never machines."""
    return {"people": queries.people_directory(db, viewer.user, _team_ids(viewer)), "teams": viewer.teams}


def _inbox_banner(db: Session, viewer: auth.Viewer) -> list[dict]:
    """§21 C7.3: one entry per team where the viewer has no inbox yet: the team, their own deliverable agents
    (``messaging.inbox_candidates``), the suggested one first, and the waiting messages. Empty once picked."""
    from raincli_server import presentation

    candidates = getattr(messaging, "inbox_candidates", None)
    waiting = getattr(messaging, "waiting_count", None)
    out = []
    for team, _ in viewer.teams:
        if presentation.inbox_of(db, team.id, viewer.user.id) is not None:
            continue
        options = list(candidates(db, viewer.user, team.id)) if candidates is not None else []
        options.sort(key=lambda c: (not c.get("suggested"), not c.get("live"), c.get("agent", "")))
        out.append({"team": team, "candidates": options,
                    "waiting": waiting(db, viewer.user, team.id) if waiting is not None else 0})
    return out


@router.get("/app", response_class=HTMLResponse)
@router.get("/app/inbox", response_class=HTMLResponse)
def inbox(request: Request, db: Session = Depends(get_db)):
    viewer = app_viewer(request, db, app_mode_ok=True)
    team_ids = _team_ids(viewer)
    rows = queries.list_conversations(db, viewer.user, team_ids)
    from raincli_server.web import thread

    if viewer.app_mode:
        return _app_inbox(request, db, viewer, team_ids, rows)
    return render(request, "app/inbox.html", viewer=viewer, conversations=rows, banners=_inbox_banner(db, viewer),
                  machines_owned=bool(queries.my_agents(db, viewer.user, team_ids)),
                  pending=queries.pending_for_user(db, viewer.user, team_ids),
                  owner_warnings=thread.owner_warnings(db, viewer.user, team_ids))


def _app_inbox(request: Request, db: Session, viewer: auth.Viewer, team_ids, rows) -> HTMLResponse:
    """Inbox in app mode. §20.6 M1: app-mode.js opens the latest conversation from here when the list is visible
    (P2 runs in the client); with no conversations, the first-run steps, step 1 done after a recent connection."""
    from raincli_server.web import present

    extras = _list_extras(db, viewer)
    first_team = viewer.teams[0][0] if viewer.teams else None
    people = queries.people_directory(db, viewer.user, team_ids)
    # §20.6 M1: Inbox keeps the owner's notice in its main pane as well as at the top of the list; computed once.
    banners = _inbox_banner(db, viewer)
    # §21 C7.3: while there is an inbox to pick, Inbox shows the banner; P2 (opening the latest conversation) waits.
    return render(request, "app_mode/inbox.html", viewer=viewer, conversations=rows,
                  owner_warnings=extras["list_owner_warnings"], banners=banners,
                  my_inbox=present.my_inbox(db, viewer.user, first_team), first_person=people[0] if people else None,
                  open_latest=present.opens_latest(viewer, request.headers.get("user-agent"))
                  and not any(b["candidates"] for b in banners), **extras)


@router.get("/app/conversations/{conversation_id}", response_class=HTMLResponse)
def conversation(request: Request, conversation_id: str, reply_to: str = "", db: Session = Depends(get_db)):
    viewer = app_viewer(request, db, app_mode_ok=True)
    view = queries.get_conversation(db, viewer.user, _team_ids(viewer), conversation_id)
    if view is None:
        raise not_found()
    if str(view.conversation.id) != conversation_id:  # migration 0010: an old id redirects to where it went
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(_url(request, f"/app/conversations/{view.conversation.id}{query}"), status_code=302)
    # Viewing acks messages to the person on the website (§16.4). In app mode it never does: live.js acks a
    # message once the window has focus and it has been half on screen (§19.2).
    if not viewer.app_mode and queries.ack_viewed(db, viewer.user, view):
        db.commit()
        view = queries.get_conversation(db, viewer.user, _team_ids(viewer), conversation_id)
    return _conversation_page(request, db, viewer, view, reply_to=reply_to)


# Live updates in app mode (§19.1, §19.2) ---------------------------------------------------------------

LIVE_INTERVAL = 5  # seconds between polls while the window is visible
LIVE_PAGE = 50  # new messages per reply; the next poll fetches the rest
ACK_MAX = 100


def live_sig(html: str) -> str:
    """A part's signature (§19.9 F4): the page and every reply carry it, so the client replaces a part (and a
    role=status warning is announced) only when it really changed, whatever localtime.js did to its text."""
    return hashlib.sha256(html.encode()).hexdigest()[:16]


def _parse_since(value: str):
    from datetime import datetime, timezone

    if not value or len(value) > 40:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _live_list(request: Request, db: Session, viewer: auth.Viewer, current_id, client_rev: str
               ) -> tuple[str | None, str]:
    """The conversation list's revision (§19.8 L2), and its HTML (the page's own template) only when the client's
    revision differs. The revision is a digest of the list's inputs (§21 C5.7, narrowed): only the viewer's own
    conversations (id, last seq, unread count, archive state, name) and the owner's warnings about their own machines.
    No teammate's presence goes into it, so its change says nothing about a teammate."""
    rows = queries.list_conversations(db, viewer.user, _team_ids(viewer))
    extras = _list_extras(db, viewer)
    warnings = extras["list_owner_warnings"]  # computed once (review L4): the list shows the same notice
    digest = json.dumps([
        [[str(r.conversation.id), r.last.seq if r.last else 0, r.unacked, r.archived, r.name] for r in rows],
        [[w.handle, w.kind, w.since.isoformat() if w.since else None] for w in warnings],
        str(current_id or ""),
    ], separators=(",", ":"))
    rev = hashlib.sha256(digest.encode()).hexdigest()[:16]
    if rev == client_rev:
        return None, rev
    html = fragment(request, "app_mode/_conv_list.html", viewer, conversations=rows, current_id=current_id, **extras)
    return html, rev


LIVE_RECENT = timedelta(seconds=30)  # §19.8 M1: messages created this recently come again, whatever the cursor
LIVE_IN_VIEW = 200  # the newest messages whose current state is sent while not final
FINAL_STATES = ("replied", "rejected")


def _list_extras(db: Session, viewer: auth.Viewer) -> dict:
    """Extra context for the conversation list, shared by the pages and GET /app/live. §21 C7.6: there is no Team
    section any more; only the owner's own notice."""
    from raincli_server.web import thread

    # §20.6 M1: the owner's "not connected" notice also sits at the top of the list on every app-mode page.
    return {"list_owner_warnings": thread.owner_warnings(db, viewer.user, _team_ids(viewer))}


@router.get("/app/live")
def live(request: Request, conversation: str = "", after: int = 0, states_since: str = "", list_rev: str = "",
         db: Session = Depends(get_db)):
    """One call per open window every 5 s (§19.1): the open thread's new messages (rendered with the page's own
    templates and Markdown renderer), the viewer's own messages whose delivery state changed, the header's
    presence, the sender warning, and the conversation list when it changed. Indexed queries only: messages by
    (conversation_id, seq), events by message, presence by machine; nothing per message. Never acks (§19.2)."""
    from raincli_server.web import thread

    viewer = app_viewer(request, db, app_mode_ok=True)
    if not viewer.app_mode:
        raise not_found()  # the website doesn't poll (until D4)
    now = identity.now()
    out: dict = {"messages": [], "states": [], "header": None, "warning": None, "list": None,
                 "now": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "retry_after": LIVE_INTERVAL}
    current_id = None
    if conversation:
        head = queries.conversation_head(db, viewer.user, _team_ids(viewer), conversation)
        if head is None:
            raise not_found()
        conv, team, me, peer, owned = head
        current_id = conv.id
        after = max(0, min(after, 2**63 - 1))
        in_conv = (Message.conversation_id == conv.id, Message.team_id == conv.team_id)
        # §19.8 M1: the cursors are hints. Messages after the cursor, and any created in the last 30 s (one that
        # committed late with a lower seq); the client merges by id. Both stay on the (conversation_id, seq) index
        # (§19.9 F6): the lowest seq created in the last 30 s is found among the newest LIVE_IN_VIEW messages only,
        # and the range starts below the lower of it and the cursor.
        recent_window = now - LIVE_RECENT
        newest_rows = (select(Message.seq, Message.created_at).where(*in_conv)
                       .order_by(Message.seq.desc()).limit(LIVE_IN_VIEW).subquery())
        recent_floor = db.scalar(select(func.min(newest_rows.c.seq)).where(newest_rows.c.created_at > recent_window))
        lower = min(after, recent_floor - 1) if recent_floor is not None else after
        fresh = list(db.scalars(
            select(Message).where(*in_conv, Message.seq > lower,
                                  or_(Message.seq > after, Message.created_at > recent_window))
            .order_by(Message.seq).limit(LIVE_PAGE).execution_options(populate_existing=True)))
        # States, for the viewer's own messages in view (the newest LIVE_IN_VIEW): every one that isn't final, and
        # every one whose state changed since the last poll or in the last 30 s, final states included (§19.9 F1:
        # delivery_updated_at is the transaction's start, so a reply that commits after a poll can carry an
        # earlier time). Applied idempotently by the client. Recent messages are included too: the client already
        # has them and applies the state to the one it has.
        mine = or_(Message.sender_user_id == viewer.user.id, Message.sender_agent_id.in_(list(owned) or [None]))
        newest_ids = select(Message.id).where(*in_conv).order_by(Message.seq.desc()).limit(LIVE_IN_VIEW).scalar_subquery()
        since = _parse_since(states_since)
        changed_after = min(since, now) - LIVE_RECENT if since is not None else recent_window
        # H1: a message to someone else as a person changes delivery_updated_at when they read it in their app, so
        # it is picked by what its sender sees instead: not yet replied, or answered by a message in this poll.
        answered = [m.in_reply_to for m in fresh if m.in_reply_to is not None]
        to_a_person = and_(Message.recipient_user_id.is_not(None), Message.recipient_user_id != viewer.user.id)
        changed = list(db.scalars(
            select(Message).where(*in_conv, Message.id.in_(newest_ids), mine,
                                  or_(Message.delivery_state.not_in(FINAL_STATES),
                                      and_(~to_a_person, Message.delivery_updated_at > changed_after),
                                      and_(to_a_person, Message.id.in_(answered or [None]))))
            .order_by(Message.seq).limit(LIVE_IN_VIEW).execution_options(populate_existing=True)))
        names = thread.Names(db)
        lines, _ = queries.message_lines(db, viewer.user, owned, fresh + changed, names)
        atts = queries.attachment_meta(db, [m.id for m in fresh]) if fresh else {}
        who = presentation.conversation_peer(db, viewer.user, conv)
        view_ctx = dict(conv_id=conv.id, can_send=queries.ConversationView(
            conv, team, me, peer, peer_user=who.user, kind=who.kind).can_send, reply_only=who.kind == "self")
        for line in lines[:len(fresh)]:
            out["messages"].append({"id": str(line.message.id), "seq": line.message.seq,
                                    "html": fragment(request, "app_mode/_message.html", viewer, line=line,
                                                     atts=atts.get(line.message.id, []), **view_ctx)})
        for line in lines[len(fresh):]:
            if line.trail is not None:
                out["states"].append({"id": str(line.message.id), "state": line.trail.outcome or
                                      line.state or line.message.delivery_state,  # §21 C13 H4: the viewer's view
                                      "html": fragment(request, "app_mode/_trail.html", viewer, line=line)})
        presence = thread.header_presence(db, team.id, who.user, now)
        header_html = fragment(request, "app_mode/_head_status.html", viewer, presence=presence)
        out["header"] = {"word": presence.word if presence else None, "chip": presence.chip if presence else None,
                         "html": header_html, "sig": live_sig(header_html)}
        warning = thread.person_warning(db, team.id, who.user, now, viewer=viewer.user)
        if warning is not None:  # null when there is none (§19.1); the client's empty slot is signed "none"
            warning_html = fragment(request, "_sender_warning.html", viewer, warning=warning)
            out["warning"] = {"kind": warning.kind, "html": warning_html, "sig": live_sig(warning_html)}
    html, rev = _live_list(request, db, viewer, current_id, list_rev)
    out["list_rev"] = rev
    if html is not None:
        out["list"] = {"html": html}
    return JSONResponse(out, headers={"Cache-Control": "no-store"})  # application/json (§19.8 L5)


@router.post("/app/ack")
async def live_ack(request: Request, db: Session = Depends(get_db)):
    """§19.2: live.js acks messages to the person that have been half on screen while the window had focus.
    JSON ``{"csrf_token", "ids": [...]}``, at most 100 ids; anything not addressed to the person is skipped."""
    try:
        data = json.loads(await request.body() or b"{}")
    except (ValueError, UnicodeDecodeError, RecursionError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("ids"), list) or len(data["ids"]) > ACK_MAX:
        return JSONResponse({"error": {"code": "invalid", "message": 'body must be {"csrf_token", "ids": [...]}'}},
                            status_code=400)
    viewer = app_viewer(request, db, str(data.get("csrf_token") or ""), app_mode_ok=True)
    if not viewer.app_mode:
        raise not_found()  # app mode only, like GET /app/live (§19.9); the website acks on view
    acked = []
    owned = set(db.scalars(select(Agent.id).where(Agent.owner_user_id == viewer.user.id)))
    for raw in data["ids"]:
        mid = queries.parse_uuid(raw)
        if mid is None:
            continue
        msg = db.get(Message, mid)
        # §21 H2: what the person sees in their app, to them or routed to their machines; person_ack refuses the rest
        if msg is None or msg.team_id not in _team_ids(viewer) or not messaging.is_unread_for(db, viewer.user.id, msg, owned):
            continue
        try:
            _, done = messaging.person_ack(db, viewer.user, mid, viewer.scope)
        except messaging.MessagingError:
            continue
        if done:
            acked.append(str(mid))
    db.commit()
    return JSONResponse({"acked": acked})


# TEMP layout diagnostic (the "everything at the top" shell bug; remove when it is closed). app-mode.js posts at most
# three reports per page load: numbers and short class names, never ids, URLs or text. One INFO line each.
def _layout_diag_on(environ=None) -> bool:
    """RAINCLI_LAYOUT_DIAG: on unless it is "0" (main can switch it off without a code change; read at start)."""
    import os

    return (os.environ if environ is None else environ).get("RAINCLI_LAYOUT_DIAG", "1").strip() != "0"


LAYOUT_DIAG = _layout_diag_on()
LAYOUT_DIAG_MAX_BODY = 4096
LAYOUT_DIAG_PER_WINDOW, LAYOUT_DIAG_WINDOW = 10, 600.0  # per app session and per server process: 10 per 10 minutes
LAYOUT_DIAG_CHILDREN = ("nav.rc-nav", "section.rc-list", "main.rc-main")


def _layout_anomaly(layout: dict) -> bool:
    """Only an anomaly is logged (as app-mode.js only sends one): the document scrolled, a guard fired, body
    children other than the nav, the list (absent on no-list pages) and main, or a pane short of the window."""
    panes = (layout["nav_bottom"], layout["list_bottom"], layout["main_bottom"], layout["compose_bottom"])
    return (layout["doc_scroll_top"] != 0 or layout["height_fix"] or layout["scroll_guard"]
            or not 2 <= layout["children_n"] <= 3 or any(c not in LAYOUT_DIAG_CHILDREN for c in layout["children"])
            or any(b is not None and abs(b - layout["inner_h"]) > 2 for b in panes))
_PRINTABLE = re.compile(r"[\x20-\x7e]*")
_INT = (-100_000, 10_000_000)


def _diag_int(value, low=_INT[0], high=_INT[1], nullable=False):
    if value is None and nullable:
        return True
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _diag_str(value, cap):
    return isinstance(value, str) and len(value) <= cap and _PRINTABLE.fullmatch(value) is not None


LAYOUT_DIAG_FIELDS = {
    "event": lambda v: v in ("load", "show", "resize"),
    "page": lambda v: v in ("inbox", "conversation", "other"),
    "inner_w": lambda v: _diag_int(v, 0, 100_000), "inner_h": lambda v: _diag_int(v, 0, 100_000),
    "client_h": lambda v: _diag_int(v, 0, 100_000),
    "dpr": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 16,
    "children_n": lambda v: _diag_int(v, 0, 10_000),
    "children": lambda v: isinstance(v, list) and len(v) <= 10 and all(_diag_str(c, 40) for c in v),
    "grid_rows": lambda v: _diag_str(v, 80), "body_height": lambda v: _diag_str(v, 40),
    "nav_bottom": lambda v: _diag_int(v, nullable=True), "list_bottom": lambda v: _diag_int(v, nullable=True),
    "main_bottom": lambda v: _diag_int(v, nullable=True), "compose_bottom": lambda v: _diag_int(v, nullable=True),
    "doc_scroll_top": lambda v: _diag_int(v), "doc_scroll_h": lambda v: _diag_int(v, 0),
    "doc_client_h": lambda v: _diag_int(v, 0),
    "nav_scroll_h": lambda v: _diag_int(v, 0, nullable=True), "nav_client_h": lambda v: _diag_int(v, 0, nullable=True),
    "visibility": lambda v: v in ("visible", "hidden", "prerender"),
    "height_fix": lambda v: isinstance(v, bool), "scroll_guard": lambda v: isinstance(v, bool),
}


def _layout_diag_log():
    import logging
    import sys

    logger = logging.getLogger("raincli_server.layout_diag")
    if not logger.handlers:  # uvicorn configures only its own loggers: give this one a stderr line at INFO
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


class _DiagLimiter:
    """A sliding window per app session, in memory (per app instance)."""

    def __init__(self, limit: int, window: float):
        import threading
        import time

        self.limit, self.window, self.clock = limit, window, time.monotonic
        self._hits: dict = {}
        self._lock = threading.Lock()

    def allow(self, key) -> bool:
        now = self.clock()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if t > now - self.window]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 10000:
                for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - self.window]:
                    del self._hits[k]
            return True


_diag_limiter = _DiagLimiter(LAYOUT_DIAG_PER_WINDOW, LAYOUT_DIAG_WINDOW)


@router.post("/app/layout-diag")
async def layout_diag(request: Request, db: Session = Depends(get_db)):
    """TEMP: one window-layout report from app-mode.js, validated strictly and logged as one line; 204."""
    from raincli_server.models import PersonSession

    def bad(status: int, code: str) -> JSONResponse:
        return JSONResponse({"error": {"code": code}}, status_code=status, headers={"Cache-Control": "no-store"})

    if not LAYOUT_DIAG:
        raise not_found()
    raw = await request.body()
    if len(raw) > LAYOUT_DIAG_MAX_BODY:
        return bad(413, "too_large")
    try:
        data = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError, RecursionError):
        data = None
    if not isinstance(data, dict) or set(data) - {"csrf_token", "layout"}:
        return bad(400, "invalid")
    viewer = app_viewer(request, db, str(data.get("csrf_token") or ""), app_mode_ok=True)
    if not viewer.app_mode:
        raise not_found()
    if not _diag_limiter.allow(viewer.web_session.id):
        return bad(429, "rate_limited")
    layout = data.get("layout")
    if (not isinstance(layout, dict) or set(layout) != set(LAYOUT_DIAG_FIELDS)
            or not all(check(layout[key]) for key, check in LAYOUT_DIAG_FIELDS.items())):
        return bad(400, "invalid")
    if not _layout_anomaly(layout):
        return Response(status_code=204, headers={"Cache-Control": "no-store"})  # a healthy window: no line
    person = db.get(PersonSession, viewer.web_session.person_session_id)
    machine = db.get(Agent, person.machine_agent_id) if person is not None else None
    version = security.app_version(request.headers.get("user-agent"))
    fields = json.dumps({key: layout[key] for key in LAYOUT_DIAG_FIELDS}, separators=(",", ":"), sort_keys=False)
    _layout_diag_log().info("layout-diag machine=%s version=%s %s", machine.handle if machine else "none",
                            ".".join(map(str, version)) if version else "none", fields)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/app/person-warning", response_class=HTMLResponse)
def person_warning(request: Request, to: str = "", team: str = "", db: Session = Depends(get_db)):
    """§21 C7.4: the warning about a person's inbox for the compose page (app mode and website), as HTML. ``to`` is
    an email, a unique name or a picker id. Only people in the viewer's teams; anything else gets the same empty
    answer, so it never confirms that a handle, an agent or an outsider exists."""
    from raincli_server.web import thread

    viewer = app_viewer(request, db, app_mode_ok=True)
    chosen = _chosen_team_ids(viewer, team)
    picked = queries.person_by_id(db, viewer.user, chosen if team else _team_ids(viewer), to) \
        if to.startswith("p.") else None
    warning = thread.person_warning(db, picked[0].id, picked[1], viewer=viewer.user) if picked else \
        thread.warning_for_text(db, chosen, to, viewer.user)
    return HTMLResponse(fragment(request, "_sender_warning.html", viewer, warning=warning),
                        headers={"Cache-Control": "no-store"})


@router.get("/app/archived", response_class=HTMLResponse)
def archived_conversations(request: Request, db: Session = Depends(get_db)):
    """§17.2: the archived conversations, each with Unarchive; the unread counts stay (§17.3 A3)."""
    viewer = app_viewer(request, db, app_mode_ok=True)
    rows = queries.list_conversations(db, viewer.user, _team_ids(viewer), archived="only")
    template = "app_mode/archived.html" if viewer.app_mode else "app/archived.html"
    return render(request, template, viewer=viewer, conversations=rows)


def _set_archived(request: Request, db: Session, conversation_id: str, csrf_token: str, archived: bool,
                  through: str = "") -> bool:
    """Archive or unarchive as the person (§17.2): either endpoint, or the owner of a machine endpoint, the
    same rule as seeing the conversation here. In the app-mode allowlist (§17.3 A5), with CSRF."""
    viewer = app_viewer(request, db, csrf_token, app_mode_ok=True)
    if queries.get_conversation(db, viewer.user, _team_ids(viewer), conversation_id) is None:
        raise not_found()
    seq = int(through) if through.isdigit() and len(through) < 19 else None
    try:
        conv = messaging.archive_as_person(db, viewer.user, conversation_id, archived, seq, viewer.scope)
    except messaging.MessagingError:
        raise not_found() from None
    state = messaging.archive_json(db, conv)["archived"]
    db.commit()
    return state


@router.post("/app/conversations/{conversation_id}/archive")
def archive_conversation(request: Request, conversation_id: str, csrf_token: str = Form(""),
                         archived_through_seq: str = Form(""), db: Session = Depends(get_db)):
    if not _set_archived(request, db, conversation_id, csrf_token, True, archived_through_seq):
        # A message newer than the page the person saw keeps it listed (§17.3 A1): say so, on the thread.
        return redirect(request, f"/app/conversations/{conversation_id}?notice=newer#latest")
    return redirect(request, "/app/inbox?notice=archived")


@router.post("/app/conversations/{conversation_id}/unarchive")
def unarchive_conversation(request: Request, conversation_id: str, csrf_token: str = Form(""),
                           back: str = Form(""), db: Session = Depends(get_db)):
    _set_archived(request, db, conversation_id, csrf_token, False)
    if back == "archived":
        return redirect(request, "/app/archived?notice=unarchived")
    return redirect(request, f"/app/conversations/{conversation_id}?notice=unarchived#latest")


def _reply_target(db: Session, viewer: auth.Viewer, view: queries.ConversationView, parent: Message | None):
    """§21 C2, C4: where the person's message goes. In a conversation, the other person (``{"person": email}``); a
    reply carries only ``in_reply_to`` (``to`` None) and the server picks its target from the parent (PR1)."""
    if parent is not None or view.peer_user is None:
        return None
    return {"person": view.peer_user.email}


def _reply_name(db: Session, viewer: auth.Viewer, parent: Message) -> str:
    """The person a reply goes to, by name (the composer's "To: …")."""
    from raincli_server.web import thread

    other = messaging.recipient_endpoint(db, parent) if parent.sender_user_id == viewer.user.id else \
        messaging.sender_endpoint(db, parent)
    return thread.Names(db).person(other, parent.team_id)


def _typed_recipient(db: Session, viewer: auth.Viewer, text: str, team_slug: str):
    """§21 C2.1, C7.2: a typed recipient (email, unique name) or a picker id ``p.<team>.<user>`` to ``(team, to)``,
    or a ``(None, message)`` refusal in presentation's words. A picker id carries its team. Typed text is resolved
    in one team (§21.13): the only one, or the chosen one; with several and none chosen (or a slug that isn't one
    of the viewer's teams) it is refused, saying when the text names people in more than one of them."""
    from raincli_server import presentation

    teams = [t for t, _ in viewer.teams]
    if team_slug:
        teams = [t for t in teams if t.slug == team_slug]
        if not teams:
            return None, CHOOSE_TEAM
    text = (text or "").strip()
    if text.startswith("p."):
        picked = queries.person_by_id(db, viewer.user, [t.id for t in teams], text)
        if picked is None:
            return None, presentation.error_text("invalid")
        return picked[0], {"person": picked[1].email}
    if len(teams) > 1:
        named = [t for t in teams if presentation.resolve_person_text(db, t.id, text, sender=viewer.user).kind
                 in ("person", "ambiguous")]
        return None, (f"{text[:80]} is in more than one of your teams. Choose a team." if len(named) > 1
                      else CHOOSE_TEAM)
    refusal = None
    for team in teams:
        found = presentation.resolve_person_text(db, team.id, text, sender=viewer.user)
        if found.kind == "person" and found.user is not None:
            if found.user.id == viewer.user.id:
                return None, "You can't send a message to yourself."
            return team, {"person": found.user.email}
        error = found.error
        if refusal is None or (error and error[1] != "address_people") or (found.mapped is not None):
            refusal = error[2] if error else refusal
    return None, refusal or presentation.error_text("invalid")


CHOOSE_TEAM = "Choose one of your teams."


def _conversation_page(
    request: Request, db: Session, viewer: auth.Viewer, view: queries.ConversationView, *, reply_to: str = "",
    status: int = 200, **ctx,
) -> HTMLResponse:
    parent = None
    rid = queries.parse_uuid(reply_to)
    if rid is not None:
        parent = next((m for m in view.messages if m.id == rid), None)
    to_target = _reply_target(db, viewer, view, parent)
    to_label = "@" + to_target["person"] if to_target else ""
    to_name = view.name if parent is None else _reply_name(db, viewer, parent)
    if viewer.app_mode:  # the signatures of the parts live.js may replace (§19.9 F4)
        ctx.setdefault("header_sig", live_sig(fragment(request, "app_mode/_head_status.html", viewer,
                                                       presence=view.presence)))
        ctx.setdefault("warning_sig", live_sig(fragment(request, "_sender_warning.html", viewer,
                                                        warning=view.warning)) if view.warning else "none")
    common = dict(viewer=viewer, status=status, view=view, parent=parent, fixed_to=to_label, to_label=to_label,
                  to_name=to_name, conversation_id=view.conversation.id, message_id=str(uuid.uuid4()), form={},
                  live_now=identity.now().strftime("%Y-%m-%dT%H:%M:%S.%fZ"), **ctx)
    if viewer.app_mode:
        return render(request, "app_mode/conversation.html",
                      conversations=queries.list_conversations(db, viewer.user, _team_ids(viewer)),
                      **_list_extras(db, viewer), **common)
    return render(request, "app/conversation.html", **common)


@router.get("/app/compose", response_class=HTMLResponse)
def compose(request: Request, to: str = "", db: Session = Depends(get_db)):
    viewer = app_viewer(request, db, app_mode_ok=True)
    return _compose_page(request, db, viewer, form={"to": to[:140], "body": "", "team": ""})


def _compose_page(request, db, viewer, *, form, status: int = 200, error: str | None = None) -> HTMLResponse:
    from raincli_server.web import thread

    chosen = _chosen_team_ids(viewer, form.get("team", ""))
    to = form.get("to", "")
    picked = queries.person_by_id(db, viewer.user, chosen if form.get("team") else _team_ids(viewer), to) \
        if to.startswith("p.") else None
    if picked is None and to:  # §21 C7.2: ?to= takes an email or a unique name, pre-picked as that person
        from raincli_server import presentation

        for team in [t for t, _ in viewer.teams if t.id in chosen]:
            found = presentation.resolve_person_text(db, team.id, to, sender=viewer.user)
            if found.kind == "person" and found.user is not None and found.user.id != viewer.user.id:
                picked = (team, found.user)
                form = {**form, "to": f"p.{team.id}.{found.user.id}"}
                break
    ctx = dict(viewer=viewer, status=status, error=error, form=form, draft=form.get("body", ""),
               message_id=str(uuid.uuid4()), conversation_id=None, **_directory(db, viewer),
               to_warning=thread.person_warning(db, picked[0].id, picked[1], viewer=viewer.user) if picked else
               thread.warning_for_text(db, chosen, to, viewer.user))
    if viewer.app_mode:
        return render(request, "app_mode/compose.html",
                      conversations=queries.list_conversations(db, viewer.user, _team_ids(viewer)),
                      **_list_extras(db, viewer), **ctx)
    return render(request, "app/compose.html", **ctx)


@router.post("/app/conversations/{conversation_id}/send", response_class=HTMLResponse)
def send_message(
    request: Request, conversation_id: str, csrf_token: str = Form(""), message_id: str = Form(""),
    to: str = Form(""), team: str = Form(""), body: str = Form(""), in_reply_to: str = Form(""),
    files: list[UploadFile] = File(default=[]), db: Session = Depends(get_db),
):
    """Send as the person (§16.9); ``conversation_id`` is "new" from the compose page."""
    viewer = app_viewer(request, db, csrf_token, app_mode_ok=True)
    team_ids = _team_ids(viewer)
    view = None
    if conversation_id != "new":
        view = queries.get_conversation(db, viewer.user, team_ids, conversation_id)
        if view is None:
            raise not_found()
    mid = queries.parse_uuid(message_id) or uuid.uuid4()
    parent_id = queries.parse_uuid(in_reply_to) if in_reply_to else None
    body = body.replace("\r\n", "\n")
    typed = to
    refusal = None
    if view is not None:
        team_obj = view.team
        parent = next((m for m in view.messages if m.id == parent_id), None) if parent_id else None
        if parent_id and parent is None:
            parent_id = None  # not a message in this conversation: an ordinary message to the person
        to = _reply_target(db, viewer, view, parent)
        conv_for_send = view.conversation.id if parent is None and to is not None else None
        if parent is None and to is None:
            refusal = "Reply to one of the messages here."  # your own agents' conversation takes replies only
    else:
        team_obj, to = _typed_recipient(db, viewer, typed, team)
        if team_obj is None:
            refusal, to = to, None
        conv_for_send = None

    def fail(status: int, message: str):
        db.rollback()
        if view is not None:
            fresh = queries.get_conversation(db, viewer.user, team_ids, conversation_id)
            return _conversation_page(request, db, viewer, fresh, reply_to=in_reply_to, status=status,
                                      error=message, draft=body)
        return _compose_page(request, db, viewer, status=status, error=message,
                             form={"to": (typed or "")[:254], "body": body, "team": team})

    if refusal is not None:
        return fail(400, refusal)
    if team_obj is None:
        return fail(400, "Choose one of your teams.")
    retry = request.app.state.web_send_limiter.check(("user", viewer.user.id))  # §16.3: per-user web limit
    if retry is not None:
        return fail(429, f"You're sending too fast. Wait {retry} seconds and try again.")
    try:
        attachments = _read_uploads(files)
        msg, created = queries.send_as_person(
            db, viewer.user, team_obj, id=mid, to=to, body=body,
            conversation_id=conv_for_send, in_reply_to=parent_id,
            max_pending=_settings(request).max_pending, attachments=attachments,
            quotas=quotas.Quotas.from_settings(_settings(request)),
        )
    except queries.SendError as exc:
        status = {"inbox_full": 429, "id_conflict": 409, "quota_exceeded": 413}.get(exc.code, 400)
        return fail(status, exc.message)
    db.commit()
    if viewer.app_mode:  # §20.2: app mode never shows the "Message stored on the server." banner
        return redirect(request, f"/app/conversations/{msg.conversation_id}#m-{msg.id}")
    notice = "sent" if created else "duplicate"
    return redirect(request, f"/app/conversations/{msg.conversation_id}?notice={notice}#m-{msg.id}")


SEND_STATUS = {"inbox_full": 429, "id_conflict": 409, "quota_exceeded": 413}


def _send_error(status: int, code: str, message: str, retry_after: int | None = None) -> JSONResponse:
    """§20.6 L5: every failure /app/send produces is {error: {code, message}} with its HTTP status."""
    error: dict = {"code": code, "message": message}
    if retry_after is not None:
        error["retry_after"] = retry_after
    return JSONResponse({"error": error}, status_code=status, headers={"Cache-Control": "no-store"})


def _message_ctx(db: Session, viewer: auth.Viewer, conv, team, me, peer) -> dict:
    """What _message.html needs besides the line: may the viewer write here, and replies-only for their own
    agents' conversation (§21 C13 H2)."""
    who = presentation.conversation_peer(db, viewer.user, conv)
    return {"can_send": queries.ConversationView(conv, team, me, peer, peer_user=who.user, kind=who.kind).can_send,
            "reply_only": who.kind == "self"}


def _sent_message(request: Request, db: Session, viewer: auth.Viewer, msg: Message, **extra) -> JSONResponse:
    """The message as the thread shows it (§19.1, the same template as /app/live), for the client's merge by id."""
    from raincli_server.web import thread

    conv, team, me, peer, owned = queries.conversation_head(db, viewer.user, _team_ids(viewer), str(msg.conversation_id))
    lines, _ = queries.message_lines(db, viewer.user, owned, [msg], thread.Names(db))
    html = fragment(request, "app_mode/_message.html", viewer, line=lines[0],
                    atts=queries.attachment_meta(db, [msg.id]).get(msg.id, []), conv_id=conv.id,
                    **_message_ctx(db, viewer, conv, team, me, peer))
    return JSONResponse({"conversation_id": str(msg.conversation_id), "id": str(msg.id), "seq": msg.seq,
                         "message_html": html, **extra}, headers={"Cache-Control": "no-store"})


@router.post("/app/send")
def send_in_place(
    request: Request, csrf_token: str = Form(""), conversation_id: str = Form(""), message_id: str = Form(""),
    body: str = Form(""), in_reply_to: str = Form(""), files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    """Send in place into an existing conversation (§20.2, §20.6 M2, L4, L5): the same fields as the form send
    plus the client's message id, answered with JSON. A replay of a stored id is answered before the rate limit;
    a different message under a stored id is a 409 that carries the stored message, when it is the viewer's."""
    try:
        viewer = app_viewer(request, db, csrf_token, app_mode_ok=True)
    except WebError as exc:
        return _send_error(exc.status, "forbidden", exc.message)
    if not viewer.app_mode:
        return _send_error(404, "not_found", "That page does not exist, or you do not have access to it.")
    team_ids = _team_ids(viewer)
    view = queries.get_conversation(db, viewer.user, team_ids, conversation_id)
    if view is None:
        return _send_error(404, "not_found", "That conversation does not exist, or you do not have access to it.")
    mid = queries.parse_uuid(message_id)
    if mid is None:
        return _send_error(400, "invalid", "This message has no id. Reload the window and try again.")
    parent_id = queries.parse_uuid(in_reply_to) if in_reply_to else None
    parent = next((m for m in view.messages if m.id == parent_id), None) if parent_id else None
    if parent_id and parent is None:
        parent_id = None
    to = _reply_target(db, viewer, view, parent)  # §21 C2, C4: the person, or a reply by in_reply_to alone
    if parent is None and to is None:
        return _send_error(400, "invalid", "Reply to one of the messages here.")
    conv_for_send = view.conversation.id if parent is None else None
    stored = db.get(Message, mid)
    if stored is None:  # §20.6 M2: a replay never counts against the limit
        retry = request.app.state.web_send_limiter.check(("user", viewer.user.id))
        if retry is not None:
            return _send_error(429, "rate_limited", f"You're sending too fast. Wait {retry} seconds and try again.",
                               retry_after=retry)
    try:
        msg, created = queries.send_as_person(
            db, viewer.user, view.team, id=mid, to=to, body=body.replace("\r\n", "\n"),
            conversation_id=conv_for_send, in_reply_to=parent_id, max_pending=_settings(request).max_pending,
            attachments=_read_uploads(files), quotas=quotas.Quotas.from_settings(_settings(request)),
        )
    except queries.SendError as exc:
        db.rollback()
        status = SEND_STATUS.get(exc.code, 400)
        if exc.code == "id_conflict":
            mine = db.get(Message, mid)
            if mine is not None and mine.sender_user_id == viewer.user.id and \
                    queries.get_conversation(db, viewer.user, team_ids, str(mine.conversation_id)) is not None:
                response = _sent_message(request, db, viewer, mine)
                payload = json.loads(response.body)
                return JSONResponse({"error": {"code": exc.code, "message": "Your earlier version was already sent."},
                                     **payload}, status_code=409, headers={"Cache-Control": "no-store"})
        return _send_error(status, exc.code, exc.message, retry_after=LIVE_INTERVAL if status == 429 else None)
    db.commit()
    return _sent_message(request, db, viewer, msg, duplicate=not created)


def _choose_team(viewer: auth.Viewer, slug: str) -> Team | None:
    teams = [team for team, _ in viewer.teams]
    if len(teams) == 1:
        return teams[0]
    return next((t for t in teams if t.slug == slug), None)


def _read_uploads(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    """(filename, exact bytes) for each chosen file; the empty part of an unused file input is skipped."""
    chosen = [f for f in files if f.filename]
    if len(chosen) > security.ATTACHMENT_MAX_COUNT:
        raise queries.SendError("invalid", f"Attach at most {security.ATTACHMENT_MAX_COUNT} files.")
    # Read one byte past the per-file limit so oversize files fail validation without buffering more.
    return [(f.filename, f.file.read(security.ATTACHMENT_MAX_BYTES + 1)) for f in chosen]


@router.get("/app/messages/{message_id}/attachments/{attachment_id}")
def download_attachment(request: Request, message_id: str, attachment_id: str, db: Session = Depends(get_db)):
    viewer = app_viewer(request, db, app_mode_ok=True)
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
        agents=queries.visible_machines(db, viewer.user, viewer.teams), **ctx,
    )


def _token_page(request: Request, viewer: auth.Viewer, agent: Agent, team: Team, token: str, rotated: bool):
    settings = _settings(request)
    return render(
        request, "app/agent_token.html", viewer=viewer, agent=agent, team=team, token=token, rotated=rotated,
        api_url=settings.public_url + settings.root_path, filename=f"raincli-{agent.handle}.json",
    )


@router.get("/app/agents", response_class=HTMLResponse)
def agents(request: Request, db: Session = Depends(get_db)):
    viewer = app_viewer(request, db, app_mode_ok=True)
    if viewer.app_mode:  # §21 C7.1: Agents became People
        return redirect(request, "/app/people")
    return _agents_page(request, db, viewer, form={})


# People, the picker and the inbox (§21 C7.1-C7.3, C8.3) -----------------------------------------

@router.get("/app/people", response_class=HTMLResponse)
def people_page(request: Request, db: Session = Depends(get_db)):
    """§21 C7.1: teammates across the viewer's teams with presence · speed and Message. Never machines or agents."""
    viewer = app_viewer(request, db, app_mode_ok=True)
    if not viewer.app_mode:
        raise not_found()  # the website keeps its Machines page (C5.8); People is the app window's
    return render(request, "app_mode/people.html", viewer=viewer, **_directory(db, viewer))


RECIPIENTS_REFRESH = 30  # §20.6 L3: the picker refetches on open, and at most every 30 s while open


@router.get("/app/recipients")
def recipients(request: Request, db: Session = Depends(get_db)):
    """§21 C7.2 (and §20.1, §20.6 L1-L3): the people the viewer can message, for the picker. People only: an
    opaque id ``p.<team>.<user>``, the name, the team (shown only with several teams), presence · speed, and hidden
    match terms (name and email, never displayed, only in this JSON). Recent people first (up to 5, by their last
    message's seq), then everyone alphabetically. ``no-store``; current teams and active members only."""
    viewer = app_viewer(request, db, app_mode_ok=True)
    if not viewer.app_mode:
        raise not_found()
    now = identity.now()
    rows = queries.people_directory(db, viewer.user, _team_ids(viewer), now)
    several = len(viewer.teams) > 1
    recent = sorted((r for r in rows if r.last_seq), key=lambda r: r.last_seq, reverse=True)[:5]
    recent_ids = {r.id for r in recent}

    def item(r, group):
        return {"id": r.id, "name": r.name, "team": r.team.name if several else None, "group": group,
                "presence": r.presence.presence, "speed": r.presence.speed, "status": r.presence.text,
                "match": [r.name.lower(), r.user.email.lower()]}

    out = [item(r, "recent") for r in recent] + [item(r, "all") for r in rows if r.id not in recent_ids]
    return JSONResponse({"recipients": out, "now": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                         "refresh": RECIPIENTS_REFRESH}, headers={"Cache-Control": "no-store"})


@router.post("/app/inbox")
def set_inbox(request: Request, csrf_token: str = Form(""), team: str = Form(""), machine: str = Form(""),
              agent: str = Form(""), choice: str = Form(""), deliver_waiting: str = Form(""), none: str = Form(""),
              back: str = Form(""), db: Session = Depends(get_db)):
    """§21 C7.3, C8.3: pick (or clear) the viewer's inbox in a team, from the hosted banner (app mode or website).
    ``machine`` is one of the viewer's own machines (its handle; theirs alone, so nothing leaks), ``agent`` its
    agent name, or both as ``choice`` = "machine|agent" (the banner's radio); ``deliver_waiting`` ("1") re-targets the
    waiting messages. Through ``messaging.set_person_inbox``, which validates and locks (C1.1, M5)."""
    if choice and "|" in choice:
        machine, agent = choice.split("|", 1)
    viewer = app_viewer(request, db, csrf_token, app_mode_ok=True)
    teams = [t for t, _ in viewer.teams if t.slug == team] if team else [t for t, _ in viewer.teams][:1]
    target = "/app/inbox" if back != "people" else "/app/people"
    if not teams:
        raise not_found()
    try:
        if none == "1":
            messaging.clear_person_inbox(db, viewer.user, teams[0].id, via="app" if viewer.app_mode else "web")
            result = {"stayed": 0}
        else:
            owned = db.scalar(select(Agent).where(Agent.owner_user_id == viewer.user.id, Agent.team_id == teams[0].id,
                                                  Agent.handle == machine, Agent.revoked_at.is_(None))) \
                if machine else None
            if owned is None or not agent:
                raise messaging.MessagingError(400, "inbox_invalid", "inbox_invalid")
            result = messaging.set_person_inbox(db, viewer.user, teams[0].id, owned.id, agent,
                                                deliver_waiting=deliver_waiting == "1",
                                                via="app" if viewer.app_mode else "web")
    except messaging.MessagingError as exc:
        db.rollback()
        if exc.code != "inbox_invalid":
            raise
        return redirect(request, f"{target}?notice=inbox-invalid")
    db.commit()
    stayed = int((result or {}).get("stayed") or 0)
    return redirect(request, f"{target}?notice=inbox-set" + (f"&stayed={stayed}" if stayed else ""))


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
        members=queries.team_members(db, team),
        # §21 C5.8 (PR11): a member sees only their own machines here; a team owner sees every machine (no agents).
        team_agents=[r for r in queries.team_agents(db, team) if is_owner or r.agent.owner_user_id == viewer.user.id],
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
    if not hasattr(app.state, "web_login_limiter"):  # create_app shares one with the API sign-in
        app.state.web_login_limiter = auth.LoginLimiter()
    from raincli_server.api import RateLimiter

    app.state.web_send_limiter = RateLimiter(settings.rate_limit_per_min)  # per user (§16.3)
    app.add_middleware(WebSecurityMiddleware, root_path=settings.root_path, cookie_secure=settings.cookie_secure)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def _error_page(request: Request, exc: WebError) -> HTMLResponse:
        db = request.app.state.sessionmaker()
        try:
            viewer = auth.load_viewer(db, request)
        finally:
            db.close()
        return render(request, "error.html", viewer=viewer, status=exc.status, code=exc.status,
                      title=exc.title, message=exc.message, link=exc.link)

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
