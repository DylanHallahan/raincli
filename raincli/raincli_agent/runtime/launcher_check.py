"""Check a candidate launcher on its real ``runtime run`` path before adopting it.

The candidate is copied into a private scratch root whose pointer names a stub
runtime (a real virtual environment holding a stub ``raincli_agent``), and is
started exactly as the service starts it: ``launch.py runtime run --config …``.
It must start the stub, switch to a second stub when the pointer changes
(stopping the first gracefully), and exit 0 when the second stub stops on its
own. Nothing in the real managed root, and no real client, is involved, so the
same check applies to a new version's launcher and to a rollback's
(review 3, M1 and L1). The candidate cannot short-circuit it: there is no
self-check flag, only the run path the service uses.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from ..fsutil import atomic_write_bytes, atomic_write_json

START_TIMEOUT = 30
SWITCH_TIMEOUT = 60
EXIT_TIMEOUT = 30

STUB = '''\
import os, pathlib, signal, sys, time
NAME, MARKERS, EXIT_AFTER_START = {name!r}, {markers!r}, {exit_after!r}
marker = lambda suffix: pathlib.Path(MARKERS, NAME + suffix)
args = sys.argv[1:]
if args == ["--version"]:
    print("raincli 0.0.0 stub " + NAME)
    sys.exit(0)
if args[:2] == ["runtime", "stop"]:  # the launcher's Windows stop request
    marker(".stop").write_text("1")
    sys.exit(0)
if args[:2] != ["runtime", "run"]:
    sys.exit(2)
marker(".started").write_text(str(os.getpid()))
stopped = []
signal.signal(signal.SIGTERM, lambda *_: stopped.append(1))
begun = time.monotonic()
while not stopped and not marker(".stop").exists() and time.monotonic() - begun < 120:
    if EXIT_AFTER_START and time.monotonic() - begun > 1:
        break
    time.sleep(0.05)
marker(".exited").write_text("1")
sys.exit(0)
'''


def isolated_env():
    return {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE")}


def make_stub(scratch, name, exit_after_start):
    """A stub version at ``scratch/versions/<name>/venv`` whose runtime writes markers."""
    env_dir = scratch / "versions" / name / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(env_dir)], check=True, timeout=90,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    purelib = subprocess.run([str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                             check=True, capture_output=True, text=True, timeout=30, env=isolated_env()).stdout.strip()
    package = Path(purelib) / "raincli_agent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(STUB.format(name=name, markers=str(scratch / "markers"),
                                                     exit_after=exit_after_start))
    return python


def wait(condition, timeout, process):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        if process.poll() is not None and not condition():
            return False
        time.sleep(0.05)
    return False


def kill(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
    else:
        try:
            os.killpg(process.pid, 9)
        except OSError:
            process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def check(data, root, base_python=None):
    """Return None when the candidate launcher ``data`` passes, else a short reason."""
    base = base_python or getattr(sys, "_base_executable", sys.executable)
    compile(data, "launch.py", "exec")
    scratch = Path(tempfile.mkdtemp(prefix=".launcher-check-", dir=str(root)))
    process = None
    try:
        os.chmod(scratch, 0o700)
        (scratch / "markers").mkdir()
        one = make_stub(scratch, "one", exit_after_start=False)
        two = make_stub(scratch, "two", exit_after_start=True)
        pointer = {"tag": "v0.0.0", "commit": "0" * 40, "python": str(one), "automatic": False,
                   "update_mode": "manual", "update_mode_chosen": True}
        atomic_write_json(scratch / "current.json", pointer)
        atomic_write_bytes(scratch / "launch.py", data)
        config = scratch / "runtime.json"
        config.write_text(json.dumps({"connectors": ["stub.json"]}))
        markers = scratch / "markers"
        version = subprocess.run([str(base), str(scratch / "launch.py"), "--version"], capture_output=True,
                                 text=True, timeout=START_TIMEOUT, env=isolated_env(), stdin=subprocess.DEVNULL)
        if version.returncode != 0 or "stub one" not in version.stdout:
            return "passthrough_failed"
        process = subprocess.Popen([str(base), str(scratch / "launch.py"), "runtime", "run", "--config", str(config)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=isolated_env(), start_new_session=os.name != "nt",
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not wait(lambda: (markers / "one.started").exists(), START_TIMEOUT, process):
            return "runtime_not_started"
        atomic_write_json(scratch / "current.json", {**pointer, "commit": "1" * 40, "python": str(two)})
        if not wait(lambda: (markers / "one.exited").exists() and (markers / "two.started").exists(),
                    SWITCH_TIMEOUT, process):
            return "switch_failed"
        try:
            code = process.wait(timeout=EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            return "did_not_exit"
        return None if code == 0 else "exit_status_%d" % code if code >= 0 else "killed"
    finally:
        if process is not None:
            kill(process)
        shutil.rmtree(scratch, ignore_errors=True)
