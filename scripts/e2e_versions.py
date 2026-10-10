"""The versions the Windows app e2e builds, derived from the source and never written down anywhere else.

``base`` is the larger of the client's ``__version__`` and its ``MIN_SAFE_VERSION``: every pushed target and
every rollback the test makes stays at or above the minimum safe version, so the client never refuses them
(``below_min_safe``). The test installs ``base``, pushes ``base + 1`` (the update), then a ``base + 2`` whose
tray exits 1 (the probation rollback to ``base + 1``), and downgrades back to ``base``.

``python scripts/e2e_versions.py`` prints ``OLD NEW BROKEN``; with ``--github-output`` it also writes
``old=``, ``new=`` and ``broken=`` to ``$GITHUB_OUTPUT`` for a workflow step.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_VERSION = r"([0-9]{1,4})\.([0-9]{1,4})\.([0-9]{1,4})"


def _read(path: Path, name: str) -> tuple[int, int, int]:
    match = re.search(rf'^{name} = "{_VERSION}"$', path.read_text("utf-8"), re.M)
    if match is None:
        raise SystemExit(f"{path}: no {name} = \"X.Y.Z\" line")
    return tuple(int(part) for part in match.groups())


def versions(root: Path = ROOT) -> tuple[str, str, str]:
    agent = root / "raincli" / "raincli_agent"
    base = max(_read(agent / "__init__.py", "__version__"), _read(agent / "security.py", "MIN_SAFE_VERSION"))
    major, minor, patch = base
    return tuple(f"{major}.{minor}.{patch + step}" for step in (0, 1, 2))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    old, new, broken = versions()
    print(old, new, broken)
    if "--github-output" in argv:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"old={old}\nnew={new}\nbroken={broken}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
