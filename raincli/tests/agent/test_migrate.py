"""Migration of existing installs (15.6 amended by 15.8 H6, M7, M8, L4)."""
import json
import os
from pathlib import Path

import pytest

from raincli_agent import migrate as migrate_mod

from raincli_agent import config as config_mod, dpapi
from raincli_agent.config import load_config
from raincli_agent.connector.queue import Queue
from raincli_agent.errors import ConfigError
from raincli_agent.fsutil import atomic_write_json
from raincli_agent.migrate import CLOSE_OLD, Migration
from raincli_agent.runtime import winapp
from raincli_agent.runtime.service import load_runtime

from .test_dpapi_machine import FakeDpapi

TOKEN = "rca_" + "B" * 43


class Registry(dict):
    def get(self, name):
        return super().get(name)

    def set(self, name, value):
        self[name] = value

    def delete(self, name):
        self.pop(name, None)


@pytest.fixture(autouse=True)
def offline_inbox_check(request, monkeypatch):
    """No test reaches a real server: the F11 inbox check answers "cannot tell" unless a test opts in."""
    if "fake_api" not in request.fixturenames:
        monkeypatch.setattr(Migration, "inbox_role", lambda self, config: None)
        monkeypatch.setattr(migrate_mod, "_check_credential", lambda agent: "unknown")  # §16.17 3: offline


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config" / "raincli").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("RAINCLI_CONFIG", raising=False)
    return home


@pytest.fixture
def windows(monkeypatch):
    dpapi.set_backend(FakeDpapi())
    monkeypatch.setattr(config_mod, "protects_tokens", lambda: True)
    yield
    dpapi.set_backend(None)


@pytest.fixture
def app(tmp_path):
    root = tmp_path / "Programs" / "RainCLI"
    (root / "versions" / "0.4.0").mkdir(parents=True)
    (root / winapp.STUB).write_text("stub")
    winapp.write_install(root, "0.4.0", None)
    return root


def cfg_dir(home):
    return home / ".config" / "raincli"


def write_agent(path, token=TOKEN):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"api_url": "https://raincli.example", "token": token})
    return path


def write_connector(path, **data):
    data.setdefault("herdr_agent", "inbox")
    path.write_text(json.dumps(data))
    return path


def started(ok=True, calls=None):
    def start(runtime_config):
        load_runtime(runtime_config)  # what the new runtime will run must load
        if calls is not None:
            calls.append(runtime_config)
        return ok
    return start


def snapshot(*paths):
    return {str(p): p.read_bytes() for p in paths}


# -- shape 1: the managed v0.2/v0.3 install -------------------------------------------------------

def managed_install(home, tmp_path):
    managed = home / ".raincli" / "client"
    managed.mkdir(parents=True)
    (managed / "current.json").write_text("{}")
    agent = write_agent(cfg_dir(home) / "agent.json")
    write_connector(cfg_dir(home) / "connector.json", agent_config="agent.json", state_dir=str(tmp_path / "queue"),
                    prompt_timeout=30)
    runtime = cfg_dir(home) / "runtime.json"
    runtime.write_text(json.dumps({"connectors": ["connector.json"], "state_dir": "runtime-state"}))
    value = f'"C:\\Python\\pythonw.exe" "{managed}/launch.py" "runtime" "run" "--config" "{runtime}"'
    return managed, agent, runtime, Registry(RainCLI=value)


