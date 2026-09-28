"""HTTP client for the RainCLI agent API (protocol sections 3 and 4).

- Redirects are never followed: a 3xx is a ``RedirectRefused`` error, so the
  bearer token is only ever sent to the configured origin.
- ``https://`` is required unless the host is loopback (checked in config).
- Every request has a timeout. Connection errors, timeouts, 502/503/504 and
  429 ``rate_limited`` are retried with jittered exponential backoff. Sends
  reuse their message id and acks are idempotent, so retries never duplicate.
"""

import http.client
import json
import random
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import __version__
from .config import AgentConfig, Secret, is_loopback_host, validate_api_url, validate_token
from .errors import (ApiError, BadResponse, RateLimited, RedirectRefused, Unreachable,
                     UsageError, error_for)
from .text import escape_text

DEFAULT_TIMEOUT = 30.0
MAX_WAIT = 25
MAX_ATTEMPTS = 5
BACKOFF_BASE = 0.5
BACKOFF_CAP = 8.0
RETRY_AFTER_CAP = 60.0
RETRY_STATUSES = (502, 503, 504)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn every 3xx into an HTTPError instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener(base_url):
    host = urllib.parse.urlsplit(base_url).hostname
    handlers = [_NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())]
    if is_loopback_host(host):
        # Never hand a loopback request (possibly plain http) to a proxy.
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    return opener


def _parse_retry_after(value):
    if not value:
        return None
    try:
        return max(0.0, min(float(value), RETRY_AFTER_CAP))
    except ValueError:
        return None


