"""The app window's local pages and outgoing links (protocol §18.2 items 2 and 3)."""

from __future__ import annotations

import logging
import socket
import types
import urllib.error
import urllib.request

import pytest

from raincli_agent.app import localserver, policy, window
from raincli_agent.app.window import LOCAL_DIR, AppWindow
from .test_app_window import LOCAL, SECRET, FakeServices, FakeWebview, make


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), b""


@pytest.fixture
def server():
    s = localserver.LocalServer(LOCAL_DIR).start()
    yield s
    s.close()


# The local page server (§18.2 item 2) ------------------------------------------------------------

def test_the_server_is_bound_before_its_port_is_read_and_holds_it(server):
    assert server.port and server.base == f"http://127.0.0.1:{server.port}/{server.secret}/"
    other = socket.socket()
    try:
        with pytest.raises(OSError):
            other.bind(("127.0.0.1", server.port))  # the port is already ours, never released and re-bound
    finally:
        other.close()
    assert not server._server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)


def test_pages_are_served_only_under_the_secret(server):
    status, headers, body = fetch(server.base + "sign-in.html?person=1")
    assert status == 200 and b"<html" in body.lower()
    assert headers["Referrer-Policy"] == "no-referrer" and headers["Cache-Control"] == "no-store"
    assert headers["Content-Type"].startswith("text/html")
    origin = f"http://127.0.0.1:{server.port}"
    for path in ("/sign-in.html", "/" + "x" * len(server.secret) + "/sign-in.html", f"/{server.secret}/",
                 f"/{server.secret}/../window.py", f"/{server.secret}/missing.html", f"/{server.secret}x/sign-in.html",
                 f"/{server.secret}/sub/sign-in.html"):
        assert fetch(origin + path)[0] == 404, path


def test_the_secret_is_random_and_long_enough():
    secrets = {localserver.new_secret() for _ in range(20)}
    assert len(secrets) == 20 and all(len(s) >= 32 for s in secrets)  # 192 bits, url-safe


def test_the_server_logs_nothing(server, caplog, capsys):
    with caplog.at_level(logging.DEBUG):
        fetch(server.base + "sign-in.html")
        fetch(f"http://127.0.0.1:{server.port}/nope")
    assert server.secret not in caplog.text and server.secret not in capsys.readouterr().err


def test_the_window_starts_its_own_server_and_pywebview_none(tmp_path):
    webview = FakeWebview()
    app = AppWindow(FakeServices(), profile_dir=tmp_path / "webview", webview=webview)
    try:
        assert app.local_base == app._local_server.base and app._local_server.port
        app.create()
        title, created = webview.created[0]
        assert created["url"] == app.local_base + "sign-in.html"
        app.start(lambda: None)
        assert webview.started[0]["http_server"] is False
        assert fetch(app.local_url("sign-in"))[0] == 200
    finally:
        app.destroy()


def test_a_page_on_the_right_port_without_the_secret_gets_no_nonce_and_no_api(tmp_path):
    app, win, _ = make(tmp_path)
    wrong = "http://127.0.0.1:43123/sign-in.html"
    assert app.navigation.decide(wrong)[0] == "block"
    win.url = wrong
    app.on_loaded()
    assert win.scripts == []  # no nonce
    win.url = LOCAL + "settings.html"
    app.on_loaded()
    nonce = win.scripts[-1].split('"')[1]
    assert app.api.open(nonce, "settings")["ok"]
    win.url = wrong  # the same load's nonce, from a page without the secret
    with pytest.raises(PermissionError):
        app.api.open(nonce, "settings")
    win.url = "http://127.0.0.1:43123/" + SECRET[:-1] + "/settings.html"
    with pytest.raises(PermissionError):
        app.api.open(nonce, "settings")


def test_the_secret_never_reaches_a_log_line(tmp_path):
    logged = []
    app, win, _ = make(tmp_path, log=logged.append)
    app.before_navigate(app.service_url() + "/app/local/settings")
    app.on_navigation_completed(False, url=LOCAL + "settings.html")
    app.before_navigate("http://127.0.0.1:43123/elsewhere")
    app.show_offline()
    assert logged and not any(SECRET in line for line in logged)
    record = logging.LogRecord("pywebview", logging.INFO, "x", 1, "Loading %s", (LOCAL + "sign-in.html",), None)
    assert window._Scrub(SECRET).filter(record) and SECRET not in record.getMessage()


# Links leaving the app (§18.2 item 3) --------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://example.org/a?b=1#c", "https://example.org/a?b=1#c"),
    ("HTTPS://Example.ORG", "https://example.org/"),
    ("https://example.org:443/x", "https://example.org/x"),
    ("https://bücher.example/x", "https://xn--bcher-kva.example/x"),
    ("mailto:a@example.org?subject=Hi&body=Hello", "mailto:a@example.org?subject=Hi&body=Hello"),
    ("mailto:a@example.org?attach=C:%5Csecret.txt&subject=x", "mailto:a@example.org?subject=x"),
    ("mailto:a@example.org,b@example.org?cc=c@example.org&bcc=d@example.org&x-other=1",
     "mailto:a@example.org,b@example.org?cc=c%40example.org&bcc=d%40example.org"),
])
def test_allowed_links_are_re_serialized(url, expected):
    assert policy.external_url(url) == expected