def test_managed_install_keeps_everything_and_disables_run_value_last(home, tmp_path, windows, app):
    managed, agent, runtime, registry = managed_install(home, tmp_path)
    original = registry["RainCLI"]
    (tmp_path / "queue").mkdir()
    (tmp_path / "queue" / "cursor.json").write_text("{\"cursor\": 7}")
    calls = []
    result = Migration(registry=registry, app_root=app, managed_root=managed).run(started(calls=calls))
    assert result["status"] == "migrated" and result["managed_install"]
    assert result["mode"] == "connector" and result["runtime_config"] == str(runtime)
    assert calls == [str(runtime)]
    data = json.loads(agent.read_text())
    assert set(data) == {"api_url", "token_dpapi"} and load_config(str(agent)).token.reveal() == TOKEN
    assert json.loads(runtime.read_text()) == {"connectors": ["connector.json"], "state_dir": "runtime-state"}
    assert (tmp_path / "queue" / "cursor.json").read_text() == "{\"cursor\": 7}"
    assert registry["RainCLI"] == winapp.run_value(app)  # the old value, recorded, then replaced
    text = (cfg_dir(home) / "runtime-state" / "migration.log").read_text()
    events = [json.loads(line) for line in text.splitlines()]
    assert [e["original"] for e in events if e["event"] == "old_run_value_disabled"] == [original]
    names = [e["event"] for e in events]
    # A converted token: the Run value moves to the app at the end of step 3 (review 1a F1).
    assert names.index("token_protected") < names.index("old_run_value_disabled") < names.index("new_runtime_ready")
    assert TOKEN not in text and "token_dpapi" not in text
    assert winapp.read_settings(app) == {"agent_config": str(agent), "runtime_config": str(runtime)}
    assert managed.joinpath("current.json").exists()  # old files are left in place
    assert "pip" in result["notice"]


def test_conversion_then_not_ready_leaves_the_run_value_on_the_stub(home, tmp_path, windows, app):
    """Review 1a F1: the old Run value cannot read a converted token, so logon must start the app."""
    managed, agent, runtime, registry = managed_install(home, tmp_path)
    result = Migration(registry=registry, app_root=app, managed_root=managed).run(started(ok=False))
    assert result["status"] == "runtime_not_ready" and result["converted"] == [str(agent)]
    assert registry == {"RainCLI": winapp.run_value(app)}


@pytest.mark.skipif(os.name == "nt", reason="no conversion off Windows")
def test_nothing_converted_and_not_ready_leaves_the_run_value(home, tmp_path, app):
    managed, agent, runtime, registry = managed_install(home, tmp_path)
    original = dict(registry)
    result = Migration(registry=registry, app_root=app, managed_root=managed).run(started(ok=False))
    assert result["status"] == "runtime_not_ready" and result["converted"] == [] and registry == original
    result = Migration(registry=registry, app_root=app, managed_root=managed).run(started())
    assert result["run_value"] == "app" and registry == {"RainCLI": winapp.run_value(app)}


def test_running_managed_runtime_is_stopped_through_its_stop_request(home, tmp_path, windows, app):
    """15.8 M7: the launcher's stop request; migration waits for the queue lock."""
    managed, agent, runtime, registry = managed_install(home, tmp_path)
    state = cfg_dir(home) / "runtime-state"
    state.mkdir()
    atomic_write_json(state / "status.json", {"status": "running", "instance": "old-instance", "updated_at": 1})
    held = Queue(str(state))
    held.acquire_run_lock()
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if (state / "stop.json").exists():
            held.release_run_lock()  # the old runtime honours the request and exits
    messages = []
    result = Migration(registry=registry, app_root=app, managed_root=managed, sleep=sleep).run(
        started(), notify=messages.append)
    assert json.loads((state / "stop.json").read_text()) == {"instance": "old-instance"}
    # The only holder is the runtime its Run value restarts: stopped without the "old window" notice.
    assert result["status"] == "migrated" and sleeps and CLOSE_OLD not in messages


# -- shape 2: an old pip/venv client with a connector config ---------------------------------------

def test_pip_client_with_connector_omitting_agent_config(home, tmp_path, windows, app):
    agent = write_agent(cfg_dir(home) / "agent.json")
    connector = write_connector(cfg_dir(home) / "bob-connector.json", state_dir=str(tmp_path / "q"),
                                prompt_timeout=120)
    registry = Registry()
    result = Migration(registry=registry, app_root=app, managed_root=home / "none").run(started())
    assert result["status"] == "migrated" and result["mode"] == "connector"
    normalized = json.loads(connector.read_text())
    assert normalized["agent_config"] == str(agent)  # written explicitly (15.8 M8)
    assert normalized["prompt_timeout"] == 60  # clamped, and logged
    runtime = json.loads((cfg_dir(home) / "runtime.json").read_text())
    assert runtime["connectors"] == [str(connector)]
    assert load_runtime(cfg_dir(home) / "runtime.json")[2][0][2].token.reveal() == TOKEN
    log = (Path(runtime["state_dir"]) / "migration.log").read_text()
    assert "prompt_timeout_clamped" in log and TOKEN not in log
    assert result["run_value"] == "app" and registry == {"RainCLI": winapp.run_value(app)}


