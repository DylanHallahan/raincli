"""Build the Windows app installer: RainCLI-Setup-<X.Y.Z>.exe and its .sha256 (protocol §15.5).

1. Copy the client package (``raincli/raincli_agent`` only, never the server) to a build
   directory, and with ``--version`` set its ``__version__`` there (the checkout is not touched).
2. Freeze it with PyInstaller (``raincli.spec``), every part onedir (§15.9): the version folder
   with ``RainCLI-app.exe`` and ``raincli.exe``, the stable stub ``RainCLI.exe`` and the PATH
   shim ``bin\\raincli.exe``, each with its own ``_internal``.
3. Check the bundle (``verify_bundle.py``): no test hooks (§15.8 M11), and the real tray with its
   GUI modules.
4. Smoke-test ``raincli.exe --version`` and ``RainCLI-app.exe --self-check`` (§15.9), and time the
   PATH shim in an assembled install root.
5. Compile ``RainCLI.iss`` with the pinned Inno Setup (downloaded, SHA-256 checked and installed
   into the work directory) and write the checksum file: one line of 64 lowercase hex characters,
   two spaces, then the installer's file name.

Usage (Windows, Python 3.14 with requirements-build.txt installed):
    python packaging/windows/build.py [--version 0.4.1] [--out DIR] [--iscc PATH]

``--no-installer`` stops after step 4 (any OS: a dry run of the freeze and the checks).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CLIENT = ROOT / "raincli" / "raincli_agent"
VERSION_RE = re.compile(r"(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})")  # §14.9
VERSION_LINE = re.compile(r'^__version__ = "[^"]*"$', re.M)
# Inno Setup is pinned by version and hash (review 1b B7); its installer goes into the work dir.
INNO_VERSION = "6.7.3"
INNO_URL = "https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe"
INNO_SHA256 = "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732"
SHIM_RUNS = 5


def say(text):
    print(text, flush=True)


def source_version():
    text = (CLIENT / "__init__.py").read_text("utf-8")
    match = re.search(r'^__version__ = "([^"]*)"$', text, re.M)
    if not match:
        raise SystemExit("raincli_agent/__init__.py has no __version__")
    return match.group(1)


def stage_source(work, version, source=CLIENT):
    """A copy of the client package with ``version`` applied. Test hooks are refused here first."""
    src = work / "src"
    if src.exists():
        shutil.rmtree(src)
    shutil.copytree(source, src / "raincli_agent",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "test_*"))
    hooks = [p for p in src.rglob("*") if "_build_test" in p.name]
    if hooks:
        raise SystemExit(f"refusing to build: test hook files in the client package: {hooks}")
    init = src / "raincli_agent" / "__init__.py"
    text = init.read_text("utf-8")
    if not VERSION_LINE.search(text):
        raise SystemExit("raincli_agent/__init__.py has no __version__ line")
    init.write_text(VERSION_LINE.sub(f'__version__ = "{version}"', text, count=1), "utf-8")
    for module in ("tray.py", "stub.py"):
        if not (src / "raincli_agent" / "app" / module).is_file():
            raise SystemExit(f"raincli_agent/app/{module} is not in the client package")
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
    (dist / "RainCLI-stub").rename(dist / "stub")
    (dist / "RainCLI-bin").rename(dist / "bin")
    (dist / "bin" / f"raincli-shim{exe}").rename(dist / "bin" / f"raincli{exe}")
    if not exe:  # a non-Windows dry run: name the outputs as on Windows so the checks read the same
        for path in [dist / "stub" / "RainCLI", dist / "bin" / "raincli", *(dist / f"RainCLI-{version}").glob("*")]:
            if path.is_file() and path.suffix == "" and os.access(path, os.X_OK):
                path.rename(path.with_name(path.name + ".exe"))
    return dist


def smoke(dist, version, work):
    folder = dist / f"RainCLI-{version}"
    out = subprocess.run([str(folder / "raincli.exe"), "--version"], capture_output=True, text=True, timeout=120)
    if out.returncode != 0 or out.stdout.strip() != f"raincli {version}":
        raise SystemExit(f"raincli.exe --version printed {out.stdout.strip()!r} (exit {out.returncode})")
    say(f"PASS: raincli.exe --version is raincli {version}")
    out = subprocess.run([str(folder / "RainCLI-app.exe"), "--self-check"], capture_output=True, text=True,
                         timeout=120)
    if out.returncode != 0:
        raise SystemExit(f"RainCLI-app.exe --self-check exited {out.returncode}: {(out.stdout + out.stderr)[-2000:]}")
    say("PASS: RainCLI-app.exe --self-check")
    time_shim(dist, version, work)


def time_shim(dist, version, work):
    """``bin\\raincli.exe --version`` in an assembled root: the hook path's start-up cost (review 1b B6)."""
    root = work / "shim-root"
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(dist / "bin", root / "bin")
    shutil.copytree(dist / f"RainCLI-{version}", root / "versions" / version)
    (root / "install.json").write_text(json.dumps({"current": version, "previous": None, "probation": None}))
    commands = {"shim": [str(root / "bin" / "raincli.exe"), "--version"],
                "direct": [str(root / "versions" / version / "raincli.exe"), "--version"]}
    times = {name: [] for name in commands}
    for _ in range(SHIM_RUNS):
        for name, command in commands.items():
            start = time.perf_counter()
            out = subprocess.run(command, capture_output=True, text=True, timeout=120)
            times[name].append(time.perf_counter() - start)
            if out.returncode != 0 or out.stdout.strip() != f"raincli {version}":
                raise SystemExit(f"{name} --version printed {out.stdout.strip()!r} (exit {out.returncode})")
    say(f"PASS: bin\\raincli.exe forwards to the current version. Wall time over {SHIM_RUNS} runs: shim median "
        f"{statistics.median(times['shim']):.3f} s (max {max(times['shim']):.3f} s); raincli.exe alone median "
        f"{statistics.median(times['direct']):.3f} s")
    shutil.rmtree(root, ignore_errors=True)


