"""Packaging of the app window (protocol §16.10, §16.11, §16.12 C16): pins, the spec, the bundle check
and the installer's WebView2 check. Static: the build itself runs on Windows CI."""

from __future__ import annotations

import importlib.util
import re
import struct
import types
from pathlib import Path

import pytest

from raincli_agent.app import window

REPO = Path(__file__).resolve().parents[3]
WIN = REPO / "packaging" / "windows"


def load_verify_bundle():
    spec = importlib.util.spec_from_file_location("verify_bundle", WIN / "verify_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pins(name):
    return [line for line in (WIN / name).read_text().splitlines() if line and not line.startswith("#")]


def test_every_build_dependency_is_hash_pinned_and_pywebview_is_build_only():
    lines = pins("requirements-build.txt") + pins("requirements-webview.txt")
    for line in lines:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]* --hash=sha256:[0-9a-f]{64}", line), line
    names = {line.split("==")[0].lower().replace("_", "-") for line in lines}
    assert {"pywebview", "proxy-tools", "pythonnet", "clr-loader", "bottle", "cffi", "pycparser",
            "typing-extensions", "pystray", "pillow", "setuptools"} <= names
    assert [line.split("==")[0] for line in pins("requirements-webview.txt")] == ["pywebview", "proxy_tools"]
    pyproject = (REPO / "raincli" / "pyproject.toml").read_text()
    assert "pywebview" not in pyproject and "pystray" not in pyproject  # the client stays stdlib-only


def test_spec_freezes_the_window_and_its_pages_and_no_tkinter():
    spec = (WIN / "raincli.spec").read_text()
    app = spec[spec.index('app = analysis("entry_app.py"'):spec.index('cli = analysis(')]
    assert '"webview"' in app and "webview.platforms.edgechromium" in app and "datas=LOCAL_PAGES" in app
    assert 'hidden=["raincli_agent.app.tray"' in app and '"tkinter"' not in app.split("hidden=")[1]
    assert 'excludes=["tkinter", "_tkinter"]' in app
    assert 'raincli_agent/app/local' in spec


def code(source):
    return compile(source, "<m>", "exec")


def test_bundle_check_refuses_debugging_switches_in_our_code_and_pages(tmp_path):
    vb = load_verify_bundle()
    assert set(vb.FORBIDDEN_DEBUG) == {"WEBVIEW2_" "ADDITIONAL_BROWSER_ARGUMENTS", "--remote-" "debugging-port",
                                       "--remote-" "debugging-pipe", "--remote-" "allow-origins",
                                       "REMOTE_" "DEBUGGING_PORT"}
    for bad in vb.FORBIDDEN_DEBUG:
        modules = {"raincli_agent.app.window": code(f"x = {bad!r}"), "webview": code(f"y = {bad!r}")}
        problems = vb.check_modules("app", modules, [])
        assert problems == [f"app: raincli_agent.app.window refers to {bad}"]  # third-party code is not ours
        assert vb.check_modules("app", {"<script>entry_app": code(f"import os; os.environ[{bad!r}]")}, [])
    local = tmp_path / "RainCLI-1.0.0" / "_internal" / "raincli_agent" / "app" / "local"
    local.mkdir(parents=True)
    (local / "local.js").write_text("// clean")
    assert vb.check_files(tmp_path) == []
    (local / "local.js").write_text("var a = '--remote-" "debugging-port=9222';")
    assert vb.check_files(tmp_path) == ["file RainCLI-1.0.0/_internal/raincli_agent/app/local/local.js refers to "
                                        "--remote-" "debugging-port"]
    other = tmp_path / "RainCLI-1.0.0" / "_internal" / "webview" / "js"
    other.mkdir(parents=True)
    (other / "api.js").write_text("REMOTE_" "DEBUGGING_PORT")  # pywebview's own files are not ours
    assert len(vb.check_files(tmp_path)) == 1


def test_bundle_check_requires_webview_and_refuses_tkinter_in_the_app():
    vb = load_verify_bundle()
    assert vb.check_absent("app", {"tkinter": None, "tkinter.ttk": None}, ["tkinter"]) == [
        "app: tkinter must not be frozen in"]
    assert vb.check_absent("app", {"tkinterx": None}, ["tkinter"]) == []
    source = (WIN / "verify_bundle.py").read_text()
    assert '"raincli_agent.app.window", "pystray", "PIL",\n                                      "webview"]' in source


def test_installer_detects_webview2_and_links_microsofts_bootstrapper():
    iss = (WIN / "RainCLI.iss").read_text()
    assert r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" in iss
    assert "https://go.microsoft.com/fwlink/p/?LinkId=2124703" in iss
    setup = iss[iss.index("function InitializeSetup"):]
    assert "if IsUpdate then" in setup and "WizardSilent" in setup and "ShellExec('open', WebView2Download" in setup
    assert window.WEBVIEW2_DOWNLOAD in iss and window.WEBVIEW2_CLIENT in iss  # one runtime check, two places


class FakeWinreg:
    HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER = "HKLM", "HKCU"

    def __init__(self, values):
        self.values = values

    def OpenKey(self, hive, path):
        if (hive, path) not in self.values:
            raise OSError("missing")
        return _Ctx(types.SimpleNamespace(value=self.values[(hive, path)]))

    @staticmethod
    def QueryValueEx(key, name):
        assert name == "pv"
        return key.value, 1