def test_pip_client_at_raincli_config_with_several_credentials(home, tmp_path, windows, app):
    other_dir = tmp_path / "elsewhere"
    agent = write_agent(other_dir / "agent.json")
    second = write_agent(other_dir / "second.json", token="rca_" + "C" * 43)
    write_connector(other_dir / "c1.json", agent_config="agent.json", state_dir=str(tmp_path / "q1"))
    write_connector(other_dir / "c2.json", agent_config="second.json", herdr_agent="other", state_dir=str(tmp_path / "q2"))
    # A legacy runtime with one connector per handle (14.8).
    (other_dir / "runtime.json").write_text(json.dumps({"connectors": ["c1.json", "c2.json"]}))
    env = {"RAINCLI_CONFIG": str(agent)}
    result = Migration(env=env, registry=Registry(), app_root=app, managed_root=home / "none").run(started())
    assert result["status"] == "migrated"
    assert sorted(result["converted"]) == sorted([str(agent), str(second)])
    for path, token in ((agent, TOKEN), (second, "rca_" + "C" * 43)):
        assert "token_dpapi" in json.loads(path.read_text()) and load_config(str(path)).token.reveal() == token


def test_foreground_connector_waits_and_can_be_cancelled(home, tmp_path, windows, app):
    agent = write_agent(cfg_dir(home) / "agent.json")
    write_connector(cfg_dir(home) / "connector.json", state_dir=str(tmp_path / "q"))
    (tmp_path / "q").mkdir()
    before = snapshot(agent, cfg_dir(home) / "connector.json")
    held = Queue(str(tmp_path / "q"))
    held.acquire_run_lock()  # `raincli connector run` in an old window
    messages, ticks = [], []
    try:
        result = Migration(registry=Registry(), app_root=app, managed_root=home / "none",
                           sleep=lambda s: ticks.append(s)).run(
            started(), notify=messages.append, cancelled=lambda: len(ticks) >= 3)
    finally:
        held.release_run_lock()
    assert result["status"] == "cancelled" and messages == [CLOSE_OLD]
    assert snapshot(agent, cfg_dir(home) / "connector.json") == before  # nothing touched
    assert not (cfg_dir(home) / "runtime.json").exists()


def test_invalid_result_aborts_with_nothing_changed(home, tmp_path, windows, app):
    agent = write_agent(cfg_dir(home) / "agent.json")
    connector = write_connector(cfg_dir(home) / "connector.json", herdr_agent="Not A Name")
    before = snapshot(agent, connector)
    with pytest.raises(ConfigError, match="nothing changed"):
        Migration(registry=Registry(), app_root=app, managed_root=home / "none").run(started())
    assert snapshot(agent, connector) == before
    assert sorted(p.name for p in cfg_dir(home).iterdir()) == sorted([".migration.lock", "agent.json",
                                                                      "connector.json"])


# -- shape 3: a pip client with no connector -----------------------------------------------------------

def test_pip_client_alone_becomes_machine_mode_keeping_its_handle(home, tmp_path, windows, app):
    agent = write_agent(cfg_dir(home) / "agent.json")
    result = Migration(registry=Registry(), app_root=app, managed_root=home / "none").run(started())
    assert result["status"] == "migrated" and result["mode"] == "machine"
    runtime = json.loads((cfg_dir(home) / "runtime.json").read_text())
    assert runtime["machine_config"] == str(agent)
    assert load_config(str(agent)).token.reveal() == TOKEN  # the same credential: no new machine


def test_nothing_to_migrate_and_idempotence(home, tmp_path, windows, app):
    assert Migration(registry=Registry(), app_root=app, managed_root=home / "none").run(started()) == \
        {"status": "nothing_to_migrate"}
    write_agent(cfg_dir(home) / "agent.json")
    first = Migration(registry=Registry(), app_root=app, managed_root=home / "none").run(started())
    runtime = (cfg_dir(home) / "runtime.json").read_bytes()
    second = Migration(registry=Registry(), app_root=app, managed_root=home / "none").run(started())
    assert first["converted"] and second["status"] == "migrated" and second["converted"] == []
    assert (cfg_dir(home) / "runtime.json").read_bytes() == runtime


