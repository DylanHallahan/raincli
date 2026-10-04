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
import errno
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


# A managed environment runs exactly its staged code: never a module search path
# inherited from the caller's environment.
# PYTHONSAFEPATH keeps the working directory (a source checkout, say) off sys.path
# for `-m raincli_agent` (Python 3.11+; review 4, finding 1).
ISOLATED_ENV = {**{k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE")},
                "PYTHONSAFEPATH": "1"}


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


WRITE_HELPER = ("import json, sys\n"
                "from raincli_agent.fsutil import atomic_write_json\n"
                "atomic_write_json(sys.argv[1], json.load(sys.stdin))\n")


def write_json(path, data, python=None):
    """Atomic private write of a managed-state file.

    It goes through the client's own helper (``fsutil.atomic_write_json``), run
    by ``python``, a managed interpreter known to work. On Windows that helper
    sets the owner to the current user and a protected DACL explicitly; a plain
    file created by an administrator would instead be owned by
    BUILTIN\\Administrators with an inherited ACL, and every later read of the
    pointer would refuse it. The result is read back and compared. Only on POSIX,
    where a 0600 file created here is equivalent, does it fall back to a local write."""
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if python is not None:
        try:
            subprocess.run([str(python), "-c", WRITE_HELPER, str(path)], input=text.encode(), env=ISOLATED_ENV,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
                           check=True, **HIDDEN)
            if json.loads(read_text(path)) == data:
                return
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if os.name == "nt":
        raise OSError("cannot write managed state through the client's private-file helper")
    _write_json_posix(path, text)


def _write_json_posix(path, text):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode())
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


STOP_MARKER = "stop-requested.json"


def awaiting_first_tick(state):
    """``updating``, or an install whose switch timed out (it may still be switched to)."""
    return state.get("state") == "updating" or (state.get("state") == "failed"
                                                 and state.get("error") == "switch_timeout")


def on_probation(root, pointer):
    """The runtime installed this version and it has not yet completed a first tick."""
    state = update_state(root)
    target = state.get("target") or {}
    return awaiting_first_tick(state) and target.get("version") == pointer.get("tag")


def stop_was_requested(root, since):
    """The runtime records a graceful stop it was asked for (a signal or ``runtime
    stop``), so such an exit during probation is not a failed first start."""
    marker = root / STOP_MARKER
    try:
        requested = marker.stat().st_mtime >= since - 1
        marker.unlink()
        return requested
    except OSError:
        return False


class LockBusy(Exception):
    pass


def update_lock(root, timeout=300):
    """The update lock that install, configure and migrate_mode hold (the same
    file and the same flock/msvcrt byte lock as filelock.py), so a rollback is
    never overwritten by a concurrent read-modify-write of the pointer."""
    directory = root / "update-lock"
    directory.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(directory / "run.lock", os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                os.close(fd)
                raise
            if time.monotonic() >= deadline:
                os.close(fd)
                raise LockBusy() from None
            time.sleep(0.2)


def update_unlock(fd):
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


MACHINE_FLOOR = (0, 4, 0)  # machine mode needs v0.4.0 or later (15.8 H8)


def machine_mode(config):
    """Whether the runtime config this launcher runs is in machine mode."""
    try:
        return config is not None and "machine_config" in json.loads(read_text(Path(config)))
    except (OSError, ValueError, TypeError):
        return False


def tag_key(tag):
    try:
        return tuple(int(part) for part in str(tag).removeprefix("v").split("."))
    except ValueError:
        return (0, 0, 0)


def roll_back(root, pointer, timeout=300, floor=None):
    """Restore the previous version after a failed first start. The update mode
    is left as it is: only an explicit rollback makes it manual.

    Under the update lock, and only if the pointer still names the failing
    version (compare and swap): a concurrent change wins and nothing is undone.
    Returns True, False, or "busy" when the lock was not free within ``timeout``."""
    try:
        fd = update_lock(root, timeout)
    except LockBusy:
        return "busy"
    except OSError:
        return False
    try:
        try:
            current = json.loads(read_text(root / "current.json"))
        except (OSError, ValueError):
            return False
        if current.get("commit") != pointer.get("commit") or not on_probation(root, current):
            return False
        previous = current.get("previous") or {}
        if not all(k in previous for k in ("tag", "commit", "python")) or not Path(previous["python"]).is_file():
            return False
        if floor is not None and tag_key(previous["tag"]) < floor:
            return False  # that version cannot run this runtime config
        restored = {**current, **{k: previous[k] for k in ("tag", "commit", "python")},
                    "previous": {k: current[k] for k in ("tag", "commit", "python")}, "automatic": False}
        state = update_state(root)
        target = state.get("target")
        # Written by the version being restored, through the client's own helper.
        helper = previous["python"]
        try:
            write_json(root / "current.json", restored, helper)
            write_json(root / "update-state.json", {**state, "state": "rolled_back", "error": "first_start_failed",
                                                    "blocked": target}, helper)
        except OSError as exc:
            print(f"raincli launcher: rollback not written: {exc}", file=sys.stderr, flush=True)
            return False
        return True
    finally:
        update_unlock(fd)


def passthrough(python, args):
    process = subprocess.Popen([str(python), "-m", "raincli_agent", *args], env=ISOLATED_ENV)
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
                               timeout=10, env=ISOLATED_ENV, **HIDDEN)
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


