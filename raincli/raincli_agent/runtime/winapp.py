"""The Windows app install (protocol 15.5, amended by 15.8): layout, pushed updates,
probation, rollback and pruning.

Layout under the install root (``%LOCALAPPDATA%\\Programs\\RainCLI``)::

    RainCLI.exe            stable stub, never changed by an update; owns probation
    bin\\raincli.exe        the PATH shim, forwarding to the current version
    versions\\<X.Y.Z>\\      one PyInstaller onedir build: RainCLI-app.exe and raincli.exe
    install.json           {"current", "previous", "probation"}
    app.json               which agent and runtime config the app runs
    heartbeat.json         the running version's readiness heartbeat
    update-state.json, update-mode.json, update-lock\\, state\\downloads\\

Process topology (15.8 H4): the Run value starts the stub; the stub starts
``versions\\<current>\\RainCLI-app.exe`` (the tray), which supervises the runtime as
its child. The updater runs in the runtime: it downloads and verifies the
installer, runs it in ``/UPDATE`` mode, verifies ``raincli.exe --version``, writes
``install.json`` with ``probation`` set to the new version, and the tray, seeing
another current version, stops the runtime and exits with ``SWITCH_EXIT``. The
stub starts the new version and waits up to ``PROBATION`` seconds for its
heartbeat; otherwise it restores the previous version and records ``rolled_back``.

Transport (15.5, 15.8 M4/M11): release metadata and both assets come from the
canonical repository's API over https on port 443, from the exact ``HOSTS``
allowlist checked on every hop. ``HOSTS``, ``API`` and ``REPO`` are constants:
no environment variable, config key or flag changes them. Trust is TLS to
GitHub plus write access to the repository; the checksum detects corruption
only, and releases are unsigned. The server names only a version.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import time
from urllib.parse import urljoin, urlsplit
import uuid

from .. import __version__
from ..errors import ConfigError
from ..fsutil import atomic_write_json, ensure_private_dir, retry_sharing
from . import updates

REPO = "DylanHallahan/raincli"
API = "https://api.github.com/repos/" + REPO
HOSTS = frozenset({"api.github.com", "github.com", "objects.githubusercontent.com",
                   "release-assets.githubusercontent.com"})
MAX_INSTALLER = 200 * 1024 * 1024
MAX_CHECKSUM = 1024
MAX_METADATA = 1024 * 1024
MAX_HOPS = 5
MIN_VERSION = (0, 4, 0)  # the first app version, and the floor in machine mode (15.8 H8)
VERSION_RE = re.compile(r"(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})")
ASSET_URL_RE = re.compile(r"https://api\.github\.com/repos/" + re.escape(REPO) + r"/releases/assets/[0-9]{1,20}")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "RainCLI"
STUB = "RainCLI.exe"
APP_EXE = "RainCLI-app.exe"
CLI_EXE = "raincli.exe"
SHIM = Path("bin") / "raincli.exe"
INSTALL = "install.json"
SETTINGS = "app.json"
HEARTBEAT = "heartbeat.json"
INSTALL_TIMEOUT = 600
SWITCH_EXIT = 75  # the tray asks the stub to start install.json's current version
# Another tray holds tray.lock: the stub waits and retries (review 3 N1). Not 3, which
# abort() and Py_FatalError also return on Windows (review 4 D1); and only believed
# while tray.lock really is held.
ALREADY_RUNNING_EXIT = 76
ALREADY_RUNNING_WAIT = 10
PROBATION = 120  # seconds for a new version's first heartbeat (15.8 H4)
GRACEFUL_STOP = 120


class NetworkError(updates.NetworkError):
    pass


class VerificationError(updates.VerificationError):
    pass


def version_key(version):
    return updates.version_key(version)


# -- the install root ----------------------------------------------------------------

def app_root(executable=None, frozen=None):
    """The install root when this process is a frozen executable under
    ``<root>\\versions\\<v>\\`` and ``<root>\\RainCLI.exe`` exists (15.8 L5), else None.
    Never decided from the server or from configs."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen:
        return None
    version_dir = Path(executable or sys.executable).absolute().parent
    versions = version_dir.parent
    if versions.name.lower() != "versions" or not VERSION_RE.fullmatch(version_dir.name):
        return None
    root = versions.parent
    return root if (root / STUB).is_file() else None


def version_dir(root, version):
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ConfigError("not an X.Y.Z version")
    return Path(root) / "versions" / version


def _version_or_none(value):
    return value if isinstance(value, str) and VERSION_RE.fullmatch(value) else None


INSTALL_KEPT = ("install_stamp", "stub")  # written by a full install; an update keeps them


