"""The Windows app updater, stub and layout (15.5 amended by 15.8 H4, H8, L5, M4-M6, M11)."""
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import raincli_agent
from raincli_agent.errors import ConfigError
from raincli_agent.runtime import pushed as pushed_mod, updates, winapp

REPO_API = "https://api.github.com/repos/DylanHallahan/raincli"
INSTALLER = b"MZ fake installer bytes " * 1000


def release(version="0.4.1", installer=INSTALLER, **overrides):
    name = f"RainCLI-Setup-{version}.exe"
    data = {"tag_name": "v" + version, "draft": False, "prerelease": False, "assets": [
        {"name": name, "size": len(installer), "url": f"{REPO_API}/releases/assets/101"},
        {"name": name + ".sha256", "size": 90, "url": f"{REPO_API}/releases/assets/102"}]}
    data.update(overrides)
    return data


class Response:
    def __init__(self, status, body=b"", headers=None):
        self.status, self.body, self.headers = status, io.BytesIO(body), headers or {}
        if status == 200:
            self.headers.setdefault("Content-Length", str(len(body)))

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, n=-1):
        return self.body.read(n)


class Web:
    """Fake HTTPS: ``routes[(host, path)] -> Response``; records every request."""

    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def connect(self, host):
        web = self

        class Connection:
            def request(self, method, path, headers=None):
                web.requests.append((host, path, dict(headers or {})))
                self.key = (host, path)

            def getresponse(self):
                response = web.routes.get(self.key)
                if response is None:
                    return Response(404)
                return Response(*response) if isinstance(response, tuple) else response

            def close(self):
                pass
        return Connection()


@pytest.fixture
def web(monkeypatch):
    def install(routes):
        fake = Web(routes)
        monkeypatch.setattr(winapp, "_connect", fake.connect)
        return fake
    return install


def standard_routes(version="0.4.1", installer=INSTALLER, checksum=None, meta=None):
    name = f"RainCLI-Setup-{version}.exe"
    checksum = checksum if checksum is not None else (
        hashlib.sha256(installer).hexdigest() + "  " + name + "\n").encode()
    return {
        ("api.github.com", f"/repos/DylanHallahan/raincli/releases/tags/v{version}"):
            (200, json.dumps(meta or release(version, installer)).encode()),
        ("api.github.com", "/repos/DylanHallahan/raincli/releases/assets/102"):
            (302, b"", {"Location": "https://objects.githubusercontent.com/sum?sig=1"}),
        ("objects.githubusercontent.com", "/sum?sig=1"): (200, checksum),
        ("api.github.com", "/repos/DylanHallahan/raincli/releases/assets/101"):
            (302, b"", {"Location": "https://release-assets.githubusercontent.com/inst?sig=2"}),
        ("release-assets.githubusercontent.com", "/inst?sig=2"): (200, installer),
    }


class Installer:
    """Stands in for the Inno Setup installer and the new version's raincli.exe."""

    def __init__(self, root, version="0.4.1", reports=None, exit_code=0):
        self.root, self.version, self.reports, self.exit_code = root, version, reports, exit_code
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[1:2] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"raincli {self.reports or self.version}\n")
        assert Path(argv[0]).is_absolute() and kwargs.get("shell") is False
        assert Path(argv[0]).read_bytes() == INSTALLER  # verified bytes, run in place
        target = Path(argv[-1].removeprefix("/DIR="))
        target.mkdir(parents=True)
        for exe in (winapp.CLI_EXE, winapp.APP_EXE):
            (target / exe).write_text("exe")
        return subprocess.CompletedProcess(argv, self.exit_code)


@pytest.fixture
def app(tmp_path):
    root = tmp_path / "RainCLI"
    for version in ("0.4.0",):
        d = root / "versions" / version
        d.mkdir(parents=True)
        (d / winapp.APP_EXE).write_text("exe")
        (d / winapp.CLI_EXE).write_text("exe")
    (root / winapp.STUB).write_text("stub")
    winapp.write_install(root, "0.4.0", None)
    return root


# -- M11: constants, no override ----------------------------------------------------------------

