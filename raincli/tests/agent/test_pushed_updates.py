"""Pushed updates and update modes (protocol 14.5, 14.7 H2/M9/L11)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from raincli_agent import __version__
from raincli_agent.config import write_config
from raincli_agent.connector.herdr import FakeHerdr
from raincli_agent.errors import ConfigError
from raincli_agent.fsutil import atomic_write_json
from raincli_agent.runtime import launcher, pushed, service, sessions, updates
from raincli_agent.runtime.service import Worker, load_bound

CURRENT = "v" + __version__
NEWER = "v%d.%d.%d" % (updates.version_key(CURRENT)[0], updates.version_key(CURRENT)[1] + 1, 0)
OLDER_OK = "v0.3.0" if CURRENT != "v0.3.0" else None


def managed_root(tmp_path, pointer=None):
    root = tmp_path / "client"
    python = root / "versions/current/venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    (root / "launch.py").write_text("")
    atomic_write_json(root / "current.json", pointer or {"tag": CURRENT, "commit": "a" * 40, "python": str(python),
                                                         "automatic": False, "update_mode": "automatic",
                                                         "update_mode_chosen": True})
    return root, python


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def controller(tmp_path, resolve=None, install=None, python=None, clock=None):
    root, venv_python = managed_root(tmp_path)
    calls = []

    def fake_install(release):
        calls.append(release)
        return {"status": "installed", **release}
    updater = pushed.PushedUpdates(root, python=python or venv_python, clock=clock or Clock(),
                                   resolve=resolve or (lambda tag: {"tag": tag, "commit": "b" * 40}),
                                   install=install or fake_install, log=lambda text: None)
    updater.calls = calls
    return updater


def settle(updater):
    if updater.thread is not None:
        updater.thread.join(timeout=10)


def target(version, allow_downgrade=False, **extra):
    return {"version": version, "allow_downgrade": allow_downgrade, **extra}


def test_upgrade_target_installs_at_once_and_reports_updating(tmp_path):
    updater = controller(tmp_path)
    assert updater.client() == {"version": __version__, "update_mode": "automatic", "update_state": "current",
                                "error": None}
    updater.consider(target(NEWER))
    settle(updater)
    assert updater.calls == [{"tag": NEWER, "commit": "b" * 40}]
    assert updater.client()["update_state"] == "updating"
    saved = updates.read_update_state(updater.root)
    assert saved["state"] == "updating" and saved["target"]["version"] == NEWER
    updater.consider(target(NEWER))  # the launcher is handing over: not installed twice
    settle(updater)
    assert len(updater.calls) == 1 or updater.calls[-1]["tag"] == NEWER


def test_same_version_target_is_current(tmp_path):
    updater = controller(tmp_path)
    updater.consider(target(CURRENT))
    updater.consider(target(__version__.join(["v", ""])))
    assert updater.calls == [] and updater.client()["update_state"] == "current"


@pytest.mark.skipif(OLDER_OK is None, reason="needs a target-aware version below the current one")
def test_downgrade_needs_the_flag(tmp_path):
    updater = controller(tmp_path)
    updater.consider(target(OLDER_OK))
    assert updater.calls == []
    assert (updater.client()["update_state"], updater.client()["error"]) == ("failed", "downgrade_not_allowed")
    updater.consider(target(OLDER_OK, allow_downgrade=True))  # a new target row: acted on at once
    settle(updater)
    assert [c["tag"] for c in updater.calls] == [OLDER_OK]


def test_downgrade_with_flag_from_a_newer_client(tmp_path, monkeypatch):
    monkeypatch.setattr(pushed, "__version__", "0.9.0")
    updater = controller(tmp_path)
    updater.consider(target("v0.4.0"))
    assert updater.calls == [] and updater.client()["error"] == "downgrade_not_allowed"
    updater.consider(target("v0.4.0", allow_downgrade=True))
    settle(updater)
    assert [c["tag"] for c in updater.calls] == ["v0.4.0"]


@pytest.mark.parametrize("bad", [
    {"version": "0.4.0", "allow_downgrade": False}, {"version": "v1.2", "allow_downgrade": False},
    {"version": "v1.2.3", "allow_downgrade": "yes"}, {"version": "v١.2.3", "allow_downgrade": False},
    {"version": "v1.2.3", "allow_downgrade": False, "url": "https://evil.example/raincli.zip"},
    "v1.2.3", ["v1.2.3"],
])
def test_bad_targets_are_refused_and_never_name_a_source(tmp_path, bad):
    resolved = []
    updater = controller(tmp_path, resolve=lambda tag: resolved.append(tag) or {"tag": tag, "commit": "b" * 40})
    updater.consider(bad)
    settle(updater)
    if isinstance(bad, dict) and bad.get("version") == "v1.2.3" and bad.get("allow_downgrade") is False:
        # Extra keys are ignored: only the version is used, and the source stays fixed.
        assert resolved == ["v1.2.3"]
    else:
        assert updater.calls == [] and updater.client()["error"] == "bad_target"


def test_target_below_first_target_aware_version_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(pushed, "__version__", "0.3.1")
    updater = controller(tmp_path)
    updater.consider(target("v0.2.0", allow_downgrade=True))
    assert updater.calls == [] and updater.client()["error"] == "target_below_minimum"


def test_network_failure_backs_off_and_a_changed_target_goes_at_once(tmp_path):
    clock = Clock()
    attempts = []

    def failing(release):
        attempts.append(release["tag"])
        raise updates.NetworkError("update request failed: timed out")
    updater = controller(tmp_path, install=failing, clock=clock)
    updater.consider(target(NEWER))
    settle(updater)
    assert (updater.client()["update_state"], updater.client()["error"]) == ("failed", "network")
    for delay in (299, 1):  # 5 minutes
        updater.consider(target(NEWER))
        settle(updater)
        clock.now += delay
    assert attempts == [NEWER]
    updater.consider(target(NEWER))
    settle(updater)
    assert attempts == [NEWER, NEWER]
    clock.now += 599  # doubled: 10 minutes
    updater.consider(target(NEWER))
    settle(updater)
    assert len(attempts) == 2
    updater.consider(target(NEWER, set_at="2026-09-30T12:00:00Z"))  # the operator set it again
    settle(updater)
    assert len(attempts) == 3
    for _ in range(12):
        updates_state = updates.read_update_state(updater.root)
        assert updates_state["next_try_at"] - clock.now <= pushed.BACKOFF_MAX
        updater._fail("network", network=True)


def test_verification_failure_is_not_retried_until_the_target_row_changes(tmp_path):
    attempts = []

    def bad_archive(release):
        attempts.append(release["tag"])
        raise updates.VerificationError("release archive does not match the resolved commit")
    clock = Clock()
    updater = controller(tmp_path, install=bad_archive, clock=clock)
    first = target(NEWER, set_at="2026-09-30T10:00:00Z")
    updater.consider(first)
    settle(updater)
    assert (updater.client()["update_state"], updater.client()["error"]) == ("failed", "verification_failed")
    clock.now += 10 * 24 * 3600
    updater.consider(first)
    settle(updater)
    assert attempts == [NEWER]
    updater.consider(target(NEWER, set_at="2026-10-01T10:00:00Z"))
    settle(updater)
    assert attempts == [NEWER, NEWER]


def test_rolled_back_target_waits_for_a_new_target_row(tmp_path):
    updater = controller(tmp_path)
    key = pushed.target_key(target(NEWER, set_at="t1"))
    updates.write_update_state(updater.root, {"state": "rolled_back", "error": "first_start_failed",
                                              "target": key, "blocked": key})
    restarted = pushed.PushedUpdates(updater.root, python=updater.python, clock=Clock(),
                                     resolve=lambda tag: {"tag": tag, "commit": "c" * 40},
                                     install=updater.install, log=lambda t: None)
    assert (restarted.client()["update_state"], restarted.client()["error"]) == ("rolled_back", "first_start_failed")
    restarted.consider(target(NEWER, set_at="t1"))
    settle(restarted)
    assert updater.calls == []
    restarted.consider(target(NEWER, set_at="t2"))
    settle(restarted)
    assert [c["tag"] for c in updater.calls] == [NEWER]
    assert updates.update_mode(updater.root) == "automatic"  # an automatic rollback never changes the mode


def test_new_version_reports_current_after_its_first_tick(tmp_path, monkeypatch):
    updater = controller(tmp_path)
    updates.write_update_state(updater.root, {"state": "updating", "error": None,
                                              "target": pushed.target_key(target(CURRENT))})
    fresh = pushed.PushedUpdates(updater.root, python=updater.python, log=lambda t: None)
    assert fresh.client()["update_state"] == "updating"
    fresh.started()
    assert fresh.client()["update_state"] == "current"
    assert updates.read_update_state(updater.root)["state"] == "current"


def test_interrupted_install_of_another_version_is_retried_later(tmp_path):
    updater = controller(tmp_path)
    updates.write_update_state(updater.root, {"state": "updating", "target": pushed.target_key(target(NEWER))})
    fresh = pushed.PushedUpdates(updater.root, python=updater.python, log=lambda t: None)
    assert (fresh.client()["update_state"], fresh.client()["error"]) == ("failed", "interrupted")


def test_manual_mode_and_unmanaged_installs_never_install(tmp_path):
    updater = controller(tmp_path)
    updates.configure(updater.root, mode="manual")
    updater.consider(target(NEWER))
    assert updater.calls == [] and updater.client()["update_mode"] == "manual"
    unmanaged = controller(tmp_path / "other", python=sys.executable)
    unmanaged.consider(target(NEWER))
    assert unmanaged.calls == [] and unmanaged.client()["update_mode"] == "manual"
    assert not (unmanaged.root / "update-state.json").exists()


def test_cleared_target_resets_the_state(tmp_path):
    updater = controller(tmp_path)
    updater.consider(target(NEWER, allow_downgrade=False, set_at="x"))
    settle(updater)
    updater.consider(None)
    assert updater.client()["update_state"] == "current" and updater.client()["error"] is None


@pytest.mark.parametrize("raw, code", [
    ("network", "network"), ("install_failed:CalledProcessError", "install_failed:calledprocesserror"),
    ("Has spaces /home/me/secret", "has_spaces__home_me_secret"), (None, None), ("x" * 99, "x" * 64),
])
def test_client_error_is_a_code_only(raw, code):
    assert pushed.error_code(raw) == code
    if code:
        assert pushed.ERROR_RE.fullmatch(code)


def test_resolve_requires_the_exact_stable_tag(monkeypatch):
    replies = {}

    def fetch(url, limit):
        for suffix, body in replies.items():
            if url.endswith(suffix):
                return json.dumps(body).encode()
        raise updates.ReleaseNotFound("release not found")
    monkeypatch.setattr(updates, "fetch", fetch)
    replies["/commits/v0.4.0"] = {"sha": "d" * 40}
    replies["/releases/tags/v0.4.0"] = {"tag_name": "v0.4.0", "draft": False, "prerelease": False}
    assert updates.resolve("v0.4.0") == {"tag": "v0.4.0", "commit": "d" * 40}
    assert all(url.startswith(updates.API) for url in [updates.API + "/releases/tags/v0.4.0"])
    for release in ({"tag_name": "v0.4.0", "draft": True}, {"tag_name": "v0.4.0", "prerelease": True},
                    {"tag_name": "v0.4.1", "draft": False, "prerelease": False}):
        replies["/releases/tags/v0.4.0"] = release
        with pytest.raises(updates.VerificationError):
            updates.resolve("v0.4.0")
    with pytest.raises(updates.VerificationError):
        updates.resolve("main")
    with pytest.raises(updates.ReleaseNotFound):
        updates.resolve("v9.9.9")


# -- update mode persistence ---------------------------------------------------------

def test_v020_unchosen_automatic_false_is_flipped_once_with_a_notice(tmp_path):
    root, python = managed_root(tmp_path, {"tag": "v0.2.0", "commit": "a" * 40, "python": "p", "automatic": False})
    notice = updates.migrate_mode(root)
    assert notice and "raincli runtime update --manual" in notice
    pointer = updates.read_pointer(root)
    assert (pointer["automatic"], pointer["update_mode"], pointer["update_mode_chosen"]) == (False, "automatic", True)
    assert updates.migrate_mode(root) is None
    updates.configure(root, mode="manual")
    assert updates.migrate_mode(root) is None and updates.update_mode(root) == "manual"


def test_v020_opted_in_pointer_stays_automatic_without_notice(tmp_path):
    root, _ = managed_root(tmp_path, {"tag": "v0.2.0", "commit": "a" * 40, "python": "p", "automatic": True})
    assert updates.migrate_mode(root) is None
    pointer = updates.read_pointer(root)
    assert (pointer["automatic"], pointer["update_mode"]) == (False, "automatic")


def test_modes_are_persisted_and_legacy_key_stays_false(tmp_path):
    root, python = managed_root(tmp_path)
    previous = root / "versions/old/venv/bin/python"
    previous.parent.mkdir(parents=True)
    previous.write_text("")
    pointer = updates.read_pointer(root)
    atomic_write_json(root / "current.json", {**pointer, "previous": {"tag": "v0.3.0", "commit": "e" * 40,
                                                                     "python": str(previous)}})
    assert updates.configure(root, mode="manual") == {"tag": CURRENT, "update_mode": "manual"}
    assert updates.configure(root, mode="automatic")["update_mode"] == "automatic"
    assert updates.read_pointer(root)["automatic"] is False
    result = updates.configure(root, rollback=True)  # explicit rollback: manual
    assert result == {"tag": "v0.3.0", "update_mode": "manual"}
    assert updates.read_pointer(root)["automatic"] is False


def test_cli_runtime_update_manual_and_automatic(tmp_path, capsys):
    from raincli_agent.cli import main
    root, _ = managed_root(tmp_path)
    assert main(["runtime", "update", "--root", str(root), "--manual"]) == 0
    assert json.loads(capsys.readouterr().out)["update_mode"] == "manual"
    assert main(["runtime", "update", "--root", str(root), "--automatic"]) == 0
    assert json.loads(capsys.readouterr().out)["update_mode"] == "automatic"
    assert main(["runtime", "update", "--root", str(root), "--automatic", "off"]) == 0
    assert json.loads(capsys.readouterr().out)["update_mode"] == "manual"
    assert updates.read_pointer(root)["automatic"] is False


def test_install_keeps_modes_and_writes_legacy_false(tmp_path, monkeypatch):
    """The real install path with a synthetic archive of this checkout."""
    import io
    import zipfile
    source = Path(__file__).resolve().parents[3]
    files = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "raincli/raincli_agent"],
                                    cwd=source, text=True).splitlines()

    def archive(url, limit):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name in files:
                if (source / name).is_file():
                    z.write(source / name, "raincli-" + url.rsplit("/", 1)[1] + "/" + name)
        return buf.getvalue()
    monkeypatch.setattr(updates, "fetch", archive)
    root = tmp_path / "managed"
    assert updates.install(root, {"tag": CURRENT, "commit": "a" * 40})["status"] == "installed"
    pointer = updates.read_pointer(root)
    assert (pointer["automatic"], pointer["update_mode"], pointer["update_mode_chosen"]) == (False, "automatic", True)
    updates.configure(root, mode="manual")
    assert updates.install(root, {"tag": CURRENT, "commit": "b" * 40})["status"] == "installed"
    pointer = updates.read_pointer(root)
    assert (pointer["automatic"], pointer["update_mode"]) == (False, "manual")
    assert pointer["previous"]["commit"] == "a" * 40


# -- launcher probation ---------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX shell stand-in for managed interpreters")
def test_launcher_rolls_back_a_version_that_fails_its_first_start(tmp_path):
    root = tmp_path / "client"
    old = root / "versions/old/bin/python"
    new = root / "versions/new/bin/python"
    for path, script in ((old, "exit 0\n"), (new, "exit 3\n")):  # the old runtime stops cleanly
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(0o755)
    (root / "launch.py").write_bytes(Path(launcher.__file__).read_bytes())
    atomic_write_json(root / "current.json", {
        "tag": "v0.4.0", "commit": "b" * 40, "python": str(new), "automatic": False, "update_mode": "automatic",
        "update_mode_chosen": True, "previous": {"tag": "v0.3.0", "commit": "a" * 40, "python": str(old)}})
    key = {"version": "v0.4.0", "allow_downgrade": False, "set_at": "t1"}
    atomic_write_json(root / "update-state.json", {"state": "updating", "error": None, "target": key})
    result = subprocess.run([sys.executable, str(root / "launch.py"), "runtime", "run", "--config", "x.json"],
                            capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    pointer = json.loads((root / "current.json").read_text())
    assert (pointer["tag"], pointer["python"], pointer["update_mode"], pointer["automatic"]) == (
        "v0.3.0", str(old), "automatic", False)
    assert pointer["previous"]["tag"] == "v0.4.0"
    state = json.loads((root / "update-state.json").read_text())
    assert (state["state"], state["error"], state["blocked"]) == ("rolled_back", "first_start_failed", key)


def test_launcher_no_longer_pulls_releases():
    source = Path(launcher.__file__).read_text()
    assert "--install" not in source and "21600" not in source


# -- the runtime report -------------------------------------------------------------------

def test_worker_reports_directory_and_client_and_stop_clears_agents(tmp_path):
    from .fake_server import FakeApi
    with FakeApi() as server:
        write_config(tmp_path / "agent.json", server.url, server.state.add_agent("machine"))
        server.state.targets["alpha"] = {"version": NEWER, "allow_downgrade": False}
        atomic_write_json(tmp_path / "connector.json", {"agent_config": "agent.json", "herdr_agent": "inbox",
                                                        "state_dir": "queue"})
        state = tmp_path / "state"
        state.mkdir(mode=0o700)
        salt = sessions.ensure_salt(str(state))
        cfg, identity, binding = load_bound(str(tmp_path / "connector.json"))
        worker = Worker(str(tmp_path / "connector.json"), cfg, identity, state, binding)
        worker.herdr = FakeHerdr()
        worker.herdr.add("inbox")
        worker.herdr.add("helper", status="working", kind="codex")

        class Process:
            pid = 7
            def poll(self): return None
        worker.process = Process()
        supervisor = service.Supervisor(tmp_path / "runtime.json", state, [], "x", salt)
        supervisor.workers = [worker]
        agents = supervisor.directories()[id(worker)]
        client = {"version": __version__, "update_mode": "manual", "update_state": "current", "error": None}
        report = worker.tick(1, agents, client)
        assert report["reported"] and report["agents"] == len(agents)
        assert worker.reply["target"] == {"version": NEWER, "allow_downgrade": False}
        directory = server.state.directory["machine"]
        assert directory[0]["role"] == "inbox" and {a["name"] for a in directory} >= {"inbox", "helper"}
        assert server.state.clients["machine"] == client
        worker.process = None
        worker.stop()
        handle, body = server.state.presence_bodies[-1]
        assert body == {"status": "offline", "agents": []} and server.state.directory["machine"] == []


def test_second_connector_publishes_only_its_inbox(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    salt = sessions.ensure_salt(str(state))

    def worker(name):
        herdr = FakeHerdr()
        for n in ("main-inbox", "second-inbox", "helper"):
            herdr.add(n)
        cfg = type("Cfg", (), {"inbox_hook": None, "herdr_agent": name, "has_inbox": True})
        return type("W", (), {"cfg": cfg, "herdr": herdr, "retired": False})()
    supervisor = service.Supervisor(tmp_path / "runtime.json", state, [], "x", salt)
    first, second = worker("main-inbox"), worker("second-inbox")
    supervisor.workers = [first, second]
    out = supervisor.directories()
    assert {a["name"] for a in out[id(first)]} >= {"main-inbox", "second-inbox", "helper"}
    assert [(a["name"], a["role"]) for a in out[id(second)]] == [("second-inbox", "inbox")]


def test_cli_agents_shows_machines_and_their_agents(tmp_path, capsys, monkeypatch):
    from raincli_agent.cli import main
    from .fake_server import FakeApi
    with FakeApi() as server:
        token = server.state.add_agent("laptop")
        server.state.clients["laptop"] = {"version": "0.3.0", "update_mode": "automatic",
                                          "update_state": "failed", "error": "network"}
        server.state.directory["laptop"] = [
            {"key": "a" * 32, "name": "helper", "type": "codex", "status": "working", "role": None,
             "reachability": None, "source": "herdr"},
            {"key": "b" * 32, "name": "inbox", "type": "claude", "status": "idle", "role": "inbox",
             "reachability": "next-turn", "source": "hook"},
            {"key": "c" * 32, "name": "\x1b[2Jsneaky", "type": "claude", "status": "unknown", "role": None,
             "reachability": None, "source": "scan"}]
        write_config(tmp_path / "agent.json", server.url, token)
        assert main(["--config", str(tmp_path / "agent.json"), "agents"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert "raincli 0.3.0 automatic failed (network)" in lines[0]
        assert lines[1].split() == ["inbox", "inbox", "claude", "idle", "(next-turn)"]
        assert ["helper", "codex", "working"] in [line.split() for line in lines[2:]]
        [scanned] = [line for line in lines if "sneaky" in line]
        assert "\x1b" not in scanned and "(detected, status unknown)" in scanned


def test_runtime_finds_the_managed_root_it_runs_from(tmp_path):
    root, python = managed_root(tmp_path)
    assert pushed.PushedUpdates(python=python).root == root.resolve()
    assert pushed.managed_root_of(sys.executable) is None or "versions" in sys.executable
