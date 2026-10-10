"""Browser sessions, CSRF tokens and login rate limiting (protocol §6).

Browser auth is separate from agent credentials: a random ``raincli_session``
cookie whose sha256 is stored in ``web_sessions`` together with a per-session
CSRF token. Forms shown before login (sign-in, invitation) use a signed
double-submit token bound to a random ``raincli_csrf`` cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import re
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta

from fastapi import Request
from fastapi.responses import Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from raincli_server import identity, security
from raincli_server.config import Settings
from raincli_server.models import Agent, PersonSession, Team, User, WebSession

SESSION_COOKIE = "raincli_session"
APP_COOKIE = "raincli_app"  # app-mode sessions from a handoff (§16.12 C3): SameSite=Strict, Path=/app/
PRE_CSRF_COOKIE = "raincli_csrf"
SESSION_TTL = timedelta(days=14)
PRE_CSRF_TTL = 60 * 60 * 12


@dataclass
class Viewer:
    user: User
    web_session: WebSession
    teams: list[tuple[Team, str]] = field(default_factory=list)

    @property
    def app_mode(self) -> bool:
        """An app-mode session: person scopes only, limited routes (§16.12 C2)."""
        return bool(self.web_session.app_mode)

    @property
    def csrf(self) -> str:
        return self.web_session.csrf_token

    def role_in(self, team_id) -> str | None:
        for team, role in self.teams:
            if team.id == team_id:
                return role
        return None

    @property
    def is_owner_anywhere(self) -> bool:
        return any(role == "owner" for _, role in self.teams)


def cookie_path(settings: Settings) -> str:
    # §6 specifies Path=/; under RAINCLI_ROOT_PATH the cookie is scoped to the prefix instead.
    return settings.root_path or "/"


def _set_cookie(response: Response, settings: Settings, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name, value, max_age=max_age, path=cookie_path(settings), secure=settings.cookie_secure,
        httponly=True, samesite="lax",
    )


def _clear_cookie(response: Response, settings: Settings, name: str) -> None:
    response.delete_cookie(
        name, path=cookie_path(settings), secure=settings.cookie_secure, httponly=True, samesite="lax",
    )


# Sessions -------------------------------------------------------------------

def start_session(db: Session, user: User, response: Response, settings: Settings) -> WebSession:
    token = security.new_token()
    ws = WebSession(
        user_id=user.id, token_hash=security.hash_token(token), csrf_token=secrets.token_urlsafe(32),
        expires_at=identity.now() + SESSION_TTL,
    )
    db.add(ws)
    db.flush()
    _set_cookie(response, settings, SESSION_COOKIE, token, int(SESSION_TTL.total_seconds()))
    _clear_cookie(response, settings, PRE_CSRF_COOKIE)
    return ws


def _session_from(db: Session, token: str | None, *, app_mode: bool,
                  user_agent: str | None = None) -> tuple[WebSession, User] | None:
    if not token or len(token) > 200:
        return None
    row = db.execute(
        select(WebSession, User).join(User, User.id == WebSession.user_id)
        .where(WebSession.token_hash == security.hash_token(token), WebSession.app_mode.is_(app_mode))
    ).first()
    if row is None:
        return None
    ws, user = row
    if ws.revoked_at is not None or ws.expires_at <= identity.now() or not user.is_active:
        return None
    if app_mode:
        # §16.14 S3: only the app install it was handed to may use it; the cookie alone is useless.
        if not security.same_install(ws.app_install_hash, user_agent):
            return None
        # An app-mode session ends no later than its person session, and with it (§16.12 C2, C6).
        ps = db.get(PersonSession, ws.person_session_id)
        machine = db.get(Agent, ps.machine_agent_id) if ps else None
        if (ps is None or ps.revoked_at is not None or machine is None or machine.revoked_at is not None
                or identity.now() >= identity.person_session_expires_at(ps)):
            return None
    return ws, user


def load_viewer(db: Session, request: Request) -> Viewer | None:
    """The browser's web session; otherwise an app-mode session from the ``raincli_app`` cookie, which
    is sent only under ``/app/`` and only same-site."""
    found = _session_from(db, request.cookies.get(SESSION_COOKIE), app_mode=False) or \
        _session_from(db, request.cookies.get(APP_COOKIE), app_mode=True, user_agent=request.headers.get("user-agent"))
    if found is None:
        return None
    ws, user = found
    return Viewer(user=user, web_session=ws, teams=identity.teams_for_user(db, user))


def start_app_session(db: Session, ps: PersonSession, user: User, response: Response, settings: Settings,
                      install_hash: str) -> WebSession:
    """An app-mode web session for a consumed handoff code (§16.10, §16.12 C2, C3), bound to the app
    install the code was bound to (§16.14 S3)."""
    token = security.new_token()
    ws = WebSession(user_id=user.id, token_hash=security.hash_token(token), csrf_token=secrets.token_urlsafe(32),
                    expires_at=identity.person_session_expires_at(ps), app_mode=True, person_session_id=ps.id,
                    app_install_hash=install_hash)
    db.add(ws)
    db.flush()
    max_age = max(1, int((ws.expires_at - identity.now()).total_seconds()))
    response.set_cookie(APP_COOKIE, token, max_age=max_age, path=app_cookie_path(settings),
                        secure=settings.cookie_secure, httponly=True, samesite="strict")
    return ws


def app_cookie_path(settings: Settings) -> str:
    return (settings.root_path or "") + "/app/"


def app_session_user(db: Session, request: Request) -> uuid.UUID | None:
    """The user of a still-valid ``raincli_app`` session in this browser, if any."""
    found = _session_from(db, request.cookies.get(APP_COOKIE), app_mode=True,
                          user_agent=request.headers.get("user-agent"))
    return found[1].id if found else None


def end_session(db: Session, viewer: Viewer | None, response: Response, settings: Settings) -> None:
    if viewer is not None:
        db.execute(
            update(WebSession).where(WebSession.id == viewer.web_session.id, WebSession.revoked_at.is_(None))
            .values(revoked_at=identity.now())
        )
    _clear_cookie(response, settings, SESSION_COOKIE)


# CSRF -----------------------------------------------------------------------

def _pre_token(settings: Settings, nonce: str) -> str:
    return hmac.new(settings.secret_key.encode(), f"raincli-precsrf|{nonce}".encode(), hashlib.sha256).hexdigest()


def pre_csrf(request: Request, response_cookies: list[str]) -> str:
    """Token for a form rendered before login. Appends a new nonce to set if needed."""
    settings: Settings = request.app.state.settings
    nonce = request.cookies.get(PRE_CSRF_COOKIE)
    if not nonce or len(nonce) > 100:
        nonce = secrets.token_urlsafe(32)
        response_cookies.append(nonce)
    return _pre_token(settings, nonce)


def set_pre_csrf_cookie(response: Response, settings: Settings, nonce: str) -> None:
    _set_cookie(response, settings, PRE_CSRF_COOKIE, nonce, PRE_CSRF_TTL)


def check_csrf(request: Request, submitted: object, viewer: Viewer | None) -> bool:
    if not isinstance(submitted, str) or not submitted or len(submitted) > 200:
        return False
    if viewer is not None:
        return security.constant_equals(submitted, viewer.csrf)
    nonce = request.cookies.get(PRE_CSRF_COOKIE)
    if not nonce or len(nonce) > 100:
        return False
    return security.constant_equals(submitted, _pre_token(request.app.state.settings, nonce))


# Login rate limiting ------------------------------------------------------------

def client_ip(request: Request) -> str:
    """The limiter's IP key, for the website and the app sign-in alike."""
    return request.client.host if request.client else "unknown"


