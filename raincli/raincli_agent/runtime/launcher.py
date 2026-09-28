"""Standalone managed launcher, copied outside versioned environments.

Uses the operator's base Python. Ordinary commands run the managed client with
stdin, stdout, stderr and the exit status passed through unchanged. For
``runtime run`` it polls a local pointer, gracefully stops the runtime (which
stops its connectors and releases their queue locks) before starting the newly
selected environment, and checks official releases only if opted in. Versioned
environments are never deleted here, so an environment still in use (Windows
keeps its files open) remains available for rollback.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

GRACEFUL_STOP = 60  # the runtime allows each connector poll_wait + 10 s, in parallel
# Started at logon by pythonw.exe there is no console: do not open one per child.
HIDDEN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" and sys.stdout is None else {}


def exit_status(code):
    # A child killed by a POSIX signal reports -N; shells report 128 + N.
    return 128 - code if code < 0 else code


def read_pointer(root):
    pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
    python = Path(pointer["python"]).absolute()
    if not python.parent.resolve().is_relative_to(root / "versions") or not python.is_file():
        raise RuntimeError("managed Python path is invalid")
    return pointer, python


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
        process.kill()
    process.wait(timeout=10)


def main():
    root = Path(__file__).resolve().parent
    args = sys.argv[1:]
    _, python = read_pointer(root)
    if args[:2] != ["runtime", "run"]:
        return passthrough(python, args)
    config = args[args.index("--config") + 1] if "--config" in args[:-1] else None
    stopped = False
    process = None

    def stop(*_):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    last_update = time.monotonic()  # wait six hours after start before checking
    current = None
    try:
        while not stopped:
            try:
                pointer, python = read_pointer(root)
            except (OSError, ValueError, KeyError, RuntimeError):
                if process is None:
                    raise
                # Keep the running version during a transient or bad pointer write.
                time.sleep(1)
                continue
            if process is None:
                current = (pointer, python)
                process = subprocess.Popen([str(python), "-m", "raincli_agent", *args], stdin=subprocess.DEVNULL, **HIDDEN)
            code = process.poll()
            if code is not None:
                return exit_status(code)
            if (pointer["commit"], python) != (current[0]["commit"], current[1]):
                # Switch only after the old runtime has fully exited.
                stop_runtime(process, current[1], config)
                process = None
                continue
            if pointer.get("automatic") and time.monotonic() - last_update >= 21600:
                last_update = time.monotonic()
                with open(root / "update.log", "wb") as log:
                    try:
                        subprocess.run([str(python), "-m", "raincli_agent", "runtime", "update", "--install", "--root", str(root)],
                                       stdout=log, stderr=log, stdin=subprocess.DEVNULL, timeout=420, **HIDDEN)
                    except subprocess.TimeoutExpired:
                        pass
            time.sleep(1)
        return 0
    finally:
        if process is not None:
            stop_runtime(process, current[1], config)


if __name__ == "__main__":
    sys.exit(main())
