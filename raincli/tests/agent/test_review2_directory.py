"""Review 2 of the directory integration: client and runtime items O1-O4 and O6-O14."""
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

import pytest

from raincli_agent.connector import queue as q
from raincli_agent.connector.queue import Queue
from raincli_agent.fsutil import atomic_write_json
from raincli_agent.runtime import discovery, hooks_install, launcher, procinfo, sessions, updates

from .conftest import send
from .test_next_turn import env  # noqa: F401 - fixture
from .test_review1_directory import fake_versions

RAINCLI = Path(__file__).resolve().parents[2]
LINUX = pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("bash"), reason="/proc and bash")
POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX")


@pytest.fixture
def state(tmp_path):
    directory = tmp_path / "runtime-state"
    directory.mkdir(mode=0o700)
    salt = sessions.ensure_salt(str(directory))
    sessions.sessions_dir(str(directory), create=True)
    return type("State", (), {"dir": str(directory), "salt": salt})


def agent_binary(tmp_path, *parts):
    """A real executable at a native-install-shaped path (a copy of bash)."""
    path = tmp_path.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(os.path.realpath(shutil.which("bash")), path)
    return path


def run_under(agent, state_dir, session_id, event="SessionStart", linger=30):
    """Start ``agent`` (bash-compatible) running the real hook through ``sh -c``, then lingering."""
    payload = Path(state_dir).parent / f"payload-{session_id}.json"
    payload.write_text(json.dumps({"session_id": session_id, "cwd": "/w/native-app"}))
    hook_cmd = f'{sys.executable} -m raincli_agent hook claude {event} --state-dir "{state_dir}" < "{payload}"'
    script = f"sh -c '{hook_cmd}'; sleep {linger}"
    return subprocess.Popen([str(agent), "-c", script], env={**os.environ, "PYTHONPATH": str(RAINCLI)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_record(state_dir, session_id, salt, predicate=lambda r: True, timeout=20):
    key = sessions.agent_key(salt, "claude:" + session_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = sessions.load_record(state_dir, key)
        if record is not None and predicate(record):
            return record
        time.sleep(0.1)
    raise AssertionError("hook record not written")


# -- O1: a native install is found, an unrelated ancestor is not ---------------------------------------

@LINUX
def test_native_install_records_its_own_pid_and_stays_live(tmp_path, state):
    agent = agent_binary(tmp_path, "share", "claude", "versions", "2.1.119")
    process = run_under(agent, state.dir, "native")
    try:
        record = wait_record(state.dir, "native", state.salt)
        assert record["pid"] == process.pid and record["pid_start"] == procinfo.process_start(process.pid)
        assert sessions.process_state(record) == "alive"
        assert [r["name"] for r in sessions.live_sessions(state.dir, "claude", "native-app",
                                                         now=time.time() + 5 * 3600)] == ["native-app"]
        # Listed once: the scan sees the native process but the hook record claims it.
        found = discovery.discover(state.dir, state.salt, None, None)
        assert sum(1 for a in found if a["type"] == "claude" and a["source"] == "scan"
                   and a["key"] == sessions.agent_key(state.salt, f"scan:claude:{process.pid}")) == 0
    finally:
        process.kill()
        process.wait()
    assert sessions.live_sessions(state.dir, "claude", "native-app") == []


@LINUX
def test_unrelated_ancestor_is_never_adopted(tmp_path, state):
    """This suite itself runs under a real Claude Code process; an editor in between stops the walk."""
    editor = agent_binary(tmp_path, "bin", "my-editor")
    process = run_under(editor, state.dir, "editor", linger=0)
    try:
        record = wait_record(state.dir, "editor", state.salt)
        assert "pid" not in record
    finally:
        process.wait(timeout=20)


def test_walk_passes_only_shells_and_python():
    table = {50: ("bash", 40, "bash"), 40: ("python3.14", 30, "python3"), 30: ("claude", 1, "claude"),
             60: ("vim", 30, "vim"), 70: ("codex", 1, "codex"), 80: ("bash", 70, "bash")}

    def fake(pid, proc="/proc"):
        if pid not in table:
            raise OSError("gone")
        return table[pid]
    original = procinfo.linux_info
    procinfo.linux_info = fake
    try:
        if sys.platform.startswith("linux"):
            assert procinfo.agent_pid("claude", start=50) == 30
            assert procinfo.agent_pid("claude", start=60) is None   # an editor in between
            assert procinfo.agent_pid("claude", start=80) is None   # another agent type
            assert procinfo.agent_pid("codex", start=80) == 70
    finally:
        procinfo.linux_info = original


@pytest.mark.parametrize("path, kind", [
    ("/home/u/.local/share/claude/versions/2.1.119", "claude"), ("C:\\Tools\\claude.exe", "claude"),
    ("/usr/bin/codex", "codex"), ("/opt/node", None), ("/x/versions/2.1.119", None)])
def test_kind_by_executable_path(path, kind):
    assert procinfo.kind_of(path) == kind


def test_macos_ps_output_is_parsed():
    assert procinfo.parse_ps_info("  123 /Users/u/.local/share/claude/versions/2.1.119") == ("claude", 123, "2.1.119")
    assert procinfo.parse_ps_info("1 /bin/zsh") == ("zsh", 1, "zsh")


# -- O2: a resumed session gets its new process ---------------------------------------------------------

@LINUX
def test_resumed_session_replaces_a_dead_pid(tmp_path, state):
    agent = agent_binary(tmp_path, "claude", "versions", "2.1.119")
    first = run_under(agent, state.dir, "resumed")
    old = wait_record(state.dir, "resumed", state.salt)["pid"]
    first.kill()
    first.wait()
    second = run_under(agent, state.dir, "resumed", event="UserPromptSubmit")
    try:
        record = wait_record(state.dir, "resumed", state.salt, lambda r: r.get("pid") != old)
        assert record["pid"] == second.pid and sessions.process_state(record) == "alive"
    finally:
        second.kill()
        second.wait()


# -- O6: pid namespaces -------------------------------------------------------------------------------------

@LINUX
def test_pid_from_another_namespace_is_not_determinable():
    me = {"pid": os.getpid(), "pid_start": procinfo.process_start(os.getpid()), "pid_ns": procinfo.pid_namespace()}
    assert procinfo.process_state(me) == "alive"
    assert procinfo.process_state({**me, "pid_ns": "pid:[1]"}) is None


# -- O7: a put-back file starts a fresh grace -----------------------------------------------------------------

def test_put_back_claim_resets_the_grace(env):  # noqa: F811
    connector = env.connector()
    env.session("s1")
    mid = send(env.api, env.api.alice, "bob", "hello")["id"]
    connector.run_once()
    key = sessions.agent_key(env.salt, "claude:s1")
    inbox = Path(env.state, "sessions", key + ".inbox")
    os.rename(inbox / (mid + ".md"), inbox / (mid + ".md.claimed"))  # a hook claims ...
    connector.run_once()
    assert connector.queue.get(mid).get("claim_seen_at") is not None
    os.rename(inbox / (mid + ".md.claimed"), inbox / (mid + ".md"))  # ... and puts it back
    connector.run_once()
    assert "claim_seen_at" not in connector.queue.get(mid)
    env.clock.now += sessions.CLAIM_GRACE * 10
    os.rename(inbox / (mid + ".md"), inbox / (mid + ".md.claimed"))  # a real claim, much later
    connector.run_once()
    assert connector.queue.get(mid)["state"] == q.HANDED_OVER  # a fresh grace, not settled at once


# -- O3 and O8: launcher checks ---------------------------------------------------------------------------------

def broken_run_branch():
    source = Path(launcher.__file__).read_text()
    assert "def supervise(" in source
    return source.replace("def supervise(root, args, config, command, own=None, probation_enabled=True):\n",
                          "def supervise(root, args, config, command, own=None, probation_enabled=True):\n"
                          "    raise RuntimeError('broken run branch')\n")


@POSIX
def test_launcher_broken_only_in_runtime_run_is_not_adopted(tmp_path):
    root, pointer, _ = fake_versions(tmp_path, "exit 0\n", old_script='echo "raincli 0.3.0"\n')
    new_dir = Path(pointer["python"]).parents[2]
    (new_dir / "launch.py").write_text(broken_run_branch())
    before = (root / "launch.py").read_bytes()
    assert updates.adopt_launcher(root, pointer["python"], base_python=sys.executable) == "candidate_failed"
    assert (root / "launch.py").read_bytes() == before
    result = subprocess.run([sys.executable, str(root / "launch.py"), "--self-check"], capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0 and "self-check ok" in result.stdout  # the kept launcher passes


@POSIX
def test_explicit_rollback_checks_the_previous_launcher(tmp_path):
    root, pointer, _ = fake_versions(tmp_path, 'echo "raincli 0.4.0"\n', old_script='echo "raincli 0.3.0"\n')
    old_dir = Path(pointer["previous"]["python"]).parents[2]
    (old_dir / "launch.py").write_text(broken_run_branch())
    before = (root / "launch.py").read_bytes()
    assert updates.configure(root, rollback=True)["tag"] == "v0.3.0"
    assert (root / "launch.py").read_bytes() == before


# -- O9: the rollback never blocks the launcher ----------------------------------------------------------------------

@POSIX
def test_rollback_lock_is_not_waited_on_and_a_stop_stays_prompt(tmp_path):
    root, pointer, _ = fake_versions(tmp_path, "exit 3\n")
    assert launcher.roll_back(root, pointer, timeout=0) is True  # free: rolled back
    root2, pointer2, _ = fake_versions(tmp_path / "busy", "exit 3\n")
    lock = Queue(str(root2 / "update-lock"))
    lock.acquire_run_lock()
    try:
        started = time.monotonic()
        assert launcher.roll_back(root2, pointer2, timeout=0) == "busy"
        assert time.monotonic() - started < 1
        process = subprocess.Popen([sys.executable, str(root2 / "launch.py"), "runtime", "run", "--config", "x"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(3)
        assert process.poll() is None  # waiting for the lock without blocking
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
    finally:
        lock.release_run_lock()
    assert json.loads((root2 / "current.json").read_text())["tag"] == "v0.4.0"  # not rolled back behind a lock


@POSIX
def test_exit_zero_on_probation_without_a_rollback_is_a_crash(tmp_path):
    root, pointer, _ = fake_versions(tmp_path, "exit 0\n")
    atomic_write_json(root / "current.json", {**pointer, "previous": {**pointer["previous"],
                                                                     "python": str(tmp_path / "gone")}})
    process = subprocess.Popen([sys.executable, str(root / "launch.py"), "runtime", "run", "--config", "x"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(3)
        assert process.poll() is None  # restarting with backoff, not "stopped on request"
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)


# -- O10: probation edges ----------------------------------------------------------------------------------------------

@POSIX
def test_requested_stop_during_probation_is_not_a_failure(tmp_path):
    root, pointer, _ = fake_versions(tmp_path, "")
    Path(pointer["python"]).write_text(f'#!/bin/sh\necho "{{}}" > "{root}/stop-requested.json"\nexit 0\n')
    result = subprocess.run([sys.executable, str(root / "launch.py"), "runtime", "run", "--config", "x"],
                            capture_output=True, timeout=60)
    assert result.returncode == 0
    assert json.loads((root / "current.json").read_text())["tag"] == "v0.4.0"  # kept: a stop, not a failure
    assert json.loads((root / "update-state.json").read_text())["state"] == "updating"


def test_switch_timeout_is_still_on_probation(tmp_path):
    root, pointer, key = fake_versions(tmp_path, "exit 0\n")
    atomic_write_json(root / "update-state.json", {"state": "failed", "error": "switch_timeout", "target": key})
    assert launcher.on_probation(root, pointer)


# -- O11, O12, O13: scan and listing ---------------------------------------------------------------------------------------

def test_agent_helpers_under_a_shell_are_not_listed(state):
    processes = {10: ("claude", 1, "claude"), 11: ("bash", 10, "bash"), 12: ("claude", 11, "ugrep"),
                 20: ("bash", 1, "bash"), 21: ("codex", 20, "codex")}
    found = discovery.linux_scan(state.salt, set(), False, processes=processes, cwd_name=lambda pid: "")
    assert sorted(a["type"] for a in found) == ["claude", "codex"] and len(found) == 2


def test_stale_hook_inbox_is_listed_once(state):
    data = {"session_id": "old", "cwd": "/w/x"}
    from raincli_agent.runtime import hook
    hook.handle("claude", "SessionStart", "inbox", state.dir, io.BytesIO(json.dumps(data).encode()), io.BytesIO(),
                now=time.time() - 720)
    found, _ = discovery.hook_entries(state.salt, state.dir, ("hook", "claude", "inbox"))
    inboxes = [a for a in found if a["name"] == "inbox"]
    assert len(inboxes) == 1 and inboxes[0]["role"] == "inbox" and inboxes[0]["status"] == "offline"
    assert inboxes[0]["key"] == sessions.agent_key(state.salt, "claude:old")


def test_lone_surrogate_herdr_name_does_not_abort_discovery(state):
    class H:
        def list_agents(self):
            return [{"name": "bad\ud800name", "kind": "claude", "status": "idle", "terminal_id": "t", "cwd": ""}]
    from .fake_server import presence_problem
    found = discovery.discover(state.dir, state.salt, H(), None, include_scan=False)
    assert len(found) == 1 and presence_problem({"status": "ready", "agents": found}) is None


# -- O14: backup pruning ------------------------------------------------------------------------------------------------------

@POSIX
def test_pruning_touches_only_our_backups_and_counters_only_grow(tmp_path, state):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude/settings.json").write_text("{}")
    mine = home / ".claude/settings.json.raincli-backup-mine"
    mine.write_text("hand-made")
    prefix = (["/usr/bin/raincli"], "test")
    for _ in range(4):
        hooks_install.install("claude", state.dir, home=home, prefix=prefix)
        hooks_install.install("claude", state.dir, home=home, prefix=prefix, remove=True)
    ours = sorted(hooks_install.own_backups(home / ".claude", "settings.json"))
    assert [c for _, c, _ in ours] == [6, 7, 8] and mine.exists()
