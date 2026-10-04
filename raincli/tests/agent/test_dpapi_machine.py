"""DPAPI credential storage (15.3, 15.8 L3) and machine mode (15.4, 15.8 H8)."""
import base64
import json
import os
from pathlib import Path

import pytest

from raincli_agent import config as config_mod, dpapi
from raincli_agent.config import load_config, write_config
from raincli_agent.errors import ConfigError
from raincli_agent.runtime import pushed as pushed_mod
from raincli_agent.runtime.service import load_runtime, machine_mode, run, status


class FakeDpapi:
    """Stands in for CryptProtectData: a blob bound to one user and computer."""

    def __init__(self, owner="alice@pc1"):
        self.owner = owner
        self.calls = []

    def protect(self, data):
        self.calls.append(("protect", len(data)))
        return b"DPAPI|" + self.owner.encode() + b"|" + bytes(b ^ 0x5A for b in data)

    def unprotect(self, blob):
        prefix = b"DPAPI|" + self.owner.encode() + b"|"
        if not blob.startswith(prefix):
            raise dpapi.DpapiError(dpapi.FOREIGN)
        return bytes(b ^ 0x5A for b in blob[len(prefix):])


@pytest.fixture
def windows_store(monkeypatch):
    backend = FakeDpapi()
    dpapi.set_backend(backend)
    monkeypatch.setattr(config_mod, "protects_tokens", lambda: True)
    yield backend
    dpapi.set_backend(None)


TOKEN = "rca_" + "A" * 43


def test_windows_writes_only_token_dpapi_and_round_trips(tmp_path, windows_store):
    path = tmp_path / "agent.json"
    write_config(str(path), "https://raincli.example", TOKEN)
    data = json.loads(path.read_text())
    assert set(data) == {"api_url", "token_dpapi"} and TOKEN not in path.read_text()
    assert base64.b64decode(data["token_dpapi"]).startswith(b"DPAPI|")
    assert load_config(str(path)).token.reveal() == TOKEN
    assert ("protect", len(TOKEN)) in windows_store.calls


def test_load_accepts_plain_token_too(tmp_path, windows_store):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"api_url": "https://raincli.example", "token": TOKEN}))
    os.chmod(path, 0o600)
    assert load_config(str(path)).token.reveal() == TOKEN


def test_foreign_blob_says_sign_in_again(tmp_path, windows_store):
    path = tmp_path / "agent.json"
    write_config(str(path), "https://raincli.example", TOKEN)
    dpapi.set_backend(FakeDpapi(owner="bob@pc2"))
    with pytest.raises(ConfigError, match="sign in again"):
        load_config(str(path))


def test_damaged_blob_and_both_forms_are_refused(tmp_path, windows_store):
    path = tmp_path / "agent.json"
    for data in ({"api_url": "https://raincli.example", "token_dpapi": "not base64!"},
                 {"api_url": "https://raincli.example", "token_dpapi": "QUJD", "token": TOKEN}):
        path.write_text(json.dumps(data))
        os.chmod(path, 0o600)
        with pytest.raises(ConfigError):
            load_config(str(path))


@pytest.mark.skipif(os.name == "nt", reason="non-Windows behaviour")
def test_non_windows_load_of_token_dpapi_is_a_clear_error(tmp_path):
    dpapi.set_backend(None)
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"api_url": "https://raincli.example", "token_dpapi": "QUJD"}))
    os.chmod(path, 0o600)
    with pytest.raises(ConfigError, match="only be read on Windows"):
        load_config(str(path))


@pytest.mark.skipif(os.name == "nt", reason="non-Windows behaviour")
def test_linux_and_macos_writes_are_unchanged(tmp_path):
    path = tmp_path / "agent.json"
    write_config(str(path), "https://raincli.example", TOKEN)
    assert json.loads(path.read_text()) == {"api_url": "https://raincli.example", "token": TOKEN}


def test_windows_backend_uses_ui_forbidden_and_entropy():
    assert dpapi.CRYPTPROTECT_UI_FORBIDDEN == 1 and dpapi.ENTROPY == b"raincli-agent-v1"


# -- machine mode -------------------------------------------------------------------------------

def machine_setup(tmp_path, fake_api, token):
    agent = tmp_path / "agent.json"
    write_config(str(agent), fake_api.url, token)
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"machine_config": "agent.json", "state_dir": "state"}))
    return agent, runtime


def test_runtime_config_needs_exactly_one_mode(tmp_path, fake_api):
    agent, runtime = machine_setup(tmp_path, fake_api, fake_api.alice)
    path, state, configs = load_runtime(runtime)
    assert machine_mode(configs) and configs[0][0] == str(agent) and state == tmp_path / "state"
    for bad in ({"machine_config": "agent.json", "connectors": ["c.json"]}, {"state_dir": "x"},
                {"machine_config": ""}, {"connectors": []}, {"machine_config": "agent.json", "extra": 1}):
        runtime.write_text(json.dumps(bad))
        with pytest.raises(ConfigError):
            load_runtime(runtime)


