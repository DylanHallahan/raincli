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
    """Where a requested URL may go: ``("allow", url)``, ``("local", page)`` or ``("external", url)``.

    The local origin includes the app's per-run path secret (§18.2 item 2): ``local_url`` is
    ``http://127.0.0.1:<port>/<secret>/``, and only URLs under that path count as local."""

    def __init__(self, service_url, local_url):
        self.service = origin(service_url)
        self.local = origin(local_url)
        if self.service is None or self.local is None:
            raise ValueError("the service and local URLs must be http(s) URLs")
        if self.service[0] != "https" and self.service[1] not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the service URL must be https unless it is a loopback address")
        self._local_secret = urlsplit(local_url).path.strip("/")

    def decide(self, url):
        where = origin(url)
        if where is not None and where == self.local:
            return ("allow", url) if self.is_local(url) else ("block", url)  # the right port, not our pages
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
        """Exactly the local origin and, when the app has one, under its path secret (compared in constant
        time)."""
        if origin(url) != self.local:
            return False
        if not self._local_secret:
            return True
        try:
            first = urlsplit(url).path.lstrip("/").split("/", 1)[0]
        except ValueError:
            return False
        return hmac.compare_digest(first.encode(), self._local_secret.encode()) and \
            urlsplit(url).path.startswith(f"/{self._local_secret}/")


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


# Links leaving the app (§18.2 item 3) ------------------------------------------------------------

EXTERNAL_MAX = 2048
MAILTO_PARAMS = ("subject", "body", "cc", "bcc")
_UNSAFE = re.compile(r"[\x00-\x20\x7f-\x9f\u2028\u2029\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def external_url(url):
    """The URL to hand to the operating system, re-serialized, or None to drop it.

    Only ``https:`` with a host (no user information, whitespace or control characters, at most 2048
    characters) and ``mailto:`` (only ``subject``, ``body``, ``cc`` and ``bcc`` kept) ever leave the app."""
    if not isinstance(url, str) or not url or len(url) > EXTERNAL_MAX or _UNSAFE.search(url):
        return None
    scheme = url.split(":", 1)[0].lower()
    if scheme == "https":
        return _https(url)
    if scheme == "mailto":
        return _mailto(url)
    return None


def _https(url):
    from urllib.parse import urlunsplit
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme != "https" or not host or parts.username is not None or parts.password is not None \
            or "@" in parts.netloc or not url.lower().startswith("https://"):
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != 443:
        netloc += f":{port}"
    return urlunsplit(("https", netloc, parts.path or "/", parts.query, parts.fragment))


def _mailto(url):
    from urllib.parse import parse_qsl, quote, unquote, urlencode
    rest = url.split(":", 1)[1]
    to, _, query = rest.partition("?")
    to = to.split("#", 1)[0]
    try:
        addresses = [unquote(a).strip() for a in to.split(",") if a.strip()]
        params = parse_qsl(query.split("#", 1)[0], keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return None
    if any(_UNSAFE.search(a) or a.count("@") != 1 or len(a) > 254 for a in addresses):
        return None
    kept = [(k.lower(), v) for k, v in params if k.lower() in MAILTO_PARAMS]
    head = ",".join(quote(a, safe="@") for a in addresses)
    return "mailto:" + head + (("?" + urlencode(kept, quote_via=quote)) if kept else "")


INBOX_OFFLINE_TOAST = "Your inbox has been offline for more than an hour. Open This computer to check it."


def toast_wanted(entry, every=False):
    """§21 PR6: whether a notification-queue entry (``person.take_notifications``) earns a toast.

    Only what needs the person: a message a teammate wrote by hand (``from_kind`` ``app``), one held for
    their approval, one waiting because they have no inbox, an escalation, and their inbox offline for
    more than an hour (``inbox_offline``). Agent-sent messages their inbox received stay quiet unless
    ``every`` ("Notify me about every message", off by default). An entry without ``from_kind`` (a server
    or runtime before v0.6) is toasted as before."""
    entry = entry if isinstance(entry, dict) else {}
    kind = entry.get("kind")
    if kind in ("inbox_offline", "escalation") or every:
        return True
    from_kind = entry.get("from_kind")
    return from_kind not in ("app", "agent") or from_kind == "app" or entry.get("hold") == "approval" \
        or entry.get("waiting") is True


def toast_text(kind, sender, hold=None):
    """C14: "New message from <name>", "Escalation from <name>" or "<name> is waiting for your approval".
    Never body text."""
    name = " ".join(str(sender or "a teammate").split())[:64] or "a teammate"
    if kind == "escalation":
        return f"Escalation from {name}"
    if hold == "approval":
        return f"A message from {name} is waiting for your approval"
    return f"New message from {name}"


def toast_sender(message):
    """The person label for a toast (C5.1, INV §1.7): the sender's display name, else their email. Never a
    machine handle or an agent name: a machine-form endpoint (a server before v0.6) gives its owner's name
    when the server sent one, else "a teammate"."""
    message = message if isinstance(message, dict) else {}
    ep = message.get("from_endpoint") if isinstance(message.get("from_endpoint"), dict) else {}
    if isinstance(ep.get("person"), str) and ep["person"]:
        name = ep.get("display_name")
        return name if isinstance(name, str) and name.strip() else ep["person"]
    owner = message.get("from_owner")
    if isinstance(owner, dict):
        owner = owner.get("display_name") or owner.get("email")
    if isinstance(owner, str) and owner.strip():
        return owner
    sender = message.get("from")
    if isinstance(sender, str) and sender.startswith("@") and "/" not in sender:
        return sender[1:]
    return "a teammate"