@dataclass(frozen=True)
class Attempt:
    """An admitted sign-in attempt: one provisional failure, tagged so that ``success`` removes exactly it."""
    keys: tuple[str, ...]
    entry: tuple[float, int]


DEVICE_FAILURES = 10  # §18.1 item 2: failures per window after which a recognised device is ignored


class LoginLimiter:
    """In-process sliding windows of failed sign-ins.

    Limits: per (email, IP) pair, per IP, and a much looser global ceiling per email.
    A single attacker IP therefore can't lock a user out; only a broad distributed
    attack against one email reaches the per-email ceiling (review LOW-3).

    ``attempt`` checks every limit and records a provisional failure in one locked
    step, so concurrent requests are admitted no further than the limits (§18.1 item 1).
    A correct password calls ``success``, which removes exactly that entry and clears
    the pair. A recognised device (``device``, its id) passes the per-email ceiling
    only, and only while it has fewer than ``DEVICE_FAILURES`` failures in the window;
    the pair and IP limits always apply.
    """

    def __init__(self, per_pair: int = 6, per_ip: int = 30, per_email: int = 50, window: float = 15 * 60,
                 per_device: int = DEVICE_FAILURES) -> None:
        self.per_pair, self.per_ip, self.per_email, self.window = per_pair, per_ip, per_email, window
        self.per_device = per_device
        self._hits: dict[str, deque[tuple[float, int]]] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count()

    def _count(self, key: str, now: float) -> int:
        q = self._hits.get(key)
        if not q:
            return 0
        while q and q[0][0] <= now - self.window:
            q.popleft()
        if not q:
            del self._hits[key]
            return 0
        return len(q)

    @staticmethod
    def _keys(ip: str, email: str) -> tuple[str, str, str]:
        email = email.strip().lower()[:254]
        return f"pair:{ip}|{email}", f"ip:{ip}", f"email:{email}"

    def _exempt(self, device: str | None, now: float) -> bool:
        return device is not None and self._count(f"device:{device}", now) < self.per_device

    def _full(self, keys: tuple[str, str, str], now: float, device: str | None) -> bool:
        pair, ip_key, email_key = keys
        return (self._count(pair, now) >= self.per_pair or self._count(ip_key, now) >= self.per_ip
                or (not self._exempt(device, now) and self._count(email_key, now) >= self.per_email))

    def _record(self, keys: tuple[str, ...], now: float) -> tuple[float, int]:
        entry = (now, next(self._ids))
        for key in keys:
            self._count(key, now)
            self._hits.setdefault(key, deque()).append(entry)
        if len(self._hits) > 50_000:  # bound memory under a spray of distinct keys
            for key in list(self._hits):
                self._count(key, now)  # drops keys whose window has expired
        return entry

    def attempt(self, ip: str, email: str, device: str | None = None) -> Attempt | None:
        """None when a limit is reached; otherwise the admitted attempt, already counted as a failure."""
        keys, now = self._keys(ip, email), time.monotonic()
        with self._lock:
            if self._full(keys, now, device):
                return None
            recorded = keys + ((f"device:{device}",) if device is not None else ())
            return Attempt(recorded, self._record(recorded, now))

    def success(self, attempt: Attempt) -> None:
        """The password was right: remove exactly this attempt's entry everywhere, and clear the pair."""
        with self._lock:
            for key in attempt.keys:
                q = self._hits.get(key)
                if q is not None:
                    try:
                        q.remove(attempt.entry)
                    except ValueError:  # already outside the window
                        pass
                    if not q:
                        del self._hits[key]
            self._hits.pop(attempt.keys[0], None)

    def blocked(self, ip: str, email: str, device: str | None = None) -> bool:
        """Whether ``attempt`` would refuse now (for tests and tools; sign-ins never check, then act)."""
        with self._lock:
            return self._full(self._keys(ip, email), time.monotonic(), device)

    def failure(self, ip: str, email: str) -> None:
        """Record a failure outright (tests and tools; sign-ins use ``attempt``)."""
        with self._lock:
            self._record(self._keys(ip, email), time.monotonic())