def _raw_install(root):
    try:
        data = json.loads(retry_sharing(lambda: (Path(root) / INSTALL).read_bytes()))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def install_identity(root):
    """``(current, install_stamp)``: what an app install token is created under (§16.15).
    Either changes on a full install, and ``current`` on every update."""
    data = _raw_install(root)
    stamp = data.get("install_stamp")
    return _version_or_none(data.get("current")), stamp if isinstance(stamp, str) and len(stamp) <= 200 else None


def read_install(root):
    """``install.json`` as ``{"current", "previous", "probation"}`` (each a version or
    None). Invalid content reads as all None. A sharing violation is retried."""
    try:
        data = json.loads(retry_sharing(lambda: (Path(root) / INSTALL).read_bytes()))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return {k: _version_or_none(data.get(k)) for k in ("current", "previous", "probation")}


_INSTALL_STAMP_RE = re.compile(r"^[A-Za-z0-9T:.+-]{1,64}$")


def read_install_meta(root):
    """What a full install records beside the versions (§16.15): ``stub`` (2 for the v0.5 stub; None when
    this install was only updated in place from v0.4) and ``install_stamp`` (new on every full install)."""
    try:
        data = json.loads(retry_sharing(lambda: (Path(root) / INSTALL).read_bytes()))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    stub, stamp = data.get("stub"), data.get("install_stamp")
    return {"stub": stub if isinstance(stub, int) and not isinstance(stub, bool) and 0 < stub < 1000 else None,
            "install_stamp": stamp if isinstance(stamp, str) and _INSTALL_STAMP_RE.match(stamp) else None}


def write_install(root, current, previous, probation=None):
    """One ``install.json``, written to a flushed temporary file and ``os.replace``d (15.8 M6). The full
    installer's ``stub`` and ``install_stamp`` are kept (§16.15)."""
    for value in (current, previous, probation):
        if value is not None and not VERSION_RE.fullmatch(value):
            raise ConfigError("not an X.Y.Z version")
    if current is None:
        raise ConfigError("install.json needs a current version")
    target = Path(root) / INSTALL
    new = target.with_name(INSTALL + ".new")
    data = {k: v for k, v in _raw_install(root).items() if k in INSTALL_KEPT}  # the installer's keys (§16.15)
    data.update(current=current, previous=previous, probation=probation)
    with open(new, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    retry_sharing(lambda: os.replace(new, target))


def current_version(root):
    return read_install(root)["current"]


# -- app settings and the Run value ------------------------------------------------------

def read_settings(root):
    try:
        data = json.loads((Path(root) / SETTINGS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in ("agent_config", "runtime_config") and isinstance(v, str)}


def write_settings(root, settings):
    atomic_write_json(Path(root) / SETTINGS, settings)


def paths(root=None):
    """The agent and runtime config the app runs: app.json's, else the defaults."""
    from ..config import default_config_path
    from ..login import runtime_config_path
    settings = read_settings(root) if root is not None else {}
    agent = settings.get("agent_config") or default_config_path()
    return agent, settings.get("runtime_config") or runtime_config_path(agent)


def run_value(root):
    return f'"{Path(root) / STUB}" --background'


class WindowsRegistry:
    """Values of the current user's Run key (a fake replaces it in tests)."""

    def __init__(self, key=RUN_KEY):
        self.key = key

    def get(self, name):
        if os.name != "nt":
            return None
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key) as key:
                return winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None

    def set(self, name, value):
        if os.name != "nt":
            raise ConfigError("the Run value exists only on Windows")
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.key) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, name):
        if os.name != "nt":
            return
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def enable_logon_start(root, registry=None):
    """Point the Run value at the stub, unless it holds something else (15.8 H7:
    an unrecorded value is never overwritten; migration records and replaces it)."""
    registry = WindowsRegistry() if registry is None else registry
    existing = registry.get(RUN_VALUE)
    if existing not in (None, run_value(root)):
        return "kept_existing"
    registry.set(RUN_VALUE, run_value(root))
    return "enabled"


def disable_logon_start(root, registry=None):
    registry = WindowsRegistry() if registry is None else registry
    if registry.get(RUN_VALUE) == run_value(root):
        registry.delete(RUN_VALUE)
        return "disabled"
    return "not_enabled"


# -- update mode and explicit rollback -----------------------------------------------------

def update_mode(root):
    saved = updates.read_mode_file(root)
    return saved["update_mode"] if saved else "automatic"


def app_floor(root):
    """v0.4.0, or v0.5.0 once the app's runtime has polled with routing=1 (§16.12 C1)."""
    from . import floors
    try:
        floor = floors.floor_for([paths(root)[1]])
    except Exception:  # noqa: BLE001 - an unreadable config keeps the base floor
        floor = None
    return max(MIN_VERSION, floor or MIN_VERSION)