def ensure_iscc(given, work):
    """``--iscc``, else the pinned Inno Setup, downloaded, hash-checked and installed into ``work``."""
    if given:
        return given
    home = work / f"inno-{INNO_VERSION}"
    iscc = home / "ISCC.exe"
    if not iscc.is_file():
        request = urllib.request.Request(INNO_URL, headers={"User-Agent": "RainCLI-build"})
        with urllib.request.urlopen(request, timeout=300) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != INNO_SHA256:
            raise SystemExit(f"innosetup-{INNO_VERSION}.exe does not match its pinned SHA-256; refusing to run it")
        setup = work / f"innosetup-{INNO_VERSION}.exe"
        setup.write_bytes(data)
        subprocess.run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", "/PORTABLE=1",
                        f"/DIR={home}", f"/LOG={work / 'inno-install.log'}"], check=True, timeout=600)
        if not iscc.is_file():
            raise SystemExit(f"Inno Setup {INNO_VERSION} did not install ISCC.exe")
    if os.name == "nt":
        found = subprocess.run(["powershell", "-NoProfile", "-Command",
                                f"(Get-Item -LiteralPath '{iscc}').VersionInfo.ProductVersion"],
                               capture_output=True, text=True, timeout=60).stdout.strip()
        if not found.startswith(INNO_VERSION):
            raise SystemExit(f"ISCC.exe reports version {found!r}, expected {INNO_VERSION}")
        say(f"Inno Setup {found} (pinned {INNO_VERSION}, SHA-256 checked)")
    return str(iscc)


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


def build(version, work, out=None, iscc=None, source=CLIENT, no_installer=False):
    """Stages 1-5 for one version; returns the installer path (None with ``no_installer``)."""
    work.mkdir(parents=True, exist_ok=True)
    say(f"build Python {platform.python_version()} ({sys.executable}); RainCLI {version}")
    src = stage_source(work, version, source)
    dist = freeze(work, src, version)
    sys.path.insert(0, str(HERE))
    import verify_bundle

    problems = verify_bundle.verify(dist, version)
    if problems:
        raise SystemExit("bundle check failed:\n  " + "\n  ".join(problems))
    say(f"PASS: bundle check: no test hooks in RainCLI {version}; the real tray and its GUI modules are frozen in")
    smoke(dist, version, work)
    if no_installer:
        return None
    setup, checksum = installer(dist, version, out, ensure_iscc(iscc, work))
    say(f"built {setup}\n      {checksum}: {checksum.read_text().strip()}")
    return setup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--version", help="X.Y.Z to build (default: raincli_agent.__version__)")
    parser.add_argument("--work", type=Path, default=ROOT / "build" / "windows")
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "windows")
    parser.add_argument("--iscc", help="path to ISCC.exe (default: the pinned Inno Setup)")
    parser.add_argument("--no-installer", action="store_true", help="freeze and check only")
    args = parser.parse_args(argv)
    version = args.version or source_version()
    if not VERSION_RE.fullmatch(version):
        parser.error(f"{version!r} is not X.Y.Z")
    build(version, args.work.resolve(), args.out.resolve(), args.iscc, no_installer=args.no_installer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