def test_signed_in_machine_mode_is_kept_as_is(home, tmp_path, windows, app):
    agent = write_agent(cfg_dir(home) / "agent.json")
    runtime = cfg_dir(home) / "runtime.json"
    runtime.write_text(json.dumps({"machine_config": "agent.json", "state_dir": "runtime-state"}))
    before = runtime.read_bytes()
    assert Migration(registry=Registry(), app_root=app, managed_root=home / "none").run(started())["status"] == "migrated"
    assert runtime.read_bytes() == before


def test_migration_takes_a_lock(home, tmp_path, app):
    write_agent(cfg_dir(home) / "agent.json")
    from raincli_agent import filelock
    fd = os.open(cfg_dir(home) / ".migration.lock", os.O_RDWR | os.O_CREAT, 0o600)
    filelock.lock(fd)
    try:
        assert Migration(registry=Registry(), app_root=app, managed_root=home / "none").run(started())["status"] == "busy"
    finally:
        filelock.unlock(fd)
        os.close(fd)


@pytest.mark.skipif(os.name == "nt", reason="Linux behaviour")
def test_linux_keeps_plain_tokens(home, tmp_path):
    agent = write_agent(cfg_dir(home) / "agent.json")
    result = Migration(registry=Registry(), managed_root=home / "none").run()
    assert result["status"] == "migrated" and result["converted"] == [] and "notice" not in result
    assert json.loads(agent.read_text())["token"] == TOKEN


# -- review 1a ---------------------------------------------------------------------------------------

def test_relative_queue_state_dir_resolves_from_the_connector_config(home, tmp_path, windows, app):
    """F2: an old pip connector with "state_dir": "queue" holding its lock is waited for."""
    agent = write_agent(cfg_dir(home) / "agent.json")
    connector = write_connector(cfg_dir(home) / "connector.json", state_dir="queue")
    queue = cfg_dir(home) / "queue"
    queue.mkdir()
    before = snapshot(agent, connector)
    held = Queue(str(queue))
    held.acquire_run_lock()
    messages, ticks = [], []
    try:
        migration = Migration(registry=Registry(), app_root=app, managed_root=home / "none",
                              sleep=lambda s: ticks.append(s))
        assert any(Path(d) == queue for d in migration.held(migration.detect()))
        result = migration.run(started(), notify=messages.append, cancelled=lambda: len(ticks) >= 2)
    finally:
        held.release_run_lock()
    assert result["status"] == "cancelled" and messages == [CLOSE_OLD]
    assert snapshot(agent, connector) == before


def test_old_runtime_is_not_stopped_while_a_window_holds_a_queue(home, tmp_path, windows, app):
    """F4: a foreign holder means nothing is stopped; the wait ends with nothing changed."""
    managed, agent, runtime, registry = managed_install(home, tmp_path)
    state = cfg_dir(home) / "runtime-state"
    state.mkdir()
    atomic_write_json(state / "status.json", {"status": "running", "instance": "old", "updated_at": 1})
    window = tmp_path / "window-queue"
    window.mkdir()
    write_connector(cfg_dir(home) / "window.json", agent_config="agent.json", herdr_agent="other",
                    state_dir=str(window))
    locks = [Queue(str(state)), Queue(str(window))]
    for lock in locks:
        lock.acquire_run_lock()
    clock = {"t": 0}

    def sleep(seconds):
        clock["t"] += seconds
    try:
        result = Migration(registry=registry, app_root=app, managed_root=managed, sleep=sleep,
                           clock=lambda: clock["t"]).run(started(), wait=5)
    finally:
        for lock in locks:
            lock.release_run_lock()
    assert result["status"] == "waiting"
    assert not (state / "stop.json").exists()  # never asked to stop
    assert "token" in json.loads(agent.read_text())


