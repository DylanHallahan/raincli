"""The Windows app e2e's job token (scripts/windows-app-e2e.py): sent only on https requests to exactly
api.github.com, never to codeload or any redirect target, only inside github_api_auth(), and never inherited by
anything the script launches. No network: a local HTTP server plays GitHub, and a test-only HTTPS handler sends
every https:// host to it (the request keeps its own Host header), so no DNS is involved."""

from __future__ import annotations

import http.client
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "ghs_test-only-token-0123456789"


@pytest.fixture(scope="module")
def e2e():
    spec = importlib.util.spec_from_file_location("app_e2e_under_test", ROOT / "scripts" / "windows-app-e2e.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def github(monkeypatch):
    """A fake for every https host. Records (host, path, Authorization) per request."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append((self.headers.get("Host"), self.path, self.headers.get("Authorization")))
            if self.path == "/to-codeload":
                self.send_response(302)
                self.send_header("Location", "https://codeload.github.com/landed")
            elif self.path == "/to-other":
                self.send_response(302)
                self.send_header("Location", "https://example.com/landed")
            else:
                body = json.dumps({"ok": True, "resources": {"core": {"remaining": 4999, "limit": 5000,
                                                                       "reset": 1760000000}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    class LocalHTTPS(urllib.request.HTTPSHandler):
        """Test-only: an https:// request of any host goes to the local server over plain HTTP."""

        def https_open(self, req):
            return self.do_open(lambda host, **kw: http.client.HTTPConnection("127.0.0.1", port, timeout=10), req)

    for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(urllib.request, "HTTPSHandler", LocalHTTPS)  # build_opener's default list reads it
    yield seen
    server.shutdown()
    server.server_close()


@pytest.fixture
def plain_updater(e2e, monkeypatch):
    """The updater's own opener (its redirect allowlist), built over the local fake."""
    monkeypatch.setattr(e2e.updates, "_OPENER", urllib.request.build_opener(e2e.updates._Redirects))
    return e2e.updates._OPENER


@pytest.fixture
def token(e2e, monkeypatch):
    monkeypatch.setattr(e2e, "_GITHUB_TOKEN", TOKEN)
    return TOKEN


def test_the_token_reaches_an_api_github_com_request(e2e, github, plain_updater, token):
    with e2e.github_api_auth() as authenticated:
        assert authenticated is True
        e2e.updates.fetch("https://api.github.com/repos/x/releases/tags/v0.3.2", 1024)
    assert github == [("api.github.com", "/repos/x/releases/tags/v0.3.2", "Bearer " + TOKEN)]


def test_a_redirect_to_codeload_carries_no_token(e2e, github, plain_updater, token):
    with e2e.github_api_auth():
        e2e.updates.fetch("https://api.github.com/to-codeload", 1024)
    assert github == [("api.github.com", "/to-codeload", "Bearer " + TOKEN),
                      ("codeload.github.com", "/landed", None)]


def test_a_redirect_to_another_host_carries_no_token(e2e, github, token):
    # The updater refuses that redirect outright (its allowlist); with a plain redirect handler, the request
    # that is followed still arrives without the token.
    opener = urllib.request.build_opener(e2e.GitHubApiAuth(TOKEN))
    opener.open("https://api.github.com/to-other", timeout=10).read()
    assert github == [("api.github.com", "/to-other", "Bearer " + TOKEN), ("example.com", "/landed", None)]
    with e2e.github_api_auth(), pytest.raises(e2e.updates.ConfigError):
        e2e.updates.fetch("https://api.github.com/to-other", 1024)
    assert github[-1] == ("api.github.com", "/to-other", "Bearer " + TOKEN) and len(github) == 3  # never followed


def test_an_authorization_header_on_any_other_host_is_stripped(e2e, github, token):
    opener = urllib.request.build_opener(e2e.GitHubApiAuth(TOKEN))
    for url in ("https://codeload.github.com/x", "https://example.com/x", "https://api.github.com.evil.test/x",
                "https://api.github.com:8443/x", "http://api.github.com/x"):
        request = urllib.request.Request(url, headers={"Authorization": "Bearer leaked"})
        if url.startswith("http://"):
            # Plain http: the processor still strips it (checked without a connection).
            assert e2e.GitHubApiAuth(TOKEN).http_request(request).get_header("Authorization") is None
            continue
        opener.open(request, timeout=10).read()
    assert [a for _, _, a in github] == [None] * 4


def test_nothing_is_sent_outside_the_context_manager(e2e, github, plain_updater, token):
    e2e.updates.fetch("https://api.github.com/before", 1024)
    with e2e.github_api_auth():
        pass
    e2e.updates.fetch("https://api.github.com/after", 1024)
    assert [a for _, _, a in github] == [None, None]


def test_the_opener_is_restored_exactly(e2e, github, plain_updater, token):
    with e2e.github_api_auth():
        assert e2e.updates._OPENER is not plain_updater
    assert e2e.updates._OPENER is plain_updater
    with pytest.raises(RuntimeError), e2e.github_api_auth():
        raise RuntimeError("a failed lookup")
    assert e2e.updates._OPENER is plain_updater


def test_without_a_token_nothing_changes(e2e, github, plain_updater, monkeypatch):
    monkeypatch.setattr(e2e, "_GITHUB_TOKEN", None)
    with e2e.github_api_auth() as authenticated:
        assert authenticated is False and e2e.updates._OPENER is plain_updater
        e2e.updates.fetch("https://api.github.com/x", 1024)
    assert github[-1][2] is None


def test_output_says_only_authenticated_and_the_rate_limit(e2e, github, plain_updater, token, capsys):
    with e2e.github_api_auth():
        e2e.report_rate_limit()
    assert e2e.github_lookup("release v0.3.2", lambda: e2e.updates.fetch("https://api.github.com/r", 1024))
    out = capsys.readouterr().out
    assert "rate_limit: core.remaining=4999 core.limit=5000 reset=2025-10-09T08:53:20Z" in out
    assert "GitHub API lookup (release v0.3.2): authenticated" in out
    assert TOKEN not in out and "Bearer" not in out and "Authorization" not in out


def test_the_variable_leaves_the_environment_at_startup(e2e, monkeypatch):
    monkeypatch.setenv("RAINCLI_E2E_GITHUB_TOKEN", TOKEN)
    monkeypatch.setattr(e2e, "_GITHUB_TOKEN", None)
    with pytest.raises(BaseException):  # off a GitHub-hosted Windows runner, main() refuses to run
        e2e.main(["--installers", "nowhere"])
    assert "RAINCLI_E2E_GITHUB_TOKEN" not in os.environ and e2e._GITHUB_TOKEN == TOKEN
    child = subprocess.run([sys.executable, "-c", "import os; print(os.environ.get('RAINCLI_E2E_GITHUB_TOKEN'))"],
                           env=dict(os.environ), capture_output=True, text=True, check=True)
    assert child.stdout.strip() == "None"  # what the script launches (it passes dict(os.environ)) never has it
