"""v0.5.1: a stale or foreign saved setup in the app window (protocol §16.17 items 3 and 4).

``login.check_credential`` and ``login.set_aside`` are the client core's (cli-builder); here they are
replaced by fakes with the §16.17 signatures, so the window's side is tested on its own.
"""

from __future__ import annotations

import json
import types

import pytest

from raincli_agent import login, person
from raincli_agent.app import services as services_mod
from raincli_agent.app import tray
from raincli_agent.app.services import STALE_SENTENCES, Services, StaleCredential
from raincli_agent.app.window import AppWindow
from raincli_agent.config import Secret

SERVICE = "https://raincli.example"


class Host:
    paused = False

    def __init__(self):
        self.calls = []

    def pause(self):
        self.calls.append("pause")

    def resume(self):
        self.calls.append("resume")

    def stop(self):
        self.calls.append("stop")


class LoginError(login.LoginError):
    def __init__(self, code):
        super().__init__("refused")
        self.code = code


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A signed-in machine (agent.json and runtime.json on disk), with the network faked."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    agent, runtime = config_dir / "agent.json", config_dir / "runtime.json"
    agent.write_text(json.dumps({"api_url": SERVICE, "token": "rca_" + "x" * 40}))
    runtime.write_text(json.dumps({"machine_config": str(agent)}))
    calls = {"check": [], "add_session": [], "set_aside": [], "login": []}
    state = {"check": "ok", "add_session_error": None, "handle": "old-pc"}

    def check_credential(agent_config, email=None):
        calls["check"].append((agent_config, email))
        return state["check"]

    def add_session(agent_config, email, password, api_url=None):
        assert isinstance(password, Secret)
        calls["add_session"].append(email)
        if state["add_session_error"]:
            raise LoginError(state["add_session_error"])

    def set_aside(agent_config):
        calls["set_aside"].append(agent_config)
        backup = config_dir / "replaced-20261006T120000Z"
        backup.mkdir()
        for path in (agent, runtime):
            path.replace(backup / path.name)
        return {"backup": str(backup), "moved": [str(backup / "agent.json"), str(backup / "runtime.json")]}

    def fake_login(email, password, **kwargs):
        assert isinstance(password, Secret)
        calls["login"].append(dict(kwargs, email=email))
        return {"handle": kwargs["machine_name"]}

    monkeypatch.setattr(login, "check_credential", check_credential, raising=False)
    monkeypatch.setattr(login, "set_aside", set_aside, raising=False)
    monkeypatch.setattr(login, "login", fake_login)
    monkeypatch.setattr(login, "prepare", lambda config_path=None, force=False: {"config": config_path})
    monkeypatch.setattr(login, "describe", lambda config_path=None: (config_path, state["handle"]))
    monkeypatch.setattr(login, "default_machine_name", lambda: "old-pc")
    monkeypatch.setattr(person, "add_session", add_session)
    monkeypatch.setattr(services_mod, "default_config_path", lambda: str(agent))
    host = Host()
    svc = Services(None, host, paths=lambda: (str(agent), str(runtime)))
    return types.SimpleNamespace(svc=svc, calls=calls, state=state, host=host, agent=agent, dir=config_dir)


PASSWORD = Secret("pw-v051")


def test_sign_in_checks_the_saved_credential_before_person_only(setup):
    setup.svc.sign_in("a@example.test", PASSWORD, machine_name="x")
    assert setup.calls["check"] == [(str(setup.agent), "a@example.test")] and setup.calls["add_session"] == ["a@example.test"]
    setup.state["check"] = "unknown"  # an older or unreachable server: person_only as before
    setup.svc.sign_in("a@example.test", PASSWORD, machine_name="x")
    assert len(setup.calls["add_session"]) == 2


@pytest.mark.parametrize("state,reason", [("invalid", "invalid"), ("not_owner", "not_owner"),
                                          ("unreadable", "unreadable")])
def test_a_revoked_or_foreign_credential_is_refused_with_its_sentence(setup, state, reason):
    setup.state["check"] = state
    with pytest.raises(StaleCredential) as exc:
        setup.svc.sign_in("a@example.test", PASSWORD, machine_name="x")
    assert exc.value.reason == reason and str(exc.value) == STALE_SENTENCES[reason]
    assert setup.calls["add_session"] == [] and setup.calls["set_aside"] == [] and setup.calls["login"] == []
    assert exc.value.old_handle == ("old-pc" if reason == "not_owner" else None)


