"""Review 1 of the directory integration: client and runtime findings (protocol 14.9)."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from raincli_agent import api as api_mod
from raincli_agent.connector import queue as q
from raincli_agent.connector.queue import ConnectorBusy, Queue
from raincli_agent.errors import ConfigError
from raincli_agent.fsutil import atomic_write_json
from raincli_agent.runtime import discovery, hook, hooks_install, launcher, pushed, sessions, updates

from .conftest import send
from .fake_server import presence_problem
from .test_next_turn import env  # noqa: F401 - fixture
from .test_pushed_updates import NEWER, Clock, controller, managed_root, settle, target

RAINCLI = Path(__file__).resolve().parents[2]
LINUX = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc")
POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX")


@pytest.fixture
def state(tmp_path):
    directory = tmp_path / "runtime-state"
    directory.mkdir(mode=0o700)
    salt = sessions.ensure_salt(str(directory))
    sessions.sessions_dir(str(directory), create=True)
    return type("State", (), {"dir": str(directory), "salt": salt})


def fire(state_dir, event, session_id="s1", name=None, now=None, kind="claude"):
    data = {"session_id": session_id, "cwd": "/w/app"}
    hook.handle(kind, event, name, state_dir, io.BytesIO(json.dumps(data).encode()), io.BytesIO(), now=now)


# -- 1. names agree with the server's rule ----------------------------------------------

@pytest.mark.parametrize("raw", ["orca_tools", "merci_bot", "circa_2024", "team/api", "v1.2 (beta)",
                                 "rca_" + "A" * 43, "x rci_" + "b" * 30 + " y"])
def test_normalized_names_are_accepted_and_tokens_never_sent(raw):
    name = sessions.normalize_name(raw, "claude")
    body = {"status": "ready", "agents": [{"key": "a" * 32, "name": name, "type": "claude", "status": "idle",
                                           "role": None, "reachability": None, "source": "hook"}]}
    assert presence_problem(body) is None
    assert "A" * 20 not in name and "b" * 20 not in name
    if "rc" not in raw:
        assert name == raw  # ordinary slashes, dots and underscores are kept


def test_inbox_name_that_looks_like_a_token_is_refused(tmp_path):
    from raincli_agent.connector.config import load_connector_config
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"agent_config": "a.json", "inbox": {"hook": "claude", "name": "rca_" + "x" * 30}}))
    with pytest.raises(ConfigError):
        load_connector_config(str(path))


# -- 7. an idle inbox whose process is alive stays the target (14.9) ---------------------------

@LINUX
def test_live_process_keeps_an_idle_session_live_and_an_exited_one_is_dropped(state, monkeypatch):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        monkeypatch.setattr(hook, "agent_pid", lambda kind: sleeper.pid)
        long_ago = time.time() - 5 * 3600
        fire(state.dir, "SessionStart", "alive", name="inbox", now=long_ago)
        [record] = sessions.read_sessions(state.dir)
        assert record["status"] == "idle" and record["pid"] == sleeper.pid and record["pid_start"]
        assert len(sessions.live_sessions(state.dir, "claude", "inbox")) == 1
        sleeper.kill()
        sleeper.wait()
        assert sessions.live_sessions(state.dir, "claude", "inbox") == []
        assert sessions.read_sessions(state.dir) == []  # the runtime drops it at once
        assert not list(Path(state.dir, "sessions").glob("*.json"))
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


@LINUX
def test_reused_pid_is_not_mistaken_for_the_session(state):
    record = {"pid": os.getpid(), "pid_start": sessions.process_start(os.getpid()) + 1}
    assert sessions.process_state(record) == "dead"
    assert sessions.process_state({"pid": os.getpid(), "pid_start": sessions.process_start(os.getpid())}) == "alive"
    assert sessions.process_state({}) is None


@LINUX
def test_next_turn_waits_for_an_idle_inbox_however_long(env, monkeypatch):  # noqa: F811
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        monkeypatch.setattr(hook, "agent_pid", lambda kind: sleeper.pid)
        connector = env.connector()
        env.session("s1")
        env.clock.now += 3 * 3600  # the user is away for hours
        mid = send(env.api, env.api.alice, "bob", "hello")["id"]
        connector.run_once()
        assert connector.queue.get(mid)["state"] == q.HANDED_OVER
        env.clock.now += 3 * 3600
        connector.run_once()
        assert connector.queue.get(mid)["state"] == q.HANDED_OVER  # never reclaimed while the process lives
        assert f"[end of RainCLI message {mid}]" in env.session("s1", "UserPromptSubmit")
        connector.run_once()
        assert connector.queue.get(mid)["state"] == q.SUBMITTED
        # Its process exits with a file pending: reclaimed and held offline.
        other = send(env.api, env.api.alice, "bob", "second")["id"]
        connector.run_once()
        assert connector.queue.get(other)["state"] == q.HANDED_OVER
        sleeper.kill()
        sleeper.wait()
        connector.run_once()
        assert (connector.queue.get(other)["state"], connector.queue.get(other)["hold_reason"]) == (q.HELD, "offline")
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


# -- 8. the claim window never loses a message ----------------------------------------------------

def test_slow_hook_claim_is_submitted_not_uncertain(env):  # noqa: F811
    connector = env.connector()
    env.session("s1")
    mid = send(env.api, env.api.alice, "bob", "hello")["id"]
    connector.run_once()
    key = sessions.agent_key(env.salt, "claude:s1")
    inbox = Path(env.state, "sessions", key + ".inbox")
    os.utime(inbox / (mid + ".md"), (1, 1))  # an ancient mtime, as rename keeps it
    texts, ids = sessions.claim(str(env.state), key)  # the hook has renamed, not yet receipted
    connector.run_once()
    assert connector.queue.get(mid)["state"] == q.HANDED_OVER
    assert (inbox / (mid + ".md.claimed")).exists()  # never settled under a running hook
    sessions.write_receipts(str(env.state), key, ids)
    connector.run_once()
    assert connector.queue.get(mid)["state"] == q.SUBMITTED


def test_hook_skips_a_file_settled_under_it(state):
    key = sessions.agent_key(state.salt, "claude:s1")
    sessions.hand_over(state.dir, key, "11111111-1111-4111-8111-111111111111", "one")
    sessions.hand_over(state.dir, key, "22222222-2222-4222-8222-222222222222", "two")
    real = sessions._read_regular
    calls = []

    def racing(path, limit):
        calls.append(path)
        if len(calls) == 1:
            os.unlink(path)  # the connector settled it in the meantime
        return real(path, limit)
    sessions._read_regular = racing
    try:
        texts, ids = sessions.claim(state.dir, key)
    finally:
        sessions._read_regular = real
    assert len(ids) == 1 and texts in (["one"], ["two"])
    sessions.write_receipts(state.dir, key, ids + ["11111111-1111-4111-8111-111111111111"])
    receipts = list(Path(state.dir, "sessions", key + ".inbox").glob("*.receipt"))
    assert len(receipts) == 1  # no receipt for a file that is no longer claimed


# -- 17. handover failures do not grow history ----------------------------------------------------

@POSIX
def test_unsafe_inbox_dir_holds_without_growing_history(env):  # noqa: F811
    connector = env.connector()
    env.session("s1")
    key = sessions.agent_key(env.salt, "claude:s1")
    inbox = Path(sessions.inbox_dir(str(env.state), key, create=True))
    inbox.chmod(0o755)
    mid = send(env.api, env.api.alice, "bob", "hello")["id"]
    for _ in range(50):
        connector.run_once()
    record = connector.queue.get(mid)
    assert (record["state"], record["hold_reason"]) == (q.HELD, "offline")
    assert len(record["history"]) <= 3


# -- 9. a maximal report fits ----------------------------------------------------------------------

def test_maximal_report_is_sent_as_utf8_under_the_body_limit():
    agents = [{"key": f"{i:032x}", "name": "\U0001F600" * 64, "type": "claude", "status": "idle",
               "role": None, "reachability": None, "source": "herdr"} for i in range(100)]
    body = {"status": "ready", "agents": agents,
            "client": {"version": "0.3.0", "update_mode": "automatic", "update_state": "current", "error": None}}
    encoded = api_mod.encode_body(body)
    assert len(encoded) < 64 * 1024 and json.loads(encoded.decode("utf-8")) == body
    assert api_mod.encode_body({"x": "\ud800"})  # a lone surrogate falls back to escapes


# -- 10 and 11. scan by comm, no duplicates ------------------------------------------------------------

def test_scan_matches_native_and_titled_agents_by_comm(state):
    processes = {
        10: ("bash", 1, "bash"),
        11: ("2.1.119", 10, "claude"),        # native Claude Code: …/versions/2.1.119
        12: ("node", 10, "gemini"),           # a Node agent that sets its process title
        13: ("node", 10, "cursor-agent"),
        14: ("node", 10, "node"),             # an unrelated Node program
        20: ("herdr", 1, "herdr"),
        21: ("2.1.119", 20, "claude"),        # in a Herdr pane
    }
    found = discovery.linux_scan(state.salt, set(), True, processes=processes, cwd_name=lambda pid: "")
    assert sorted(a["type"] for a in found) == ["claude", "cursor", "gemini"]


@LINUX
def test_linux_processes_honours_the_proc_root(tmp_path):
    proc = tmp_path / "proc"
    (proc / "42").mkdir(parents=True)
    (proc / "42/stat").write_text("42 (my agent) S 7 1 1")
    (proc / "42/comm").write_text("claude\n")
    os.symlink("/opt/claude/versions/2.1.119", proc / "42/exe")
    (proc / "self").mkdir()
    assert discovery.linux_processes(str(proc)) == {42: ("claude", 7, "claude")}  # native path counts as claude


def test_hook_session_inside_herdr_is_listed_once(state):
    hooked = [{"key": "a" * 32, "name": "app", "type": "claude", "status": "idle", "role": None,
               "reachability": None, "source": "hook", "_pid": 31},
              {"key": "b" * 32, "name": "inbox", "type": "claude", "status": "idle", "role": "inbox",
               "reachability": "next-turn", "source": "hook", "_pid": 32},
              {"key": "c" * 32, "name": "term", "type": "claude", "status": "idle", "role": None,
               "reachability": None, "source": "hook", "_pid": 41}]
    processes = {30: ("herdr", 1, "herdr"), 31: ("claude", 30, "claude"), 32: ("claude", 30, "claude"),
                 40: ("bash", 1, "bash"), 41: ("claude", 40, "claude")}
    kept, _ = discovery.without_duplicates(hooked, [], True, processes, None)
    assert [h["name"] for h in kept] == ["inbox", "term"]  # Herdr lists app; the mapped inbox stays
    kept, _ = discovery.without_duplicates(hooked, [], False, processes, None)
    assert len(kept) == 3  # Herdr unreadable: nobody else would list them


def test_windows_hooked_sessions_are_not_scanned_again(monkeypatch):
    monkeypatch.setattr(discovery.os, "name", "nt")
    hooked = [{"key": "a" * 32, "name": "proj", "type": "claude", "status": "idle", "role": None,
               "reachability": None, "source": "hook", "_pid": None}]
    scanned = [discovery.entry(b"s" * 32, f"scan:claude:{pid}", "claude", "claude", "unknown", "scan")
               for pid in (1, 2)] + [discovery.entry(b"s" * 32, "scan:codex:3", "codex", "codex", "unknown", "scan")]
    _, kept = discovery.without_duplicates(hooked, scanned, False, None, None)
    assert sorted(a["type"] for a in kept) == ["claude", "codex"]


def test_discover_never_reports_local_pids(state, monkeypatch):
    monkeypatch.setattr(hook, "agent_pid", lambda kind: os.getpid())
    fire(state.dir, "SessionStart")
    found = discovery.discover(state.dir, state.salt, None, None, include_scan=False)
    assert all(set(a) == {"key", "name", "type", "status", "role", "reachability", "source"} for a in found)
    assert presence_problem({"status": "ready", "agents": found}) is None


# -- 13. strict versions ---------------------------------------------------------------------------------

@pytest.mark.parametrize("tag, ok", [("v0.3.0", True), ("v10.0.9999", True), ("v0.03.0", False),
                                     ("v0.4.00", False), ("v01.0.0", False), ("v0.3", False)])
def test_tags_have_no_leading_zeros(tag, ok, tmp_path):
    assert bool(updates.TAG_RE.fullmatch(tag)) is ok
    updater = controller(tmp_path)
    updater.consider(target(tag))
    settle(updater)
    if not ok:
        assert updater.calls == [] and updater.client()["error"] == "bad_target"


# -- 4 and 6. one install per target, transient errors back off -------------------------------------------

def test_installed_target_awaiting_the_switch_is_not_installed_again(tmp_path):
    clock = Clock()
    updater = controller(tmp_path, clock=clock)
    updater.consider(target(NEWER))
    settle(updater)
    for _ in range(5):
        updater.consider(target(NEWER))
        settle(updater)
        clock.now += 30
    assert len(updater.calls) == 1 and updater.client()["update_state"] == "updating"
    clock.now += pushed.SWITCH_TIMEOUT
    updater.consider(target(NEWER))
    assert (updater.client()["update_state"], updater.client()["error"]) == ("failed", "switch_timeout")
    clock.now += pushed.BACKOFF_START
    updater.consider(target(NEWER))
    settle(updater)
    assert len(updater.calls) == 2  # retried after the backoff, not blocked


def test_install_that_finds_the_pointer_already_switched_keeps_probation(tmp_path):
    updater = controller(tmp_path, install=lambda release: {"status": "current", **release})
    updater.consider(target(NEWER))
    settle(updater)
    assert updater.client()["update_state"] == "updating"  # never "current" before the new version runs


@pytest.mark.parametrize("error", [ConnectorBusy("another update"), OSError(28, "No space left on device"),
                                   subprocess.TimeoutExpired(["venv"], 90),
                                   subprocess.CalledProcessError(1, ["venv"]), ValueError("bad JSON")])
def test_transient_install_errors_back_off_and_do_not_block(tmp_path, error):
    clock = Clock()
    calls = []

    def failing(release):
        calls.append(release)
        raise error
    updater = controller(tmp_path, install=failing, clock=clock)
    updater.consider(target(NEWER))
    settle(updater)
    client = updater.client()
    assert client["update_state"] == "failed" and client["error"].startswith("install_failed:")
    assert pushed.ERROR_RE.fullmatch(client["error"]) and not updater.data.get("blocked")
    clock.now += pushed.BACKOFF_START
    updater.consider(target(NEWER))
    settle(updater)
    assert len(calls) == 2


def test_staged_client_failures_are_verification_failures(tmp_path, monkeypatch):
    """A staged client that crashes on --version is the release's fault: blocked."""
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        root = "raincli-" + "c" * 40 + "/raincli/raincli_agent/"
        z.writestr(root + "__init__.py", '__version__ = "9.9.9"\n')
        z.writestr(root + "__main__.py", "raise SystemExit(5)\n")
        z.writestr(root + "runtime/launcher.py", "\n")
    monkeypatch.setattr(updates, "fetch", lambda url, limit: buf.getvalue())
    with pytest.raises(updates.VerificationError):
        updates.install(tmp_path / "m", {"tag": "v9.9.9", "commit": "c" * 40})