def configure(root, mode=None, rollback=False):
    """``runtime update --automatic|--manual|--rollback`` for an app install. An
    explicit rollback makes the previous version current and sets ``manual``; a
    previous version below v0.4.0 is refused (15.8 H8)."""
    root = Path(root)
    lock = updates.lock_root(root)
    try:
        if rollback:
            state = read_install(root)
            previous = state["previous"]
            if previous is None or not (version_dir(root, previous) / APP_EXE).is_file():
                raise ConfigError("no previous app version is available")
            floor = app_floor(root)
            if version_key(previous) < floor:
                raise ConfigError("rollback below v%d.%d.%d is refused: that version cannot run this machine" % floor)
            write_install(root, previous, state["current"])
            mode = "manual"
        if mode is not None:
            updates.write_mode_file(root, {"update_mode": mode, "update_mode_chosen": True})
        return {"version": current_version(root), "update_mode": update_mode(root)}
    finally:
        lock.release_run_lock()


# -- release transport -------------------------------------------------------------------------

def check_hop(url):
    """Every request and redirect hop: https, port 443, an exact ``HOSTS`` entry."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise NetworkError("update request has an invalid port") from None
    if (parts.scheme != "https" or parts.hostname not in HOSTS or port not in (None, 443)
            or parts.username is not None or parts.password is not None):
        raise NetworkError("update request left GitHub's https release hosts")


def _connect(host):
    return http.client.HTTPSConnection(host, 443, timeout=60, context=ssl.create_default_context())


def fetch(url, limit, sink=None, accept="application/vnd.github+json"):
    """GET ``url``, following at most ``MAX_HOPS`` redirects, each checked before it
    is requested. No credentials or cookies are sent; ``Accept`` goes to the first
    hop only. A body over ``limit`` bytes is refused. With ``sink`` (a callable
    taking each chunk) the body is streamed to it, else returned."""
    first = True
    for _ in range(MAX_HOPS + 1):
        check_hop(url)
        parts = urlsplit(url)
        try:
            connection = _connect(parts.hostname)
            try:
                headers = {"User-Agent": "RainCLI-app-updater"}
                if first:
                    headers["Accept"] = accept
                connection.request("GET", (parts.path or "/") + ("?" + parts.query if parts.query else ""),
                                   headers=headers)
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise NetworkError("update redirect without a location")
                    url, first = urljoin(url, location), False
                    continue
                if response.status == 404:
                    raise updates.ReleaseNotFound("release or asset not found")
                if response.status != 200:
                    raise NetworkError(f"update request failed: HTTP {response.status}")
                declared = response.getheader("Content-Length")
                if declared is not None and declared.strip().isdigit() and int(declared) > limit:
                    raise NetworkError("update download exceeds its size limit")
                return _read(response, limit, sink)
            finally:
                connection.close()
        except (OSError, http.client.HTTPException) as exc:
            if isinstance(exc, ssl.SSLCertVerificationError):
                raise NetworkError("TLS certificate verification failed for a release host") from None
            raise NetworkError(f"update request failed: {type(exc).__name__}") from None
    raise NetworkError("too many update redirects")


def _read(response, limit, sink):
    total, chunks = 0, []
    while True:
        chunk = response.read(min(1024 * 1024, limit + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise NetworkError("update download exceeds its size limit")
        if sink is None:
            chunks.append(chunk)
        else:
            sink(chunk)
    return b"".join(chunks) if sink is None else total


def asset_names(version):
    installer = f"RainCLI-Setup-{version}.exe"
    return installer, installer + ".sha256"


def resolve(tag, get=fetch):
    """The canonical repository's stable (non-draft, non-prerelease) release with
    exactly this tag, and its two assets matched by exact name (15.8 M4)."""
    if not updates.TAG_RE.fullmatch(tag):
        raise VerificationError("target is not a vMAJOR.MINOR.PATCH tag")
    try:
        release = json.loads(get(API + "/releases/tags/" + tag, MAX_METADATA))
    except (ValueError, UnicodeDecodeError):
        raise VerificationError("the release metadata is not JSON") from None
    if (not isinstance(release, dict) or release.get("draft") is not False
            or release.get("prerelease") is not False or release.get("tag_name") != tag):
        raise VerificationError(f"the release is not a stable release tagged {tag}")
    installer, checksum = asset_names(tag[1:])
    assets = [a for a in release.get("assets") or [] if isinstance(a, dict)]
    found = {}
    for name, limit in ((installer, MAX_INSTALLER), (checksum, MAX_CHECKSUM)):
        matches = [a for a in assets if a.get("name") == name]
        if not matches:
            raise updates.ReleaseNotFound(f"the release has no {name} asset")
        asset = matches[0]
        if len(matches) != 1 or not isinstance(asset.get("url"), str) or not ASSET_URL_RE.fullmatch(asset["url"]):
            raise VerificationError(f"the release asset {name} has no canonical API URL")
        size = asset.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= limit:
            raise VerificationError(f"the release asset {name} is empty or over its size limit")
        found[name] = {"name": name, "url": asset["url"], "size": size}
    return {"tag": tag, "version": tag[1:], "installer": found[installer], "checksum": found[checksum]}


CHECKSUM_RE = re.compile(rb"([0-9a-f]{64})  ([^\r\n]{1,128})\r?\n?")


def parse_checksum(raw, installer_name):
    """One line: 64 lowercase hex, two spaces, the installer asset's exact name."""
    match = CHECKSUM_RE.fullmatch(raw)
    if not match or match.group(2) != installer_name.encode():
        raise VerificationError("the checksum asset is not '<64 hex>  <installer asset name>'")
    return match.group(1).decode()


