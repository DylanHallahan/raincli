"""Graceful stop, update handoff and updater hardening (review-1 H3, H4, M2-M6, L2-L4)."""
import io
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile

import pytest

from raincli_agent import __version__
from raincli_agent.config import write_config
from raincli_agent.errors import ConfigError
from raincli_agent.fsutil import atomic_write_json, ensure_private_dir
from raincli_agent.runtime import service, updates
from raincli_agent.runtime.launcher import __file__ as LAUNCHER
from raincli_agent.runtime.service import Worker, load_bound, load_runtime

from .conftest import send
from .test_runtime import fake_managed_root


def mapping(tmp_path, **extra):
    write_config(tmp_path / "agent.json", "http://127.0.0.1:1", "rca_" + "a" * 43)
    atomic_write_json(tmp_path / "connector.json",
                      {"agent_config": "agent.json", "herdr_agent": "inbox", "state_dir": "queue", **extra})
    return load_bound(str(tmp_path / "connector.json"))


def test_stop_request_lets_in_flight_submission_finish(fake_api, connector_env):
    """H3: a supervisor stop never turns a delivery into submission_uncertain."""
    send(fake_api, fake_api.alice, "bob", "hello")
    connector = connector_env.connector()
    stop = []
    original = connector_env.herdr.prompt

    def prompt(name, text, timeout):
        stop.append(True)  # the stop arrives mid-submission
        return original(name, text, timeout)
    connector_env.herdr.prompt = prompt
    connector.run_forever(stop_requested=lambda: bool(stop))
    [record] = connector.queue.all()
    assert record["state"] == "submitted"


def test_stop_during_slow_first_tick_is_not_lost(tmp_path, monkeypatch):
    """H4: the instance is published before the first tick, and a stop request
    made during that tick ends the runtime as soon as the tick returns."""
    mapping(tmp_path)
    config = tmp_path / "runtime.json"
    atomic_write_json(config, {"connectors": ["connector.json"], "state_dir": "state"})
    ensure_private_dir(tmp_path / "state")
    atomic_write_json(tmp_path / "state/status.json", {"instance": "previous-run", "status": "stopped", "updated_at": 0})
    ticking = threading.Event()

    def slow_tick(self, now):
        ticking.set()
        time.sleep(1.5)
        return {"connector": self.path, "status": "offline", "reported": False}
    monkeypatch.setattr(Worker, "tick", slow_tick)
    monkeypatch.setattr(Worker, "stop", lambda self: None)
    monkeypatch.setattr(service.signal, "signal", lambda *a: None)  # not the main thread
    runner = threading.Thread(target=service.run, args=(config,))
    runner.start()
    assert ticking.wait(10)
    assert service.status(config)["status"] == "starting"
    assert service.request_stop(config) == {"status": "stop_requested"}
    started = time.monotonic()
    runner.join(timeout=10)
    assert not runner.is_alive() and time.monotonic() - started < 5
    assert service.status(config)["status"] == "stopped"


def test_local_state_files_of_the_wrong_shape_are_ignored(tmp_path):
    atomic_write_json(tmp_path / "runtime.json", {"connectors": ["c.json"], "state_dir": "state"})
    ensure_private_dir(tmp_path / "state")
    atomic_write_json(tmp_path / "state/status.json", ["not", "a", "record"])
    assert service.status(tmp_path / "runtime.json") == {"status": "not_observed"}
    assert service.request_stop(tmp_path / "runtime.json") == {"status": "not_running"}
    atomic_write_json(tmp_path / "state/stop.json", ["x"])
    assert not service.stop_requested(tmp_path / "state", "x")


def test_one_runtime_owns_a_connector_and_state_dirs_may_not_overlap(tmp_path):
    cfg, identity, binding = mapping(tmp_path)
    first, second = (Worker(str(tmp_path / "connector.json"), cfg, identity, tmp_path / name, binding)
                     for name in ("state-a", "state-b"))
    for worker in (first, second):
        worker.handle = "inbox-agent"
    assert first._claim() and not second._claim()
    first._release()
    assert second._claim()
    second._release()
    atomic_write_json(tmp_path / "runtime.json", {"connectors": ["connector.json"], "state_dir": "queue"})
    with pytest.raises(ConfigError, match="distinct queue state"):
        load_runtime(tmp_path / "runtime.json")


