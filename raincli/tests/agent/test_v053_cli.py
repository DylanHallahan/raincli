"""v0.5.3 client hardening (protocol §18.2): the minimum safe version and input validation."""
import json
from pathlib import Path

import pytest

from raincli_agent import __version__, cli, security
from raincli_agent.errors import ConfigError
from raincli_agent.runtime import launcher, pushed as pushed_mod, updates, winapp

from .test_winapp import app  # noqa: F401 - the app-install fixture


def test_the_release_carries_its_floor():
    assert security.MIN_SAFE_VERSION == "0.5.3" and security.MIN_SAFE == (0, 5, 3)
    assert launcher.MIN_SAFE == security.MIN_SAFE  # the standalone launcher's copy
    assert updates.version_key(__version__) >= security.MIN_SAFE


# -- pushed targets ------------------------------------------------------------------------------

def driver(tmp_path, state_dir=None):
    d = pushed_mod.PushedUpdates(root=tmp_path / "root", python=tmp_path / "py")
    d.managed = lambda: True
    d.mode = lambda: "automatic"
    d.state_dir = state_dir
    return d


@pytest.mark.parametrize("machine_mode", [False, True])
def test_a_pushed_target_below_the_floor_is_refused(tmp_path, machine_mode):
    d = driver(tmp_path)
    d.machine_mode = machine_mode
    d.consider({"version": "v0.5.2", "allow_downgrade": True, "set_at": "t"})
    assert (d.data["state"], d.data["error"]) == ("failed", "below_min_safe")
    assert d.client()["error"] == "below_min_safe"


def test_the_app_refuses_a_pushed_target_below_the_floor(tmp_path, app):  # noqa: F811
    d = pushed_mod.PushedUpdates(app_root=app)
    d.managed = lambda: True
    d.mode = lambda: "automatic"
    d.consider({"version": "v0.5.2", "allow_downgrade": True, "set_at": "t"})
    assert d.data["error"] == "below_min_safe"


def test_the_ratchet_raises_the_floor_and_survives_a_reinstall(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / security.RATCHET_FILE).write_text(json.dumps({"min_safe": "0.5.9"}))  # a later release ran here
    assert security.effective_min(state) == (0, 5, 9)
    d = driver(tmp_path, state)
    d.consider({"version": "v0.5.5", "allow_downgrade": True, "set_at": "t"})
    assert d.data["error"] == "below_min_safe"
    assert security.record(state) == (0, 5, 9)  # recording never lowers it
    assert json.loads((state / security.RATCHET_FILE).read_text()) == {"min_safe": "0.5.9"}
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert security.record(fresh) == security.MIN_SAFE
    # A reinstall of this release reads the same record again.
    again = driver(tmp_path, state)
    again.consider({"version": "v0.5.5", "allow_downgrade": True, "set_at": "t2"})
    assert again.data["error"] == "below_min_safe"


def test_the_runtime_records_the_ratchet(tmp_path, fake_api):
    from raincli_agent.config import write_config
    from raincli_agent.runtime.service import run
    agent = tmp_path / "agent.json"
    write_config(str(agent), fake_api.url, fake_api.alice)
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"machine_config": "agent.json", "state_dir": "state"}))
    run(runtime, once=True, pushed=pushed_mod.PushedUpdates(root=tmp_path / "unmanaged", python=tmp_path / "py"))
    assert json.loads((tmp_path / "state" / security.RATCHET_FILE).read_text()) == {"min_safe": "0.5.3"}


# -- rollbacks -----------------------------------------------------------------------------------

def managed_install(tmp_path, previous_tag):
    python = tmp_path / "versions" / "old" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    from raincli_agent.fsutil import atomic_write_json
    atomic_write_json(tmp_path / "current.json", {"tag": "v0.5.3", "commit": "b" * 40, "python": "x",
                                                  "previous": {"tag": previous_tag, "commit": "a" * 40,
                                                               "python": str(python)}})
    return tmp_path


def test_an_explicit_rollback_below_the_floor_needs_allow_insecure(tmp_path):
    root = managed_install(tmp_path, "v0.5.2")
    with pytest.raises(ConfigError, match="below_min_safe"):
        updates.configure(root, rollback=True)
    assert json.loads((root / "current.json").read_text())["tag"] == "v0.5.3"
    result = updates.configure(root, rollback=True, allow_insecure=True)
    assert result["tag"] == "v0.5.2" and "older than v0.5.3" in result["warning"]


def test_a_rollback_at_or_above_the_floor_gives_no_warning(tmp_path):
    root = managed_install(tmp_path, "v0.5.3")
    assert "warning" not in updates.configure(root, rollback=True)


def test_the_cli_flag_is_rollback_only_and_never_for_the_app(monkeypatch, capsys, tmp_path):
    assert cli.main(["runtime", "update", "--manual", "--allow-insecure"]) == 2
    monkeypatch.setattr(winapp, "app_root", lambda *a, **k: tmp_path)
    assert cli.main(["runtime", "update", "--rollback", "--allow-insecure"]) == 2
    assert "never rolls back below" in capsys.readouterr().err


def test_the_cli_override_warns(tmp_path, capsys):
    root = managed_install(tmp_path, "v0.5.2")
    assert cli.main(["runtime", "update", "--root", str(root), "--rollback"]) == 1
    assert "below_min_safe" in capsys.readouterr().err
    assert cli.main(["runtime", "update", "--root", str(root), "--rollback", "--allow-insecure"]) == 0
    assert "warning: this version is older than v0.5.3" in capsys.readouterr().err