def hidden():
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}


def verify_installed(root, version, run=subprocess.run):
    """``versions\\<X.Y.Z>\\raincli.exe --version`` must print ``raincli X.Y.Z``."""
    directory = version_dir(root, version)
    if not (directory / CLI_EXE).is_file() or not (directory / APP_EXE).is_file():
        raise VerificationError("the installer did not produce the version's executables")
    try:
        result = run([str(directory / CLI_EXE), "--version"], capture_output=True, text=True, timeout=30,
                     stdin=subprocess.DEVNULL, shell=False, **hidden())
    except (OSError, subprocess.SubprocessError) as exc:
        raise VerificationError(f"the installed version did not run: {type(exc).__name__}") from None
    if result.returncode != 0 or result.stdout.strip() != "raincli " + version:
        raise VerificationError("the installed version does not report the release's version")


def download_installer(root, release, expected, get=fetch):
    """Into a fresh owner-only directory holding only the installer, hashed as it
    is written (15.8 M5). Returns ``(directory, installer path)``."""
    directory = Path(root) / "state" / "downloads" / uuid.uuid4().hex
    ensure_private_dir(str(directory))
    try:
        return directory, _download(directory, release, expected, get)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _download(directory, release, expected, get):
    path = directory / release["installer"]["name"]
    digest = hashlib.sha256()
    from ..fsutil import create_private
    fd = create_private(str(path))
    try:
        def sink(chunk):
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(fd, view):]
        size = get(release["installer"]["url"], MAX_INSTALLER, sink, accept="application/octet-stream")
        os.fsync(fd)
    finally:
        os.close(fd)
    if size != release["installer"]["size"] or digest.hexdigest() != expected:
        raise VerificationError("the installer does not match its published SHA-256")
    return path


