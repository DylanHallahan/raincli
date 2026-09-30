"""Standalone managed launcher, copied outside versioned environments.

Uses the operator's base Python. Ordinary commands run the managed client with
stdin, stdout, stderr and the exit status passed through unchanged. For
``runtime run`` it polls a local pointer, gracefully stops the runtime (which
stops its connectors and releases their queue locks) before starting the newly
selected environment. It never checks for releases itself: the runtime installs
a team's pushed target (protocol 14.5). A version the runtime just installed is
on probation until it reports ``current``: if it exits with an error first, the
previous version is restored and ``rolled_back`` recorded (14.7 H2/M9).
Versioned environments are never deleted here, so an environment still in use
(Windows keeps its files open) remains available for rollback.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

PROBATION = 300  # a newly installed version must reach its first tick within this
GRACEFUL_STOP = 120  # the runtime needs <= 100 s (service.py stop budget), connectors in parallel
# Started at logon by pythonw.exe there is no console: do not open one per child.
HIDDEN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" and sys.stdout is None else {}


def runtime_output(root):
    """Where the runtime's own output goes. With a console it is inherited; under
    pythonw.exe there is none, so errors go to a private, size-capped runtime.log."""
    if not HIDDEN:
        return None
    path = root / "runtime.log"
    try:
        if path.stat().st_size > 1024 * 1024:
            os.replace(path, root / "runtime.log.1")
    except OSError:
        pass
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)


def exit_status(code):
    # A child killed by a POSIX signal reports -N; shells report 128 + N.
    return 128 - code if code < 0 else code


def read_text(path):
    # Standalone copy of fsutil.retry_sharing: on Windows, reading while the
    # updater replaces the file fails transiently with PermissionError.
    for attempt in range(20):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if os.name != "nt" or attempt == 19:
                raise
            time.sleep(min(0.1, 0.005 * 2 ** attempt))


def read_pointer(root):
    pointer = json.loads(read_text(root / "current.json"))
    python = Path(pointer["python"]).absolute()
    if not python.parent.resolve().is_relative_to(root / "versions") or not python.is_file():
        raise RuntimeError("managed Python path is invalid")
    return pointer, python


def write_json(path, data):
    """Atomic private write (a standalone copy of fsutil.atomic_write_json)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 19:
                raise
            time.sleep(0.05)


def update_state(root):
    try:
        data = json.loads(read_text(root / "update-state.json"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def on_probation(root, pointer):
    """The runtime installed this version and it has not yet completed a first tick."""
    state = update_state(root)
    target = state.get("target") or {}
    return state.get("state") == "updating" and target.get("version") == pointer.get("tag")


def roll_back(root, pointer):
    """Restore the previous version after a failed first start. The update mode
    is left as it is: only an explicit rollback makes it manual."""
    previous = pointer.get("previous") or {}
    if not all(k in previous for k in ("tag", "commit", "python")) or not Path(previous["python"]).is_file():
        return False
    restored = {**pointer, **{k: previous[k] for k in ("tag", "commit", "python")},
                "previous": {k: pointer[k] for k in ("tag", "commit", "python")}, "automatic": False}
    state = update_state(root)
    target = state.get("target")
    write_json(root / "current.json", restored)
    write_json(root / "update-state.json", {**state, "state": "rolled_back", "error": "first_start_failed",
                                            "blocked": target})
    return True


def passthrough(python, args):
    process = subprocess.Popen([str(python), "-m", "raincli_agent", *args])
    if os.name != "nt":
        # The terminal delivers Ctrl-C to both processes; the client handles it.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, lambda *_: process.terminate())
    return exit_status(process.wait())


def stop_runtime(process, python, config):
    """Stop a runtime gracefully; kill its whole process tree only as a last resort."""
    if process.poll() is not None:
        return
    deadline = time.monotonic() + GRACEFUL_STOP
    if os.name != "nt":
        process.send_signal(signal.SIGTERM)  # handled: connectors stop, locks released
    while process.poll() is None and time.monotonic() < deadline:
        if os.name == "nt" and config:
            # Windows has no deliverable SIGTERM. Repeat the file-based request:
            # it applies once the runtime has published its instance.
            try:
                subprocess.run([str(python), "-m", "raincli_agent", "runtime", "stop", "--config", config],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, **HIDDEN)
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    if process.poll() is not None:
        return
    if os.name == "nt":
        # Include connector children so no orphan keeps a queue lock.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **HIDDEN)
    else:
        # The runtime leads its own process group: take its connectors with it.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


def main():
    root = Path(__file__).resolve().parent
    args = sys.argv[1:]
    _, python = read_pointer(root)
    if args[:2] != ["runtime", "run"]:
        return passthrough(python, args)
    config = args[args.index("--config") + 1] if "--config" in args[:-1] else None
    own = Path(__file__).read_bytes()
    stopped = False
    process = None
    failures, started, next_start = 0, 0.0, 0.0
    probation = False

    def stop(*_):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    current = None
    try:
        while not stopped:
            try:
                pointer, python = read_pointer(root)
            except (OSError, ValueError, KeyError, RuntimeError):
                if process is None and current is None:
                    raise
                # Keep the running version during a transient or bad pointer write.
                time.sleep(1)
                continue
            if process is None:
                if time.monotonic() < next_start:
                    time.sleep(1)
                    continue
                current = (pointer, python)
                output = runtime_output(root)
                try:
                    # POSIX: a separate process group, so a terminal Ctrl-C reaches only
                    # this launcher (which stops the runtime gracefully) and a last-resort
                    # kill can include the connectors.
                    process = subprocess.Popen([str(python), "-m", "raincli_agent", *args], stdin=subprocess.DEVNULL,
                                               stdout=output, stderr=output, start_new_session=os.name != "nt",
                                               **HIDDEN)
                finally:
                    if output is not None:
                        os.close(output)
                started = time.monotonic()
                probation = on_probation(root, pointer)
            code = process.poll()
            if code == 0:
                process = None
                return 0  # stopped on request
            if (code is not None and probation and time.monotonic() - started < PROBATION
                    and on_probation(root, pointer) and roll_back(root, pointer)):
                process, next_start, failures = None, 0.0, 0
                continue  # the loop starts the restored version
            if code is not None:
                # Crashed: restart with bounded backoff. Windows logon startup has
                # no service manager to do this.
                failures = 0 if time.monotonic() - started > 600 else failures + 1
                next_start = time.monotonic() + min(300, 2 ** min(failures, 8))
                process = None
                continue
            if (pointer["commit"], python) != (current[0]["commit"], current[1]):
                # Switch only after the old runtime has fully exited.
                stop_runtime(process, current[1], config)
                process = None
                if Path(__file__).read_bytes() != own:
                    return relaunch(args)  # the new release shipped a new launcher
                continue
            time.sleep(1)
        return 0
    finally:
        if process is not None:
            stop_runtime(process, current[1], config)


def relaunch(args):
    argv = [sys.executable, str(Path(__file__).resolve()), *args]
    sys.stdout and sys.stdout.flush()
    if os.name == "nt":
        # Windows exec creates a new process anyway; start it detached from us.
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, **HIDDEN)
        return 0
    os.execv(sys.executable, argv)

if __name__ == "__main__":
    sys.exit(main())
