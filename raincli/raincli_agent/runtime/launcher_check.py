"""Check a candidate launcher on its real ``runtime run`` path before adopting it.

The candidate is copied into a private scratch root whose pointer names stub
runtimes (real virtual environments holding a stub ``raincli_agent`` plus the
client's own ``fsutil``, which the launcher's rollback writes through), and is
started exactly as the service starts it: ``launch.py runtime run --config …``.
Phases, each bounded:

1. **start:** stub one runs;
2. **relaunch and switch:** ``launch.py`` is rewritten and the pointer moves
   to stub two; stub one is stopped gracefully, the launcher re-executes its new
   file and starts stub two;
3. **probation and rollback:** the update state says ``updating`` to stub three,
   which fails its first start (exit 3); the launcher must restore stub two
   (pointer back, state ``rolled_back``) and start it again;
4. **clean exit:** stub two stops on its own and the launcher exits 0.

Nothing in the real managed root, and no real client, is involved, so the same
check applies to a new version's launcher and to a rollback's (review 3 M1 and
L1, review 4 findings 1, 2 and 5). There is no self-check flag to short-circuit.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from ..fsutil import atomic_write_bytes, atomic_write_json

PREFIX = ".launcher-check-"
START_TIMEOUT = 30
SWITCH_TIMEOUT = 60
EXIT_TIMEOUT = 30
STUB_LIFETIME = 150  # a stub exits on its own even if a check is interrupted
STALE_SCRATCH = 900

STUB = '''\
import os, pathlib, signal, sys, time
NAME, MARKERS, MODE, LIFETIME = {name!r}, {markers!r}, {mode!r}, {lifetime!r}
marker = lambda suffix: pathlib.Path(MARKERS, NAME + suffix)
def note(suffix, text):
    with open(marker(suffix), "a") as fh:
        fh.write(text + "\\n")
args = sys.argv[1:]
if args == ["--version"]:
    print("raincli 0.0.0 stub " + NAME)
    sys.exit(0)
if args[:2] == ["runtime", "stop"]:  # the launcher's Windows stop request
    marker(".stop").write_text("1")
    sys.exit(0)
if args[:2] != ["runtime", "run"]:
    sys.exit(2)
try:
    marker(".stop").unlink()
except OSError:
    pass
note(".started", str(os.getpid()))
if MODE == "fail":
    sys.exit(3)  # a failed first start
stopped = []
signal.signal(signal.SIGTERM, lambda *_: stopped.append(1))
begun = time.monotonic()
while not stopped and not marker(".stop").exists() and time.monotonic() - begun < LIFETIME:
    if marker(".release").exists():
        break
    time.sleep(0.05)
note(".exited", str(os.getpid()))
sys.exit(0)
'''


def isolated_env():
    """No inherited module path, and never the working directory on sys.path
    (PYTHONSAFEPATH, Python 3.11+); the checks also run with cwd=scratch."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE")}
    env["PYTHONSAFEPATH"] = "1"
    return env


def make_stub(scratch, name, mode):
    """A stub version at ``scratch/versions/<name>/venv``."""
    env_dir = scratch / "versions" / name / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(env_dir)], check=True, timeout=90,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   cwd=str(scratch), env=isolated_env())
    python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    purelib = subprocess.run([str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                             check=True, capture_output=True, text=True, timeout=30, cwd=str(scratch),
                             env=isolated_env()).stdout.strip()
    package = Path(purelib) / "raincli_agent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(STUB.format(name=name, markers=str(scratch / "markers"), mode=mode,
                                                     lifetime=STUB_LIFETIME))
    # The launcher's rollback writes through raincli_agent.fsutil (the owner and
    # DACL on Windows): give the stub the client's own copy.
    client = Path(__file__).resolve().parents[1]
    for module in ("fsutil.py", "errors.py", "_winfiles.py"):
        shutil.copyfile(client / module, package / module)
    return python


def lines(path):
    try:
        return [line for line in path.read_text().splitlines() if line.strip()]
    except OSError:
        return []


