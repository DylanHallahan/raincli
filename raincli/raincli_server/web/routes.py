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

from sqlalchemy import func, or_, select

from raincli_server import identity, messaging, quotas, security
from raincli_server.config import Settings
from raincli_server.models import Agent, HandoffCode, Message, PersonSession, Team, User
from raincli_server.web import auth, queries
from raincli_server.web import team as team_section
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
}
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
        "viewer": viewer,
        "csrf": csrf if csrf is not None else (viewer.csrf if viewer else ""),
        "path": _route_path(request),
        "notice": NOTICES.get(request.query_params.get("notice", "")),
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
    machines, people = queries.team_directory(db, viewer.user, _team_ids(viewer))
    return {"machines": machines, "people": people, "teams": viewer.teams}


@router.get("/app", response_class=HTMLResponse)
@router.get("/app/inbox", response_class=HTMLResponse)
def inbox(request: Request, db: Session = Depends(get_db)):
    viewer = app_viewer(request, db, app_mode_ok=True)
    team_ids = _team_ids(viewer)
    rows = queries.list_conversations(db, viewer.user, team_ids)
    from raincli_server.web import thread

    if viewer.app_mode:
        return _app_inbox(request, db, viewer, team_ids, rows)
    return render(request, "app/inbox.html", viewer=viewer, conversations=rows,
                  machines_owned=bool(queries.my_agents(db, viewer.user, team_ids)),
                  pending=queries.pending_for_user(db, viewer.user, team_ids),
                  owner_warnings=thread.owner_warnings(db, viewer.user, team_ids))


def _app_inbox(request: Request, db: Session, viewer: auth.Viewer, team_ids, rows) -> HTMLResponse:
    """Inbox in app mode. §20.6 M1: app-mode.js opens the latest conversation from here when the list is visible
    (P2 runs in the client); with no conversations, the first-run steps, step 1 done after a recent connection."""
    from raincli_server.web import present, thread

    return render(request, "app_mode/inbox.html", viewer=viewer, conversations=rows,
                  owner_warnings=thread.owner_warnings(db, viewer.user, team_ids),
                  connected=present.connected_machine(db, viewer.user, team_ids), **_list_extras(db, viewer))