def test_a_stopped_old_runtime_is_restarted_on_timeout(home, tmp_path, windows, app):
    """F4: stopped (the only holder), then it never released: started again from its Run value."""
    managed, agent, runtime, registry = managed_install(home, tmp_path)
    state = cfg_dir(home) / "runtime-state"
    state.mkdir()
    atomic_write_json(state / "status.json", {"status": "running", "instance": "old", "updated_at": 1})
    held = Queue(str(state))
    held.acquire_run_lock()
    spawned, clock = [], {"t": 0}

    def sleep(seconds):
        clock["t"] += seconds
    try:
        result = Migration(registry=registry, app_root=app, managed_root=managed, sleep=sleep,
                           clock=lambda: clock["t"], spawn=spawned.append).run(started(), wait=5)
    finally:
        held.release_run_lock()
    assert result["status"] == "waiting" and (state / "stop.json").exists()
    assert spawned == [["C:\\Python\\pythonw.exe", f"{managed}/launch.py", "runtime", "run", "--config", str(runtime)]]
    assert registry["RainCLI"].startswith('"C:')  # untouched


def test_the_apps_own_runtime_is_paused_without_a_notice(home, tmp_path, windows, app):
    """F10, F5: the tray's runtime already runs the config being migrated."""
    agent = write_agent(cfg_dir(home) / "agent.json")
    runtime = cfg_dir(home) / "runtime.json"
    runtime.write_text(json.dumps({"machine_config": "agent.json", "state_dir": "runtime-state"}))
    state = cfg_dir(home) / "runtime-state"
    state.mkdir()
    held = Queue(str(state))
    held.acquire_run_lock()
    events, messages = [], []

    def stop_own():
        events.append("paused")
        held.release_run_lock()
    result = Migration(registry=Registry(), app_root=app, managed_root=home / "none", own_runtime=str(runtime),
                       stop_own=stop_own, restart_own=lambda: events.append("resumed"),
                       own_running=lambda: "paused" not in events,
                       sleep=lambda s: None).run(started(), notify=messages.append)
    assert result["status"] == "migrated" and events == ["paused"] and CLOSE_OLD not in messages


def test_pending_skips_a_finished_migration(home, tmp_path, windows, app):
    write_agent(cfg_dir(home) / "agent.json")
    migration = Migration(registry=Registry(), app_root=app, managed_root=home / "none")
    assert migration.pending()
    migration.run(started())
    assert not Migration(registry=Registry(app and {"RainCLI": winapp.run_value(app)}), app_root=app,
                         managed_root=home / "none").pending()
    assert not Migration(registry=Registry(), app_root=app, managed_root=tmp_path / "none",
                         env={"RAINCLI_CONFIG": str(tmp_path / "absent" / "agent.json")}).pending()


def test_handle_with_an_inbox_role_asks_for_its_connector_config(home, tmp_path, windows, app):
    """F11: never silently machine mode for a handle that has delivered through a connector."""
    agent = write_agent(cfg_dir(home) / "agent.json")
    before = snapshot(agent)
    messages = []
    result = Migration(registry=Registry(), app_root=app, managed_root=home / "none",
                       inbox=lambda config: True).run(started(), notify=messages.append)
    assert result["status"] == "connector_config_required" and "--connector-config" in messages[0]
    assert snapshot(agent) == before and not (cfg_dir(home) / "runtime.json").exists()
    project = tmp_path / "project"
    project.mkdir()
    connector = write_connector(project / "inbox.json", agent_config=str(agent), state_dir="q")
    result = Migration(registry=Registry(), app_root=app, managed_root=home / "none", inbox=lambda config: True,
                       connector_configs=[connector]).run(started())
    assert result["status"] == "migrated" and result["mode"] == "connector"
    assert json.loads((cfg_dir(home) / "runtime.json").read_text())["connectors"] == [str(connector)]


def test_delivery_history_check_against_the_server(home, tmp_path, fake_api, app):
    """Review 2 R4: GET /me's delivery_history, not the live directory."""
    from raincli_agent.config import write_config
    agent = cfg_dir(home) / "agent.json"
    write_config(str(agent), fake_api.url, fake_api.alice)
    migration = Migration(registry=Registry(), app_root=app, managed_root=home / "none")
    assert migration.inbox_role(str(agent)) is False
    fake_api.state.delivered.add(fake_api.state.tokens[fake_api.alice])  # an inactive inbox: nothing live
    assert fake_api.state.directory.get("alice") is None
    assert migration.inbox_role(str(agent)) is True
    result = migration.run(started())
    assert result["status"] == "connector_config_required"
    log = (cfg_dir(home) / "runtime-state" / "migration.log").read_text()
    assert '"result": true' in log
    write_config(str(agent), "http://127.0.0.1:9", fake_api.alice, force=True)
    assert migration.inbox_role(str(agent)) is None  # offline: cannot tell, logged as null