# -- 2, 3 and 5. launcher -------------------------------------------------------------------------------------

def fake_versions(tmp_path, new_script, old_script="exit 0\n"):
    root = tmp_path / "client"
    old = root / "versions/old/venv/bin/python"
    new = root / "versions/new/venv/bin/python"
    for path, script in ((old, old_script), (new, new_script)):
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(0o755)
    (root / "launch.py").write_bytes(Path(launcher.__file__).read_bytes())
    pointer = {"tag": "v0.4.0", "commit": "b" * 40, "python": str(new), "automatic": False,
               "update_mode": "automatic", "update_mode_chosen": True,
               "previous": {"tag": "v0.3.0", "commit": "a" * 40, "python": str(old)}}
    atomic_write_json(root / "current.json", pointer)
    key = {"version": "v0.4.0", "allow_downgrade": False, "set_at": None}
    atomic_write_json(root / "update-state.json", {"state": "updating", "error": None, "target": key})
    return root, pointer, key


@POSIX
def test_first_start_that_exits_zero_is_rolled_back(tmp_path):
    root, pointer, key = fake_versions(tmp_path, "exit 0\n")
    result = subprocess.run([sys.executable, str(root / "launch.py"), "runtime", "run", "--config", "x.json"],
                            capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads((root / "current.json").read_text())["tag"] == "v0.3.0"
    assert json.loads((root / "update-state.json").read_text())["state"] == "rolled_back"


@POSIX
def test_rollback_waits_for_the_update_lock_and_never_undoes_a_newer_change(tmp_path):
    root, pointer, key = fake_versions(tmp_path, "exit 3\n")
    lock = Queue(str(root / "update-lock"))
    lock.acquire_run_lock()
    released = threading.Timer(0.5, lock.release_run_lock)
    released.start()
    started = time.monotonic()
    assert launcher.roll_back(root, pointer)
    assert time.monotonic() - started >= 0.4  # it waited for the lock
    released.join()
    assert json.loads((root / "current.json").read_text())["tag"] == "v0.3.0"

    root2, pointer2, _ = fake_versions(tmp_path / "second", "exit 3\n")
    moved = {**pointer2, "commit": "f" * 40, "tag": "v0.5.0"}  # an operator install won the race
    atomic_write_json(root2 / "current.json", moved)
    assert not launcher.roll_back(root2, pointer2)
    assert json.loads((root2 / "current.json").read_text()) == moved


@POSIX
def test_configure_serialises_with_a_launcher_rollback(tmp_path):
    root, pointer, key = fake_versions(tmp_path, "exit 3\n")
    done = []
    lock = Queue(str(root / "update-lock"))
    lock.acquire_run_lock()
    worker = threading.Thread(target=lambda: done.append(updates.configure(root, mode="manual")))
    worker.start()
    time.sleep(0.3)
    assert not done  # configure waits for the lock rather than failing
    lock.release_run_lock()
    worker.join(10)
    assert done and done[0]["update_mode"] == "manual"


@POSIX
def test_broken_new_launcher_is_never_adopted(tmp_path):
    root, pointer, key = fake_versions(tmp_path, "exit 0\n", old_script='echo "raincli 0.3.0"\n')
    new_dir = Path(pointer["python"]).parents[2]
    (new_dir / "launch.py").write_text("raise RuntimeError('broken at import')\n")
    before = (root / "launch.py").read_bytes()
    assert updates.adopt_launcher(root, pointer["python"], base_python=sys.executable) == "candidate_failed"
    assert (root / "launch.py").read_bytes() == before
    (new_dir / "launch.py").write_bytes(before + b"\n# a working new launcher\n")
    atomic_write_json(root / "current.json", {**pointer, "python": pointer["previous"]["python"]})
    assert updates.adopt_launcher(root, pointer["python"], base_python=sys.executable) == "adopted"


# -- 15 and 16. mode migration -----------------------------------------------------------------------------------

def test_migrate_mode_never_blocks_on_a_busy_update_lock(tmp_path):
    root, _ = managed_root(tmp_path, {"tag": "v0.2.0", "commit": "a" * 40, "python": "p", "automatic": False})
    lock = Queue(str(root / "update-lock"))
    lock.acquire_run_lock()
    try:
        assert updates.migrate_mode(root) is None  # retried on a later start
    finally:
        lock.release_run_lock()
    assert updates.migrate_mode(root)  # now it runs, with the notice


def test_manual_choice_survives_a_pointer_rebuilt_by_v020(tmp_path):
    root, _ = managed_root(tmp_path)
    updates.configure(root, mode="manual")
    # An explicit rollback to v0.2 followed by a v0.2 --install rebuilds the pointer without the keys.
    atomic_write_json(root / "current.json", {"tag": "v0.3.0", "commit": "c" * 40, "python": "p", "automatic": False})
    assert updates.migrate_mode(root) is None  # no notice, and ...
    assert updates.read_pointer(root)["update_mode"] == "manual" and updates.update_mode(root) == "manual"


# -- 18 and 19. hook edge cases ----------------------------------------------------------------------------------

def test_name_dash_h_is_a_name_not_help(state):
    result = subprocess.run([sys.executable, "-m", "raincli_agent", "hook", "claude", "SessionStart",
                             "--name", "-h", "--state-dir", state.dir],
                            input=json.dumps({"session_id": "s"}).encode(), capture_output=True, timeout=20,
                            env={**os.environ, "PYTHONPATH": str(RAINCLI)})
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    assert [r["name"] for r in sessions.read_sessions(state.dir)] == ["-h"]


def test_hook_help_has_one_usage_line():
    result = subprocess.run([sys.executable, "-m", "raincli_agent", "hook", "--help"], capture_output=True,
                            text=True, timeout=20, env={**os.environ, "PYTHONPATH": str(RAINCLI)})
    assert result.stdout.count("usage:") == 1


@POSIX
def test_backups_go_beside_the_link_and_are_pruned(tmp_path, state):
    home = tmp_path / "home"
    dotfiles = tmp_path / "dotfiles"
    (home / ".claude").mkdir(parents=True)
    dotfiles.mkdir()
    (dotfiles / "settings.json").write_text("{}")
    os.symlink(dotfiles / "settings.json", home / ".claude/settings.json")
    prefix = (["/usr/bin/raincli"], "test")
    for n in range(5):
        hooks_install.install("claude", state.dir, home=home, prefix=prefix)
        hooks_install.install("claude", state.dir, home=home, prefix=prefix, remove=True)
    assert not list(dotfiles.glob("*backup*"))  # nothing written into the dotfiles repository
    assert len(list((home / ".claude").glob("settings.json.raincli-backup-*"))) == hooks_install.KEEP_BACKUPS
    assert (home / ".claude/settings.json").is_symlink()
