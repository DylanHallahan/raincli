"""``bin\\raincli.exe``: the CLI on the user's PATH for the Windows app (protocol §15.8 L2).

It forwards to the current version's console CLI, ``versions\\<current>\\raincli.exe``,
where ``current`` comes from ``<root>\\install.json`` (§15.8 M6). Like the stub, it is
installed only by a full install and never by ``/UPDATE``, so it must stay correct
from v0.4.0: it reads nothing but ``install.json`` and passes arguments through
unchanged. Standard library only.
"""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

INSTALL_FILE = "install.json"
CLI_EXE = "raincli.exe"
VERSION_RE = re.compile(r"(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})")  # §14.9
SHARING_RETRIES = 20
SHARING_DELAY = 0.1


class ShimError(Exception):
    pass


def install_root(executable=None):
    """``<root>`` for ``<root>\\bin\\raincli.exe``."""
    return Path(executable or sys.executable).resolve().parent.parent


def read_install(root, sleep=time.sleep):
    """``install.json``, retrying while an update holds it open (a sharing violation)."""
    path = Path(root) / INSTALL_FILE
    for attempt in range(SHARING_RETRIES):
        try:
            raw = path.read_bytes()
            break
        except FileNotFoundError:
            raise ShimError(f"{path} is missing") from None
        except PermissionError:
            if attempt == SHARING_RETRIES - 1:
                raise ShimError(f"{path} stayed locked") from None
            sleep(SHARING_DELAY)
        except OSError as exc:
            raise ShimError(f"{path} is unreadable ({type(exc).__name__})") from None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ShimError(f"{path} is not valid JSON") from None
    if not isinstance(data, dict):
        raise ShimError(f"{path} is not a JSON object")
    return data


def current_cli(root, sleep=time.sleep):
    current = read_install(root, sleep).get("current")
    if not isinstance(current, str) or not VERSION_RE.fullmatch(current):
        raise ShimError("install.json names no valid current version")
    exe = Path(root) / "versions" / current / CLI_EXE
    if not exe.is_file():
        raise ShimError(f"version {current} has no {CLI_EXE}")
    return exe


def main(argv=None, root=None, run=subprocess.call, sleep=time.sleep):
    argv = sys.argv[1:] if argv is None else argv
    try:
        exe = current_cli(root or install_root(), sleep)
    except ShimError as exc:
        print(f"raincli: the RainCLI app install is damaged: {exc}. Reinstall RainCLI.", file=sys.stderr)
        return 1
    # Ctrl+C reaches the whole console group; the child decides, and its exit code is ours.
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return run([str(exe), *argv])
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    sys.exit(main())