def test_connector_output_goes_to_a_private_rotated_log(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))  # the child imports this checkout
    cfg, identity, binding = mapping(tmp_path)
    worker = Worker(str(tmp_path / "connector.json"), cfg, identity, tmp_path, binding)
    worker.log_path.write_bytes(b"x" * (service.LOG_LIMIT + 1))
    atomic_write_json(tmp_path / "connector.json", {"agent_config": "missing.json", "herdr_agent": "inbox"})
    process = worker._spawn()  # a real child that fails on the edited config
    assert process.wait(timeout=30) != 0
    assert Path(str(worker.log_path) + ".1").stat().st_size == service.LOG_LIMIT + 1
    text = worker.log_path.read_text()
    assert "rca_" not in text and "missing.json" in text
    if os.name != "nt":
        assert worker.log_path.stat().st_mode & 0o777 == 0o600


def test_update_requests_stay_on_https_github_hosts():
    for url in ("http://api.github.com/x", "https://example.com/x", "https://codeload.github.com:8443/x",
                "https://api.github.com.evil.test/x"):
        with pytest.raises(ConfigError):
            updates.check_url(url)
    updates.check_url("https://codeload.github.com/DylanHallahan/raincli/zip/" + "a" * 40)
    handler = updates._Redirects()
    request = urllib.request.Request("https://api.github.com/repos/x")
    for target in ("http://codeload.github.com/x", "https://objects.example.net/x"):
        with pytest.raises(ConfigError):
            handler.redirect_request(request, None, 302, "Found", {}, target)
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://codeload.github.com/x") is not None


def test_network_failures_are_reported_without_traceback(monkeypatch):
    def fail(*a, **kw):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(updates._OPENER, "open", fail)
    with pytest.raises(ConfigError, match="offline"):
        updates.fetch(updates.API + "/releases/latest", 10)


def release_archive(commit, root_name=None, launcher_text=None):
    """A codeload-like archive holding this checkout's client package."""
    package = Path(__file__).resolve().parents[2] / "raincli_agent"
    root = root_name or "raincli-" + commit
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for path in package.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                data = path.read_bytes()
                if launcher_text is not None and path == Path(LAUNCHER).resolve():
                    data = launcher_text.encode()
                archive.writestr(f"{root}/raincli/raincli_agent/{path.relative_to(package).as_posix()}", data)
    return buf.getvalue()


def test_install_copies_verified_client_without_pip_and_syncs_launcher(tmp_path, monkeypatch):
    new_launcher = Path(LAUNCHER).read_text() + "\n# release marker\n"
    monkeypatch.setattr(updates, "fetch", lambda url, limit: release_archive(url.rsplit("/", 1)[1], launcher_text=new_launcher))
    ran = []
    real_run = subprocess.run

    def run(argv, *a, **kw):
        ran.append([str(x) for x in argv])
        return real_run(argv, *a, **kw)
    monkeypatch.setattr(updates.subprocess, "run", run)
    (tmp_path / "launch.py").write_text("# an older launcher\n")
    assert updates.install(tmp_path, {"tag": "v" + __version__, "commit": "c" * 40})["status"] == "installed"
    assert not any(argv[1:3] == ["-m", "pip"] for argv in ran)  # no build backend or index
    assert any("--without-pip" in argv for argv in ran)
    assert (tmp_path / "launch.py").read_text() == new_launcher
    pointer = updates.read_pointer(tmp_path)
    version = real_run([pointer["python"], "-m", "raincli_agent", "--version"], capture_output=True, text=True)
    assert version.stdout.strip() == "raincli " + __version__

    # An archive whose root does not name the resolved commit is refused.
    monkeypatch.setattr(updates, "fetch", lambda url, limit: release_archive("d" * 40, root_name="raincli-" + "e" * 40))
    with pytest.raises(ConfigError, match="resolved commit"):
        updates.install(tmp_path, {"tag": "v" + __version__, "commit": "d" * 40})
    assert updates.read_pointer(tmp_path) == pointer

    # "latest" never downgrades or follows a moved tag automatically.
    monkeypatch.setattr(updates, "latest", lambda: {"tag": "v0.0.1", "commit": "f" * 40})
    assert updates.install(tmp_path)["status"] == "not_newer"
    assert updates.read_pointer(tmp_path) == pointer


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell stand-in for the managed interpreter")
def test_launcher_restarts_a_crashed_runtime(tmp_path):
    runs = tmp_path / "runs"
    root = fake_managed_root(tmp_path, f'echo run >> "{runs}"\n'
                                       f'[ $(wc -l < "{runs}") -ge 2 ] && exit 0\nexit 1\n')
    result = subprocess.run([sys.executable, str(root / "launch.py"), "runtime", "run", "--config", "x.json"],
                            capture_output=True, timeout=30)
    assert result.returncode == 0 and runs.read_text() == "run\nrun\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec")
