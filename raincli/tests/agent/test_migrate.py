"""Migration of existing installs (15.6 amended by 15.8 H6, M7, M8, L4)."""
import json
import os
from pathlib import Path

import pytest

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
    assert events[-1]["event"] == "old_run_value_disabled"  # after new_runtime_ready (15.8 H6)
    assert events[-2]["event"] == "new_runtime_ready"
    assert TOKEN not in text and "token_dpapi" not in text
    assert winapp.read_settings(app) == {"agent_config": str(agent), "runtime_config": str(runtime)}
    assert managed.joinpath("current.json").exists()  # old files are left in place
    assert "pip" in result["notice"]


def test_run_value_untouched_when_new_runtime_is_not_ready(home, tmp_path, windows, app):
    managed, agent, runtime, registry = managed_install(home, tmp_path)
    original = dict(registry)
    result = Migration(registry=registry, app_root=app, managed_root=managed).run(started(ok=False))
    assert result["status"] == "runtime_not_ready" and registry == original


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
    assert result["status"] == "migrated" and sleeps and CLOSE_OLD in messages


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
    assert result["run_value"] == "none" and registry == {}


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