@router.get("/app/conversations/{conversation_id}", response_class=HTMLResponse)
def conversation(request: Request, conversation_id: str, reply_to: str = "", db: Session = Depends(get_db)):
    viewer = app_viewer(request, db, app_mode_ok=True)
    view = queries.get_conversation(db, viewer.user, _team_ids(viewer), conversation_id)
    if view is None:
        raise not_found()
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
    revision differs. The revision is a digest of the list's inputs: each conversation's id, last seq, unread count
    and archive state, the Team section's presence words (minute-stable), and the owner's warnings."""
    from raincli_server.web import thread

    rows = queries.list_conversations(db, viewer.user, _team_ids(viewer))
    extras = _list_extras(db, viewer)
    warnings = thread.owner_warnings(db, viewer.user, _team_ids(viewer))
    digest = json.dumps([
        [[str(r.conversation.id), r.last.seq if r.last else 0, r.unacked, r.archived, r.name] for r in rows],
        [[a.get("to"), a.get("status")] for a in extras.get("team_agents", [])],
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
    """Extra context for the conversation list (the sidebar's Team section, §19.5), shared by the pages and
    GET /app/live."""
    from raincli_server.web import thread

    # §20.6 M1: the owner's "not connected" notice also sits at the top of the list on every app-mode page.
    return {"team_agents": team_section.team_agents(db, viewer),
            "list_owner_warnings": thread.owner_warnings(db, viewer.user, _team_ids(viewer))}


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
        changed = list(db.scalars(
            select(Message).where(*in_conv, Message.id.in_(newest_ids), mine,
                                  or_(Message.delivery_state.not_in(FINAL_STATES),
                                      Message.delivery_updated_at > changed_after))
            .order_by(Message.seq).limit(LIVE_IN_VIEW).execution_options(populate_existing=True)))
        names = thread.Names(db)
        lines, _ = queries.message_lines(db, viewer.user, owned, fresh + changed, names)
        atts = queries.attachment_meta(db, [m.id for m in fresh]) if fresh else {}
        view_ctx = dict(conv_id=conv.id, can_send=queries.ConversationView(conv, team, me, peer).can_send,
                        machine_inbox=peer.endpoint.kind == "machine")
        for line in lines[:len(fresh)]:
            out["messages"].append({"id": str(line.message.id), "seq": line.message.seq,
                                    "html": fragment(request, "app_mode/_message.html", viewer, line=line,
                                                     atts=atts.get(line.message.id, []), **view_ctx)})
        for line in lines[len(fresh):]:
            if line.trail is not None:
                out["states"].append({"id": str(line.message.id), "state": line.trail.outcome or
                                      line.message.delivery_state,
                                      "html": fragment(request, "app_mode/_trail.html", viewer, line=line)})
        presence = thread.header_presence(db, peer.endpoint, now)
        header_html = fragment(request, "app_mode/_head_status.html", viewer, presence=presence)
        out["header"] = {"word": presence.word if presence else None, "chip": presence.chip if presence else None,
                         "html": header_html, "sig": live_sig(header_html)}
        warning = thread.endpoint_warning(db, peer.endpoint)
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
    for raw in data["ids"]:
        mid = queries.parse_uuid(raw)
        if mid is None:
            continue
        msg = db.get(Message, mid)
        if msg is None or msg.recipient_user_id != viewer.user.id or msg.team_id not in _team_ids(viewer):
            continue
        try:
            _, done = messaging.person_ack(db, viewer.user, mid)
        except messaging.MessagingError:
            continue
        if done:
            acked.append(str(mid))
    db.commit()
    return JSONResponse({"acked": acked})


@router.get("/app/recipient-warning", response_class=HTMLResponse)
def recipient_warning(request: Request, to: str = "", team: str = "", db: Session = Depends(get_db)):
    """§19.6: the sender warning for a recipient chosen on the compose page (app mode and website), as HTML. Only
    a recipient in the viewer's current team: the one the page chose, when it names one (§19.8 L4)."""
    from raincli_server.web import thread

    viewer = app_viewer(request, db, app_mode_ok=True)
    chosen = [t.id for t, _ in viewer.teams if t.slug == team] if team else _team_ids(viewer)
    warning = thread.recipient_warning(db, chosen, to)
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
        conv = messaging.archive_as_person(db, viewer.user, conversation_id, archived, seq)
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


def _reply_target(db: Session, viewer: auth.Viewer, view: queries.ConversationView, parent: Message | None) -> str:
    """Where the person's message goes: the parent's other participant, else the conversation's peer."""
    if parent is None:
        return view.peer.label
    other = messaging.recipient_endpoint(db, parent) if parent.sender_user_id == viewer.user.id else \
        messaging.sender_endpoint(db, parent)
    return other.label()


def _reply_name(db: Session, viewer: auth.Viewer, parent: Message) -> str:
    from raincli_server.web import thread

    other = messaging.recipient_endpoint(db, parent) if parent.sender_user_id == viewer.user.id else \
        messaging.sender_endpoint(db, parent)
    return thread.Names(db).endpoint(other)


def _conversation_page(
    request: Request, db: Session, viewer: auth.Viewer, view: queries.ConversationView, *, reply_to: str = "",
    status: int = 200, **ctx,
) -> HTMLResponse:
    parent = None
    rid = queries.parse_uuid(reply_to)
    if rid is not None:
        parent = next((m for m in view.messages if m.id == rid), None)
    to_label = _reply_target(db, viewer, view, parent)
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

    ctx = dict(viewer=viewer, status=status, error=error, form=form, draft=form.get("body", ""),
               message_id=str(uuid.uuid4()), conversation_id=None, **_directory(db, viewer),
               to_warning=thread.recipient_warning(
                   db, [t.id for t, _ in viewer.teams if t.slug == form.get("team")] if form.get("team")
                   else _team_ids(viewer), form.get("to", "")))
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
    if view is not None:
        team_obj = view.team
        parent = next((m for m in view.messages if m.id == parent_id), None) if parent_id else None
        to = _reply_target(db, viewer, view, parent)
        conv_for_send = view.conversation.id if view.me.endpoint.kind == "person" and parent is None and \
            to == view.peer.label else None
    else:
        team_obj = _choose_team(viewer, team)
        conv_for_send = None

    def fail(status: int, message: str):
        db.rollback()
        if view is not None:
            fresh = queries.get_conversation(db, viewer.user, team_ids, conversation_id)
            return _conversation_page(request, db, viewer, fresh, reply_to=in_reply_to, status=status,
                                      error=message, draft=body)
        return _compose_page(request, db, viewer, status=status, error=message,
                             form={"to": to[:140], "body": body, "team": team})

    if team_obj is None:
        return fail(400, "Choose one of your teams.")
    retry = request.app.state.web_send_limiter.check(("user", viewer.user.id))  # §16.3: per-user web limit
    if retry is not None:
        return fail(429, f"You're sending too fast. Wait {retry} seconds and try again.")
    try:
        attachments = _read_uploads(files)
        msg, created = queries.send_as_person(
            db, viewer.user, team_obj, id=mid, to=queries.endpoint_from_text(to), body=body,
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


def _sent_message(request: Request, db: Session, viewer: auth.Viewer, msg: Message, **extra) -> JSONResponse:
    """The message as the thread shows it (§19.1, the same template as /app/live), for the client's merge by id."""
    from raincli_server.web import thread

    conv, team, me, peer, owned = queries.conversation_head(db, viewer.user, _team_ids(viewer), str(msg.conversation_id))
    lines, _ = queries.message_lines(db, viewer.user, owned, [msg], thread.Names(db))
    html = fragment(request, "app_mode/_message.html", viewer, line=lines[0],
                    atts=queries.attachment_meta(db, [msg.id]).get(msg.id, []), conv_id=conv.id,
                    can_send=queries.ConversationView(conv, team, me, peer).can_send,
                    machine_inbox=peer.endpoint.kind == "machine")
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
    to = _reply_target(db, viewer, view, parent)
    conv_for_send = view.conversation.id if view.me.endpoint.kind == "person" and parent is None and \
        to == view.peer.label else None
    stored = db.get(Message, mid)
    if stored is None:  # §20.6 M2: a replay never counts against the limit
        retry = request.app.state.web_send_limiter.check(("user", viewer.user.id))
        if retry is not None:
            return _send_error(429, "rate_limited", f"You're sending too fast. Wait {retry} seconds and try again.",
                               retry_after=retry)
    try:
        msg, created = queries.send_as_person(
            db, viewer.user, view.team, id=mid, to=queries.endpoint_from_text(to), body=body.replace("\r\n", "\n"),
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
        agents=queries.team_machines(db, viewer.user, _team_ids(viewer)), **ctx,
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
    if viewer.app_mode:  # the directory and picker only; machine management stays on the website (C2)
        return render(request, "app_mode/agents.html", viewer=viewer, **_directory(db, viewer))
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
