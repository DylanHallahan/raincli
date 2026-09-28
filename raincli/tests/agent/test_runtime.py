import concurrent.futures
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from raincli_agent.config import write_config
from raincli_agent.connector.config import ConnectorConfig
from raincli_agent.connector.herdr import FakeHerdr
from raincli_agent.errors import ConfigError
from raincli_agent.fsutil import atomic_write_json, ensure_private_dir
from raincli_agent.runtime.service import availability, load_bound, load_runtime, Worker
from raincli_agent.runtime.startup import systemd_quote
from raincli_agent.runtime import launcher, service, startup, updates


def test_presence_respects_explicit_pins_and_readiness():
    herdr = FakeHerdr()
    herdr.add("inbox", pane_id="w1:p2", cwd="/work")
    cfg = ConnectorConfig(herdr_agent="inbox", expect_pane_id="w1:p2", expect_cwd="/work")
    assert availability(cfg, herdr, False) == "offline"
    assert availability(cfg, herdr, True) == "ready"
    herdr.set_status("inbox", "working")
    assert availability(cfg, herdr, True) == "busy"
    herdr.set_status("inbox", "blocked")
    assert availability(cfg, herdr, True) == "blocked"
    herdr.add("inbox", pane_id="w1:p3", cwd="/work")
    assert availability(cfg, herdr, True) == "blocked"
    herdr.remove("inbox")
    assert availability(cfg, herdr, True) == "offline"


def test_worker_waits_for_authenticated_queue_owner(tmp_path):
    identity = write_config(tmp_path / "agent.json", "http://127.0.0.1:1", "rca_" + "a" * 43)
    atomic_write_json(tmp_path / "connector.json", {"agent_config": "agent.json", "herdr_agent": "inbox", "state_dir": "queue"})
    cfg, identity, binding = load_bound(str(tmp_path / "connector.json"))
    worker = Worker(str(tmp_path / "connector.json"), cfg, identity, tmp_path, binding)
    worker.herdr = FakeHerdr()
    worker.herdr.add("inbox")
    class Process:
        pid = 123
        def poll(self): return None
    class Api:
        def me(self):
            return {"agent": {"handle": "inbox-agent"}}
        def publish_presence(self, state):
            return {"expires_at": "soon"}
    worker.process, worker.api = Process(), Api()
    assert worker.tick(1)["status"] == "offline"
    atomic_write_json(worker.ready_path, {"pid": 123})  # pid alone no longer suffices
    assert worker.tick(2)["status"] == "offline"
    # A record at an earlier spawn's handshake path is never accepted; pids are not
    # compared, because a Windows venv python.exe redirector has a different pid.
    earlier = worker.ready_path
    worker._new_handshake()
    atomic_write_json(earlier, {"pid": 456, "handle": "inbox-agent", **binding})
    assert worker.tick(3)["status"] == "offline"
    atomic_write_json(worker.ready_path, {"pid": 123, "handle": "someone-else", **binding})
    assert worker.tick(4)["status"] == "offline"
    atomic_write_json(worker.ready_path, {"pid": 123, "handle": "inbox-agent", **binding})
    assert worker.tick(5)["status"] == "ready"


class FakeProcess:
    """A connector child that exits once the supervisor requests a stop."""
    pids = iter(range(1000, 2000))

    def __init__(self, argv, **_):
        self.argv, self.pid, self.code, self.terminated = argv, next(self.pids), None, False
        self.stop_file = Path(argv[argv.index("--runtime-ready") + 1] + ".stop")

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        if self.code is None:
            if not self.stop_file.exists():
                raise AssertionError("supervisor did not request a graceful stop")
            self.code = 0
        return self.code

    def terminate(self):
        self.terminated, self.code = True, -15

    kill = terminate


