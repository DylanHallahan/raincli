"""``RainCLI.exe --background``: the stable stub at the install root (15.5, 15.8 H4).

It is never changed by an update, so it stays deliberately small; the logic is
``runtime.winapp.Stub``. It starts install.json's current version of the tray,
relaunches it after a version switch, and owns a new version's probation.
``--quit`` asks a running app to stop gracefully."""
from pathlib import Path
import sys


def main(argv=None):
    from raincli_agent.runtime import winapp
    argv = sys.argv[1:] if argv is None else argv
    root = Path(sys.executable).absolute().parent
    if argv == ["--background"]:
        return winapp.stub_main(root)
    if argv == ["--quit"]:
        return winapp.stub_quit(root)  # 0 once stopped (within 120 s), 1 if still running (15.9)
    print("usage: RainCLI.exe --background | --quit", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
