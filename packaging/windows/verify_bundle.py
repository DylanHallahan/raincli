"""Refuse a Windows app bundle that carries a test hook or the wrong code (protocol §15.8 M11).

Release hosts, the API and the repository are constants in shipped code. No module,
file or name in the bundle may redirect them. This lists every frozen module in each
executable's archive and checks:

- no ``_build_test`` module or file, and no test package;
- no code object naming ``TEST_RELEASE_BASE``, ``TEST_CERT_SHA256`` or ``_build_test``;
- no server, test-runner or build-tool module;
- the app executable freezes ``raincli_agent.app.tray``, the window and its GUI modules (``pystray``,
  ``PIL``, ``webview``) and never ``tkinter``; the CLI freezes ``raincli_agent.cli``, the stub freezes
  ``raincli_agent.app.stub``, and the PATH shim freezes no client code at all;
- the app icon (``app.ico``) is embedded in RainCLI-app.exe, raincli.exe and the RainCLI.exe stub, and the
  four tray icons are bundled with the app, as are the local pages and every file they load (``localtime.js``
  among them);
- the GUI test boundary (§16.11, §16.12 C16): no ``raincli_agent`` module, entry script or bundled
  ``raincli_agent`` file (the local pages) contains a WebView2 debugging switch or its variable.
  Third-party modules are covered by the test asserting ``debug=False`` and no debugging settings.

Usage: python verify_bundle.py <dist-dir> --version X.Y.Z
Exit status 0 when clean; 1 with one line per problem otherwise.
"""
import argparse
from pathlib import Path
import struct
import sys
import types

HERE = Path(__file__).resolve().parent
TRAY_STATES = ("ready", "offline", "updating", "error")  # raincli_agent.app.status.ICON_STATES
# The local pages and the files they load (raincli_agent/app/local); localtime.js is the server's copy (§17.3 A6).
LOCAL_FILES = ("sign-in.html", "this-computer.html", "settings.html", "offline.html", "local.css", "tokens.css",
               "local.js", "localtime.js", "status.js", "sign-in.js", "this-computer.js", "settings.js", "offline.js")

FORBIDDEN_NAMES = ("TEST_RELEASE_BASE", "TEST_CERT_SHA256", "_build_test")
FORBIDDEN_MODULES = ("raincli_server", "pytest", "PyInstaller", "fastapi", "sqlalchemy", "uvicorn")
# Built from parts, so this checker never matches itself if it is ever bundled.
FORBIDDEN_DEBUG = tuple("".join(parts) for parts in (
    ("WEBVIEW2_", "ADDITIONAL_BROWSER_ARGUMENTS"), ("--remote-", "debugging-port"), ("--remote-", "debugging-pipe"),
    ("--remote-", "allow-origins"), ("REMOTE_", "DEBUGGING_PORT")))


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
            for bad in FORBIDDEN_NAMES + FORBIDDEN_DEBUG:
                if any(bad in s for s in strings):
                    problems.append(f"{label}: {short} refers to {bad}")
    for name in required:
        if name not in modules:
            problems.append(f"{label}: {name} is not frozen in")
    return problems


def check_absent(label, modules, absent):
    return [f"{label}: {name} must not be frozen in" for name in absent
            if any(m == name or m.startswith(name + ".") for m in modules)]


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
        if path.is_file() and "raincli_agent" in path.parts:  # our own data files, such as the local pages
            data = path.read_bytes()
            for bad in FORBIDDEN_DEBUG:
                if bad.encode() in data:
                    problems.append(f"file {path.relative_to(dist)} refers to {bad}")
    return problems


def ico_frames(path):
    """The image data of every frame in an .ico file."""
    data = Path(path).read_bytes()
    _, kind, count = struct.unpack_from("<HHH", data)
    if kind != 1:
        raise ValueError(f"{path} is not an icon")
    frames = []
    for i in range(count):
        size, offset = struct.unpack_from("<II", data, 6 + 16 * i + 8)
        frames.append(data[offset:offset + size])
    return frames


def exe_icon_frames(exe):
    """The RT_ICON resources of a Windows executable (PyInstaller embeds each .ico frame unchanged)."""
    import pefile

    pe = pefile.PE(str(exe), fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
    frames = []
    for kind in getattr(pe, "DIRECTORY_ENTRY_RESOURCE", types.SimpleNamespace(entries=[])).entries:
        if kind.id != pefile.RESOURCE_TYPE["RT_ICON"]:
            continue
        for name in kind.directory.entries:
            for lang in name.directory.entries:
                frames.append(pe.get_data(lang.data.struct.OffsetToData, lang.data.struct.Size))
    return frames


def check_icons(dist, folder, exes, ico=HERE / "app.ico", frames_of=exe_icon_frames):
    """The app's icon is in every executable the user sees, and the tray icons are bundled."""
    problems = []
    wanted = ico_frames(ico)
    for exe in exes:
        if exe.is_file():
            have = frames_of(exe)
            if not all(frame in have for frame in wanted):
                problems.append(f"{exe.relative_to(dist)} does not carry app.ico")
    for state in TRAY_STATES:
        icon = folder / "_internal" / "raincli_agent" / "app" / "icons" / f"tray-{state}-64.png"
        if not icon.is_file():
            problems.append(f"{icon.relative_to(dist)} is missing")
    return problems


def check_local_pages(dist, folder):
    """Every local page and the scripts and styles it loads are bundled with the app."""
    local = folder / "_internal" / "raincli_agent" / "app" / "local"
    return [f"{(local / name).relative_to(dist)} is missing" for name in LOCAL_FILES if not (local / name).is_file()]


def verify(dist, version):
    dist = Path(dist)
    folder = dist / f"RainCLI-{version}"
    problems = check_files(dist)
    targets = [
        (folder / "RainCLI-app.exe", ["raincli_agent.app.tray", "raincli_agent.app.window", "pystray", "PIL",
                                      "webview"]),
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
        problems += check_absent(str(exe.relative_to(dist)), modules,
                                 ["tkinter"] if exe.name == "RainCLI-app.exe" else ["webview", "pystray", "tkinter"])
        if exe.parent.name == "bin" and any(m.startswith("raincli_agent") for m in modules):
            problems.append("bin/raincli.exe must not bundle the client; it only forwards to the current version")
    problems += check_icons(dist, folder, [folder / "RainCLI-app.exe", folder / "raincli.exe", dist / "stub" / "RainCLI.exe"])
    problems += check_local_pages(dist, folder)
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
        print(f"bundle check passed: RainCLI {args.version} carries no test hooks and no debugging switches")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
