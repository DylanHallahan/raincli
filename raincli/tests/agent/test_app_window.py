"""The app window's rules and behaviour without a GUI (protocol §16.10, §16.12 C4, C14, C16).

pywebview is replaced by a recording fake; the window logic, the js_api guard, navigation, the
handoff, offline/Retry, sign-in and sign-out, toasts and the tray's supervisor are exercised here.
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest

import raincli_agent
from raincli_agent import login
from raincli_agent.app import policy, shortcut, tray, window
from raincli_agent.app.window import Api, AppWindow
from raincli_agent.config import Secret
from raincli_agent.runtime import winapp

SERVICE = "https://raincli.example"
APP_DIR = Path(raincli_agent.__file__).parent / "app"


# -- fakes -------------------------------------------------------------------------------------------------

class Events:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class FakeWindow:
    def __init__(self):
        self.url = None
        self.loads, self.scripts, self.shown, self.hidden, self.cookies_cleared = [], [], 0, 0, 0
        self.destroyed = False
        self.events = types.SimpleNamespace(loaded=Events(), closing=Events(), before_show=Events())

    def load_url(self, url):
        self.loads.append(url)
        self.url = url

    def get_current_url(self):
        return self.url

    def evaluate_js(self, script):
        self.scripts.append(script)

    def show(self):
        self.shown += 1

    def restore(self):
        pass

    def hide(self):
        self.hidden += 1

    def destroy(self):
        self.destroyed = True

    def clear_cookies(self):
        self.cookies_cleared += 1


class FakeWebview:
    def __init__(self):
        self.settings = {"ALLOW_DOWNLOADS": False, "ALLOW_FILE_URLS": True, "OPEN_EXTERNAL_LINKS_IN_BROWSER": True}
        self.created, self.started = [], []
        self.window = FakeWindow()

    def create_window(self, title, **kwargs):
        self.created.append((title, kwargs))
        return self.window

    def start(self, func=None, **kwargs):
        self.started.append(kwargs)


class FakeHost:
    def __init__(self):
        self.paused = False
        self.calls = []

    def pause(self):
        self.paused = True
        self.calls.append("pause")

    def resume(self):
        self.paused = False
        self.calls.append("resume")

    def stop(self):
        self.calls.append("stop")


class FakeServices:
    def __init__(self, *, signed_in=True, person=True, handoff=None):
        self.host = FakeHost()
        self._signed_in, self._person = signed_in, person
        self.handoffs = 0
        self.handoff = handoff or (lambda: f"{SERVICE}/app/handoff?code=c{self.handoffs}")
        self.signed_out = False
        self.sign_ins = []
        self.sign_in_error = None

    def service_url(self):
        return SERVICE

    def signed_in(self):
        return self._signed_in

    def has_person_session(self):
        return self._person

    def handoff_url(self, path=None):
        self.handoffs += 1
        return self.handoff()

    def machine_handle(self):
        return "alice-laptop"

    install_token = "install-token-1"

    def app_install_token(self):
        return self.install_token

    def sign_in(self, email, password, **kwargs):
        assert isinstance(password, Secret)
        self.sign_ins.append((email, password.reveal(), kwargs))
        if self.sign_in_error is not None:
            raise self.sign_in_error
        self._signed_in = self._person = True
        return {"handle": kwargs["machine_name"]}

    def sign_out(self):
        self.signed_out = True

    def status(self):
        return {"connection": "connected", "machine": "alice-laptop"}

    def sign_in_defaults(self):
        return {"machine_name": "alice-laptop"}


class Timer:
    made = []

    def __init__(self, seconds, fn):
        self.seconds, self.fn, self.cancelled, self.daemon = seconds, fn, False, False
        Timer.made.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.fn()


def make(tmp_path, services=None, **kw):
    webview = FakeWebview()
    opened = []
    app = AppWindow(services or FakeServices(), profile_dir=tmp_path / "state" / "webview", webview=webview,
                    browser_open=opened.append, local_port=43123, timer=Timer, later=lambda fn, *a: fn(*a), **kw)
    app.create()
    return app, webview.window, opened


LOCAL = "http://127.0.0.1:43123/"


def nonce_of(win):
    script = win.scripts[-1]
    return script.split('"')[1]


# -- policy ------------------------------------------------------------------------------------------------

def test_origins_compare_scheme_host_and_port_exactly():
    nav = policy.Navigation(SERVICE, LOCAL)
    assert nav.decide(SERVICE + "/app/inbox") == ("allow", SERVICE + "/app/inbox")
    assert nav.decide("https://RAINCLI.example:443/app/inbox")[0] == "allow"
    assert nav.decide(LOCAL + "settings.html")[0] == "allow"
    for other in ("http://raincli.example/app/inbox", "https://raincli.example:8443/", "https://evil.example/",
                  "https://raincli.example.evil.example/", "http://127.0.0.1:43124/x", "http://localhost:43123/",
                  "https://user@raincli.example/"):
        assert nav.decide(other) == ("external", other), other
    for blocked in ("javascript:alert(1)", "data:text/html,x", "file:///C:/x", "about:blank", "blob:x"):
        assert nav.decide(blocked)[0] == "block"


def test_local_sentinel_maps_to_bundled_pages():
    nav = policy.Navigation(SERVICE, LOCAL)
    assert nav.decide(SERVICE + "/app/local/settings") == ("local", "settings")
    assert nav.decide(SERVICE + "/app/local/this-computer/") == ("local", "this-computer")
    assert nav.decide(SERVICE + "/app/local/unknown") == ("local", "this-computer")
    assert nav.decide("https://evil.example/app/local/settings")[0] == "external"


def test_the_service_must_be_https_unless_loopback():
    with pytest.raises(ValueError):
        policy.Navigation("http://raincli.example", LOCAL)
    policy.Navigation("http://127.0.0.1:8000", LOCAL)


def test_nonce_only_for_the_local_origin_and_only_the_current_one():
    gate = policy.NonceGate(policy.Navigation(SERVICE, LOCAL))
    first = gate.on_loaded(LOCAL + "settings.html")
    assert first and gate.check(first, LOCAL + "settings.html")
    assert gate.on_loaded(SERVICE + "/app/inbox") is None  # hosted pages never get one
    assert not gate.check(first, LOCAL + "settings.html")  # the load retired it
    second = gate.on_loaded(LOCAL + "settings.html")
    assert not gate.check(second, SERVICE + "/app/inbox")  # the current page is not local
    assert not gate.check(None, LOCAL) and not gate.check("", LOCAL) and not gate.check(first, LOCAL)


def test_toast_text_names_the_sender_only():
    assert policy.toast_text("message", "Bob") == "New message from Bob"
    assert policy.toast_text("escalation", "bob-laptop") == "Escalation from bob-laptop"
    assert policy.toast_text("message", "  x\n" * 40).startswith("New message from x x")
    assert len(policy.toast_text("message", "y" * 500)) == len("New message from ") + 64
    assert policy.toast_sender({"from_endpoint": {"person": "b@x", "display_name": "Bob"}, "body": "secret"}) == "Bob"
    assert policy.toast_sender({"from_endpoint": {"machine": "bob-laptop", "agent": "rev"}}) == "bob-laptop"


# -- the js_api (C4) ---------------------------------------------------------------------------------------

def test_js_api_refuses_a_missing_stale_or_foreign_nonce(tmp_path):
    app, win, _ = make(tmp_path)
    api = app.api
    app.load(LOCAL + "this-computer.html")
    app.on_loaded()
    nonce = nonce_of(win)
    assert api.status(nonce)["machine"] == "alice-laptop"
    for bad in (None, "", "x" * 43):
        with pytest.raises(PermissionError):
            api.status(bad)
    app.load(LOCAL + "settings.html")
    app.on_loaded()
    with pytest.raises(PermissionError):
        api.status(nonce)  # stale
    current = nonce_of(win)
    win.url = SERVICE + "/app/inbox"  # a hosted page calling the bridge with a leaked nonce
    with pytest.raises(PermissionError):
        api.status(current)


def test_the_nonce_is_injected_only_into_local_pages(tmp_path):
    app, win, _ = make(tmp_path)
    app.load(SERVICE + "/app/inbox")
    app.on_loaded()
    assert win.scripts == []
    app.load(LOCAL + "settings.html")
    app.on_loaded()
    assert len(win.scripts) == 1 and "__rcNonce" in win.scripts[0]


def test_js_api_never_returns_a_credential(tmp_path):
    names = [n for n in dir(Api) if not n.startswith("_")]
    assert set(names) == {"sign_in_defaults", "sign_in", "status", "settings", "save_settings", "toggle_pause",
                          "open_log", "sign_out", "open", "retry"}
    source = (APP_DIR / "services.py").read_text()
    tree = ast.parse(source)
    status = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "status")
    assert "token" not in ast.unparse(status).replace("token=", "")


# -- navigation ----------------------------------------------------------------------------------------------

def test_navigation_blocks_other_origins_and_opens_them_in_the_browser(tmp_path):
    app, win, opened = make(tmp_path)
    assert app.before_navigate("https://example.org/docs") is True and opened == ["https://example.org/docs"]
    assert app.before_navigate("javascript:alert(1)") is True and opened == ["https://example.org/docs"]
    assert app.before_navigate(SERVICE + "/app/local/settings") is True and win.loads[-1] == LOCAL + "settings.html"
    assert app.before_navigate(SERVICE + "/app/inbox") is False
    assert app.before_navigate(LOCAL + "offline.html") is False


def test_a_page_that_slipped_through_is_replaced(tmp_path):
    app, win, opened = make(tmp_path)
    win.url = "https://evil.example/"
    app.on_loaded()
    assert win.loads[-1] == LOCAL + "offline.html" and opened == ["https://evil.example/"] and win.scripts == []
    win.url = SERVICE + "/app/local/settings"
    app.on_loaded()
    assert win.loads[-1] == LOCAL + "settings.html"


def test_native_hook_cancels_before_loading(tmp_path):
    app, win, opened = make(tmp_path)
    starting, completed = Events(), Events()
    win.native = types.SimpleNamespace(browser=types.SimpleNamespace(
        webview=types.SimpleNamespace(NavigationStarting=starting, NavigationCompleted=completed,
                                      CoreWebView2InitializationCompleted=Events())))
    assert app.attach_native() is True
    args = types.SimpleNamespace(Uri="https://evil.example/", Cancel=False)
    starting.handlers[0](None, args)
    assert args.Cancel is True and opened == ["https://evil.example/"]
    ok = types.SimpleNamespace(Uri=SERVICE + "/app/inbox", Cancel=False)
    starting.handlers[0](None, ok)
    assert ok.Cancel is False


# -- the handoff, offline and Retry ---------------------------------------------------------------------------

def test_home_hands_off_then_follows_the_pending_path(tmp_path):
    app, win, _ = make(tmp_path)
    app.home()
    assert win.loads[-1] == f"{SERVICE}/app/handoff?code=c1" and app.handed_off
    app.open_hosted(thread_id="t1")  # already handed off: direct
    assert win.loads[-1] == f"{SERVICE}/app/conversations/t1"
    app.handed_off = False
    app.open_hosted("/app/agents")
    win.url = SERVICE + "/app/inbox"  # the handoff redirects to the inbox
    app.on_loaded()
    assert win.loads[-1] == SERVICE + "/app/agents" and app.pending_path is None


def test_an_ended_app_session_hands_off_again(tmp_path):
    app, win, _ = make(tmp_path)
    app.home()
    win.url = SERVICE + "/login?next=/app/inbox"
    app.on_loaded()
    assert app.services.handoffs == 2 and win.loads[-1].startswith(SERVICE + "/app/handoff")


def test_without_a_person_session_home_is_sign_in(tmp_path):
    app, win, _ = make(tmp_path, FakeServices(signed_in=True, person=False))
    app.home()
    assert win.loads[-1] == LOCAL + "sign-in.html?person=1"
    app2, win2, _ = make(tmp_path, FakeServices(signed_in=False, person=False))
    app2.home()
    assert win2.loads[-1] == LOCAL + "sign-in.html"


def test_failed_or_timed_out_loads_show_offline_and_retry_recovers(tmp_path):
    services = FakeServices()
    app, win, _ = make(tmp_path, services)
    services.handoff = lambda: (_ for _ in ()).throw(OSError("unreachable"))
    app.home()
    assert win.loads[-1] == LOCAL + "offline.html"
    services.handoff = lambda: f"{SERVICE}/app/handoff?code=ok"
    win.url = LOCAL + "offline.html"
    app.on_loaded()
    assert app.api.retry(nonce_of(win))["ok"] and win.loads[-1] == f"{SERVICE}/app/handoff?code=ok"
    Timer.made[-1].fire()  # the hosted load never finished
    assert win.loads[-1] == LOCAL + "offline.html"
    app.open_hosted("/app/inbox")
    win.url = SERVICE + "/app/inbox"
    app.on_navigation_completed(False)  # a network error
    assert win.loads[-1] == LOCAL + "offline.html"
    app.open_hosted("/app/account")
    win.url = SERVICE + "/app/account"
    app.on_navigation_completed(False, 403)  # the service's own answer stays on screen
    assert win.loads[-1] == SERVICE + "/app/account"


def test_loaded_disarms_the_timeout(tmp_path):
    app, win, _ = make(tmp_path)
    app.home()
    timer = Timer.made[-1]
    win.url = SERVICE + "/app/inbox"
    app.on_loaded()
    assert timer.cancelled


# -- sign-in and sign-out ---------------------------------------------------------------------------------------

def test_sign_in_maps_refusals_and_never_echoes_the_password(tmp_path):
    services = FakeServices(signed_in=False, person=False)
    app, win, _ = make(tmp_path, services)
    app.load(LOCAL + "sign-in.html")
    app.on_loaded()
    nonce = nonce_of(win)
    request = {"email": " a@example.test ", "machine_name": "alice-laptop", "team": None, "replace": False}
    services.sign_in_error = login.TeamChoiceRequired("choose", [{"slug": "acme", "name": "Acme"}])
    result = app.api.sign_in(nonce, request, "pw-123")
    assert result["code"] == "team_choice_required" and result["teams"] == [{"slug": "acme", "name": "Acme"}]
    services.sign_in_error = login.NameInUse("in use")
    assert app.api.sign_in(nonce, request, "pw-123")["code"] == "name_in_use"
    services.sign_in_error = login.LoginError("Wrong email or password.")
    assert app.api.sign_in(nonce, request, "pw-123")["message"] == "Wrong email or password."
    services.sign_in_error = RuntimeError("boom")
    assert "pw-123" not in str(app.api.sign_in(nonce, request, "pw-123"))
    services.sign_in_error = None
    result = app.api.sign_in(nonce, request, "pw-123")
    assert result == {"ok": True, "message": "Signed in."}
    assert services.sign_ins[-1][0] == "a@example.test" and services.sign_ins[-1][1] == "pw-123"
    assert services.host.calls[-1] == "resume" and win.loads[-1].startswith(SERVICE + "/app/handoff")


def test_sign_out_confirms_clears_the_profile_and_shows_sign_in(tmp_path):
    answers = [False, True]
    app, win, _ = make(tmp_path, confirm=lambda title, text: answers.pop(0))
    app.home()
    assert app.sign_out() == {"ok": False, "message": ""} and not app.services.signed_out
    assert app.sign_out()["ok"] and app.services.signed_out
    assert win.cookies_cleared == 1 and not app.handed_off and win.loads[-1] == LOCAL + "sign-in.html"
    profile = tmp_path / "state" / "webview"
    (profile / "EBWebView").mkdir(parents=True)
    window.reset_profile_if_marked(profile)
    assert not profile.exists() and not (profile.parent / window.PROFILE_RESET_MARKER).exists()


def test_closing_hides_and_quit_closes(tmp_path):
    app, win, _ = make(tmp_path)
    assert app.on_closing() is False and win.hidden == 1
    app.destroy()
    assert app.on_closing() is True and win.destroyed


# -- C16: never debugging ------------------------------------------------------------------------------------------

def test_start_passes_debug_false_and_no_debugging_settings(tmp_path):
    webview = FakeWebview()
    app = AppWindow(FakeServices(), profile_dir=tmp_path / "webview", webview=webview, local_port=43123)
    app.create()
    app.start(lambda: None)
    (kwargs,) = webview.started
    assert kwargs["debug"] is False and kwargs["http_server"] is True and kwargs["http_port"] == 43123
    assert kwargs["private_mode"] is False and kwargs["storage_path"] == str(tmp_path / "webview")
    assert webview.settings == {"ALLOW_DOWNLOADS": True, "ALLOW_FILE_URLS": False, "OPEN_EXTERNAL_LINKS_IN_BROWSER": True}
    assert not any("DEBUG" in k for k in window.pywebview_settings())
    title, created = webview.created[0]
    assert created["hidden"] is True and created["js_api"] is app.api and "debug" not in created


FORBIDDEN = ("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--remote-debugging-port", "--remote-debugging-pipe",
             "--remote-allow-origins", "REMOTE_DEBUGGING_PORT")


def test_shipped_app_code_never_mentions_debugging():
    """§16.11/C16 over the source: the bundle check refuses the same strings in the frozen build."""
    roots = [Path(raincli_agent.__file__).parent]
    for path in (p for root in roots for p in root.rglob("*") if p.suffix in (".py", ".html", ".js", ".css")):
        text = path.read_text("utf-8")
        for needle in FORBIDDEN:
            assert needle not in text, (path, needle)
    for call in (n for n in ast.walk(ast.parse((APP_DIR / "window.py").read_text())) if isinstance(n, ast.Call)):
        name = ast.unparse(call.func)
        if name.endswith(".start"):
            debug = [k for k in call.keywords if k.arg == "debug"]
            if debug:
                assert ast.literal_eval(debug[0].value) is False


# -- the tray: supervisor, toasts, open requests -------------------------------------------------------------------

class FakeIcon:
    def __init__(self):
        self.notes = []

    def notify(self, text, title):
        self.notes.append((text, title))


def fake_tray(tmp_path, services):
    t = tray.Tray.__new__(tray.Tray)
    t.services, t.icon, t.toast_thread = services, FakeIcon(), None
    t.signed_in = lambda: True
    return t


def test_toasts_carry_only_the_sender_and_open_the_thread(tmp_path):
    services = FakeServices()
    services.notifications = lambda: [{"id": "m1", "kind": "message", "at": "t"},
                                      {"id": "m2", "kind": "escalation", "at": "t"}]
    summaries = {"m1": {"kind": "message", "sender": "Bob", "conversation_id": "c1"},
                 "m2": {"kind": "escalation", "sender": "bob-laptop", "conversation_id": "c2"}}
    services.message_summary = lambda mid: summaries[mid]
    t = fake_tray(tmp_path, services)
    t.toasts()
    assert t.icon.notes == [("New message from Bob", "RainCLI"), ("Escalation from bob-laptop", "RainCLI")]
    assert t.toast_thread == "c2"
    posted = []
    t.post = lambda fn, *a: posted.append((fn.__name__, a))
    t.toast_clicked()
    assert posted == [("open_thread", ("c2",))]
    opened = []
    t.window = types.SimpleNamespace(show=lambda: opened.append("show"),
                                     open_hosted=lambda path=None, thread_id=None: opened.append(thread_id or path))
    t.open_thread("c2")
    assert opened == ["show", "c2"]


def test_a_toast_without_the_service_still_has_no_body(tmp_path):
    services = FakeServices()
    services.notifications = lambda: [{"id": "m1", "kind": "message", "at": "t"}]
    services.message_summary = lambda mid: (_ for _ in ()).throw(OSError("offline"))
    t = fake_tray(tmp_path, services)
    t.toasts()
    assert t.icon.notes == [("New message from a teammate", "RainCLI")] and t.toast_thread is None


def test_toast_click_hook_wraps_pystray_notify(monkeypatch):
    win32 = types.SimpleNamespace(WM_NOTIFY=0x8000)
    monkeypatch.setitem(sys.modules, "pystray", types.ModuleType("pystray"))
    monkeypatch.setitem(sys.modules, "pystray._util", types.SimpleNamespace(win32=win32))
    seen, clicks = [], []
    icon = types.SimpleNamespace(_message_handlers={0x8000: lambda w, l: seen.append(l)})
    assert tray.hook_toast_click(icon, lambda: clicks.append(1))
    icon._message_handlers[0x8000](0, tray.NIN_BALLOONUSERCLICK)
    icon._message_handlers[0x8000](0, 0x0202)
    assert clicks == [1] and seen == [0x0202]
    assert tray.hook_toast_click(types.SimpleNamespace(), lambda: None) is False


def test_open_requests_are_taken_once(tmp_path):
    assert not winapp.take_show_request(tmp_path)
    winapp.request_show(tmp_path)
    assert winapp.take_show_request(tmp_path) and not winapp.take_show_request(tmp_path)


def test_stub_without_arguments_asks_for_the_window(tmp_path, monkeypatch):
    from raincli_agent.app import stub
    monkeypatch.setattr(sys, "executable", str(tmp_path / "RainCLI.exe"))
    calls = []
    monkeypatch.setattr(winapp, "stub_main", lambda root, open_window=False: calls.append(open_window) or 0)
    assert stub.main([]) == 0 and calls == [True] and (tmp_path / "app-lock" / winapp.SHOW).exists()
    assert winapp.take_show_request(tmp_path)
    assert stub.main(["--open"]) == 0 and calls == [True, True] and winapp.take_show_request(tmp_path)
    assert stub.main(["--background"]) == 0 and calls == [True, True, False]
    assert not (tmp_path / "app-lock" / winapp.SHOW).exists()


def test_a_background_start_discards_a_stale_open_request(tmp_path, monkeypatch):
    winapp.request_show(tmp_path)
    monkeypatch.setattr(winapp.Stub, "run", lambda self: 0)
    assert winapp.stub_main(tmp_path) == 0
    assert not (tmp_path / "app-lock" / winapp.SHOW).exists()
    winapp.request_show(tmp_path)
    assert winapp.stub_main(tmp_path, open_window=True) == 0
    assert (tmp_path / "app-lock" / winapp.SHOW).exists()  # the app it started takes it


def test_supervisor_answers_quit_and_open_requests(tmp_path, monkeypatch):
    t = tray.Tray.__new__(tray.Tray)
    t.root_dir = tmp_path
    stops = iter([False, False, True])
    t.stopping = types.SimpleNamespace(wait=lambda s: next(stops))
    posted, quits = [], []
    t.post = lambda fn, *a: posted.append(fn)
    t.window = types.SimpleNamespace(show=lambda: None)
    t.quit = quits.append
    winapp.request_show(tmp_path)
    monkeypatch.setattr(tray, "STEP_EVERY", 99)
    t.supervise()
    assert posted == [t.window.show] and quits == []
    (tmp_path / "app-lock" / winapp.QUIT).write_bytes(b"")
    t.stopping = types.SimpleNamespace(wait=lambda s: False)
    t.supervise()
    assert quits == [0]


def test_locked_host_serialises_calls_and_keeps_attributes():
    host = FakeHost()
    host.config = "a"
    locked = tray.LockedHost(host)
    locked.pause()
    assert locked.paused and host.calls == ["pause"]
    locked.config = "b"
    assert host.config == "b" and locked.config == "b"


def test_without_webview2_the_app_never_opens_a_window_and_links_the_runtime(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(tray.webbrowser, "open", opened.append)
    t = tray.Tray.__new__(tray.Tray)
    t.icon, t.stopping = FakeIcon(), types.SimpleNamespace(set=lambda: opened.append("stopped"))
    t.window = tray.NoWindow(t)
    t.window.show()
    t.window.open_hosted(thread_id="c1")
    assert opened == [window.WEBVIEW2_DOWNLOAD] * 2 and "WebView2" in t.icon.notes[0][0]
    assert t.confirm("RainCLI", "Choose the connector config file?") is False  # no dialog, the safe answer
    t.notice("Waiting for the old window to close.", None)
    assert t.icon.notes[-1] == ("Waiting for the old window to close.", "RainCLI")
    t.window.destroy()
    assert opened[-1] == "stopped"


# -- §16.14 S3: the install token rides only in the User-Agent, only to the service ------------------------

def test_user_agent_carries_the_install_token_to_the_service_only(tmp_path):
    """Review 4 A2: the global User-Agent stays at its base; our own filter; the current token only on
    requests to the exact service origin."""
    app, win, _ = make(tmp_path)
    init, requested, starting = Events(), Events(), Events()
    control = types.SimpleNamespace(NavigationStarting=starting, NavigationCompleted=Events(),
                                    CoreWebView2InitializationCompleted=init)
    win.native = types.SimpleNamespace(browser=types.SimpleNamespace(webview=control))
    app.attach_native()
    filters = []
    core = types.SimpleNamespace(Settings=types.SimpleNamespace(UserAgent="Mozilla/5.0 Edg/131.0"),
                                 WebResourceRequested=requested,
                                 AddWebResourceRequestedFilter=lambda uri, ctx: filters.append((uri, ctx)))
    app._core_ready(core, context_all="All")
    assert core.Settings.UserAgent == "Mozilla/5.0 Edg/131.0"  # never the token
    assert filters == [(SERVICE + "/*", "All")]

    def request(url):
        headers = {}
        req = types.SimpleNamespace(Uri=url, Headers=types.SimpleNamespace(SetHeader=headers.__setitem__))
        requested.handlers[0](None, types.SimpleNamespace(Request=req))
        return headers
    assert request(SERVICE + "/app/inbox") == {"User-Agent": "Mozilla/5.0 Edg/131.0 RainCLIApp/install-token-1"}
    for other in (LOCAL + "settings.html", "https://example.org/", "http://raincli.example/app/inbox",
                  "https://raincli.example:8443/app/inbox", "https://raincli.example.evil.example/"):
        assert "RainCLIApp/" not in str(request(other)), other
    app.services.install_token = "install-token-2"  # sign-in or sign-out rotated it
    app.refresh_install_token()
    assert request(SERVICE + "/x") == {"User-Agent": "Mozilla/5.0 Edg/131.0 RainCLIApp/install-token-2"}
    # A new service origin (signed in to another server) gets its filter before its first navigation.
    app.services.service_url = lambda: "https://other.example"
    app.refresh_navigation()
    starting.handlers[0](None, types.SimpleNamespace(Uri="https://other.example/app/inbox", Cancel=False))
    assert filters[-1] == ("https://other.example/*", "All") and len(filters) == 2
    starting.handlers[0](None, types.SimpleNamespace(Uri="https://other.example/app/x", Cancel=False))
    assert len(filters) == 2


def test_handoff_refreshes_the_token_and_sign_in_starts_a_new_app_session(tmp_path):
    services = FakeServices()
    app, win, _ = make(tmp_path, services)
    app.home()
    assert app.handed_off and app.install_token == "install-token-1"
    services.install_token = "install-token-2"
    app.load(LOCAL + "sign-in.html")
    app.on_loaded()
    app.api.sign_in(nonce_of(win), {"email": "a@example.test", "machine_name": "alice-laptop", "again": True}, "pw")
    assert app.install_token == "install-token-2" and services.handoffs == 2


# -- §16.15: install.json keeps the full installer's keys; the v0.4 shortcut is fixed ---------------------

def test_install_json_keeps_stub_and_install_stamp(tmp_path):
    import json
    (tmp_path / "install.json").write_text(json.dumps({"current": "0.5.0", "previous": None, "probation": None,
                                                       "stub": 2, "install_stamp": "20261005T121400-ab12cd34ef56"}))
    winapp.write_install(tmp_path, "0.5.1", "0.5.0", probation="0.5.1")  # a pushed update
    data = json.loads((tmp_path / "install.json").read_text())
    assert data == {"current": "0.5.1", "previous": "0.5.0", "probation": "0.5.1", "stub": 2,
                    "install_stamp": "20261005T121400-ab12cd34ef56"}
    assert winapp.read_install(tmp_path) == {"current": "0.5.1", "previous": "0.5.0", "probation": "0.5.1"}
    for bad in ({"stub": True}, {"stub": "2"}, {"install_stamp": "a b"}, {"install_stamp": 7}):
        (tmp_path / "install.json").write_text(json.dumps({"current": "0.5.0", **bad}))
        assert set(winapp.read_install_meta(tmp_path).values()) == {None}, bad


class Run:
    def __init__(self, out="rewritten", code=0):
        self.calls, self.out, self.code = [], out, code

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return types.SimpleNamespace(returncode=self.code, stdout=self.out + "\n", stderr="")


WINDOWS_ENV = {"SystemRoot": r"C:\Windows"}


@pytest.mark.parametrize("root,expected", [
    (r"C:\Windows", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"),
    (r"D:\WINNT", r"D:\WINNT\System32\WindowsPowerShell\v1.0\powershell.exe"),
    (None, None), ("", None), ("Windows", None), (r"\Windows", None), (r"C:Windows", None),
    (r"\\server\share\Windows", None), (r"C:\Windows\..\Temp", None), ("C:\\Win\ndows", None),
])
def test_powershell_runs_only_by_absolute_path_under_systemroot(root, expected):
    """Review 6 L1: never a bare "powershell.exe", which CreateProcess also seeks in the current directory."""
    assert shortcut.powershell_path({} if root is None else {"SystemRoot": root}) == expected


def test_without_a_valid_systemroot_the_shortcut_is_left_alone(tmp_path):
    appdata = tmp_path / "AppData"
    (appdata / shortcut.SHORTCUT).parent.mkdir(parents=True)
    (appdata / shortcut.SHORTCUT).write_bytes(b"lnk")
    root = tmp_path / "RainCLI"
    root.mkdir()
    winapp.write_install(root, "0.5.0", "0.4.0")
    never, logged = Run(), []
    assert shortcut.fix_v04_shortcut(root, run=never, appdata=str(appdata), log=lambda r, t: logged.append(t),
                                     environ={"SystemRoot": "Windows"}) == "skipped"
    assert never.calls == [] and "SystemRoot is not an absolute path" in logged[0]


def test_v04_shortcut_is_pointed_at_background_once(tmp_path):
    import json
    appdata = tmp_path / "AppData"
    link = appdata / shortcut.SHORTCUT
    link.parent.mkdir(parents=True)
    link.write_bytes(b"lnk")
    root = tmp_path / "RainCLI"
    root.mkdir()
    winapp.write_install(root, "0.5.0", "0.4.0")  # updated in place: no "stub" key
    logged, run = [], Run()
    assert shortcut.fix_v04_shortcut(root, run=run, appdata=str(appdata), log=lambda r, t: logged.append(t),
                                     environ=WINDOWS_ENV) == "rewritten"
    argv, kw = run.calls[0]
    assert argv[0] == r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" and "-NoProfile" in argv and str(link) not in " ".join(argv)
    assert kw["env"]["RAINCLI_LNK"] == str(link) and kw["env"]["RAINCLI_STUB"] == str(root / "RainCLI.exe")
    assert logged == ["Start menu shortcut for the v0.4 stub: rewritten (RainCLI.lnk --background)"]
    quiet = []
    assert shortcut.fix_v04_shortcut(root, run=Run("unchanged"), appdata=str(appdata), environ=WINDOWS_ENV,
                                   log=lambda r, t: quiet.append(t)) == "unchanged" and quiet == []
    assert shortcut.fix_v04_shortcut(root, run=Run("x", code=1), appdata=str(appdata), log=lambda r, t: None,
                                     environ=WINDOWS_ENV) == "failed"
    (root / "install.json").write_text(json.dumps({"current": "0.5.0", "stub": 2}))  # a full v0.5 install
    never = Run()
    assert shortcut.fix_v04_shortcut(root, run=never, appdata=str(appdata)) is None and never.calls == []
    link.unlink()
    (root / "install.json").write_text(json.dumps({"current": "0.5.0"}))
    assert shortcut.fix_v04_shortcut(root, run=never, appdata=str(appdata)) is None and never.calls == []


def test_a_window_that_fails_to_start_leaves_the_tray_running(tmp_path, monkeypatch):
    """The window is optional: if pywebview or WebView2 can't start, delivery carries on headless."""
    t = tray.Tray.__new__(tray.Tray)
    t.root_dir, t.icon, t.stopping, t._started = tmp_path, None, __import__("threading").Event(), \
        __import__("threading").Event()
    t.exit_code = 0

    class Broken:
        def create(self):
            pass

        def start(self, func):
            raise RuntimeError("no WebView2 environment")
    t.window = Broken()
    ran = []
    t.started = lambda: (ran.append(type(t.window).__name__), t._started.set())
    t.menu = lambda: None
    fake_pystray = types.SimpleNamespace(Icon=lambda *a: types.SimpleNamespace(
        run_detached=lambda: None, stop=lambda: None, _message_handlers={}))
    monkeypatch.setattr(tray, "icon_image", lambda state: None)
    assert t._run(fake_pystray) == 0
    assert ran == ["NoWindow"] and "running in the tray only" in (tmp_path / "app-lock" / "app.log").read_text()