class _Ctx:
    def __init__(self, key):
        self.key = key

    def __enter__(self):
        return self.key

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("values,expected", [
    ({}, None),
    ({("HKLM", "SOFTWARE\\WOW6432Node\\" + window.WEBVIEW2_CLIENT): "0.0.0.0"}, None),
    ({("HKLM", "SOFTWARE\\WOW6432Node\\" + window.WEBVIEW2_CLIENT): "130.0.2849.80"}, "130.0.2849.80"),
    ({("HKCU", "Software\\" + window.WEBVIEW2_CLIENT): "131.0.1"}, "131.0.1"),
])
def test_webview2_detection(values, expected):
    assert window.webview2_version(FakeWinreg(values)) == expected


def test_full_install_records_the_v05_stub_and_a_fresh_install_stamp():
    """§16.15: install.json gets "stub": 2 and a new install_stamp on every full install; the Start menu
    shortcut keeps no arguments (the v0.5 stub opens the window)."""
    iss = (WIN / "RainCLI.iss").read_text()
    body = iss[iss.index("procedure WriteInstallJson;"):iss.index("{ -- L2 and H6")]
    assert '"stub": 2, "install_stamp": "\' + InstallStamp' in body and "GetSHA256OfString(" in iss[iss.index("function InstallStamp"):]
    assert "IntToHex" not in iss and "Random(" not in iss  # not in Inno's Pascal Script
    assert "external 'GetTickCount@kernel32.dll stdcall'" in iss
    icon = next(line for line in iss.splitlines() if line.startswith('Name: "{userprograms}\\RainCLI\\RainCLI";'))
    assert "Parameters" not in icon
    assert "WriteInstallJson" in iss[iss.index("procedure CurStepChanged"):]
    assert "raincli_agent.app.shortcut" in (WIN / "raincli.spec").read_text()


# -- the v0.5.1 logo: exe and installer icons, tray icons ------------------------------------------------------

def test_every_tray_state_has_its_bundled_icon():
    from raincli_agent.app import status, tray
    for state in status.ICON_STATES:
        path = tray.icon_path(state)
        assert path.name == f"tray-{state}-64.png" and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", path.read_bytes()[16:24])
        assert (width, height) == (64, 64)
    with pytest.raises(ValueError):
        tray.icon_path("paused")
    assert tray.icon_view(True, "ready") == ("offline", "RainCLI: Paused")
    assert tray.icon_view(False, "updating") == ("updating", "RainCLI: updating")
    assert not hasattr(tray, "COLOURS")


def test_paused_shows_the_offline_icon_and_says_paused(monkeypatch):
    from raincli_agent.app import tray
    monkeypatch.setattr(tray, "icon_image", lambda state: f"<{state}>")
    t = tray.Tray.__new__(tray.Tray)
    t.host = types.SimpleNamespace(paused=True)
    t.icon, t.icon_state = types.SimpleNamespace(icon=None, title=None), None
    t.status = lambda: {"status": "running"}
    t.refresh_icon()
    assert (t.icon.icon, t.icon.title) == ("<offline>", "RainCLI: Paused")
    t.host.paused = False
    monkeypatch.setattr(tray.model, "icon_state", lambda status: "ready")
    t.refresh_icon()
    assert (t.icon.icon, t.icon.title) == ("<ready>", "RainCLI: ready")


def test_app_ico_has_every_size_and_a_png_256_frame():
    data = (WIN / "app.ico").read_bytes()
    _, kind, count = struct.unpack_from("<HHH", data)
    sizes, frames = [], {}
    for i in range(count):
        w, h, _c, _r, _p, bpp, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        sizes.append(w or 256)
        frames[w or 256] = data[offset:offset + size]
    assert kind == 1 and sorted(sizes) == [16, 20, 24, 32, 40, 48, 64, 256]
    assert frames[256][:8] == b"\x89PNG\r\n\x1a\n" and len(data) < 64 * 1024  # re-encoded losslessly
    assert all(frames[s][:4] == b"\x28\x00\x00\x00" for s in sizes if s != 256)  # the others untouched


def test_executables_and_installer_carry_the_app_icon():
    spec = (WIN / "raincli.spec").read_text()
    assert spec.count("icon=ICON") == 3 and 'ICON = str(HERE / "app.ico")' in spec
    assert "datas=LOCAL_PAGES + TRAY_ICONS" in spec and '"raincli_agent/app/icons").glob("tray-*-64.png")' in spec
    assert "SetupIconFile=app.ico" in (WIN / "RainCLI.iss").read_text()


def test_bundle_check_requires_the_icons(tmp_path):
    vb = load_verify_bundle()
    folder = tmp_path / "RainCLI-1.0.0"
    icons = folder / "_internal" / "raincli_agent" / "app" / "icons"
    icons.mkdir(parents=True)
    exes = [folder / "RainCLI-app.exe", folder / "raincli.exe", tmp_path / "stub" / "RainCLI.exe"]
    for exe in exes:
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"MZ")
    frames = vb.ico_frames(WIN / "app.ico")
    embedded = {str(exe): list(frames) for exe in exes}
    check = lambda: vb.check_icons(tmp_path, folder, exes, frames_of=lambda exe: embedded[str(exe)])  # noqa: E731
    assert check() == [f"RainCLI-1.0.0/_internal/raincli_agent/app/icons/tray-{s}-64.png is missing"
                       for s in ("ready", "offline", "updating", "error")]
    for state in ("ready", "offline", "updating", "error"):
        (icons / f"tray-{state}-64.png").write_bytes(b"\x89PNG")
    assert check() == []
    embedded[str(exes[1])] = frames[:-1]  # raincli.exe without the 256 px frame
    assert check() == ["RainCLI-1.0.0/raincli.exe does not carry app.ico"]
    assert vb.TRAY_STATES == __import__("raincli_agent.app.status", fromlist=["x"]).ICON_STATES