@pytest.mark.parametrize("code,reason", [("machine_credential_invalid", "invalid"), ("not_machine_owner", "not_owner")])
def test_person_onlys_409s_become_the_same_offer(setup, code, reason):
    setup.state["add_session_error"] = code
    with pytest.raises(StaleCredential) as exc:
        setup.svc.sign_in("a@example.test", PASSWORD, machine_name="x")
    assert exc.value.reason == reason and setup.calls["set_aside"] == []
    setup.state["add_session_error"] = "invalid_credentials"  # anything else stays its own error
    with pytest.raises(login.LoginError):
        setup.svc.sign_in("a@example.test", PASSWORD, machine_name="x")


def test_new_machine_sets_the_old_setup_aside_then_signs_in_fresh(setup):
    setup.svc.sign_in("a@example.test", PASSWORD, machine_name="old-pc", new_machine=True)
    assert setup.calls["set_aside"] == [str(setup.agent)] and setup.host.calls == ["pause"]  # §16.18 V6
    (call,) = setup.calls["login"]
    assert call["person_session"] is True and call["api_url"] == SERVICE  # the old setup's service
    assert call["machine_name"] != "old-pc" and call["machine_name"] == "old-pc-new"
    assert (setup.dir / "replaced-20261006T120000Z" / "agent.json").is_file() and not setup.agent.exists()
    assert setup.calls["add_session"] == []  # never person_only with the old credential


def test_after_migration_set_it_aside_a_fresh_sign_in_uses_the_backups_service(setup):
    login.set_aside(str(setup.agent))  # as migration's fresh_sign_in_needed leaves it
    assert not setup.svc.signed_in()
    setup.svc.sign_in("a@example.test", PASSWORD, machine_name="new-pc")
    (call,) = setup.calls["login"]
    assert call["api_url"] == SERVICE and call["person_session"] is True and setup.calls["check"] == []


def test_fresh_api_url_falls_back_to_the_default(setup, tmp_path):
    setup.agent.unlink()
    assert setup.svc.fresh_api_url() == login.DEFAULT_API_URL
    (setup.dir / "replaced-20261001T000000Z").mkdir()
    (setup.dir / "replaced-20261001T000000Z" / "agent.json").write_text(json.dumps({"api_url": "javascript:x"}))
    assert setup.svc.fresh_api_url() == login.DEFAULT_API_URL  # an unusable URL is ignored


def test_suggested_name_is_never_the_old_handle(setup, monkeypatch):
    assert setup.svc.suggested_machine_name(avoid="old-pc") == "old-pc-new"
    assert setup.svc.suggested_machine_name(avoid="other") == "old-pc"
    monkeypatch.setattr(login, "default_machine_name", lambda: "a" * 32)
    name = setup.svc.suggested_machine_name(avoid="a" * 32)
    assert name != "a" * 32 and len(name) <= 32 and login.HANDLE_RE.fullmatch(name)


# -- the window ------------------------------------------------------------------------------------------------

class FakeWindow:
    def __init__(self):
        self.url, self.loads, self.scripts = None, [], []
        self.events = types.SimpleNamespace(loaded=Ev(), closing=Ev(), before_show=Ev())

    def load_url(self, url):
        self.loads.append(url)
        self.url = url

    def get_current_url(self):
        return self.url

    def evaluate_js(self, script):
        self.scripts.append(script)


class Ev:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


