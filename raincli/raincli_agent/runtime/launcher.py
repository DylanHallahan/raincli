"""Standalone managed launcher, copied outside versioned environments.

Uses the operator's base Python. Polls a local pointer, gracefully stops the
runtime before switching versions, and checks official releases only if opted in.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    root = Path(__file__).resolve().parent
    args = sys.argv[1:]
    runtime = args[:2] == ["runtime", "run"]
    stopped = False
    process = None
    def stop(*_):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    last_update = time.monotonic()  # wait six hours after start before checking
    current = None
    def stop_child():
        if process is None or process.poll() is not None:
            return
        if runtime and "--config" in args:
            config = args[args.index("--config") + 1]
            try:
                subprocess.run([current["python"], "-m", "raincli_agent", "runtime", "stop", "--config", config],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                process.wait(timeout=40)
                return
            except subprocess.TimeoutExpired:
                pass
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    try:
        while not stopped:
            pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
            python = Path(pointer["python"]).absolute()
            if not python.parent.resolve().is_relative_to(root / "versions") or not python.is_file():
                raise RuntimeError("managed Python path is invalid")
            if process is None:
                current = pointer
                process = subprocess.Popen([str(python), "-m", "raincli_agent", *args], stdin=subprocess.DEVNULL)
            code = process.poll()
            if code is not None:
                return code
            if not runtime:
                time.sleep(0.2)
                continue
            if pointer["commit"] != current["commit"]:
                stop_child()
                process = None
                continue
            if pointer.get("automatic") and time.monotonic() - last_update >= 21600:
                last_update = time.monotonic()
                with open(root / "update.log", "wb") as log:
                    try:
                        subprocess.run([str(python), "-m", "raincli_agent", "runtime", "update", "--install", "--root", str(root)],
                                       stdout=log, stderr=log, stdin=subprocess.DEVNULL, timeout=420)
                    except subprocess.TimeoutExpired:
                        pass
            time.sleep(1)
        return 0
    finally:
        stop_child()


if __name__ == "__main__":
    sys.exit(main())