def install(root, tag, *, get=fetch, run=subprocess.run, resolved=None):
    """Install release ``tag`` into ``versions\\<X.Y.Z>`` and put it on probation.

    Returns ``{"status": "installed"|"current", ...}``. Raises ``NetworkError`` or
    ``ReleaseNotFound`` (retried with backoff) or ``VerificationError`` (blocked
    until the target changes)."""
    root = Path(root)
    lock = updates.lock_root(root)
    work = None
    try:
        release = resolved or resolve(tag, get)
        version = release["version"]
        state = read_install(root)
        if state["current"] == version:
            return {"status": "current", "tag": tag}
        raw = get(release["checksum"]["url"], MAX_CHECKSUM, accept="application/octet-stream")
        expected = parse_checksum(raw, release["installer"]["name"])
        work, installer = download_installer(root, release, expected, get)
        target = version_dir(root, version)
        try:
            result = run([str(installer.absolute()), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE",
                          f"/DIR={target}"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, timeout=INSTALL_TIMEOUT, shell=False, **hidden())
        except subprocess.TimeoutExpired:
            raise NetworkError("the installer did not finish in time") from None
        except OSError as exc:
            raise VerificationError(f"the installer did not start: {type(exc).__name__}") from None
        if result.returncode != 0:
            raise VerificationError(f"the installer failed with exit status {result.returncode}")
        verify_installed(root, version, run)
        write_install(root, version, state["current"], probation=version)
        return {"status": "installed", "tag": tag, "previous": state["current"]}
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
        lock.release_run_lock()


def prune(root, running=None):
    """Remove versions other than current, previous, probation and the running one."""
    root = Path(root)
    state = read_install(root)
    if state["current"] is None:
        return []  # an unreadable install.json names nothing to keep (review 1a F7)
    keep = {v for v in state.values() if v}
    running = Path(running or sys.executable).absolute().parent
    removed = []
    versions = root / "versions"
    for entry in sorted(versions.iterdir()) if versions.is_dir() else []:
        if (entry.is_symlink() or not entry.is_dir() or entry.name in keep
                or not VERSION_RE.fullmatch(entry.name)
                or os.path.normcase(str(entry.absolute())) == os.path.normcase(str(running))):
            continue
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            removed.append(entry.name)
    return removed


# -- readiness heartbeat -------------------------------------------------------------------------

def beat(root, version):
    """Written by the runtime after each completed supervision tick."""
    atomic_write_json(Path(root) / HEARTBEAT, {"version": version, "pid": os.getpid(), "at": time.time()})


def heartbeat_since(root, version, since):
    try:
        data = json.loads(retry_sharing(lambda: (Path(root) / HEARTBEAT).read_bytes()))
    except (OSError, ValueError):
        return False
    return (isinstance(data, dict) and data.get("version") == version
            and isinstance(data.get("at"), (int, float)) and data["at"] >= since)


# -- the tray's runtime host -------------------------------------------------------------------------

def self_command(*args):
    """This client's own command line: the frozen executable, or ``python -m raincli_agent``."""
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        cli = executable.with_name(CLI_EXE)
        return [str(cli if cli.is_file() else executable), *args]
    return [sys.executable, "-m", "raincli_agent", *args]


class AppHost:
    """The tray's runtime child: start, pause, resume and stop it gracefully,
    restart it after a crash, and notice when ``install.json`` names another
    current version, so the tray exits with ``SWITCH_EXIT`` for the stub. The
    stub, not this, decides a new version's probation."""

    def __init__(self, root, version, runtime_config, *, command=None, log_path=None, clock=time.monotonic,
                 request_stop=None):
        from .service import request_stop as stop_request
        self.root, self.version, self.config = (Path(root) if root else None), version, str(runtime_config)
        self.command = command or (lambda: self_command("runtime", "run", "--config", self.config))
        self.log_path = Path(log_path) if log_path else None
        self.clock = clock
        self.request_stop = request_stop or stop_request
        self.process = None
        self.job = None  # Windows: the runtime dies with the tray, never orphaned (review 1a F10)
        self.paused = False
        self.failures, self.started, self.next_start = 0, 0.0, 0.0

    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        if self.running():
            return
        output = subprocess.DEVNULL
        if self.log_path is not None:
            try:
                if self.log_path.stat().st_size > 1024 * 1024:
                    os.replace(self.log_path, str(self.log_path) + ".1")
            except OSError:
                pass
            ensure_private_dir(str(self.log_path.parent))
            output = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            self.process = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL, stdout=output,
                                            stderr=output, **hidden())
        finally:
            if output is not subprocess.DEVNULL:
                os.close(output)
        self.job = kill_on_close_job(self.process)
        self.started = self.clock()

    def stop(self, timeout=GRACEFUL_STOP):
        """The runtime's file-based stop request, repeated until it exits; its
        process tree is killed only as a last resort."""
        if not self.running():
            self.process = None
            self._close_job()
            return
        deadline = self.clock() + timeout
        while self.process.poll() is None and self.clock() < deadline:
            try:
                self.request_stop(self.config)
            except (ConfigError, OSError):
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if self.process.poll() is None:
            from .service import kill_tree
            kill_tree(self.process)
        self.process = None
        self._close_job()

    def _close_job(self):
        if self.job is not None:
            self.job.close()
            self.job = None

    def pause(self):
        self.paused = True
        self.stop()

    def resume(self):
        self.paused = False
        self.next_start = 0.0
        self.start()

    def switched(self):
        if self.root is None:
            return False
        current = current_version(self.root)
        return current is not None and current != self.version

    def step(self):
        """One supervision step: None to continue, or ``SWITCH_EXIT`` after the
        runtime was stopped because another version became current."""
        if self.switched():
            self.stop()
            return SWITCH_EXIT
        if self.paused:
            return None
        if self.process is not None:
            if self.process.poll() is None:
                return None
            self.process = None
            self._close_job()
            self.failures = 0 if self.clock() - self.started > 600 else self.failures + 1
            self.next_start = self.clock() + min(300, 2 ** min(self.failures, 8))
        if self.clock() >= self.next_start:
            self.start()
        return None