def test_the_window_offers_a_new_machine_and_never_reuses_the_password(setup, monkeypatch):
    window = FakeWindow()
    webview = types.SimpleNamespace(settings={}, create_window=lambda *a, **k: window, start=lambda *a, **k: None)
    app = AppWindow(setup.svc, profile_dir=setup.dir / "webview", webview=webview, local_port=43123,
                    later=lambda fn, *a: fn(*a))
    monkeypatch.setattr(app, "open_hosted", lambda *a, **k: True)
    app.create()
    setup.state["check"] = "not_owner"
    result = app.sign_in({"email": "a@example.test", "machine_name": "old-pc"}, Secret("pw-1"))
    assert result["ok"] is False and result["code"] == "stale_credential" and result["offer_new_machine"]
    assert result["message"] == STALE_SENTENCES["not_owner"] and result["machine_name"] == "old-pc-new"
    assert result["message"].endswith("The other machine stays active for its owner until they revoke it.")
    assert "pw-1" not in json.dumps(result) and setup.calls["set_aside"] == []
    result = app.sign_in({"email": "a@example.test", "machine_name": result["machine_name"], "new_machine": True},
                         Secret("pw-2"))  # the user pressed the button and typed the password again
    assert result == {"ok": True, "message": "Signed in."}
    assert setup.calls["set_aside"] and setup.calls["login"][0]["machine_name"] == "old-pc-new"
    for truthy in ("yes", 1, "true"):  # only an explicit true is the user's choice
        setup.calls["set_aside"].clear()
        setup.agent.write_text(json.dumps({"api_url": SERVICE, "token": "rca_" + "y" * 40}))
        (setup.dir / "runtime.json").write_text("{}")
        app.sign_in({"email": "a@example.test", "machine_name": "x", "new_machine": truthy}, Secret("pw"))
        assert setup.calls["set_aside"] == []


# -- the tray's first run -------------------------------------------------------------------------------------------

def test_first_run_shows_sign_in_after_migration_set_a_revoked_credential_aside(tmp_path, monkeypatch):
    import raincli_agent.migrate as migrate_mod
    from raincli_agent.runtime import winapp

    class Migration:
        def __init__(self, **kwargs):
            pass

        def pending(self):
            return True

        def old_run_value(self):
            return None

        def run(self, start, notify, cancelled):
            return {"status": "fresh_sign_in_needed", "backup": "replaced-x"}

    monkeypatch.setattr(migrate_mod, "Migration", Migration)
    monkeypatch.setattr(winapp, "read_install", lambda root: {"probation": None})
    monkeypatch.setattr(winapp, "paths", lambda root: (str(tmp_path / "agent.json"), str(tmp_path / "runtime.json")))
    shown = []
    t = tray.Tray.__new__(tray.Tray)
    t.root_dir, t.runtime_config, t.agent_config = tmp_path, str(tmp_path / "runtime.json"), str(tmp_path / "a.json")
    t.host = types.SimpleNamespace(start=lambda: None, stop=lambda: shown.append("stop"), resume=lambda: None,
                                   pause=lambda: None, running=lambda: False, paused=False, config="")
    t.signed_in = lambda: False
    t.migrating = False
    t.post = lambda fn, *a: fn(*a)
    t.background = lambda fn, done=None: done(fn(), None)
    t.sign_in = lambda again=False: shown.append("sign-in")
    t.first_run()
    assert shown == ["stop", "sign-in"] and not t.migrating
    assert "set a revoked credential aside" in (tmp_path / "app-lock" / "app.log").read_text()


def test_the_sentences_are_the_contracts():
    assert STALE_SENTENCES == {
        "invalid": "This computer's saved RainCLI setup belongs to a machine that was revoked.",
        "not_owner": "This computer's saved RainCLI setup belongs to a machine owned by another account. "
                     "The other machine stays active for its owner until they revoke it.",
        "unreadable": "This computer's saved RainCLI setup can't be read by this Windows account.",
    }


# -- §16.19 item 3: connecting Codex and Claude Code, only on the user's click --------------------------------

class FakeHooks:
    def __init__(self, states):
        self.states, self.calls = dict(states), []

    def status(self, kind, runtime_config):
        if self.states.get(kind) == "boom":
            raise OSError("unreadable")
        return self.states[kind]

    def connect(self, kind, runtime_config):
        self.calls.append(("connect", kind, runtime_config))
        self.states[kind] = "needs_approval" if kind == "codex" else "connected"

    def install(self, kind, state_dir, remove=False):
        self.calls.append(("install", kind, state_dir, remove))
        self.states[kind] = "not_connected"

    def state_dir_from_runtime(self, runtime_config):
        return "state-dir"