def test_config_edit_never_republishes_old_readiness_under_new_mapping(tmp_path, monkeypatch):
    from .fake_server import FakeApi
    monkeypatch.setattr(service.subprocess, "Popen", FakeProcess)
    with FakeApi() as server:
        write_config(tmp_path / "alpha.json", server.url, server.state.add_agent("alpha"))
        write_config(tmp_path / "beta.json", server.url, server.state.add_agent("beta"))
        connector = tmp_path / "connector.json"
        atomic_write_json(connector, {"agent_config": "alpha.json", "herdr_agent": "inbox", "poll_wait": 1, "state_dir": "queue"})
        atomic_write_json(tmp_path / "runtime.json", {"connectors": ["connector.json"], "state_dir": "state"})
        path, state, configs = load_runtime(tmp_path / "runtime.json")
        ensure_private_dir(state)
        supervisor = service.Supervisor(path, state, configs, service.file_sha256(path))
        herdr = FakeHerdr()
        herdr.add("inbox")
        herdr.add("other")
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        try:
            [old] = supervisor.workers
            old.herdr = herdr
            assert old.tick(1)["status"] == "offline"  # child started, not yet confirmed
            child = old.process
            atomic_write_json(old.ready_path, {"pid": child.pid, "handle": "alpha", **old.binding})
            assert old.tick(2)["status"] == "ready"
            assert server.state.presence == {"alpha": "ready"}

            # Remap the same connector file to another credential and Herdr agent.
            atomic_write_json(connector, {"agent_config": "beta.json", "herdr_agent": "other", "poll_wait": 1, "state_dir": "queue"})
            report = old.tick(3)
            assert report == {"connector": old.path, "status": "offline", "reported": False,
                              "process_running": False, "child_pid": None, "error": "config_changed"}
            assert child.code == 0 and not child.terminated  # graceful stop, lock released
            assert server.state.presence == {"alpha": "offline"}

            assert supervisor.changed()
            supervisor.refresh(pool)
            [new] = supervisor.workers
            assert new is not old and new.binding["agent_config"].endswith("beta.json")
            new.herdr = herdr
            new.tick(4)
            # A readiness record for the old mapping, even carrying the new child's pid.
            atomic_write_json(new.ready_path, {"pid": new.process.pid, "handle": "alpha", **old.binding})
            assert new.tick(5)["status"] == "offline"
            assert server.state.presence == {"alpha": "offline", "beta": "offline"}
            atomic_write_json(new.ready_path, {"pid": new.process.pid, "handle": "beta", **new.binding})
            assert new.tick(6)["status"] == "ready"
            assert server.state.presence == {"alpha": "offline", "beta": "ready"}

            # An invalid edit retires the mapping and publishes nothing further.
            (tmp_path / "connector.json").write_text("{")
            assert new.tick(7)["error"] == "config_changed"
            supervisor.refresh(pool)
            assert supervisor.error == "config_invalid" and not supervisor.changed()
            assert supervisor.workers[0].tick(8)["error"] == "config_changed"
            assert server.state.presence == {"alpha": "offline", "beta": "offline"}
        finally:
            for worker in supervisor.workers:
                worker.retire()
            pool.shutdown()


def test_connector_stops_between_iterations_on_request(connector_env):
    connector = connector_env.connector()
    calls = []
    connector.run_once = lambda wait=0: calls.append(wait)
    connector.run_forever(stop_requested=lambda: len(calls) >= 2)
    assert len(calls) == 2


def test_stop_request_ignores_stopped_runtime(tmp_path):
    atomic_write_json(tmp_path / "runtime.json", {"connectors": ["missing.json"], "state_dir": "state"})
    ensure_private_dir(tmp_path / "state")
    atomic_write_json(tmp_path / "state/status.json", {"instance": "x", "status": "stopped"})
    # Works even though the connector mapping is currently invalid.
    assert service.request_stop(tmp_path / "runtime.json") == {"status": "not_running"}
    atomic_write_json(tmp_path / "state/status.json", {"instance": "y", "updated_at": 0})
    assert service.request_stop(tmp_path / "runtime.json") == {"status": "stop_requested"}


def fake_managed_root(tmp_path, script):
    root = tmp_path / "client"
    python = root / "versions/v1/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n" + script)
    python.chmod(0o755)
    (root / "launch.py").write_bytes(Path(launcher.__file__).read_bytes())
    atomic_write_json(root / "current.json", {"tag": "v1.0.0", "commit": "a" * 40, "python": str(python)})
    return root


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell stand-in for the managed interpreter")
def test_launcher_passes_stdio_and_exit_code_through(tmp_path):
    root = fake_managed_root(tmp_path, 'test "$*" = "-m raincli_agent send --body-file -" || exit 99\n'
                                       'cat; echo diagnostic >&2; exit 7\n')
    result = subprocess.run([sys.executable, str(root / "launch.py"), "send", "--body-file", "-"],
                            input=b"line one\nline two\n", capture_output=True, timeout=30)
    assert (result.returncode, result.stdout, result.stderr) == (7, b"line one\nline two\n", b"diagnostic\n")