def kill_on_close_job(process):
    """Windows: put ``process`` (and the connectors it starts) in a Job object that is
    killed when its last handle closes, so a crashed tray never leaves its runtime
    running. The handle is kept by the returned object; elsewhere returns None."""
    if os.name != "nt":
        return None
    import ctypes as c
    from ctypes import wintypes as w

    class Basic(c.Structure):
        _fields_ = [("PerProcessUserTimeLimit", c.c_int64), ("PerJobUserTimeLimit", c.c_int64),
                    ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", c.c_size_t),
                    ("MaximumWorkingSetSize", c.c_size_t), ("ActiveProcessLimit", w.DWORD),
                    ("Affinity", c.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

    class Io(c.Structure):
        _fields_ = [(name, c.c_uint64) for name in ("Read", "Write", "Other", "ReadT", "WriteT", "OtherT")]

    class Extended(c.Structure):
        _fields_ = [("Basic", Basic), ("Io", Io), ("ProcessMemoryLimit", c.c_size_t),
                    ("JobMemoryLimit", c.c_size_t), ("PeakProcessMemoryUsed", c.c_size_t),
                    ("PeakJobMemoryUsed", c.c_size_t)]
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        return None
    info = Extended()
    info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not (kernel.SetInformationJobObject(job, 9, c.byref(info), c.sizeof(info))  # ExtendedLimitInformation
            and kernel.AssignProcessToJobObject(job, int(process._handle))):
        kernel.CloseHandle(job)
        return None

    class Job:
        handle = job

        def close(self):
            if self.handle:
                kernel.CloseHandle(self.handle)
                self.handle = None
    return Job()


# -- the stub ----------------------------------------------------------------------------------------

def rollback(root, failed):
    """The stub's rollback after ``failed`` missed its heartbeat: the previous
    version becomes current and ``rolled_back`` is recorded, blocking that target
    until it changes. The update mode is unchanged. True when rolled back."""
    root = Path(root)
    state = read_install(root)
    previous = state["previous"]
    if (state["current"] != failed or previous is None or previous == failed
            or version_key(previous) < app_floor(root) or not (version_dir(root, previous) / APP_EXE).is_file()):
        return False
    write_install(root, previous, failed, probation=None)
    update = updates.read_update_state(root)
    try:
        updates.write_update_state(root, {**update, "state": "rolled_back", "error": "first_start_failed",
                                          "blocked": update.get("target")})
    except OSError:
        pass
    return True


QUIT = "quit"  # <root>\\app-lock\\quit (15.9)


def quit_requested(root):
    return (Path(root) / "app-lock" / QUIT).exists()


SHOW = "show"  # <root>\\app-lock\\show: RainCLI.exe without arguments asks the app to show its window (§16.15)


def request_show(root):
    directory = Path(root) / "app-lock"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SHOW).write_bytes(b"")


def take_show_request(root):
    """True once per request: the running app shows and focuses its window."""
    try:
        (Path(root) / "app-lock" / SHOW).unlink()
    except OSError:
        return False
    return True


def app_log(root, text):
    """``<root>\\app-lock\\app.log``: the app's own short lines, kept under 64 KiB with one ``.1``."""
    path = Path(root) / "app-lock" / "app.log"
    try:
        path.parent.mkdir(exist_ok=True)
        if path.exists() and path.stat().st_size > 64 * 1024:
            os.replace(path, str(path) + ".1")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + text + "\n")
    except OSError:
        pass


def clear_quit(root):
    try:
        (Path(root) / "app-lock" / QUIT).unlink()
    except OSError:
        pass


