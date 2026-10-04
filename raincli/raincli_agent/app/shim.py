"""``bin\\raincli.exe``: the PATH shim (15.8 L2). Runs install.json's current
version's ``raincli.exe`` with the same arguments, standard streams and exit status."""
from pathlib import Path
import subprocess
import sys


def main(argv=None):
    from raincli_agent.runtime import winapp
    argv = sys.argv[1:] if argv is None else argv
    root = Path(sys.executable).absolute().parent.parent
    version = winapp.current_version(root)
    if version is None:
        print("raincli: the RainCLI app has no current version installed", file=sys.stderr)
        return 1
    return subprocess.call([str(winapp.version_dir(root, version) / winapp.CLI_EXE), *argv])


if __name__ == "__main__":
    sys.exit(main())