def test_hosts_api_repo_are_constants_without_override():
    assert winapp.HOSTS == frozenset({"api.github.com", "github.com", "objects.githubusercontent.com",
                                      "release-assets.githubusercontent.com"})
    assert winapp.API == "https://api.github.com/repos/DylanHallahan/raincli" and winapp.REPO == "DylanHallahan/raincli"
    assert updates.REPO == winapp.REPO and updates.HOSTS == {"api.github.com", "codeload.github.com"}
    package = Path(raincli_agent.__file__).parent
    for name in ("runtime/winapp.py", "runtime/updates.py", "runtime/pushed.py"):
        tree = ast.parse((package / name).read_text())
        for node in ast.walk(tree):
            # Neither the environment nor any config can reach the transport. (updates.install
            # copies the environment only into its verification subprocesses, minus PYTHONPATH.)
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv") and name != "runtime/updates.py":
                pytest.fail(f"{name} reads the environment")
        constants = {t.id: n.value for n in tree.body if isinstance(n, ast.Assign) for t in n.targets
                     if isinstance(t, ast.Name) and t.id in ("HOSTS", "API", "REPO")}
        for key, value in constants.items():
            # A literal, or built only from literals and these constants.
            for leaf in ast.walk(value):
                if isinstance(leaf, ast.Name):
                    assert leaf.id in ("REPO", "frozenset"), (name, key, leaf.id)
                assert not isinstance(leaf, ast.Attribute), (name, key)
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Attribute) and t.attr in ("HOSTS", "API", "REPO"):
                        pytest.fail(f"{name} reassigns {t.attr}")
    for path in package.rglob("*.py"):
        text = path.read_text()
        assert "_build_test" not in text and "TEST_RELEASE_BASE" not in text, path
    assert not list(package.rglob("_build_test*"))


def test_environment_cannot_change_release_hosts(monkeypatch, web):
    for name in ("RAINCLI_RELEASE_BASE", "RAINCLI_UPDATE_HOST", "TEST_RELEASE_BASE", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "https://127.0.0.1:8443")
    fake = web(standard_routes())
    winapp.resolve("v0.4.1")
    assert [h for h, _, _ in fake.requests] == ["api.github.com"]
    with pytest.raises(winapp.NetworkError):
        winapp.fetch("https://127.0.0.1:8443/repos/DylanHallahan/raincli/releases/tags/v0.4.1", 100)


@pytest.mark.parametrize("url", [
    "http://api.github.com/x",  # not https
    "https://api.github.com:8443/x",  # not port 443
    "https://evil.example/x",
    "https://raw.githubusercontent.com/x",  # a githubusercontent.com host not on the list
    "https://evil-objects.githubusercontent.com/x",  # wildcard lookalikes
    "https://objects.githubusercontent.com.evil.example/x",
    "https://xobjects.githubusercontent.com/x",
    "https://user@github.com/x",
    "https://codeload.github.com/x",  # the v0.3 archive host is not an app asset host
])
def test_hostile_hosts_refused(url):
    with pytest.raises(winapp.NetworkError):
        winapp.check_hop(url)


def test_hostile_redirect_refused_before_request(web):
    fake = web({("api.github.com", "/repos/DylanHallahan/raincli/releases/assets/101"):
                (302, b"", {"Location": "https://objects.githubusercontent.com.evil.example/i"})})
    with pytest.raises(winapp.NetworkError):
        winapp.fetch(f"{REPO_API}/releases/assets/101", 1000)
    assert [h for h, _, _ in fake.requests] == ["api.github.com"]  # the hostile hop was never requested


def test_redirect_loop_and_size_bounds(web):
    web({("api.github.com", "/a"): (302, b"", {"Location": "/a"})})
    with pytest.raises(winapp.NetworkError, match="too many"):
        winapp.fetch("https://api.github.com/a", 10)
    web({("api.github.com", "/big"): (200, b"x" * 2000)})
    with pytest.raises(winapp.NetworkError, match="size limit"):
        winapp.fetch("https://api.github.com/big", 1024)
    web({("api.github.com", "/lying"): Response(200, b"x" * 2000, {"Content-Length": "10"})})
    with pytest.raises(winapp.NetworkError, match="size limit"):
        winapp.fetch("https://api.github.com/lying", 1024)


