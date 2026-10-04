"""``RainCLI.exe --background``: the stable stub at the install root (15.5, 15.8 H4).

It is never changed by an update, so it stays deliberately small; the logic is
``runtime.winapp.Stub``. It starts install.json's current version of the tray,
relaunches it after a version switch, and owns a new version's probation."""
from pathlib import Path
import sys


def main(argv=None):
    from raincli_agent.runtime import winapp
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["--background"]:
        print("usage: RainCLI.exe --background", file=sys.stderr)
        return 2
    return winapp.stub_main(Path(sys.executable).absolute().parent)


if __name__ == "__main__":
    sys.exit(main())