def test_launcher_real_client_reads_body_from_stdin(tmp_path):
    from .fake_server import FakeApi
    raincli = Path(__file__).resolve().parents[2]
    root = fake_managed_root(tmp_path, f'PYTHONPATH="{raincli}" exec "{sys.executable}" "$@"\n')
    with FakeApi() as server:
        write_config(tmp_path / "agent.json", server.url, server.state.add_agent("alice"))
        server.state.add_agent("bob")
        base = [sys.executable, str(root / "launch.py"), "--config", str(tmp_path / "agent.json")]
        result = subprocess.run([*base, "send", "--to", "bob", "--body-file", "-"],
                                input="piped body", capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert [m["body"] for m in server.state.messages.values()] == ["piped body"]
        failed = subprocess.run([*base, "send", "--to", "nobody", "--body-file", "-"],
                                input="x", capture_output=True, text=True, timeout=30)
        assert failed.returncode not in (0, None)


def test_launcher_exit_status_for_signals():
    assert launcher.exit_status(3) == 3
    assert launcher.exit_status(-15) == 143


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal delivery")
def test_launcher_stops_runtime_gracefully(tmp_path):
    marker = tmp_path / "graceful"
    child = subprocess.Popen([sys.executable, "-c",
                              "import signal, sys, time\n"
                              "def stop(*_):\n"
                              f"    open({str(marker)!r}, 'w').close(); sys.exit(0)\n"
                              "signal.signal(signal.SIGTERM, stop)\n"
                              "print('ready', flush=True)\n"
                              "time.sleep(60)\n"], stdout=subprocess.PIPE)
    assert child.stdout.readline() == b"ready\n"
    launcher.stop_runtime(child, sys.executable, None)
    assert child.returncode == 0 and marker.exists()


def write_systemctl(tmp_path, monkeypatch, active):
    log, state = tmp_path / "systemctl.log", tmp_path / "active"
    state.write_text("0" if active else "3")
    fake = tmp_path / "bin/systemctl"
    fake.parent.mkdir(exist_ok=True)
    fake.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\n'
                    f'if [ "$2" = is-active ]; then exit $(cat "{state}"); fi\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    def calls():
        lines = log.read_text().splitlines() if log.exists() else []
        log.unlink(missing_ok=True)
        return lines
    return calls, state


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="systemd user unit")
def test_linux_startup_restarts_only_when_unit_or_config_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    calls, active = write_systemctl(tmp_path, monkeypatch, active=False)
    write_config(tmp_path / "agent.json", "http://127.0.0.1:1", "rca_" + "a" * 43)
    atomic_write_json(tmp_path / "connector.json", {"agent_config": "agent.json", "herdr_agent": "inbox"})
    config = tmp_path / "runtime.json"
    atomic_write_json(config, {"connectors": ["connector.json"]})
    unit = tmp_path / "home/.config/systemd/user" / startup.NAME
    name = startup.NAME

    startup.install(config)
    assert calls() == ["--user daemon-reload", f"--user enable {name}", f"--user is-active --quiet {name}",
                       f"--user start {name}"]
    active.write_text("0")
    first = unit.read_text()
    assert "config-sha256: " in first and "rca_" not in first
    assert "KillMode=mixed" in first and "network-online" not in first

    startup.install(config)  # nothing changed: the running service is left alone
    assert calls() == [f"--user enable {name}", f"--user is-active --quiet {name}"]

    atomic_write_json(tmp_path / "connector.json", {"agent_config": "agent.json", "herdr_agent": "other"})
    startup.install(config)
    assert calls() == ["--user daemon-reload", f"--user enable {name}", f"--user is-active --quiet {name}",
                       f"--user restart {name}"]
    assert unit.read_text() != first


def test_windows_run_value_quotes_every_argument():
    argv = [r"C:\Program Files\Python311\pythonw.exe", r"C:\Users\me\.raincli\client\launch.py",
            "runtime", "run", "--config", r"C:\cfg dir\runtime.json", "C:\\trailing\\"]
    assert startup.windows_command_line(argv) == (
        r'"C:\Program Files\Python311\pythonw.exe" "C:\Users\me\.raincli\client\launch.py" '
        r'"runtime" "run" "--config" "C:\cfg dir\runtime.json" "C:\trailing\\"')
    with pytest.raises(ConfigError):
        startup.windows_command_line(['C:\\bad"quote'])


def test_duplicate_runtime_credentials_are_rejected(tmp_path):
    write_config(tmp_path / "agent.json", "http://127.0.0.1:1", "rca_" + "a" * 43)
    for name in ("a", "b"):
        atomic_write_json(tmp_path / (name + ".json"), {"agent_config": "agent.json", "herdr_agent": name})
    config = tmp_path / "runtime.json"
    atomic_write_json(config, {"connectors": ["a.json", "b.json"]})
    with pytest.raises(ConfigError, match="distinct credentials"):
        load_runtime(config)


@pytest.mark.parametrize("name", ["../escape", "/escape", "root/../../escape", "root/C:escape", "root\\escape", "root/escape. "])
def test_update_archive_rejects_unsafe_names(tmp_path, name):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(name, "bad")
    with pytest.raises(ConfigError):
        updates.unpack(buf.getvalue(), tmp_path)


def test_update_archive_rejects_symlinks(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        link = zipfile.ZipInfo("root/link")
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "../../escape")
    with pytest.raises(ConfigError):
        updates.unpack(buf.getvalue(), tmp_path)


def test_failed_update_never_changes_pointer(tmp_path, monkeypatch):
    old = {"tag": "v0.1.0", "commit": "a" * 40, "python": "/old/python", "automatic": False}
    atomic_write_json(tmp_path / "current.json", old)
    monkeypatch.setattr(updates, "fetch", lambda *a: b"invalid zip")
    with pytest.raises(zipfile.BadZipFile):
        updates.install(tmp_path, {"tag": "v0.2.0", "commit": "b" * 40})
    assert updates.read_pointer(tmp_path) == old
    assert not list((tmp_path / "versions").iterdir())


def test_update_rejects_unexpected_release(monkeypatch):
    monkeypatch.setattr(updates, "fetch", lambda *a: json.dumps({"tag_name": "main", "prerelease": False}).encode())
    with pytest.raises(ConfigError):
        updates.latest()


def test_systemd_escaping():
    assert systemd_quote('/a %h $HOME "x"') == '"/a %%h $$HOME \\"x\\""'
    with pytest.raises(ConfigError):
        systemd_quote("bad\npath")