# -- resolve and assets (M4) ----------------------------------------------------------------------

@pytest.mark.parametrize("change", [
    {"draft": True}, {"prerelease": True}, {"tag_name": "v0.4.2"}, {"draft": None},
])
def test_resolve_requires_the_exact_stable_release(web, change):
    web(standard_routes(meta=release(**change)))
    with pytest.raises(updates.VerificationError):
        winapp.resolve("v0.4.1")


def test_resolve_matches_assets_by_exact_name_and_api_url(web):
    meta = release()
    meta["assets"][0]["url"] = "https://github.com/DylanHallahan/raincli/releases/download/v0.4.1/x.exe"
    web(standard_routes(meta=meta))
    with pytest.raises(updates.VerificationError, match="canonical API URL"):
        winapp.resolve("v0.4.1")
    meta = release()
    meta["assets"][0]["size"] = winapp.MAX_INSTALLER + 1
    web(standard_routes(meta=meta))
    with pytest.raises(updates.VerificationError, match="size limit"):
        winapp.resolve("v0.4.1")
    meta = release()
    meta["assets"][1]["name"] = "RainCLI-Setup-0.4.1.exe.sha256.txt"
    web(standard_routes(meta=meta))
    with pytest.raises(updates.ReleaseNotFound):
        winapp.resolve("v0.4.1")


def test_assets_fetched_from_api_url_with_octet_stream(app, web):
    fake = web(standard_routes())
    winapp.install(app, "v0.4.1", run=Installer(app))
    first_hops = [(h, p, hd.get("Accept")) for h, p, hd in fake.requests if "assets" in p]
    assert first_hops == [("api.github.com", "/repos/DylanHallahan/raincli/releases/assets/102", "application/octet-stream"),
                          ("api.github.com", "/repos/DylanHallahan/raincli/releases/assets/101", "application/octet-stream")]
    assert all("Authorization" not in hd and "Cookie" not in hd for _, _, hd in fake.requests)


@pytest.mark.parametrize("checksum", [
    b"0" * 64 + b"  RainCLI-Setup-0.4.1.exe\n",  # wrong hash
    hashlib.sha256(INSTALLER).hexdigest().encode() + b"  RainCLI-Setup-0.4.2.exe\n",  # another file's name
    hashlib.sha256(INSTALLER).hexdigest().upper().encode() + b"  RainCLI-Setup-0.4.1.exe\n",
    hashlib.sha256(INSTALLER).hexdigest().encode() + b" RainCLI-Setup-0.4.1.exe\n",
])
def test_hash_mismatch_or_bad_checksum_line_refused(app, web, checksum):
    web(standard_routes(checksum=checksum))
    runner = Installer(app)
    with pytest.raises(updates.VerificationError):
        winapp.install(app, "v0.4.1", run=runner)
    assert runner.calls == []  # never run
    assert winapp.read_install(app)["current"] == "0.4.0"
    downloads = app / "state" / "downloads"
    assert not downloads.exists() or not any(downloads.iterdir())  # the download directory is removed


# -- install, swap, verification -----------------------------------------------------------------

def test_install_runs_silent_update_verifies_and_swaps(app, web):
    web(standard_routes())
    runner = Installer(app)
    result = winapp.install(app, "v0.4.1", run=runner)
    assert result == {"status": "installed", "tag": "v0.4.1", "previous": "0.4.0"}
    argv = runner.calls[0][0]
    assert argv[1:6] == ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE",
                         f"/DIR={app / 'versions' / '0.4.1'}"]
    assert runner.calls[1][0] == [str(app / "versions" / "0.4.1" / "raincli.exe"), "--version"]
    assert json.loads((app / "install.json").read_text()) == {"current": "0.4.1", "previous": "0.4.0",
                                                              "probation": "0.4.1"}
    assert not (app / "install.json.new").exists()
    assert not any((app / "state" / "downloads").iterdir())
    assert winapp.install(app, "v0.4.1", run=runner)["status"] == "current"


def test_installed_version_must_report_the_release(app, web):
    web(standard_routes())
    with pytest.raises(updates.VerificationError, match="does not report"):
        winapp.install(app, "v0.4.1", run=Installer(app, reports="0.4.0"))
    assert winapp.read_install(app)["current"] == "0.4.0"