@pytest.mark.parametrize("url", [
    "http://example.org/", "https://user:pw@example.org/", "https://user@example.org/", "https:example.org",
    "https:///nohost", "https://example.org/a b", "https://example.org/\u202eevil", "https://exa\tmple.org/",
    "javascript:alert(1)", "file:///C:/Windows/notepad.exe", "ms-settings:privacy", "custom-app://open",
    "\\\\server\\share\\x", "data:text/html,hi", "mailto:not-an-address", "mailto:a@b.org%0d%0abcc:x@y.org",
    "https://example.org/" + "a" * policy.EXTERNAL_MAX, "", None, 7,
])
def test_everything_else_is_dropped(url):
    assert policy.external_url(url) is None


def test_a_new_window_with_a_custom_scheme_opens_nothing(tmp_path):
    logged = []
    app, win, opened = make(tmp_path, log=logged.append)
    loads = len(win.loads)
    app.new_window("custom-app://do-something")
    assert opened == [] and len(win.loads) == loads
    assert any("link dropped" in line and "custom-app" in line for line in logged)


def test_new_windows_go_through_the_policy(tmp_path):
    app, win, opened = make(tmp_path)
    app.new_window("https://example.org/page")
    app.new_window("mailto:a@example.org?attach=x&subject=hi")
    assert opened == ["https://example.org/page", "mailto:a@example.org?subject=hi"]
    app.new_window(app.service_url() + "/app/inbox")
    assert win.loads[-1] == app.service_url() + "/app/inbox"  # an allowed origin stays in the main window
    app.new_window(app.service_url() + "/app/local/settings")
    assert win.loads[-1] == LOCAL + "settings.html"


def test_a_new_window_request_is_always_cancelled(tmp_path):
    app, win, opened = make(tmp_path)
    args = types.SimpleNamespace(Uri="custom-app://x", Handled=False)
    app._native_new_window(args)
    assert args.Handled is True and opened == []
    args = types.SimpleNamespace(Uri="https://example.org/", Handled=False)
    app._native_new_window(args)
    assert args.Handled is True and opened == ["https://example.org/"]


def test_navigation_to_another_scheme_is_dropped_not_handed_over(tmp_path):
    app, win, opened = make(tmp_path)
    assert app.before_navigate("ms-settings:privacy") is True
    assert app.before_navigate("http://example.org/") is True
    assert opened == []
    assert app.before_navigate("https://example.org/") is True and opened == ["https://example.org/"]


def test_pywebview_never_opens_links_itself():
    assert window.pywebview_settings()["OPEN_EXTERNAL_LINKS_IN_BROWSER"] is False


# The recognised-device token on this install (§18.1 item 2) --------------------------------------------------

from raincli_agent import login, person  # noqa: E402
from raincli_agent.config import Secret  # noqa: E402

from .test_login import EMAIL, PASSWORD, account, config_path, home  # noqa: E402,F401 - fixtures


def sign_in(fake_api, home, **kw):  # noqa: F811
    kw.setdefault("machine_name", "work-pc")
    return login.login(EMAIL, Secret(PASSWORD), api_url=fake_api.url, config_path=str(config_path(home)), **kw)


def test_a_sign_in_keeps_the_device_token_and_sends_it_next_time(account, home):
    sign_in(account, home)
    first = account.state.login_bodies[-1]
    assert "known_device" not in first  # a fresh install has none
    stored = person.load_known_device(str(config_path(home)))
    assert stored and person.DEVICE_TOKEN_RE.fullmatch(stored)
    assert oct(person.device_path(str(config_path(home))).stat().st_mode & 0o777) == "0o600"
    sign_in(account, home, force=True)  # signing in again on this install
    assert account.state.login_bodies[-1]["known_device"] == stored
    assert person.load_known_device(str(config_path(home))) != stored  # the newer one replaces it


def test_the_device_token_survives_sign_out_and_not_a_reinstall_or_forgetting(account, home, monkeypatch):
    cfg = str(config_path(home))
    sign_in(account, home)
    token = person.load_known_device(cfg)
    person.clear_session(cfg)  # what every sign-out does
    assert person.load_known_device(cfg) == token
    monkeypatch.setattr(person, "_installed_under", lambda: ["0.5.3", "a-new-install-stamp"])
    assert person.load_known_device(cfg) is None and not person.device_path(cfg).exists()
    person.save_known_device(cfg, token)
    assert person.forget_known_device(cfg) is True and person.load_known_device(cfg) is None


def test_a_malformed_reply_token_is_never_stored(tmp_path):
    cfg = str(tmp_path / "agent.json")
    for bad in (None, 7, "", "rkd1_x", "rkd1_" + "a" * 32 + "." + "b" * 32 + ".1.0." + "c" * 64 + "\n"):
        person.save_known_device(cfg, bad)
        assert not person.device_path(cfg).exists()
    person.device_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    person.device_path(cfg).write_text("{not json")
    assert person.load_known_device(cfg) is None and not person.device_path(cfg).exists()


def test_forget_device_needs_the_nonce_and_forgets(tmp_path):
    app, win, _ = make(tmp_path)
    forgotten = []
    app.services.forget_device = lambda: forgotten.append(1) or True
    with pytest.raises(PermissionError):
        app.api.forget_device("stale")
    win.url = LOCAL + "settings.html"
    app.on_loaded()
    nonce = win.scripts[-1].split('"')[1]
    assert app.api.forget_device(nonce)["ok"] and forgotten == [1]