def test_launcher_reexecutes_itself_after_a_launcher_update(tmp_path):
    marker = tmp_path / "relaunched"
    root = fake_managed_root(tmp_path, 'trap "exit 0" TERM\nwhile :; do sleep 0.1; done\n')
    process = subprocess.Popen([sys.executable, str(root / "launch.py"), "runtime", "run", "--config", "x.json"])
    try:
        time.sleep(1.5)
        second = root / "versions/v2/bin/python"
        second.parent.mkdir(parents=True)
        second.write_text(f'#!/bin/sh\necho "$@" > "{marker}"\nexit 0\n')
        second.chmod(0o755)
        # The new release ships a new launcher: it must take over at the switch.
        (root / "launch.py").write_text((root / "launch.py").read_text() + "\n# v2\n")
        atomic_write_json(root / "current.json", {"tag": "v2.0.0", "commit": "b" * 40, "python": str(second)})
        assert process.wait(timeout=30) == 0
        assert marker.read_text().split() == ["-m", "raincli_agent", "runtime", "run", "--config", "x.json"]
    finally:
        if process.poll() is None:
            process.kill()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell stand-in for the Windows venv redirector")
def test_readiness_through_an_interpreter_redirector(tmp_path, monkeypatch):
    """Windows venv python.exe starts the real interpreter as a child with another
    pid. Readiness must still be confirmed, and the stop must stay graceful."""
    from .fake_server import FakeApi
    redirector = tmp_path / "python"
    raincli = Path(__file__).resolve().parents[2]
    # Deliberately not exec: the interpreter runs as a child of this wrapper.
    redirector.write_text(f'#!/bin/sh\nPYTHONPATH="{raincli}" "{sys.executable}" "$@"\nexit $?\n')
    redirector.chmod(0o755)
    monkeypatch.setattr(service.sys, "executable", str(redirector))
    with FakeApi() as server:
        write_config(tmp_path / "agent.json", server.url, server.state.add_agent("runtime-test"))
        atomic_write_json(tmp_path / "connector.json", {"agent_config": "agent.json", "herdr_agent": "inbox",
                                                        "herdr_bin": "intentionally-missing-herdr",
                                                        "state_dir": "queue", "poll_wait": 1})
        cfg, identity, binding = load_bound(str(tmp_path / "connector.json"))
        ensure_private_dir(tmp_path / "state")
        worker = Worker(str(tmp_path / "connector.json"), cfg, identity, tmp_path / "state", binding)
        try:
            deadline = time.monotonic() + 30
            report = worker.tick(time.monotonic())
            while report["status"] != "unknown" and time.monotonic() < deadline:
                time.sleep(0.2)
                report = worker.tick(time.monotonic())
            # "unknown" = the connector confirmed readiness and only Herdr is unavailable.
            assert report["status"] == "unknown", worker.log_path.read_text()
            record = __import__("json").loads(worker.ready_path.read_text())
            assert record["pid"] != worker.process.pid  # the redirector's pid differs
            process = worker.process
        finally:
            worker.retire()
        assert process.returncode == 0  # stopped via the stop file, not killed
        assert server.state.presence["runtime-test"] == "offline"


