"""Security headers and a request-size cap for every web (non-API) response."""

from __future__ import annotations

import re

CSP = "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
WEB_BODY_LIMIT = 64 * 1024
UPLOAD_BODY_LIMIT = 2 * 1024 * 1024  # multipart sends with Markdown attachments (protocol §8)
STATIC_CACHE = b"public, max-age=31536000, immutable"
HEALTH_PATH = "/api/v1/health"
# Browsers that still hold the pre-cutover site get one Clear-Site-Data: "cache" on their first
# HTML page, marked by this cookie so the purge never repeats (immutable static caching keeps working).
CACHE_VERSION_COOKIE = "raincli_cv"
CACHE_VERSION_MAX_AGE = 365 * 24 * 3600
UPLOAD_PATH_RE = re.compile(r"^/app/conversations/[^/]+/send$")  # mirrors the Nginx location

_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"x-content-type-options", b"nosniff"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), interest-cohort=()"),
]


class _TooLarge(Exception):
    pass


class WebSecurityMiddleware:
    """Pure ASGI (no buffering), so it never interferes with API long-polls, which it skips."""

    def __init__(self, app, root_path: str = "", cookie_secure: bool = True) -> None:
        self.app = app
        self.root_path = root_path
        self.cookie_secure = cookie_secure

    def _route_path(self, scope) -> str:
        path = scope.get("path", "")
        root = self.root_path or scope.get("root_path", "")
        if root and (path == root or path.startswith(root + "/")):
            path = path[len(root):] or "/"
        return path

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = self._route_path(scope)
        if path == HEALTH_PATH:
            return await self.app(scope, receive, _with_default_no_store(send))
        if path == "/api" or path.startswith("/api/"):
            return await self.app(scope, receive, _with_default_no_store(send))
        request_headers = scope.get("headers") or []
        # One-time purge of any pre-cutover browser cache (see CACHE_VERSION_COOKIE).
        purge = scope.get("method") == "GET" and not _has_cookie(request_headers, CACHE_VERSION_COOKIE, b"1")
        static = path.startswith("/static/")
        # Invitation and app handoff URLs carry a secret: never send them as a referrer and never cache them.
        invite = path.startswith("/invite/") or path == "/app/handoff"
        limit = UPLOAD_BODY_LIMIT if UPLOAD_PATH_RE.match(path) else WEB_BODY_LIMIT

        if scope.get("method") in ("POST", "PUT", "PATCH", "DELETE"):
            length = dict(scope.get("headers") or []).get(b"content-length")
            try:
                too_big = length is not None and int(length) > limit
            except ValueError:
                too_big = True
            if too_big:
                return await _plain(send, 413, b"Request too large")

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLarge()
            return message

        started = False

        async def send_with_headers(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                own = message.get("headers", [])
                # A response may set a stricter CSP of its own (attachment downloads use sandbox).
                keep_csp = any(k.lower() == b"content-security-policy" for k, _ in own)
                headers = [(k, v) for k, v in own if k.lower() not in dict(_HEADERS) or
                           (keep_csp and k.lower() == b"content-security-policy")]
                headers.extend(h for h in _HEADERS if not (keep_csp and h[0] == b"content-security-policy"))
                if invite:
                    headers = [(k, v) for k, v in headers if k.lower() not in (b"referrer-policy", b"cache-control")]
                    headers += [(b"referrer-policy", b"no-referrer"), (b"cache-control", b"no-store")]
                is_html = any(k.lower() == b"content-type" and v.lower().startswith(b"text/html") for k, v in headers)
                if purge and is_html:
                    headers.append((b"clear-site-data", b'"cache"'))
                    headers.append((b"set-cookie", self._cache_version_cookie()))
                if not any(k.lower() == b"cache-control" for k, _ in headers):
                    # Static URLs carry a content hash (?v=...), so a successful response never changes.
                    cacheable = static and message.get("status") in (200, 304)
                    headers.append((b"cache-control", STATIC_CACHE if cacheable else b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except _TooLarge:
            if not started:
                await _plain(send_with_headers, 413, b"Request too large")


    def _cache_version_cookie(self) -> bytes:
        parts = [f"{CACHE_VERSION_COOKIE}=1", f"Max-Age={CACHE_VERSION_MAX_AGE}", f"Path={self.root_path or '/'}",
                 "HttpOnly", "SameSite=Lax"]
        if self.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts).encode()


def _has_cookie(headers, name: str, value: bytes) -> bool:
    for key, raw in headers:
        if key.lower() != b"cookie":
            continue
        for part in raw.split(b";"):
            k, _, v = part.strip().partition(b"=")
            if k == name.encode() and v == value:
                return True
    return False


def _with_default_no_store(send):
    async def wrapped(message):
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            if not any(k.lower() == b"cache-control" for k, _ in headers):
                headers.append((b"cache-control", b"no-store"))
            message = {**message, "headers": headers}
        await send(message)
    return wrapped


async def _plain(send, status: int, body: bytes) -> None:
    await send({
        "type": "http.response.start", "status": status,
        "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]
        + _HEADERS + [(b"cache-control", b"no-store")],
    })
    await send({"type": "http.response.body", "body": body})
