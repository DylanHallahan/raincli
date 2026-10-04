"""Build the Windows app installer: RainCLI-Setup-<X.Y.Z>.exe and its .sha256 (protocol §15.5).

1. Copy the client package (``raincli/raincli_agent`` only, never the server) to a build
   directory, and with ``--version`` set its ``__version__`` there (the checkout is not touched).
2. Freeze it with PyInstaller (``raincli.spec``): the version folder with ``RainCLI-app.exe``
   and ``raincli.exe``, the stable stub ``RainCLI.exe`` and the PATH shim ``bin\\raincli.exe``.
3. Check the bundle (``verify_bundle.py``): no test hooks (§15.8 M11) and the real app.
4. Smoke-test ``raincli.exe --version``.
5. Compile ``RainCLI.iss`` with Inno Setup and write the checksum file: one line of 64
   lowercase hex characters, two spaces, then the installer's file name.

Usage (Windows, Python 3.14 with requirements-build.txt installed):
    python packaging/windows/build.py [--version 0.4.1] [--out DIR] [--iscc PATH]

``--placeholder-app`` freezes a stand-in tray and stub when ``raincli_agent.app`` does not exist
yet, for a dry run of the pipeline only; ``verify_bundle.py`` refuses it unless told otherwise,
and the build workflow never passes it. ``--no-installer`` stops after step 4 (any OS).
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CLIENT = ROOT / "raincli" / "raincli_agent"
VERSION_RE = re.compile(r"(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})")  # §14.9
VERSION_LINE = re.compile(r'^__version__ = "[^"]*"$', re.M)

PLACEHOLDER_APP = '''"""Build placeholder for the tray (RAINCLI-PLACEHOLDER-APP); never shipped."""
import sys

MARK = "RAINCLI-PLACEHOLDER-APP"


def main(argv=None):
    print(MARK + ": the real tray app (raincli_agent.app) was not in this build", file=sys.stderr)
    return 3
'''
PLACEHOLDER_STUB = PLACEHOLDER_APP.replace("the tray", "the stub").replace("raincli_agent.app)", "raincli_agent.app.stub)")


def say(text):
    print(text, flush=True)


def source_version():
    text = (CLIENT / "__init__.py").read_text("utf-8")
    match = re.search(r'^__version__ = "([^"]*)"$', text, re.M)
    if not match:
        raise SystemExit("raincli_agent/__init__.py has no __version__")
    return match.group(1)


def stage_source(work, version, placeholder):
    """A copy of the client package with ``version`` applied. Test hooks are refused here first."""
    src = work / "src"
    if src.exists():
        shutil.rmtree(src)
    shutil.copytree(CLIENT, src / "raincli_agent",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "test_*"))
    hooks = [p for p in src.rglob("*") if "_build_test" in p.name]
    if hooks:
        raise SystemExit(f"refusing to build: test hook files in the client package: {hooks}")
    init = src / "raincli_agent" / "__init__.py"
    text = init.read_text("utf-8")
    if not VERSION_LINE.search(text):
        raise SystemExit("raincli_agent/__init__.py has no __version__ line")
    init.write_text(VERSION_LINE.sub(f'__version__ = "{version}"', text, count=1), "utf-8")
    app = src / "raincli_agent" / "app"
    if not (app / "__init__.py").is_file() or not (app / "stub.py").is_file():
        if not placeholder:
            raise SystemExit("raincli_agent.app (the tray) and raincli_agent.app.stub are not in this checkout; "
                             "pass --placeholder-app only for a dry run")
        app.mkdir(exist_ok=True)
        if not (app / "__init__.py").is_file():
            (app / "__init__.py").write_text(PLACEHOLDER_APP, "utf-8")
        if not (app / "stub.py").is_file():
            (app / "stub.py").write_text(PLACEHOLDER_STUB, "utf-8")
        say("WARNING: froze the placeholder tray and stub; this build is a dry run and must not be released")
    return src


def freeze(work, src, version):
    dist = work / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    env = {**os.environ, "RAINCLI_BUILD_SRC": str(src), "RAINCLI_BUILD_VERSION": version, "PYTHONHASHSEED": "0"}
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
                    "--distpath", str(dist), "--workpath", str(work / "pyinstaller"), str(HERE / "raincli.spec")],
                   check=True, env=env, cwd=str(work))
    exe = ".exe" if os.name == "nt" else ""
    (dist / "bin").mkdir()
    shutil.move(str(dist / f"raincli-shim{exe}"), str(dist / "bin" / f"raincli{exe}"))
    if not exe:  # a non-Windows dry run: name the outputs as on Windows so the checks read the same
        for path in [dist / "RainCLI", dist / "bin" / "raincli", *(dist / f"RainCLI-{version}").glob("*")]:
            if path.is_file() and path.suffix == "" and os.access(path, os.X_OK):
                path.rename(path.with_name(path.name + ".exe"))
    return dist


def smoke(dist, version):
    cli = dist / f"RainCLI-{version}" / "raincli.exe"
    out = subprocess.run([str(cli), "--version"], capture_output=True, text=True, timeout=120)
    if out.returncode != 0 or out.stdout.strip() != f"raincli {version}":
        raise SystemExit(f"{cli} --version printed {out.stdout.strip()!r} (exit {out.returncode})")
    say(f"PASS: {cli.name} --version is raincli {version}")


def find_iscc(given):
    candidates = [given, shutil.which("iscc"), shutil.which("ISCC"),
                  r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", r"C:\Program Files\Inno Setup 6\ISCC.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise SystemExit("Inno Setup 6 (ISCC.exe) not found; pass --iscc")


def installer(dist, version, out, iscc):
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run([iscc, "/Q", f"/DAppVersion={version}", f"/DDistDir={dist}", f"/O{out}",
                    str(HERE / "RainCLI.iss")], check=True)
    setup = out / f"RainCLI-Setup-{version}.exe"
    if not setup.is_file():
        raise SystemExit(f"Inno Setup did not produce {setup.name}")
    return setup, write_checksum(setup)


def write_checksum(setup):
    """``<64 lowercase hex>  <file name>`` and a newline, as §15.5 specifies."""
    digest = hashlib.sha256()
    with open(setup, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    path = setup.with_name(setup.name + ".sha256")
    path.write_bytes(f"{digest.hexdigest()}  {setup.name}\n".encode("ascii"))
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--version", help="X.Y.Z to build (default: raincli_agent.__version__)")
    parser.add_argument("--work", type=Path, default=ROOT / "build" / "windows")
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "windows")
    parser.add_argument("--iscc", help="path to ISCC.exe")
    parser.add_argument("--no-installer", action="store_true", help="freeze and check only")
    parser.add_argument("--placeholder-app", action="store_true", help="dry run without raincli_agent.app")
    args = parser.parse_args(argv)
    version = args.version or source_version()
    if not VERSION_RE.fullmatch(version):
        parser.error(f"{version!r} is not X.Y.Z")
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    src = stage_source(work, version, args.placeholder_app)
    dist = freeze(work, src, version)
    sys.path.insert(0, str(HERE))
    import verify_bundle

    problems = verify_bundle.verify(dist, version, allow_placeholder=args.placeholder_app)
    if problems:
        raise SystemExit("bundle check failed:\n  " + "\n  ".join(problems))
    say(f"PASS: bundle check: no test hooks in RainCLI {version}")
    smoke(dist, version)
    if args.no_installer:
        return 0
    setup, checksum = installer(dist, version, args.out.resolve(), find_iscc(args.iscc))
    say(f"built {setup}\n      {checksum}: {checksum.read_text().strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
