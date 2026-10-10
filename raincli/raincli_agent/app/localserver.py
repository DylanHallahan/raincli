"""The server for the app's bundled local pages (protocol §18.2 item 2).

- It binds to ``127.0.0.1`` on a port the system picks, before anything reads the port, so there is never
  a moment when another program could take it. On Windows the socket is ``SO_EXCLUSIVEADDRUSE``; it never
  sets ``SO_REUSEADDR``.
- Every page is served under ``/<secret>/``, a random path segment of 192 bits made for this run; anything
  else is 404. The window treats only that path as the local origin, so the js_api nonce goes to these
  pages alone. The secret is never logged: this server logs nothing, and the window removes it from every
  line it writes.
- Only the files in ``app/local`` are served, with ``Referrer-Policy: no-referrer`` and ``no-store``.
"""
from __future__ import annotations

import hmac
import secrets
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8"}


def new_secret():
    return secrets.token_urlsafe(24)  # 192 bits


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = False  # never SO_REUSEADDR
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: no other socket may share this port
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)  # not HTTPServer's, which looks the host name up


class LocalServer:
    """``LocalServer(directory)`` is bound on creation; ``start()`` serves in a daemon thread."""

    def __init__(self, directory, secret=None, host="127.0.0.1"):
        self.directory = Path(directory)
        self.secret = secret or new_secret()
        self._files = {p.name: p for p in self.directory.iterdir() if p.is_file() and p.suffix in TYPES}
        handler = type("LocalHandler", (_Handler,), {"server_files": self._files, "secret": self.secret})
        self._server = _Server((host, 0), handler)
        self.port = self._server.server_address[1]
        self._thread = None

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}/{self.secret}/"

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._server.serve_forever, name="raincli-local-pages",
                                            daemon=True)
            self._thread.start()
        return self

    def close(self):
        if self._thread is not None:
            self._server.shutdown()
        self._server.server_close()


class _Handler(BaseHTTPRequestHandler):
    server_files: dict = {}
    secret = ""
    server_version = "RainCLI"
    sys_version = ""

    def do_GET(self):  # noqa: N802 - http.server's naming
        self._serve(body=True)

    def do_HEAD(self):  # noqa: N802
        self._serve(body=False)

    def _serve(self, body):
        path = urlsplit(self.path).path
        _, first, rest = path.split("/", 2) if path.count("/") >= 2 else ("", "", "")
        target = self.server_files.get(rest) if hmac.compare_digest(first.encode(), self.secret.encode()) else None
        if target is None:
            self.send_response(404)
            self._headers("text/plain; charset=utf-8", 9)
            if body:
                self.wfile.write(b"not found")
            return
        data = target.read_bytes()
        self.send_response(200)
        self._headers(TYPES[target.suffix], len(data))
        if body:
            self.wfile.write(data)

    def _headers(self, content_type, length):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - never log a request line (it carries the secret)
        pass