def test_sentinel_loads_the_local_page_and_its_cancelled_navigation_is_not_offline(tmp_path):
    """The e2e's D6 bug: WebView2 reports the navigation the app cancelled (the /app/local sentinel) as an
    unsuccessful NavigationCompleted (OperationCanceled), while the window still shows the hosted page.
    That must never trigger the offline page, which would replace the local page being loaded."""
    logged = []
    app, win, _ = make(tmp_path, log=logged.append)
    starting, completed = Events(), Events()
    win.native = types.SimpleNamespace(browser=types.SimpleNamespace(
        webview=types.SimpleNamespace(NavigationStarting=starting, NavigationCompleted=completed,
                                      CoreWebView2InitializationCompleted=Events())))
    app.attach_native()
    win.url = SERVICE + "/app/inbox"  # the hosted inbox is showing
    args = types.SimpleNamespace(Uri=SERVICE + "/app/local/this-computer", Cancel=False, NavigationId=7)
    starting.handlers[0](None, args)
    assert args.Cancel is True and win.loads[-1] == LOCAL + "this-computer.html"
    win.url = SERVICE + "/app/inbox"  # the cancelled navigation completes before the local load commits
    completed.handlers[0](None, types.SimpleNamespace(IsSuccess=False, HttpStatusCode=0, NavigationId=7,
                                                      WebErrorStatus="OperationCanceled"))
    assert win.loads[-1] == LOCAL + "this-computer.html"  # not offline.html
    # A load replaced by a newer one is also OperationCanceled, even without our id.
    completed.handlers[0](None, types.SimpleNamespace(IsSuccess=False, HttpStatusCode=0, NavigationId=8,
                                                      WebErrorStatus="OperationCanceled"))
    assert win.loads[-1] == LOCAL + "this-computer.html"
    win.url = LOCAL + "this-computer.html"
    completed.handlers[0](None, types.SimpleNamespace(IsSuccess=True, HttpStatusCode=200, NavigationId=9,
                                                      WebErrorStatus="Unknown"))
    app.on_loaded()
    assert "__rcNonce" in win.scripts[-1] and app.api.status(nonce_of(win))["machine"] == "alice-laptop"
    # A real network failure of a hosted load still shows the offline page.
    win.url = SERVICE + "/app/inbox"
    completed.handlers[0](None, types.SimpleNamespace(IsSuccess=False, HttpStatusCode=0, NavigationId=10,
                                                      WebErrorStatus="CannotConnect"))
    assert win.loads[-1] == LOCAL + "offline.html"
    text = "\n".join(logged)
    assert "sentinel " + SERVICE + "/app/local/this-computer -> local page this-computer" in text
    assert "cancelled by the app" in text and "offline page shown" in text
    win.url = SERVICE + "/app/handoff?code=rch_secret"
    completed.handlers[0](None, types.SimpleNamespace(IsSuccess=True, HttpStatusCode=303, NavigationId=11))
    assert "rch_secret" not in "\n".join(logged)  # never a query in the log