class Stub:
    """``RainCLI.exe --background`` (15.8 H4, M6). It must stay correct from v0.4.0:
    an update never replaces it. It starts install.json's current version and,
    for a version on probation, waits up to ``PROBATION`` seconds for its
    heartbeat before rolling back to the previous one."""

    def __init__(self, root, *, popen=subprocess.Popen, sleep=time.sleep, clock=time.monotonic, wall=time.time,
                 stop_app=None):
        self.root = Path(root)
        self.popen, self.sleep, self.clock, self.wall = popen, sleep, clock, wall
        self.stop_app = stop_app or self._stop_app

    def launch(self, version):
        exe = version_dir(self.root, version) / APP_EXE
        if not exe.is_file():
            return None
        return self.popen([str(exe), "--background"], stdin=subprocess.DEVNULL, **hidden())

    def _stop_app(self, process):
        """Graceful first (the runtime's stop request), then the process tree."""
        from .service import kill_tree, request_stop
        _, runtime_config = paths(self.root)
        deadline = self.clock() + 30
        while process.poll() is None and self.clock() < deadline:
            try:
                request_stop(runtime_config)
            except (ConfigError, OSError):
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if process.poll() is None:
            kill_tree(process)

    def probation(self, version, process, since):
        """Wait for the heartbeat; True when ``version`` proved itself."""
        deadline = self.clock() + PROBATION
        while self.clock() < deadline:
            if heartbeat_since(self.root, version, since):
                state = read_install(self.root)
                if state["current"] == version and state["probation"] == version:
                    write_install(self.root, version, state["previous"], probation=None)
                return True
            if process.poll() is not None or quit_requested(self.root):
                # A quit, or another tray already running (retried), is not a failed start.
                return quit_requested(self.root) or self.already_running(process.poll())
            self.sleep(1)
        return heartbeat_since(self.root, version, since)

    def already_running(self, code):
        """Exit 76 counts as "another tray runs" only while tray.lock is actually held;
        if the stub can take the lock, it was a failed start (review 4 D1)."""
        if code != ALREADY_RUNNING_EXIT:
            return False
        lock = tray_lock(self.root)
        if lock is None:
            return True
        os.close(lock)
        self.log(f"tray exited {code} but tray.lock is free: a failed start")
        return False

    def log(self, text):
        """``<root>\\app-lock\\stub.log``: short lines, kept under 64 KiB with one ``.1``."""
        path = self.root / "app-lock" / "stub.log"
        try:
            path.parent.mkdir(exist_ok=True)
            if path.exists() and path.stat().st_size > 64 * 1024:
                os.replace(path, str(path) + ".1")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + text + "\n")
        except OSError:
            pass

    def wait_or_quit(self, process):
        """The tray's exit status, or None after a quit request: the tray sees the same
        request and stops its runtime first (``Tray.quit`` -> ``host.stop()``); a tray
        that has not exited within ``GRACEFUL_STOP`` seconds has its tree ended."""
        while True:
            code = process.poll()
            if code is not None:
                return None if quit_requested(self.root) else code
            if quit_requested(self.root):
                deadline = self.clock() + GRACEFUL_STOP - 10
                while process.poll() is None and self.clock() < deadline:
                    self.sleep(1)
                if process.poll() is None:
                    from .service import kill_tree
                    kill_tree(process)
                return None
            self.sleep(1)

    def run(self):
        failures = 0
        while True:
            if quit_requested(self.root):
                return 0
            state = read_install(self.root)
            version = state["current"]
            if version is None:
                return 1
            since = self.wall() - 1
            started = self.clock()
            process = self.launch(version)
            if process is not None and state["probation"] == version and not self.probation(version, process, since):
                if process.poll() is None:
                    self.stop_app(process)
                if rollback(self.root, version):
                    continue
                return 1
            if process is None:
                if rollback(self.root, version):
                    continue
                return 1
            code = self.wait_or_quit(process)
            if code is None:
                return 0  # quit requested: the tray (and so the runtime) is stopped
            if code == SWITCH_EXIT:
                failures = 0
                continue
            if self.already_running(code):
                # A tray that outlived its stub still runs: wait for it, a fixed 10 s at a
                # time, answering a quit request; never treat this as a quit.
                self.log(f"tray {version} already running; retrying in {ALREADY_RUNNING_WAIT} s")
                for _ in range(ALREADY_RUNNING_WAIT):
                    if quit_requested(self.root):
                        return 0
                    self.sleep(1)
                continue
            if code == 0:
                return 0
            failures = 0 if self.clock() - started > 600 else failures + 1
            if failures > 8:
                return code
            for _ in range(min(300, 2 ** failures)):
                if quit_requested(self.root):
                    return 0
                self.sleep(1)


