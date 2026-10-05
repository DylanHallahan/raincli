"""Pure rules for the app window (protocol §16.10, §16.12 C4, C14). No GUI imports, so they are tested
everywhere.

- **Origins.** The window shows only the configured service origin and the bundled local origin,
  compared exactly (scheme, host and port). Anything else opens in the default browser.
- **``/app/local/<page>``** on the service origin is a sentinel: the window shows the bundled page.
- **The js_api nonce.** Every ``loaded`` event makes a new random nonce. It is passed to the page only
  when the loaded URL's origin is exactly the local origin, and every js_api call must carry it.
- **Toasts** name the sender only: never body text.
"""
from __future__ import annotations

import hmac
import re
import secrets
import threading
from urllib.parse import urlsplit

LOCAL_PAGES = ("sign-in", "this-computer", "settings", "offline")
_LOCAL_SENTINEL = re.compile(r"^/app/local/([a-z][a-z0-9-]{0,31})/?$")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def origin(url):
    """``(scheme, host, port)`` with the default port made explicit, or None for anything unusable."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname or parts.username or parts.password:
        return None
    return scheme, parts.hostname.lower(), port if port is not None else _DEFAULT_PORTS[scheme]


def path_of(url):
    try:
        return urlsplit(url).path or "/"
    except ValueError:
        return ""


def same_origin(url, other):
    a, b = origin(url), origin(other)
    return a is not None and a == b


class Navigation:
    """Where a requested URL may go: ``("allow", url)``, ``("local", page)`` or ``("external", url)``."""

    def __init__(self, service_url, local_url):
        self.service = origin(service_url)
        self.local = origin(local_url)
        if self.service is None or self.local is None:
            raise ValueError("the service and local URLs must be http(s) URLs")
        if self.service[0] != "https" and self.service[1] not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the service URL must be https unless it is a loopback address")

    def decide(self, url):
        where = origin(url)
        if where is not None and where == self.local:
            return "allow", url
        if where is not None and where == self.service:
            path = urlsplit(url).path
            match = _LOCAL_SENTINEL.match(path)
            if match:
                page = match.group(1)
                return ("local", page) if page in LOCAL_PAGES else ("local", "this-computer")
            return "allow", url
        if isinstance(url, str) and url.split(":", 1)[0].lower() in ("about", "data", "blob", "javascript", "file"):
            return "block", url  # never shown, never handed to the browser
        return "external", url

    def is_local(self, url):
        return origin(url) == self.local


class NonceGate:
    """§16.12 C4: one nonce per load, handed only to the local origin; calls must present it."""

    def __init__(self, navigation, token=secrets.token_urlsafe):
        self.navigation = navigation
        self._token = token
        self._lock = threading.Lock()
        self._current = None

    def on_loaded(self, url):
        """A new nonce for this load. Returns it when the page may have it (local origin), else None."""
        with self._lock:
            self._current = self._token(32)
            return self._current if self.navigation.is_local(url) else None

    def check(self, nonce, current_url):
        """The call is accepted only from the local origin with the current nonce."""
        with self._lock:
            current = self._current
        return (isinstance(nonce, str) and current is not None and self.navigation.is_local(current_url)
                and hmac.compare_digest(nonce.encode(), current.encode()))


def toast_text(kind, sender):
    """C14: "New message from <display name>" or "Escalation from <machine>". Never body text."""
    name = " ".join(str(sender or "a teammate").split())[:64] or "a teammate"
    return f"Escalation from {name}" if kind == "escalation" else f"New message from {name}"


def toast_sender(message):
    """The display name for a toast, from a person-API message: a person's display name, else the machine."""
    ep = (message or {}).get("from_endpoint") or {}
    if "person" in ep:
        return ep.get("display_name") or ep["person"]
    return ep.get("machine") or (message or {}).get("from") or "a teammate"
