"""Refuse a Windows app bundle that carries a test hook or the wrong code (protocol §15.8 M11).

Release hosts, the API and the repository are constants in shipped code. No module,
file or name in the bundle may redirect them. This lists every frozen module in each
executable's archive and checks:

- no ``_build_test`` module or file, and no test package;
- no code object naming ``TEST_RELEASE_BASE``, ``TEST_CERT_SHA256`` or ``_build_test``;
- no server, test-runner or build-tool module;
- the tray executable freezes ``raincli_agent.app.tray`` and its GUI modules (``pystray``,
  ``PIL``, ``tkinter``), the CLI freezes ``raincli_agent.cli``, the stub freezes
  ``raincli_agent.app.stub``, and the PATH shim freezes no client code at all.

Usage: python verify_bundle.py <dist-dir> --version X.Y.Z
Exit status 0 when clean; 1 with one line per problem otherwise.
"""
import argparse
from pathlib import Path
import sys
import types

FORBIDDEN_NAMES = ("TEST_RELEASE_BASE", "TEST_CERT_SHA256", "_build_test")
FORBIDDEN_MODULES = ("raincli_server", "pytest", "PyInstaller", "fastapi", "sqlalchemy", "uvicorn")


def frozen_modules(exe):
    """{module name: code object or None} from every PYZ inside ``exe``'s CArchive."""
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(exe))
    modules = {}
    for name, entry in archive.toc.items():
        typecode = entry[-1]
        if typecode == "z":  # a PYZ
            pyz = archive.open_embedded_archive(name)
            for module in pyz.toc:
                try:
                    modules[module] = pyz.extract(module)
                except Exception:  # a package marker or an unreadable entry
                    modules[module] = None
        elif typecode in ("s", "m", "M"):  # entry-point scripts and bootstrap modules
            try:
                modules["<script>" + name] = archive.extract(name) if typecode == "s" else None
            except Exception:
                modules["<script>" + name] = None
    if not modules:
        raise SystemExit(f"{exe}: no frozen modules found; is this a PyInstaller executable?")
    return modules


def code_strings(code):
    """Every name and string constant in a code object, recursively."""
    if isinstance(code, bytes):
        import marshal

        try:
            code = marshal.loads(code)
        except (ValueError, EOFError, TypeError):
            return
    if not isinstance(code, types.CodeType):
        return
    yield from code.co_names
    yield from code.co_varnames
    for const in code.co_consts:
        if isinstance(const, str):
            yield const
        elif isinstance(const, types.CodeType):
            yield from code_strings(const)


def check_modules(label, modules, required):
    problems = []
    for name, code in modules.items():
        short = name.removeprefix("<script>")
        if "_build_test" in short or short.startswith(("raincli_agent.tests", "tests.")):
            problems.append(f"{label}: test module {short} is frozen in")
        if short.split(".")[0] in FORBIDDEN_MODULES:
            problems.append(f"{label}: {short} must never be bundled")
        if short.startswith("raincli_agent") or name.startswith("<script>"):
            strings = set(code_strings(code))
            for bad in FORBIDDEN_NAMES:
                if any(bad in s for s in strings):
                    problems.append(f"{label}: {short} refers to {bad}")
    for name in required:
        if name not in modules:
            problems.append(f"{label}: {name} is not frozen in")
    return problems


def check_files(dist):
    problems = []
    for path in dist.rglob("*"):
        if "_build_test" in path.name:
            problems.append(f"file {path.relative_to(dist)} is a test hook")
        elif path.is_file() and path.suffix.lower() in (".py", ".pyc", ".pyw"):
            data = path.read_bytes()
            for bad in FORBIDDEN_NAMES:
                if bad.encode() in data:
                    problems.append(f"file {path.relative_to(dist)} refers to {bad}")
    return problems


def verify(dist, version):
    dist = Path(dist)
    folder = dist / f"RainCLI-{version}"
    problems = check_files(dist)
    targets = [
        (folder / "RainCLI-app.exe", ["raincli_agent.app.tray", "pystray", "PIL", "tkinter"]),
        (folder / "raincli.exe", ["raincli_agent.cli"]),
        (dist / "stub" / "RainCLI.exe", ["raincli_agent.app.stub"]),
        (dist / "bin" / "raincli.exe", []),
    ]
    for exe, required in targets:
        if not exe.is_file():
            problems.append(f"{exe.relative_to(dist)} is missing")
            continue
        modules = frozen_modules(exe)
        problems += check_modules(str(exe.relative_to(dist)), modules, required)
        if exe.parent.name == "bin" and any(m.startswith("raincli_agent") for m in modules):
            problems.append("bin/raincli.exe must not bundle the client; it only forwards to the current version")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("dist")
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    problems = verify(args.dist, args.version)
    for problem in problems:
        print("BUNDLE CHECK FAILED: " + problem)
    if not problems:
        print(f"bundle check passed: RainCLI {args.version} carries no test hooks")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