def test_installer_failure_is_a_verification_failure(app, web):
    web(standard_routes())
    with pytest.raises(updates.VerificationError):
        winapp.install(app, "v0.4.1", run=Installer(app, exit_code=5))


def test_install_json_validation(app):
    (app / "install.json").write_text('{"current": "../../evil", "previous": "0.4.0"}')
    assert winapp.read_install(app) == {"current": None, "previous": "0.4.0", "probation": None}
    with pytest.raises(ConfigError):
        winapp.write_install(app, "1.2", None)
    with pytest.raises(ConfigError):
        winapp.version_dir(app, "01.2.3")


def test_app_install_detection(app, tmp_path):
    exe = app / "versions" / "0.4.0" / "raincli.exe"
    assert winapp.app_root(exe, frozen=True) == app
    assert winapp.app_root(exe, frozen=False) is None  # Python installs are never app installs
    (app / winapp.STUB).unlink()
    assert winapp.app_root(exe, frozen=True) is None
    assert winapp.app_root(tmp_path / "x" / "raincli.exe", frozen=True) is None


# -- pushed updates through the app path ----------------------------------------------------------

def test_pushed_target_installs_through_the_app_path(app, monkeypatch):
    monkeypatch.setattr(pushed_mod, "__version__", "0.4.0")
    installed = []
    driver = pushed_mod.PushedUpdates(app_root=app, python=app / "versions" / "0.4.0" / "raincli.exe",
                                      resolve=lambda tag: {"tag": tag}, install=lambda r: installed.append(r) or
                                      {"status": "installed"}, log=lambda t: None)
    assert driver.managed() and driver.mode() == "automatic" and driver.floor() == (0, 4, 0)
    driver.consider({"version": "v0.4.1", "allow_downgrade": False, "set_at": "t1"})
    driver.thread.join(5)
    assert installed == [{"tag": "v0.4.1"}] and driver.data["state"] == "updating"
    assert updates.read_update_state(app)["target"]["version"] == "v0.4.1"


def test_app_refuses_targets_below_v040_and_downgrades(app, monkeypatch):
    monkeypatch.setattr(pushed_mod, "__version__", "0.4.1")
    driver = pushed_mod.PushedUpdates(app_root=app, python=app / "versions" / "0.4.0" / "raincli.exe",
                                      install=lambda r: pytest.fail("installed"), log=lambda t: None)
    driver.consider({"version": "v0.3.2", "allow_downgrade": True, "set_at": "t"})
    assert driver.data["error"] == "target_below_minimum"
    driver.consider({"version": "v0.4.0", "allow_downgrade": False, "set_at": "t2"})
    assert driver.data["error"] == "downgrade_not_allowed"


def test_manual_mode_and_explicit_rollback(app):
    assert winapp.configure(app, mode="manual")["update_mode"] == "manual"
    with pytest.raises(ConfigError, match="no previous"):
        winapp.configure(app, rollback=True)
    d = app / "versions" / "0.4.1"
    d.mkdir()
    (d / winapp.APP_EXE).write_text("exe")
    winapp.write_install(app, "0.4.1", "0.4.0")
    assert winapp.configure(app, rollback=True) == {"version": "0.4.0", "update_mode": "manual"}
    assert winapp.read_install(app)["previous"] == "0.4.1"


def test_rollback_refuses_previous_below_v040(app):
    d = app / "versions" / "0.3.9"
    d.mkdir()
    (d / winapp.APP_EXE).write_text("exe")
    winapp.write_install(app, "0.4.0", "0.3.9")
    with pytest.raises(ConfigError, match="below v0.4.0"):
        winapp.configure(app, rollback=True)
    winapp.write_install(app, "0.4.0", "0.3.9", probation="0.4.0")
    assert winapp.rollback(app, "0.4.0") is False


# -- the heartbeat, the stub and the tray's host --------------------------------------------------

class Process:
    def __init__(self, code=None, on_wait=None):
        self.code, self.on_wait, self.pid = code, on_wait, 4242
        self.stopped = False

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        if self.on_wait:
            self.on_wait()
        return self.code if self.code is not None else 0


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def add_version(app, version):
    d = app / "versions" / version
    d.mkdir(parents=True)
    (d / winapp.APP_EXE).write_text("exe")
    (d / winapp.CLI_EXE).write_text("exe")


