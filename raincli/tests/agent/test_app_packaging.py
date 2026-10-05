"""Packaging of the app window (protocol §16.10, §16.11, §16.12 C16): pins, the spec, the bundle check
and the installer's WebView2 check. Static: the build itself runs on Windows CI."""

from __future__ import annotations

import importlib.util
import re
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