class ApiClient:
    def __init__(self, api_url, token, *, timeout=DEFAULT_TIMEOUT, max_attempts=MAX_ATTEMPTS,
                 sleep=time.sleep, rng=None):
        self.api_url = validate_api_url(api_url)
        self._token = token if isinstance(token, Secret) else validate_token(token)
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._opener = _build_opener(self.api_url)

    @classmethod
    def from_config(cls, config: AgentConfig, **kwargs):
        return cls(config.api_url, config.token, **kwargs)

    def __repr__(self):
        return f"ApiClient(api_url={self.api_url!r})"

    # -- transport -------------------------------------------------------

    def _url(self, path, query=None):
        url = self.api_url + "/api/v1" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def _redact(self, text):
        text = escape_text(str(text), allow_newlines=False)
        return text.replace(self._token.reveal(), "***")[:500]

    def _once(self, method, url, data, auth, timeout, raw=False):
        headers = {"Accept": "application/json", "User-Agent": f"raincli-agent/{__version__}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = "Bearer " + self._token.reveal()
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                status, body = resp.status, resp.read()
                retry_after, resp_headers = None, resp.headers
        except urllib.error.HTTPError as exc:
            status, retry_after = exc.code, _parse_retry_after(exc.headers.get("Retry-After"))
            try:
                body = exc.read()
            except (OSError, http.client.HTTPException):
                body = b""
            finally:
                exc.close()
        if 300 <= status < 400:
            raise RedirectRefused(f"server answered {status}; redirects are never followed",
                                  status=status)
        payload = None
        if status >= 400 or not raw:
            try:
                payload = json.loads(body) if body else {}
            except (ValueError, UnicodeDecodeError):
                payload = None
        if status >= 400:
            code, message = None, f"HTTP {status}"
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                code = payload["error"].get("code")
                message = payload["error"].get("message") or message
                code = self._redact(code) if isinstance(code, str) else None
            raise error_for(status, code, self._redact(message), retry_after=retry_after)
        if raw:
            return status, body, resp_headers
        if not isinstance(payload, dict):
            raise BadResponse("server returned a non-JSON response", status=status)
        return status, payload

    def request(self, method, path, *, body=None, query=None, auth=True, timeout=None, raw=False):
        url = self._url(path, query)
        data = None if body is None else json.dumps(body).encode()
        timeout = self.timeout if timeout is None else timeout
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._once(method, url, data, auth, timeout, raw)
            except ApiError as exc:
                retriable = (exc.status in RETRY_STATUSES
                             or (isinstance(exc, RateLimited) and exc.code == "rate_limited"))
                if not retriable or attempt >= self.max_attempts:
                    raise
                delay = exc.retry_after if exc.retry_after is not None else self._backoff(attempt)
            except (urllib.error.URLError, http.client.HTTPException, socket.timeout,
                    TimeoutError, ConnectionError, OSError) as exc:
                if isinstance(getattr(exc, "reason", exc), ssl.SSLCertVerificationError):
                    raise Unreachable(f"TLS certificate verification failed for {self.api_url}") from None
                if attempt >= self.max_attempts:
                    reason = getattr(exc, "reason", None) or exc.__class__.__name__
                    raise Unreachable(f"cannot reach {self.api_url}: {self._redact(reason)}") from None
                delay = self._backoff(attempt)
            self._sleep(delay)

    def _backoff(self, attempt):
        return self._rng.uniform(0, min(BACKOFF_CAP, BACKOFF_BASE * (2 ** (attempt - 1))))

    # -- endpoints -------------------------------------------------------

    def health(self):
        return self.request("GET", "/health", auth=False)[1]

    def me(self):
        return self.request("GET", "/me")[1]

    def agents(self):
        return self.request("GET", "/agents")[1]["agents"]

    def send(self, to, body, *, message_id=None, conversation_id=None, in_reply_to=None,
             attachments=None):
        """Send a message. Returns ``(message, created)``.

        The id is fixed before the first attempt, so every retry is the same
        idempotent request.
        """
        payload = {"id": str(message_id or uuid.uuid4()), "to": to, "body": body}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if in_reply_to:
            payload["in_reply_to"] = in_reply_to
        if attachments:
            payload["attachments"] = attachments  # from attachments.load_for_send
        _, data = self.request("POST", "/messages", body=payload)
        return data["message"], bool(data.get("created"))

    def inbox(self, *, after=0, limit=100, wait=0, include_acked=False, timeout=None):
        wait = max(0, min(int(wait), MAX_WAIT))
        query = {"after": int(after), "limit": int(limit), "wait": wait,
                 "include_acked": "true" if include_acked else "false"}
        if timeout is None:
            timeout = max(self.timeout, wait + 15)
        data = self.request("GET", "/inbox", query=query, timeout=timeout)[1]
        return data["messages"], data.get("cursor", after)

    def get_message(self, message_id):
        return self.request("GET", f"/messages/{_path_id(message_id)}")[1]["message"]

    def ack(self, message_id):
        data = self.request("POST", f"/messages/{_path_id(message_id)}/ack", body={})[1]
        return data["message"], bool(data.get("acked"))

    def event(self, message_id, state, detail=""):
        body = {"state": state, "detail": (detail or "")[:500]}
        return self.request("POST", f"/messages/{_path_id(message_id)}/events", body=body)[1]["message"]

    def download_attachment(self, message_id, attachment_id):
        """Return ``(bytes, sha256 header or None)``. The caller verifies them."""
        _, body, headers = self.request(
            "GET", f"/messages/{_path_id(message_id)}/attachments/{_path_id(attachment_id)}", raw=True)
        return body, headers.get("X-RainCLI-SHA256")

    def conversations(self, limit=50):
        return self.request("GET", "/conversations", query={"limit": int(limit)})[1]["conversations"]

    def conversation_messages(self, conversation_id, *, after=0, limit=100):
        data = self.request("GET", f"/conversations/{_path_id(conversation_id)}/messages",
                            query={"after": int(after), "limit": int(limit)})[1]
        return data["messages"], data.get("cursor", after)


def _path_id(value):
    """Ids go into URL paths, so they must be UUIDs."""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise UsageError(f"not a valid id: {escape_text(str(value), allow_newlines=False)}") from None