def supervise(root, args, config, command, own=None, probation_enabled=True):
    """The ``runtime run`` loop: start the pointer's version, restart it after a
    crash, switch versions gracefully and supervise a new version's first start."""
    stopped = False
    process = None
    failures, started, started_wall, next_start = 0, 0.0, 0.0, 0.0
    probation = rollback_pending = False

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
                    (root / STOP_MARKER).unlink()
                except OSError:
                    pass
                try:
                    # POSIX: a separate process group, so a terminal Ctrl-C reaches only
                    # this launcher (which stops the runtime gracefully) and a last-resort
                    # kill can include the connectors.
                    process = subprocess.Popen(command(python), stdin=subprocess.DEVNULL,
                                               stdout=output, stderr=output, start_new_session=os.name != "nt",
                                               env=ISOLATED_ENV, **HIDDEN)
                finally:
                    if output is not None:
                        os.close(output)
                started, started_wall = time.monotonic(), time.time()
                probation = probation_enabled and on_probation(root, pointer)
                rollback_pending = False
            code = process.poll()
            # On probation any exit this launcher did not ask for, even status 0,
            # is a failed first start (review 1, finding 5), unless the runtime
            # recorded that it was asked to stop (review 2, O10).
            if code is not None and probation and (rollback_pending or time.monotonic() - started < PROBATION):
                if not rollback_pending and stop_was_requested(root, started_wall):
                    probation = False
                elif on_probation(root, pointer):
                    # Never wait on the lock here: a stop must stay prompt (review 2, O9).
                    result = roll_back(root, pointer, timeout=0,
                                       floor=MACHINE_FLOOR if machine_mode(config) else None)
                    if result == "busy":
                        rollback_pending = True
                        time.sleep(1)
                        continue
                    if result is True:
                        process, next_start, failures = None, 0.0, 0
                        continue  # the loop starts the restored version
                    code = code or 1  # not rolled back: a failed start, never a clean stop
            if code == 0:
                process = None
                return 0  # stopped on request
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
                if own is not None and Path(__file__).read_bytes() != own:
                    return relaunch(args)  # a newer launcher was adopted meanwhile
                continue
            time.sleep(1)
        return 0
    finally:
        if process is not None:
            stop_runtime(process, current[1], config)


def main():
    root = Path(__file__).resolve().parent
    args = sys.argv[1:]
    _, python = read_pointer(root)
    if args[:2] != ["runtime", "run"]:
        return passthrough(python, args)
    config = args[args.index("--config") + 1] if "--config" in args[:-1] else None
    return supervise(root, args, config, lambda python: [str(python), "-m", "raincli_agent", *args],
                     own=Path(__file__).read_bytes())


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