def test_hooks_status_per_agent_and_connect_only_when_asked(setup, monkeypatch):
    hooks = FakeHooks({"codex": "not_connected", "claude": "boom"})
    monkeypatch.setattr(services_mod, "_hooks", lambda: hooks)
    assert setup.svc.hooks() == [{"kind": "codex", "name": "Codex", "state": "not_connected"},
                                 {"kind": "claude", "name": "Claude Code", "state": "unknown"}]
    assert hooks.calls == []  # reading the state installs nothing
    assert setup.svc.unconnected_agents() == ["Codex"]
    assert setup.svc.connect_hooks("codex") == "needs_approval"
    assert hooks.calls == [("connect", "codex", str(setup.dir / "runtime.json"))]
    assert setup.svc.connect_hooks("codex", connect=False) == "not_connected"
    assert hooks.calls[-1] == ("install", "codex", "state-dir", True)  # no disconnect(): the remove path
    with pytest.raises(services_mod.ServiceError):
        setup.svc.connect_hooks("cursor")


def test_the_js_api_connects_with_the_codex_approval_sentence(setup, monkeypatch):
    from raincli_agent.app.window import CONNECTED
    hooks = FakeHooks({"codex": "not_connected", "claude": "not_installed_agent"})
    monkeypatch.setattr(services_mod, "_hooks", lambda: hooks)
    window = FakeWindow()
    webview = types.SimpleNamespace(settings={}, create_window=lambda *a, **k: window, start=lambda *a, **k: None)
    app = AppWindow(setup.svc, profile_dir=setup.dir / "webview", webview=webview, local_port=43123)
    app.create()
    window.url = "http://127.0.0.1:43123/this-computer.html"
    app.on_loaded()
    nonce = window.scripts[-1].split('"')[1]
    with pytest.raises(PermissionError):
        app.api.connect_hooks("stale", "codex")
    assert hooks.calls == []
    result = app.api.connect_hooks(nonce, "codex", True)
    assert result["ok"] and result["state"] == "needs_approval"
    assert "/hooks" in result["message"] and "new Codex session" in result["message"]
    assert result["message"] == CONNECTED[("codex", "needs_approval")]
    assert app.api.hooks(nonce)[0]["state"] == "needs_approval"


def test_a_fresh_sign_in_calls_the_notice_once_and_person_only_does_not(setup, monkeypatch):
    fresh = []
    window = FakeWindow()
    webview = types.SimpleNamespace(settings={}, create_window=lambda *a, **k: window, start=lambda *a, **k: None)
    app = AppWindow(setup.svc, profile_dir=setup.dir / "webview", webview=webview, local_port=43123,
                    later=lambda fn, *a: fn(*a), on_fresh_sign_in=lambda: fresh.append(1))
    monkeypatch.setattr(app, "open_hosted", lambda *a, **k: True)
    app.create()
    app.sign_in({"email": "a@example.test", "machine_name": "x"}, Secret("pw"))  # person_only on this machine
    assert fresh == []
    app.sign_in({"email": "a@example.test", "machine_name": "pc-new", "new_machine": True}, Secret("pw"))
    assert fresh == [1]


def test_the_tray_shows_one_notice_pointing_to_this_computer(tmp_path):
    notes = []
    t = tray.Tray.__new__(tray.Tray)
    t.icon = types.SimpleNamespace(notify=lambda text, title: notes.append(text))
    t.services = types.SimpleNamespace(unconnected_agents=lambda: ["Codex", "Claude Code"])
    opened = []
    t.post = lambda fn, *a: fn(*a)
    t.window = types.SimpleNamespace(show=lambda: opened.append("show"), local_url=lambda page: page,
                                     load=lambda url: opened.append(url))
    t.offer_connect()
    t.offer_connect()  # once
    assert notes == ["Codex and Claude Code are installed but not connected to RainCLI. Open This computer to connect."]
    t.toast_clicked()
    assert opened == ["show", "this-computer"]
    t2 = tray.Tray.__new__(tray.Tray)
    t2.icon = types.SimpleNamespace(notify=lambda text, title: notes.append(text))
    t2.services = types.SimpleNamespace(unconnected_agents=lambda: [])
    t2.offer_connect()
    assert len(notes) == 1  # nothing to connect: no notice