def test_stub_promotes_a_version_that_beats_within_probation(app):
    add_version(app, "0.4.1")
    winapp.write_install(app, "0.4.1", "0.4.0", probation="0.4.1")
    clock, started = Clock(), []

    def popen(argv, **kw):
        version = Path(argv[0]).parent.name
        started.append(version)
        winapp.beat(app, version)
        return Process(code=0)
    assert winapp.Stub(app, popen=popen, sleep=clock.sleep, clock=clock, wall=lambda: 0).run() == 0
    assert started == ["0.4.1"]
    assert winapp.read_install(app) == {"current": "0.4.1", "previous": "0.4.0", "probation": None}


def test_stub_rolls_back_a_version_without_heartbeat(app):
    add_version(app, "0.4.1")
    winapp.write_install(app, "0.4.1", "0.4.0", probation="0.4.1")
    updates.write_update_state(app, {"state": "updating", "target": {"version": "v0.4.1", "set_at": "t"}})
    clock, started, stopped = Clock(), [], []

    def popen(argv, **kw):
        version = Path(argv[0]).parent.name
        started.append(version)
        return Process(code=None) if version == "0.4.1" else Process(code=0)
    stub = winapp.Stub(app, popen=popen, sleep=clock.sleep, clock=clock, wall=lambda: 0,
                       stop_app=lambda p: stopped.append(p))
    assert stub.run() == 0
    assert started == ["0.4.1", "0.4.0"] and len(stopped) == 1
    assert winapp.read_install(app) == {"current": "0.4.0", "previous": "0.4.1", "probation": None}
    state = updates.read_update_state(app)
    assert (state["state"], state["error"], state["blocked"]["version"]) == ("rolled_back", "first_start_failed", "v0.4.1")
    assert winapp.update_mode(app) == "automatic"  # a rollback never changes the mode


def test_stub_relaunches_after_a_switch_and_rolls_back_a_crash_on_probation(app):
    add_version(app, "0.4.1")
    clock, started = Clock(), []

    def popen(argv, **kw):
        version = Path(argv[0]).parent.name
        started.append(version)
        if len(started) == 1:  # the running 0.4.0 installs 0.4.1 and exits with the switch code
            return Process(code=winapp.SWITCH_EXIT, on_wait=lambda: winapp.write_install(app, "0.4.1", "0.4.0", "0.4.1"))
        if version == "0.4.1":
            return Process(code=3)  # crashes before its first heartbeat
        return Process(code=0)
    assert winapp.Stub(app, popen=popen, sleep=clock.sleep, clock=clock, wall=lambda: 0).run() == 0
    assert started == ["0.4.0", "0.4.1", "0.4.0"]
    assert winapp.read_install(app)["current"] == "0.4.0"


def test_stub_refuses_a_missing_version_executable(app):
    winapp.write_install(app, "0.4.1", "0.4.0", "0.4.1")  # versions/0.4.1 does not exist
    clock = Clock()
    started = []
    stub = winapp.Stub(app, popen=lambda argv, **kw: started.append(argv) or Process(code=0),
                       sleep=clock.sleep, clock=clock, wall=lambda: 0)
    assert stub.run() == 0
    assert [Path(a[0]).parent.name for a in started] == ["0.4.0"]


def test_heartbeat_comes_from_the_runtime_tick(app, monkeypatch):
    monkeypatch.setattr(pushed_mod, "__version__", "0.4.1")
    add_version(app, "0.4.1")
    winapp.write_install(app, "0.4.1", "0.4.0", probation="0.4.1")
    updates.write_update_state(app, {"state": "updating", "target": {"version": "v0.4.1", "allow_downgrade": False,
                                                                     "set_at": "t"}})
    monkeypatch.setattr(winapp, "__version__", "0.4.1")
    driver = pushed_mod.PushedUpdates(app_root=app, python=app / "versions" / "0.4.1" / "raincli.exe",
                                      log=lambda t: None)
    driver.started()
    assert driver.data["state"] == "current"
    assert winapp.heartbeat_since(app, "0.4.1", 0)