def test_machine_mode_publishes_ready_client_and_directory_without_inbox(tmp_path, fake_api, monkeypatch):
    agent, runtime = machine_setup(tmp_path, fake_api, fake_api.alice)
    hooked = tmp_path / "state"
    from raincli_agent.runtime import sessions
    hooked.mkdir(mode=0o700)
    salt = sessions.ensure_salt(str(hooked))
    sessions.sessions_dir(str(hooked), create=True)
    record = {"key": sessions.agent_key(salt, "claude:s1"), "type": "claude", "name": "project",
              "status": "working", "updated_at": __import__("time").time()}
    from raincli_agent.fsutil import atomic_write_json
    atomic_write_json(Path(sessions.sessions_dir(str(hooked))) / (record["key"] + ".json"), record)
    driver = pushed_mod.PushedUpdates(root=tmp_path / "not-managed", python=tmp_path / "python")
    run(runtime, once=True, pushed=driver)
    bodies = [b for h, b in fake_api.state.presence_bodies if h == "alice"]
    first, last = bodies[0], bodies[-1]
    assert first["status"] == "ready" and first["client"]["update_mode"] == "manual"
    assert all(a["role"] is None for a in first["agents"])
    assert ("project", "claude", "working") in [(a["name"], a["type"], a["status"]) for a in first["agents"]]
    assert last == {"status": "offline", "agents": []}
    record = status(runtime)
    assert record["status"] == "stopped"
    assert driver.machine_mode is True


def test_machine_mode_refuses_targets_below_v040(tmp_path):
    driver = pushed_mod.PushedUpdates(root=tmp_path, python=tmp_path / "py")
    driver.managed = lambda: True
    driver.mode = lambda: "automatic"
    driver.machine_mode = True
    driver.consider({"version": "v0.3.9", "allow_downgrade": True, "set_at": "t"})
    assert driver.data["error"] == "target_below_minimum"
    driver.machine_mode = False
    assert driver.floor() == (0, 3, 0)


def test_launcher_rollback_refuses_previous_below_v040_in_machine_mode(tmp_path):
    from raincli_agent.runtime import launcher
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"machine_config": "agent.json"}))
    assert launcher.machine_mode(str(runtime))
    python = tmp_path / "versions" / "old" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    pointer = {"tag": "v0.4.0", "commit": "b" * 40, "python": "x",
               "previous": {"tag": "v0.3.2", "commit": "a" * 40, "python": str(python)}}
    (tmp_path / "current.json").write_text(json.dumps(pointer))
    (tmp_path / "update-state.json").write_text(json.dumps({"state": "updating", "target": {"version": "v0.4.0"}}))
    assert launcher.roll_back(tmp_path, pointer, timeout=0, floor=launcher.MACHINE_FLOOR) is False
    assert json.loads((tmp_path / "current.json").read_text())["tag"] == "v0.4.0"


def test_explicit_rollback_floor(tmp_path):
    from raincli_agent.runtime import updates
    python = tmp_path / "versions" / "old" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    from raincli_agent.fsutil import atomic_write_json
    atomic_write_json(tmp_path / "current.json", {"tag": "v0.4.0", "commit": "b" * 40, "python": "x",
                                                  "previous": {"tag": "v0.3.2", "commit": "a" * 40,
                                                               "python": str(python)}})
    with pytest.raises(ConfigError, match="below v0.4.0"):
        updates.configure(tmp_path, rollback=True, floor=(0, 4, 0))


def test_startup_digest_and_unit_for_machine_mode(tmp_path, fake_api):
    from raincli_agent.runtime import startup
    _, runtime = machine_setup(tmp_path, fake_api, fake_api.alice)
    assert len(startup.config_digest(runtime)) == 64
    assert str(runtime.resolve()) in startup.systemd_unit(runtime)


def test_machine_config_change_retires_and_republishes(tmp_path, fake_api):
    from raincli_agent.runtime.service import MachineWorker, load_machine
    agent, _ = machine_setup(tmp_path, fake_api, fake_api.alice)
    path, _, identity, binding = load_machine(agent)
    worker = MachineWorker(path, identity, tmp_path, binding)
    assert worker.tick(0)["status"] == "ready"
    write_config(str(agent), fake_api.url, fake_api.bob, force=True)
    report = worker.tick(1)
    assert report["error"] == "config_changed" and worker.retired
    assert fake_api.state.presence["alice"] == "offline"  # the old credential reported offline
    assert "bob" not in fake_api.state.presence