FAKE_HERDR = r'''#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
cmd = sys.argv[1:3]
if cmd == ["agent", "get"]:
    print(json.dumps({{"id": "1", "result": {{"type": "agent", "agent": {{
        "agent": "claude", "agent_status": "idle", "pane_id": "w1:p1", "cwd": "/work", "focused": False}}}}}}))
elif cmd == ["agent", "prompt"]:
    open(os.path.join(here, "prompt-started"), "w").close()
    time.sleep(float(open(os.path.join(here, "prompt-seconds")).read()))
    open(os.path.join(here, "prompt-finished"), "w").close()
    print(json.dumps({{"id": "2", "result": {{"type": "agent_prompt"}}}}))
else:
    sys.exit(2)
'''


@pytest.fixture
def supervised(tmp_path, monkeypatch, fake_api):
    """A Worker supervising a real connector child with default poll_wait and
    prompt_timeout, a fake herdr executable and the FakeApi relay."""
    import stat
    herdr = tmp_path / "bin/herdr"
    herdr.parent.mkdir()
    herdr.write_text(FAKE_HERDR.format(python=sys.executable))
    herdr.chmod(herdr.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    write_config(tmp_path / "bob.json", fake_api.url, fake_api.bob)
    atomic_write_json(tmp_path / "connector.json", {"agent_config": "bob.json", "herdr_agent": "inbox",
                                                    "herdr_bin": str(herdr), "state_dir": "queue",
                                                    "trusted_senders": ["alice"]})
    cfg, identity, binding = load_bound(str(tmp_path / "connector.json"))
    assert (cfg.poll_wait, cfg.prompt_timeout) == (25, 30)  # the defaults
    ensure_private_dir(tmp_path / "state")
    worker = Worker(str(tmp_path / "connector.json"), cfg, identity, tmp_path / "state", binding)
    deadline = time.monotonic() + 30
    while worker.tick(time.monotonic())["status"] != "ready":
        assert time.monotonic() < deadline, worker.log_path.read_text()
        time.sleep(0.2)
    worker.dir = herdr.parent
    yield worker
    worker.retire()


def records(worker):
    from raincli_agent.connector.queue import Queue
    return Queue(worker.cfg.state_dir).all()


def test_no_submission_starts_after_a_stop_during_the_long_poll(fake_api, supervised):
    """R2-H1 repro 2: the stop comes while the connector idles in its long poll and
    a message arrives afterwards. It must stay queued, not be submitted and killed."""
    (supervised.dir / "prompt-seconds").write_text("40")
    process = supervised.process
    stopper = threading.Thread(target=supervised.stop)
    started = time.monotonic()
    stopper.start()
    time.sleep(1)
    send(fake_api, fake_api.alice, "bob", "arrives after the stop request")
    stopper.join(timeout=supervised.stop_budget() + 15)
    assert not stopper.is_alive()
    assert process.returncode == 0  # exited on the stop file, not killed
    assert time.monotonic() - started < 15  # within one poll slice, not after a 25 s long poll
    assert not (supervised.dir / "prompt-started").exists()
    assert all(r["state"] in ("received", "held") for r in records(supervised))


def test_a_submission_in_progress_at_stop_completes(fake_api, supervised):
    (supervised.dir / "prompt-seconds").write_text("6")
    process = supervised.process
    send(fake_api, fake_api.alice, "bob", "in flight when the stop comes")
    deadline = time.monotonic() + 30
    while not (supervised.dir / "prompt-started").exists():
        assert time.monotonic() < deadline
        time.sleep(0.1)
    supervised.stop()
    assert process.returncode == 0 and (supervised.dir / "prompt-finished").exists()
    assert [r["state"] for r in records(supervised)] == ["submitted"]


def test_runtime_rejects_prompt_timeouts_beyond_the_stop_budget(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with pytest.raises(ConfigError, match="prompt_timeout <= 60"):
        mapping(tmp_path / "a", prompt_timeout=61)
    cfg, identity, binding = mapping(tmp_path / "b", prompt_timeout=60)
    worker = Worker(str(tmp_path / "b/connector.json"), cfg, identity, tmp_path, binding)
    assert worker.stop_budget() == 5 + 60 + service.STOP_MARGIN == 80