def test_prune_keeps_current_previous_probation_and_running(app):
    for version in ("0.4.1", "0.4.2", "0.4.3", "0.4.4"):
        add_version(app, version)
    winapp.write_install(app, "0.4.3", "0.4.2")
    removed = winapp.prune(app, running=app / "versions" / "0.4.1" / "raincli.exe")
    assert removed == ["0.4.0", "0.4.4"]
    assert sorted(p.name for p in (app / "versions").iterdir()) == ["0.4.1", "0.4.2", "0.4.3"]


def test_app_host_switches_restarts_and_pauses(app):
    clock = Clock()
    processes = []

    def command():
        processes.append(Process(code=None))
        return ["runtime"]
    host = winapp.AppHost(app, "0.4.0", app / "runtime.json", clock=clock, request_stop=lambda c: None)
    host.command = command
    launched = []
    original = subprocess.Popen

    class Popen:
        def __init__(self, argv, **kw):
            launched.append(argv)
            self.proc = processes[-1]

        def poll(self):
            return self.proc.code

        def wait(self, timeout=None):
            return 0
    subprocess.Popen = Popen
    try:
        assert host.step() is None and len(launched) == 1
        processes[-1].code = 1  # crashed: restarted after a backoff
        assert host.step() is None and len(launched) == 1
        clock.now += 3
        assert host.step() is None and len(launched) == 2
        host.paused = True
        assert host.step() is None
        host.paused = False
        add_version(app, "0.4.1")
        winapp.write_install(app, "0.4.1", "0.4.0", "0.4.1")
        processes[-1].code = 0
        assert host.step() == winapp.SWITCH_EXIT
    finally:
        subprocess.Popen = original


def test_logon_start_never_overwrites_an_unrecorded_value(app):
    class Registry(dict):
        def get(self, name):
            return super().get(name)

        def set(self, name, value):
            self[name] = value

        def delete(self, name):
            self.pop(name, None)
    registry = Registry(RainCLI='"C:\\Python\\pythonw.exe" "C:\\old\\launch.py" "runtime" "run"')
    assert winapp.enable_logon_start(app, registry) == "kept_existing"
    registry.clear()
    assert winapp.enable_logon_start(app, registry) == "enabled"
    assert registry["RainCLI"] == f'"{app / "RainCLI.exe"}" --background'
    assert winapp.disable_logon_start(app, registry) == "disabled" and "RainCLI" not in registry


def test_tray_status_model():
    from raincli_agent.app import status
    running = {"status": "running", "stale": False, "connectors": [{"reported": True}],
               "client": {"version": "0.4.0", "update_mode": "automatic", "update_state": "current"}}
    assert status.icon_state(running) == "ready"
    assert status.icon_state({**running, "client": {"update_state": "updating"}}) == "updating"
    assert status.icon_state({**running, "client": {"update_state": "rolled_back"}}) == "error"
    assert status.icon_state({**running, "stale": True}) == "offline"
    assert status.icon_state({"status": "not_observed"}) == "offline"
    assert status.icon_state({**running, "connectors": [{"reported": False, "error": "Unreachable"}]}) == "offline"
    lines = status.describe(running, handle="work-pc", agents=[{"name": "a", "type": "claude", "status": "idle",
                                                                 "role": None}])
    assert lines[0] == "Connection: ready" and "Machine: work-pc" in lines


def test_tray_imports_gui_libraries_lazily():
    source = (Path(raincli_agent.__file__).parent / "app" / "tray.py").read_text()
    tree = ast.parse(source)
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name.split(".")[0] for n in top for a in n.names} | {
        (n.module or "").split(".")[0] for n in top if isinstance(n, ast.ImportFrom)}
    assert not names & {"pystray", "PIL", "tkinter"}
    result = subprocess.run([sys.executable, "-c", "import sys, raincli_agent.app.tray, raincli_agent.cli; "
                             "assert not {'pystray', 'PIL', 'tkinter'} & set(sys.modules), sys.modules.keys()"],
                            capture_output=True, text=True, cwd=Path(raincli_agent.__file__).parents[1])
    assert result.returncode == 0, result.stderr
