"""Browser sessions, CSRF tokens and login rate limiting (protocol §6).

Browser auth is separate from agent credentials: a random ``raincli_session``
cookie whose sha256 is stored in ``web_sessions`` together with a per-session
CSRF token. Forms shown before login (sign-in, invitation) use a signed
double-submit token bound to a random ``raincli_csrf`` cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta

from fastapi import Request
from fastapi.responses import Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from raincli_server import identity, security
from raincli_server.config import Settings
from raincli_server.models import Team, User, WebSession

SESSION_COOKIE = "raincli_session"
PRE_CSRF_COOKIE = "raincli_csrf"
SESSION_TTL = timedelta(days=14)
PRE_CSRF_TTL = 60 * 60 * 12


@dataclass
class Viewer:
    user: User
    web_session: WebSession
    teams: list[tuple[Team, str]] = field(default_factory=list)

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


def load_viewer(db: Session, request: Request) -> Viewer | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > 200:
        return None
    row = db.execute(
        select(WebSession, User).join(User, User.id == WebSession.user_id)
        .where(WebSession.token_hash == security.hash_token(token))
    ).first()
    if row is None:
        return None
    ws, user = row
    if ws.revoked_at is not None or ws.expires_at <= identity.now() or not user.is_active:
        return None
    return Viewer(user=user, web_session=ws, teams=identity.teams_for_user(db, user))


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

class LoginLimiter:
    """In-process sliding windows of failed sign-ins.

    Limits: per (email, IP) pair, per IP, and a much looser global ceiling per email.
    A single attacker IP therefore can't lock a user out; only a broad distributed
    attack against one email reaches the per-email ceiling (review LOW-3).
    """

    def __init__(self, per_pair: int = 6, per_ip: int = 30, per_email: int = 50, window: float = 15 * 60) -> None:
        self.per_pair, self.per_ip, self.per_email, self.window = per_pair, per_ip, per_email, window
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _count(self, key: str, now: float) -> int:
        q = self._hits.get(key)
        if not q:
            return 0
        while q and q[0] <= now - self.window:
            q.popleft()
        if not q:
            del self._hits[key]
            return 0
        return len(q)

    @staticmethod
    def _keys(ip: str, email: str) -> tuple[str, str, str]:
        email = email.strip().lower()[:254]
        return f"pair:{ip}|{email}", f"ip:{ip}", f"email:{email}"

    def blocked(self, ip: str, email: str) -> bool:
        pair, ip_key, email_key = self._keys(ip, email)
        now = time.monotonic()
        with self._lock:
            return (
                self._count(pair, now) >= self.per_pair
                or self._count(ip_key, now) >= self.per_ip
                or self._count(email_key, now) >= self.per_email
            )

    def failure(self, ip: str, email: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in self._keys(ip, email):
                self._count(key, now)
                self._hits.setdefault(key, deque()).append(now)
            if len(self._hits) > 50_000:  # bound memory under a spray of distinct keys
                for key in list(self._hits):
                    self._count(key, now)  # drops keys whose window has expired

    def success(self, ip: str, email: str) -> None:
        with self._lock:
            self._hits.pop(self._keys(ip, email)[0], None)