def wait(condition, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def kill_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def kill_stubs(markers):
    """Stub runtimes run in their own sessions: stop every one this check started."""
    for path in markers.glob("*.started"):
        for text in lines(path):
            try:
                pid = int(text)
            except ValueError:
                continue
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
                else:
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def sweep(root):
    """Remove scratch roots left by a check that was interrupted (a killed runtime)."""
    for path in Path(root).glob(PREFIX + "*"):
        try:
            if path.is_dir() and time.time() - path.stat().st_mtime > STALE_SCRATCH:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def drain(scratch, markers, process):
    """Let a still-running candidate end on its own: every stub is released, no
    probation, and the pointer moves to a stub that exits 0 at once, which makes
    a working launcher return 0. Needed on Windows, where a re-launched launcher
    is detached and cannot be killed by process handle."""
    if os.name != "nt" and (process is None or process.poll() is not None):
        return
    try:
        for name in ("one", "two", "three"):
            (markers / f"{name}.release").write_text("1")
        (scratch / "update-state.json").unlink(missing_ok=True)
        before = len(lines(markers / "one.exited"))
        pointer = read_json(scratch / "current.json")
        one = scratch / "versions" / "one" / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        atomic_write_json(scratch / "current.json", {**pointer, "tag": "v0.0.9", "commit": "9" * 40,
                                                     "python": str(one), "previous": {}})
        wait(lambda: len(lines(markers / "one.exited")) > before
             or (process is not None and os.name != "nt" and process.poll() is not None), 15)
    except OSError:
        pass


def check(data, root, base_python=None):
    """Return None when the candidate launcher ``data`` passes, else a short reason."""
    base = base_python or getattr(sys, "_base_executable", sys.executable)
    compile(data, "launch.py", "exec")
    sweep(root)
    scratch = Path(tempfile.mkdtemp(prefix=PREFIX, dir=str(root)))
    markers = scratch / "markers"
    process = None
    passed = False
    run = dict(cwd=str(scratch), env=isolated_env(), stdin=subprocess.DEVNULL)
    try:
        os.chmod(scratch, 0o700)
        markers.mkdir()
        one = make_stub(scratch, "one", "wait")
        two = make_stub(scratch, "two", "wait")
        three = make_stub(scratch, "three", "fail")
        base_pointer = {"automatic": False, "update_mode": "manual", "update_mode_chosen": True}
        atomic_write_json(scratch / "current.json", {**base_pointer, "tag": "v0.0.1", "commit": "1" * 40,
                                                     "python": str(one)})
        atomic_write_bytes(scratch / "launch.py", data)
        config = scratch / "runtime.json"
        config.write_text(json.dumps({"connectors": ["stub.json"]}))

        version = subprocess.run([str(base), str(scratch / "launch.py"), "--version"], capture_output=True,
                                 text=True, timeout=START_TIMEOUT, **run)
        if version.returncode != 0 or "stub one" not in version.stdout:
            return "passthrough_failed"
        process = subprocess.Popen([str(base), str(scratch / "launch.py"), "runtime", "run", "--config", str(config)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=os.name != "nt",
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **run)
        # 1. start
        if not wait(lambda: len(lines(markers / "one.started")) == 1, START_TIMEOUT):
            return "runtime_not_started"
        # 2. relaunch and switch: a changed launch.py is re-executed at the switch.
        atomic_write_bytes(scratch / "launch.py", data + b"\n# relaunch check\n")
        atomic_write_json(scratch / "current.json", {**base_pointer, "tag": "v0.0.2", "commit": "2" * 40,
                                                     "python": str(two)})
        if not wait(lambda: lines(markers / "one.exited") and len(lines(markers / "two.started")) == 1,
                    SWITCH_TIMEOUT):
            return "switch_failed"
        # 3. probation: stub three fails its first start and must be rolled back.
        target = {"version": "v0.0.3", "allow_downgrade": False, "set_at": None}
        atomic_write_json(scratch / "update-state.json", {"state": "updating", "error": None, "target": target})
        atomic_write_json(scratch / "current.json", {
            **base_pointer, "tag": "v0.0.3", "commit": "3" * 40, "python": str(three),
            "previous": {"tag": "v0.0.2", "commit": "2" * 40, "python": str(two)}})

        def rolled_back():
            return (read_json(scratch / "current.json").get("tag") == "v0.0.2"
                    and read_json(scratch / "update-state.json").get("state") == "rolled_back"
                    and len(lines(markers / "two.started")) == 2)
        if not wait(rolled_back, SWITCH_TIMEOUT):
            return "rollback_failed"
        # 4. clean exit when the runtime stops on its own.
        (markers / "two.release").write_text("1")
        if not wait(lambda: len(lines(markers / "two.exited")) == 2, EXIT_TIMEOUT):
            return "did_not_stop"
        if os.name == "nt":
            # Windows re-launches as a detached process: no further start means it exited.
            time.sleep(3)
            if len(lines(markers / "two.started")) != 2:
                return "restarted_after_exit"
            passed = True
            return None
        try:
            code = process.wait(timeout=EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            return "did_not_exit"
        if code != 0:
            return "exit_status_%d" % code if code >= 0 else "killed"
        passed = True
        return None
    finally:
        if not passed:
            drain(scratch, markers, process)
        if process is not None:
            kill_process(process)
        kill_stubs(markers)
        shutil.rmtree(scratch, ignore_errors=True)