def test_managed_launcher_on_the_apps_own_config_gets_its_stop_request(home, tmp_path, windows, app):
    """Review 2 R1 (e2e part C): the managed Run value and own_runtime name the same default
    runtime.json, and the tray's host is not running it, so the holder is the old
    launcher's runtime: it is asked to stop, with no "old window" notice, and migration finishes."""
    managed, agent, runtime, registry = managed_install(home, tmp_path)
    state = cfg_dir(home) / "runtime-state"
    state.mkdir()
    atomic_write_json(state / "status.json", {"status": "running", "instance": "launcher", "updated_at": 1})
    held = Queue(str(state))
    held.acquire_run_lock()
    events, messages = [], []

    def sleep(seconds):
        if (state / "stop.json").exists() and "released" not in events:
            events.append("released")
            held.release_run_lock()  # the old runtime honours its stop request
    migration = Migration(registry=registry, app_root=app, managed_root=managed, own_runtime=str(runtime),
                          stop_own=lambda: events.append("paused"), restart_own=lambda: events.append("resumed"),
                          own_running=lambda: False, sleep=sleep)
    assert migration.old_run_value() and migration.pending()
    result = migration.run(started(), notify=messages.append)
    assert json.loads((state / "stop.json").read_text()) == {"instance": "launcher"}
    assert result["status"] == "migrated" and events == ["released"] and CLOSE_OLD not in messages
    assert registry["RainCLI"] == winapp.run_value(app)


def test_tray_does_not_start_its_runtime_before_migrating_an_old_run_value(home, tmp_path, windows, app, monkeypatch):
    """Review 2 R1: with migration pending and an old Run value, the tray's host waits."""
    from raincli_agent.app import tray as tray_mod
    managed, agent, runtime, registry = managed_install(home, tmp_path)
    monkeypatch.setattr(winapp, "WindowsRegistry", lambda key=None: registry)
    import raincli_agent.migrate as migrate_mod
    monkeypatch.setattr(migrate_mod.Migration, "inbox_role", lambda self, config: None)

    class Host:
        def __init__(self):
            self.calls = []

        def start(self):
            self.calls.append("start")

        def running(self):
            return False

        resume = pause = lambda self: None

    fake = type("T", (), {})()
    fake.root_dir, fake.runtime_config, fake.agent_config = app, str(runtime), str(agent)
    fake.host = Host()
    fake.signed_in = lambda: True
    fake.background = lambda fn, done=None: None  # the migration itself is not run here
    fake.post = lambda *a: None
    fake.migrating = False
    tray_mod.Tray.first_run.__get__(fake)()
    assert fake.host.calls == [] and fake.migrating
    registry["RainCLI"] = winapp.run_value(app)  # no old Run value: the runtime starts first
    fake.migrating = False
    tray_mod.Tray.first_run.__get__(fake)()
    assert fake.host.calls == ["start"]


def test_repoints_after_an_interrupted_conversion(home, tmp_path, windows, app):
    """Review 2 R2: converted earlier, not repointed; a later run that converts nothing and
    is not ready still moves the Run value to the app."""
    managed, agent, runtime, registry = managed_install(home, tmp_path)
    from raincli_agent.config import load_config as load, stored_form
    cfg = load(str(agent))
    atomic_write_json(agent, stored_form(cfg.api_url, cfg.token))  # the earlier run's conversion
    original = registry["RainCLI"]
    result = Migration(registry=registry, app_root=app, managed_root=managed).run(started(ok=False))
    assert result["status"] == "runtime_not_ready" and result["converted"] == []
    assert registry == {"RainCLI": winapp.run_value(app)}
    log = (cfg_dir(home) / "runtime-state" / "migration.log").read_text()
    assert json.dumps(original)[1:-1] in log