# Recognised devices (§18.1 item 2) ------------------------------------------------
# A signed token for a browser or app where the user signed in successfully. It only lets that device's
# sign-ins pass the per-email ceiling: it never authenticates, never skips the password and is not a session.

KNOWN_DEVICE_COOKIE = "raincli_device"
KNOWN_DEVICE_TTL = timedelta(days=30)
KNOWN_DEVICE_RE = re.compile(r"rkd1_([0-9a-f]{32})\.([0-9a-f]{32})\.([0-9]{1,12})\.([0-9]{1,9})\.([0-9a-f]{64})")
KNOWN_DEVICE_PATHS = ("/login", "/app/account/password")  # the only routes the cookie is sent to


def _known_device_key(settings: Settings) -> bytes:
    """A key used only for these tokens, derived from the server secret."""
    return hmac.new(settings.secret_key.encode(), b"raincli-known-device-key-v1", hashlib.sha256).digest()


def _known_device_sig(settings: Settings, user_id: str, device: str, issued: int, epoch: int) -> str:
    message = f"raincli-known-device|{user_id}|{device}|{issued}|{epoch}".encode()
    return hmac.new(_known_device_key(settings), message, hashlib.sha256).hexdigest()


def issue_known_device(settings: Settings, user: User) -> str:
    """A new token for ``user`` under their current credential epoch, valid for 30 days."""
    device, issued = secrets.token_hex(16), int(time.time())
    epoch = user.credential_epoch or 0
    return f"rkd1_{user.id.hex}.{device}.{issued}.{epoch}.{_known_device_sig(settings, user.id.hex, device, issued, epoch)}"


def known_device_id(settings: Settings, token: object, user: User | None) -> str | None:
    """The device id of a valid token for this user, or None: wrong user, expired, from an older credential
    epoch, malformed or not signed by this server."""
    if user is None or not user.is_active or not isinstance(token, str) or len(token) > 200:
        return None
    match = KNOWN_DEVICE_RE.fullmatch(token)
    if match is None:
        return None
    user_id, device, issued, epoch, sig = match.groups()
    if user_id != user.id.hex or int(epoch) != (user.credential_epoch or 0):
        return None
    now = time.time()
    if not now - KNOWN_DEVICE_TTL.total_seconds() < int(issued) <= now + 60:
        return None
    if not hmac.compare_digest(sig, _known_device_sig(settings, user_id, device, int(issued), int(epoch))):
        return None
    return device


def known_device_for(db: Session, settings: Settings, token: object, email: str) -> str | None:
    """The device id when ``token`` is a valid token of the account ``email`` names (no password check runs)."""
    if not isinstance(token, str) or not token or not email:
        return None
    return known_device_id(settings, token, identity.find_user_by_email(db, email))


def set_known_device(response: Response, settings: Settings, user: User) -> None:
    """The website cookie: HttpOnly, Secure, SameSite=Strict, sent only to the sign-in and password routes."""
    token = issue_known_device(settings, user)
    for path in KNOWN_DEVICE_PATHS:
        response.set_cookie(KNOWN_DEVICE_COOKIE, token, max_age=int(KNOWN_DEVICE_TTL.total_seconds()),
                            path=(settings.root_path or "") + path, secure=settings.cookie_secure,
                            httponly=True, samesite="strict")