def test_a_timeout_armed_before_a_successful_load_never_shows_offline(tmp_path):
    """The e2e's D5: a 20 s timer left armed by the js_api thread racing the UI thread fired after the
    hosted page had loaded, and replaced it with the offline page."""
    logged = []
    app, win, _ = make(tmp_path, log=logged.append)
    completed = Events()
    win.native = types.SimpleNamespace(browser=types.SimpleNamespace(
        webview=types.SimpleNamespace(NavigationStarting=Events(), NavigationCompleted=completed,
                                      CoreWebView2InitializationCompleted=Events())))
    app.attach_native()
    app.home()
    orphan = Timer.made[-1]
    app.load_timer = None  # lost to the race: never cancelled
    win.url = None  # get_current_url() can't answer on the UI thread
    sender = types.SimpleNamespace(Source=SERVICE + "/app/inbox")
    completed.handlers[0](sender, types.SimpleNamespace(IsSuccess=True, HttpStatusCode=200, NavigationId=3))
    before = list(win.loads)
    orphan.fire()
    assert win.loads == before and "load ok " + SERVICE + "/app/inbox" in "\n".join(logged)
    app.open_hosted("/app/agents")  # a new load with no success afterwards still times out
    Timer.made[-1].fire()
    assert win.loads[-1] == LOCAL + "offline.html" and "load timed out" in "\n".join(logged)