def test_the_apps_explicit_rollback_has_no_override(app):  # noqa: F811
    for version in ("0.5.2", "0.5.3"):
        d = app / "versions" / version
        d.mkdir(exist_ok=True)
        (d / winapp.APP_EXE).write_text("exe")
    winapp.write_install(app, "0.5.3", "0.5.2")
    with pytest.raises(ConfigError, match="below_min_safe"):
        winapp.configure(app, rollback=True)
    assert winapp.read_install(app)["current"] == "0.5.3"


def test_a_failed_update_goes_back_to_the_version_it_replaced(app):  # noqa: F811
    """0.5.2 -> 0.5.3 crashes in probation: the stub restores 0.5.2, recorded as below the floor."""
    for version in ("0.5.2", "0.5.3"):
        d = app / "versions" / version
        d.mkdir(exist_ok=True)
        (d / winapp.APP_EXE).write_text("exe")
    winapp.write_install(app, "0.5.3", "0.5.2", probation="0.5.3")
    assert winapp.rollback(app, "0.5.3") is True
    assert winapp.read_install(app)["current"] == "0.5.2"
    assert updates.read_update_state(app)["error"] == "rolled_back_below_min_safe"


# -- §18.2 item 4: validation -----------------------------------------------------------------------

def test_agent_names_never_start_with_a_dash():
    from raincli_agent.connector.runner import message_target
    from raincli_agent.runtime import sessions
    base = {"to_endpoint": {"machine": "bob"}}
    for name in ("-x", "--help", "-", "- spaced"):
        assert message_target({**base, "to_endpoint": {"machine": "bob", "agent": name}}) is False
        assert not sessions.normalize_name(name, "claude").startswith("-")
    assert sessions.normalize_name("--", "claude") == "claude"
    assert sessions.normalize_name("-proj", "claude") == "proj"
    assert message_target({**base, "to_endpoint": {"machine": "bob", "agent": "re-viewer"}}) == "re-viewer"


def test_server_ids_and_handles_must_match_exactly():
    from raincli_agent.connector.runner import message_problem
    good = {"id": "0b7f3c1e-2a4d-4c6e-9f10-1a2b3c4d5e6f", "from": "alice", "body": "x", "seq": 1,
            "to_endpoint": {"machine": "bob"}}
    assert message_problem(good) is None
    for mid in ("0B7F3C1E-2A4D-4C6E-9F10-1A2B3C4D5E6F", "{0b7f3c1e-2a4d-4c6e-9f10-1a2b3c4d5e6f}",
                "urn:uuid:0b7f3c1e-2a4d-4c6e-9f10-1a2b3c4d5e6f", "0b7f3c1e2a4d4c6e9f101a2b3c4d5e6f",
                "0b7f3c1e-2a4d-4c6e-9f10-1a2b3c4d5e6f\n"):
        assert message_problem({**good, "id": mid}) == "bad id", mid
    for sender in ("alice\n", "Alice", "-alice"):
        assert message_problem({**good, "from": sender}) == "bad sender", sender


def test_config_sender_lists_use_whole_string_matches(tmp_path):
    from raincli_agent.connector.config import load_connector_config
    path = tmp_path / "c.json"
    for key in ("trusted_senders", "blocked_senders"):
        path.write_text(json.dumps({"herdr_agent": "bob-claude", key: ["alice\n"]}))
        with pytest.raises(ConfigError):
            load_connector_config(str(path))
    from raincli_agent import login
    with pytest.raises(login.InvalidRequest):
        login.check_machine_name("work-pc\n")


def test_a_returned_message_must_carry_the_requested_id(fake_api):
    from raincli_agent.api import ApiClient
    from raincli_agent.errors import BadResponse
    from .conftest import send
    msg = send(fake_api, fake_api.alice, "bob", "x")
    api = ApiClient(fake_api.url, fake_api.bob, max_attempts=1)
    assert api.get_message(msg["id"].upper())["id"] == msg["id"]  # the request is canonicalised
    for wrong in ("C:", "NUL", "x:y", msg["id"].upper(), "0b7f3c1e-2a4d-4c6e-9f10-1a2b3c4d5e6f"):
        fake_api.state.messages[msg["id"]]["id"] = wrong
        with pytest.raises(BadResponse):
            api.get_message(msg["id"])
    fake_api.state.messages[msg["id"]]["id"] = msg["id"]


def test_fetch_folders_refuse_drive_stream_and_device_names(tmp_path):
    from raincli_agent import attachments as att
    for bad in ("C:", "x:y", "NUL", "con.txt", "folder.", "folder "):
        with pytest.raises(att.AttachmentError):
            att.prepare_dir(str(tmp_path), "raincli-attachments", bad)
    assert att.prepare_dir(str(tmp_path), "raincli-attachments", "0b7f3c1e-2a4d-4c6e-9f10-1a2b3c4d5e6f")


def test_only_fixed_codes_reach_the_server(fake_api, connector_env):
    """A Herdr failure's own text (paths, session names) stays local; the event carries a code."""
    from raincli_agent.connector import queue as q
    from raincli_agent.connector.herdr import HerdrError
    from raincli_agent.connector.runner import report_code
    from .conftest import send
    msg = send(fake_api, fake_api.alice, "bob", "hello")
    connector_env.herdr.prompt_results = [HerdrError("herdr agent prompt failed (x): /home/bob/secret-project pane w9:p1")]
    conn = connector_env.connector()
    conn.run_once()
    record = conn.queue.get(msg["id"])
    assert record["state"] == q.UNCERTAIN and "secret-project" in record["detail"]  # kept locally
    events = [(s, d) for m, s, d in fake_api.state.events if m == msg["id"]]
    assert events == [("submission_uncertain", "herdr_failed")]
    assert report_code({"state": q.SUBMITTED, "code": "bad code with spaces"}) == "delivered"
    assert report_code({"state": q.REJECTED}) == "rejected"