def single_instance(root, name="stub.lock"):
    """The stub's lock (or another app lock): one per user. A descriptor, or None when held."""
    from .. import filelock
    directory = Path(root) / "app-lock"
    directory.mkdir(exist_ok=True)
    fd = os.open(directory / name, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        filelock.lock(fd, blocking=False)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def tray_lock(root):
    """Held by the running tray (review 2 R3), so ``--quit`` sees a tray whose stub died."""
    return single_instance(root, "tray.lock")


def process_table():
    """``{pid: (parent pid, executable path or None, start time or None)}`` for every
    process. Windows: one Toolhelp snapshot, ``QueryFullProcessImageNameW`` and
    ``GetProcessTimes``. Elsewhere: /proc. Fields that cannot be read are None."""
    from . import procinfo
    table = {}
    if os.name == "nt":
        c, w, k, _ = procinfo._windows()
        query = k.QueryFullProcessImageNameW
        query.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
        for pid, (_, parent) in procinfo.windows_snapshot().items():
            path = None
            handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                try:
                    buf, size = c.create_unicode_buffer(32768), w.DWORD(32768)
                    if query(handle, 0, buf, c.byref(size)):
                        path = buf.value
                finally:
                    k.CloseHandle(handle)
            table[pid] = (parent, path, procinfo.windows_start(pid))
        return table
    for entry in Path("/proc").iterdir() if Path("/proc").is_dir() else []:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            _, parent, _ = procinfo.linux_info(pid)
        except (OSError, ValueError, IndexError):
            continue
        try:
            path = os.readlink(entry / "exe")
        except OSError:
            path = None
        table[pid] = (parent, path, procinfo.linux_start(pid))
    return table


def ancestors(pid, table):
    """``pid``'s parent chain. It stops at an unknown parent, a cycle, or a parent that
    started after its child: that pid was reused by an unrelated process."""
    out, seen, child = set(), {pid}, pid
    while child in table:
        parent = table[child][0]
        if not parent or parent in seen or parent not in table:
            break
        child_start, parent_start = table[child][2], table[parent][2]
        if child_start is not None and parent_start is not None and parent_start > child_start:
            break
        out.add(parent)
        seen.add(parent)
        child = parent
    return out


UNINSTALLER_RE = re.compile(r"unins[0-9]*\.exe", re.IGNORECASE)


def processes_under(root, table=None, self_pid=None):
    """``[(pid, executable path)]`` of processes whose executable is under ``root``,
    leaving out this process and its ancestors (the installer or uninstaller that
    called ``--quit``) and ``<root>\\unins*.exe``, the uninstaller's first phase."""
    root = Path(root).absolute()
    prefix = os.path.normcase(str(root)) + os.sep
    table = process_table() if table is None else table
    self_pid = os.getpid() if self_pid is None else self_pid
    skip = {0, self_pid} | ancestors(self_pid, table)
    found = []
    for pid, (_, path, _) in sorted(table.items()):
        if pid in skip or not path or not os.path.normcase(path).startswith(prefix):
            continue
        name = os.path.basename(path)
        if UNINSTALLER_RE.fullmatch(name) and os.path.normcase(os.path.dirname(path)) == os.path.normcase(str(root)):
            continue
        found.append((pid, path))
    return found


def blockers(root, running_from=processes_under):
    """What keeps the app running: held app locks and executables running from the root."""
    out = []
    for name, what in (("stub.lock", "RainCLI.exe (the app's stub)"), ("tray.lock", "RainCLI-app.exe (the tray)")):
        lock = single_instance(root, name)
        if lock is None:
            out.append(what)
        else:
            os.close(lock)
    try:
        out.extend(f"{path} (pid {pid})" for pid, path in running_from(root))
    except OSError:
        pass
    return out


def app_running(root, running_from=processes_under):
    """Whether any part of the app runs: the stub's or the tray's lock is held, or a
    process runs from the install root."""
    return bool(blockers(root, running_from))


def stub_main(root, open_window=False):
    lock = single_instance(root)
    if lock is None:
        return 0  # already running (a show request reaches its app)
    try:
        clear_quit(root)  # a request left by a previous session
        if not open_window:
            take_show_request(root)  # likewise: --background never shows the window (§16.15)
        return Stub(root).run()
    finally:
        clear_quit(root)
        os.close(lock)


BLOCKERS = "quit-blockers.txt"


def stub_quit(root, timeout=GRACEFUL_STOP, sleep=time.sleep, clock=time.monotonic, running_from=processes_under,
              out=None):
    """``RainCLI.exe --quit`` (15.9): create ``<root>\\app-lock\\quit``, which the running
    stub answers by stopping the tray (runtime first) and exiting. Waits up to
    ``timeout`` seconds until the stub's and the tray's locks are free and no process
    runs from ``<root>`` (review 2 R3): 0 then (or when nothing was running), 1 when
    the app is still running. On 1 it prints what it waited on and writes it, one per
    line, to ``<root>\\app-lock\\quit-blockers.txt`` for the installer (review 3 N2).
    It never kills anything."""
    root = Path(root)
    out = out or (lambda text: print(text, file=sys.stderr, flush=True))
    (root / "app-lock").mkdir(parents=True, exist_ok=True)
    report = root / "app-lock" / BLOCKERS
    try:
        report.unlink()
    except OSError:
        pass
    if not app_running(root, running_from):
        clear_quit(root)
        return 0
    (root / "app-lock" / QUIT).write_bytes(b"")
    deadline = clock() + timeout
    while True:
        waiting = blockers(root, running_from)
        if not waiting:
            clear_quit(root)
            return 0
        if clock() >= deadline:
            out("RainCLI is still running; close these and try again:")
            for line in waiting:
                out("  " + line)
            try:
                report.write_text("".join(line + "\n" for line in waiting), encoding="utf-8")
            except OSError:
                pass
            return 1
        sleep(1)