def test_app_log_never_holds_a_query_code_or_the_install_token(tmp_path):
    """Review 6 L2: app-lock\\app.log carries origins and paths only, whatever the window loads."""
    root = tmp_path / "RainCLI"
    root.mkdir()  # the install root always exists; app_log creates app-lock under it
    services = FakeServices()
    services.install_token = "SeCrEt_install_token_0123456789abcdefghijklm"
    services.handoff = lambda: f"{SERVICE}/app/handoff?code=rch_secretcode"
    app, win, _ = make(tmp_path, services, log=lambda text: winapp.app_log(root, text))
    starting, completed, init, requested = Events(), Events(), Events(), Events()
    win.native = types.SimpleNamespace(browser=types.SimpleNamespace(webview=types.SimpleNamespace(
        NavigationStarting=starting, NavigationCompleted=completed, CoreWebView2InitializationCompleted=init)))
    app.attach_native()
    core = types.SimpleNamespace(Settings=types.SimpleNamespace(UserAgent="Mozilla/5.0"), WebResourceRequested=requested,
                                 AddWebResourceRequestedFilter=lambda uri, ctx: None)
    app._core_ready(core, context_all="All")
    app.home()  # the handoff, with its code in the query
    handoff = win.loads[-1]
    starting.handlers[0](None, types.SimpleNamespace(Uri=handoff, Cancel=False, NavigationId=1))
    completed.handlers[0](types.SimpleNamespace(Source=handoff),
                          types.SimpleNamespace(IsSuccess=True, HttpStatusCode=303, NavigationId=1))
    sentinel = SERVICE + "/app/local/settings?code=rch_other&x=1#frag"
    starting.handlers[0](None, types.SimpleNamespace(Uri=sentinel, Cancel=False, NavigationId=2))
    completed.handlers[0](types.SimpleNamespace(Source=SERVICE + "/app/inbox?code=rch_x"),
                          types.SimpleNamespace(IsSuccess=False, HttpStatusCode=0, NavigationId=2,
                                                WebErrorStatus="OperationCanceled"))
    failed = SERVICE + "/app/conversations/c1?code=rch_failed&reply_to=m1"
    app.open_hosted("/app/conversations/c1?code=rch_failed")
    completed.handlers[0](types.SimpleNamespace(Source=failed),
                          types.SimpleNamespace(IsSuccess=False, HttpStatusCode=0, NavigationId=3,
                                                WebErrorStatus="CannotConnect"))
    app.open_hosted("/app/agents?code=rch_timeout")
    Timer.made[-1].fire()
    text = (root / "app-lock" / "app.log").read_text("utf-8")
    assert "sentinel" in text and "load failed" in text and "offline page shown" in text and "load timed out" in text
    for forbidden in ("code=", "?", "RainCLIApp/", "rch_", services.install_token):
        assert forbidden not in text, forbidden
