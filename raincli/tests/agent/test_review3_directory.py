"""Review 3: the launcher check runs the real `runtime run` path (M1, L1, L2), macOS
start times do not depend on TZ or locale (N1), pruning (N2), namespaced pids (N3)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from raincli_agent.connector.queue import Queue
from raincli_agent.fsutil import atomic_write_json
from raincli_agent.runtime import discovery, hooks_install, launcher, launcher_check, procinfo, sessions, updates

from .test_review1_directory import fake_versions

POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX")
LAUNCHER = Path(launcher.__file__).read_text()
BRANCH = "    config = args[args.index"


def mutate(old, new):
    assert old in LAUNCHER, old
    return LAUNCHER.replace(old, new, 1)


CANDIDATES = {
    "a raise after the run branch": mutate(BRANCH, "    raise RuntimeError('run path bug')\n" + BRANCH),
    "b early exit 0": mutate(BRANCH, "    return 0\n" + BRANCH),
    "c hang without a runtime": mutate(BRANCH, "    time.sleep(10**6)\n" + BRANCH),
    "d broken switch": mutate('if (pointer["commit"], python) != (current[0]["commit"], current[1]):',
                              'if (pointer["comit"], python) != (current[0]["commit"], current[1]):'),
    "e wrong runtime module": mutate('"-m", "raincli_agent", *args],\n                     own=',
                                     '"-m", "raincli_agnt", *args],\n                     own='),
    "f raise only when own is set": mutate("    stopped = False\n    process = None\n",
                                           "    if own is not None: raise RuntimeError('own bug')\n"
                                           "    stopped = False\n    process = None\n"),
}


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(launcher_check, "START_TIMEOUT", 8)
    monkeypatch.setattr(launcher_check, "SWITCH_TIMEOUT", 15)
    monkeypatch.setattr(launcher_check, "EXIT_TIMEOUT", 10)


@POSIX
@pytest.mark.parametrize("name", sorted(CANDIDATES))
def test_launchers_broken_on_the_run_path_are_refused(tmp_path, quick, name):
    root, pointer, _ = fake_versions(tmp_path, "exit 0\n")
    atomic_write_json(root / "update-state.json", {"state": "current", "error": None})
    new_dir = Path(pointer["python"]).parents[2]
    (new_dir / "launch.py").write_text(CANDIDATES[name])
    before = (root / "launch.py").read_bytes()
    assert updates.adopt_launcher(root, pointer["python"], base_python=sys.executable) == "candidate_failed"
    assert (root / "launch.py").read_bytes() == before
    assert not list(root.glob(".launcher-check-*"))  # the scratch root is removed


@POSIX
def test_a_healthy_new_launcher_passes_the_real_run_path(tmp_path, quick):
    root, pointer, _ = fake_versions(tmp_path, "exit 0\n")
    new_dir = Path(pointer["python"]).parents[2]
    (new_dir / "launch.py").write_text(LAUNCHER + "\n# a new release\n")
    started = time.monotonic()
    assert updates.adopt_launcher(root, pointer["python"], base_python=sys.executable) == "adopted"
    assert (root / "launch.py").read_text().endswith("# a new release\n")
    assert time.monotonic() - started < 60


def test_the_check_never_touches_the_real_root(tmp_path, quick):
    root = tmp_path / "client"
    root.mkdir()
    atomic_write_json(root / "current.json", {"tag": "v9.9.9"})
    assert launcher_check.check(LAUNCHER.encode(), root, sys.executable) is None
    assert json.loads((root / "current.json").read_text()) == {"tag": "v9.9.9"}
    assert sorted(p.name for p in root.iterdir()) == ["current.json"]


@POSIX
def test_rollback_checks_the_previous_launcher_without_the_current_client(tmp_path, quick):
    """L1: the current client is broken (the reason for the rollback); the previous
    version's launcher is still checked (against stubs) and restored."""
    root, pointer, _ = fake_versions(tmp_path, "exit 7\n", old_script="exit 0\n")
    old_dir = Path(pointer["previous"]["python"]).parents[2]
    (old_dir / "launch.py").write_text(LAUNCHER + "\n# the previous release\n")
    assert updates.configure(root, rollback=True)["tag"] == "v0.3.0"
    assert (root / "launch.py").read_text().endswith("# the previous release\n")


@POSIX
def test_pushed_adoption_takes_the_update_lock(tmp_path, quick):
    root, pointer, _ = fake_versions(tmp_path, "exit 0\n")
    (Path(pointer["python"]).parents[2] / "launch.py").write_text(LAUNCHER + "\n# new\n")
    lock = Queue(str(root / "update-lock"))
    lock.acquire_run_lock()
    try:
        assert updates.adopt_launcher_locked(root, pointer["python"]) is None  # busy: retried on a later tick
    finally:
        lock.release_run_lock()
    assert updates.adopt_launcher_locked(root, pointer["python"]) == "adopted"


# -- N1 ---------------------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt" or not shutil.which("ps"), reason="ps")
def test_ps_start_time_is_the_same_under_any_tz_and_locale(monkeypatch):
    pid = os.getpid()
    readings = set()
    for tz, lang in (("UTC", "C"), ("Asia/Tokyo", "de_DE.UTF-8"), ("America/New_York", "ja_JP.UTF-8")):
        monkeypatch.setenv("TZ", tz)
        monkeypatch.setenv("LANG", lang)
        monkeypatch.setenv("LC_ALL", lang)
        readings.add(procinfo.ps_start(pid))
    assert len(readings) == 1 and None not in readings
    assert procinfo.ps_env()["TZ"] == "UTC" and procinfo.ps_env()["LC_ALL"] == "C"


# -- N2 ---------------------------------------------------------------------------------------

@POSIX
def test_the_new_backup_survives_a_clock_step_back(tmp_path, monkeypatch):
    directory = tmp_path / "claude"
    directory.mkdir()
    for n, stamp in enumerate(("20260930-120000", "20260930-120100", "20260930-120200"), 1):
        (directory / f"settings.json.raincli-backup-{stamp}-{n}").write_text("old")
    monkeypatch.setattr(hooks_install.time, "strftime", lambda fmt, *a: "20260930-110000")  # DST fall-back
    path = directory / "settings.json"
    path.write_text("{}")
    backup = hooks_install.write(path, b"{}", {"hooks": {}})
    assert backup.exists() and backup.name.endswith("-4")
    assert sorted(c for _, c, _ in hooks_install.own_backups(directory, "settings.json")) == [2, 3, 4]


# -- N3 ---------------------------------------------------------------------------------------

def test_a_pid_from_another_namespace_is_not_claimed(tmp_path, monkeypatch):
    state = tmp_path / "s"
    state.mkdir(mode=0o700)
    salt = sessions.ensure_salt(str(state))
    sessions.sessions_dir(str(state), create=True)
    key = sessions.agent_key(salt, "claude:ns")
    sessions.write_record(str(state), {"key": key, "type": "claude", "name": "inbox", "status": "idle",
                                       "updated_at": time.time(), "pid": 2, "pid_start": 1, "pid_ns": "pid:[1]"})
    monkeypatch.setattr(procinfo, "pid_namespace", lambda: "pid:[4026531836]")
    found, pids = discovery.hook_entries(salt, str(state), None)
    assert pids == set() and found[0]["_pid"] is None
