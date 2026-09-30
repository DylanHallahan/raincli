"""Opt-in releases from the canonical repository, staged in separate environments."""
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid
import zipfile

from ..errors import ConfigError
from ..fsutil import atomic_write_bytes, atomic_write_json, ensure_private_dir, read_private_file
from ..connector.queue import Queue

REPO = "DylanHallahan/raincli"
API = "https://api.github.com/repos/" + REPO
MAX_ARCHIVE = 32 * 1024 * 1024


def default_root():
    return Path.home() / ".raincli/client"


HOSTS = {"api.github.com", "codeload.github.com"}


class NetworkError(ConfigError):
    """A download or network failure: retried with backoff (14.7 M9)."""


class ReleaseNotFound(NetworkError):
    pass


class VerificationError(ConfigError):
    """The release or staged install failed verification: never retried for the same target."""


def check_url(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in HOSTS or parts.port not in (None, 443):
        raise ConfigError("update request left GitHub's https release hosts")


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect before following it unless it stays on the allowlist."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_Redirects)  # default verified TLS context


def fetch(url, limit):
    """GET over https from GitHub's release hosts only. No credentials are sent.

    Integrity rests on TLS to GitHub plus the tag -> commit resolution and the
    archive's commit-named root; release signatures are not verified."""
    check_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "RainCLI-updater", "Accept": "application/vnd.github+json"})
    try:
        with _OPENER.open(request, timeout=30) as response:
            check_url(response.url)
            raw = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ReleaseNotFound("release not found") from None
        raise NetworkError(f"update request failed: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise NetworkError(f"update request failed: {getattr(exc, 'reason', exc)}") from None
    if len(raw) > limit:
        raise NetworkError("update response exceeds the download limit")
    return raw


TAG_RE = re.compile(r"v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")
# The first target-aware client (14.7 H2): older targets are refused, because a
# pre-0.3 client never reports or follows targets again.
MIN_TARGET = (0, 3, 0)


def version_key(tag):
    """Numeric tuple, after stripping a leading "v" (14.7 L11)."""
    return tuple(int(part) for part in tag.removeprefix("v").split("."))


def _stable(release, tag=None):
    name = release.get("tag_name", "") if isinstance(release, dict) else ""
    if (not isinstance(release, dict) or release.get("draft") or release.get("prerelease")
            or not TAG_RE.fullmatch(name) or (tag is not None and name != tag)):
        raise VerificationError("the release is not a stable vMAJOR.MINOR.PATCH release" +
                                (f" tagged {tag}" if tag else ""))
    # Resolve the release tag to an immutable commit before fetching its archive.
    commit = json.loads(fetch(API + "/commits/" + name, 1024 * 1024)).get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise VerificationError("GitHub returned an invalid release commit")
    return {"tag": name, "commit": commit}


def latest():
    try:
        release = json.loads(fetch(API + "/releases/latest", 1024 * 1024))
    except ReleaseNotFound:
        return None
    return _stable(release)


def resolve(tag):
    """The canonical repository's non-draft, non-prerelease release with exactly this tag."""
    if not TAG_RE.fullmatch(tag):
        raise VerificationError("target is not a vMAJOR.MINOR.PATCH tag")
    return _stable(json.loads(fetch(API + "/releases/tags/" + tag, 1024 * 1024)), tag)


def unpack(raw, directory):
    """Extract regular files only, bounded and without path traversal/links."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if len(members) > 5000 or sum(m.file_size for m in members) > 100 * 1024 * 1024:
            raise ConfigError("release archive is too large")
        roots = set()
        seen = set()
        for member in members:
            path = PurePosixPath(member.filename)
            if (path.is_absolute() or ".." in path.parts or not path.parts or "\\" in member.filename
                    or ":" in member.filename or any(part.rstrip(" .") != part for part in path.parts)):
                raise ConfigError("unsafe release archive path")
            key = member.filename.casefold().rstrip("/")
            if key in seen:
                raise ConfigError("duplicate release archive path")
            seen.add(key)
            mode = (member.external_attr >> 16) & 0o170000
            if mode not in (0, 0o040000, 0o100000):
                raise ConfigError("release archive contains links or special files")
            roots.add(path.parts[0])
        if len(roots) != 1:
            raise ConfigError("release archive must have one root")
        archive.extractall(directory)
        return directory / roots.pop()


def read_pointer(root):
    try:
        return json.loads(read_private_file(root / "current.json", "managed client pointer"))
    except ConfigError:
        if not (root / "current.json").exists():
            return {}
        raise


def write_pointer(root, pointer):
    # atomic_write_json retries a Windows sharing violation while the launcher
    # briefly has the pointer open.
    atomic_write_json(root / "current.json", pointer)


def sync_launcher(root, python):
    """Install the launcher shipped with the selected version, if it differs.

    A running launcher re-executes itself when it next switches versions."""
    source = Path(python).parents[2] / "launch.py"
    if not source.is_file():
        return  # versions staged before per-version launchers keep the current one
    data = source.read_bytes()
    compile(data, str(source), "exec")
    try:
        if (root / "launch.py").read_bytes() == data:
            return
    except FileNotFoundError:
        pass
    atomic_write_bytes(root / "launch.py", data)


def install(root=None, release=None):
    root = Path(root or default_root()).expanduser().resolve()
    ensure_private_dir(root)
    lock = Queue(str(root / "update-lock"))
    lock.acquire_run_lock()
    stage = None
    try:
        explicit = release is not None
        release = release or latest()
        if release is None:
            return {"status": "no_release"}
        old = read_pointer(root)
        if old.get("commit") == release["commit"]:
            sync_launcher(root, old["python"])
            return {"status": "current", **release}
        if not explicit and old.get("tag") and version_key(release["tag"]) <= version_key(old["tag"]):
            # Never downgrade, or follow a moved tag, automatically; rollback is explicit.
            return {"status": "not_newer", **release, "installed": old["tag"]}
        raw = fetch(f"https://codeload.github.com/{REPO}/zip/{release['commit']}", MAX_ARCHIVE)
        stage = root / "versions" / (release["commit"][:12] + "-" + uuid.uuid4().hex[:8])
        ensure_private_dir(stage)
        source = unpack(raw, stage)
        # GitHub names an archive's root after the repository and the exact commit requested.
        if source.name != REPO.split("/")[1] + "-" + release["commit"]:
            raise VerificationError("release archive does not match the resolved commit")
        # The client is pure stdlib Python: copy it from the verified archive into
        # a fresh environment. No build backend, index or network is involved.
        env = stage / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(env)], check=True, timeout=90)
        python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        purelib = subprocess.run([str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                 check=True, capture_output=True, text=True, timeout=15).stdout.strip()
        shutil.copytree(source / "raincli/raincli_agent", Path(purelib) / "raincli_agent",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copyfile(source / "raincli/raincli_agent/runtime/launcher.py", stage / "launch.py")
        check = subprocess.run([str(python), "-m", "raincli_agent", "--version"],
                               check=True, capture_output=True, text=True, timeout=15)
        if check.stdout.strip() != "raincli " + release["tag"][1:]:
            raise VerificationError("installed client version does not match the release tag")
        subprocess.run([str(python), "-m", "raincli_agent", "runtime", "--help"],
                       check=True, capture_output=True, timeout=15)
        # A fresh managed install starts automatic (14.5) without a migration notice.
        keys = mode_keys(old) if old else {"automatic": False, "update_mode": "automatic", "update_mode_chosen": True}
        pointer = {**release, "python": str(python), **keys,
                   "previous": {k: old[k] for k in ("tag", "commit", "python") if k in old}}
        # Launcher first: a running launcher checks for its own update right after
        # it sees the new pointer and stops the old runtime.
        sync_launcher(root, python)
        write_pointer(root, pointer)
        stage = None  # successful versions remain available for rollback
        return {"status": "installed", **release, "launcher": str(root / "launch.py")}
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        lock.release_run_lock()


def mode_keys(pointer):
    """The update-mode keys to keep (14.5, 14.7 H2). The legacy ``automatic`` key is
    always false, so a pre-0.3 launcher never pulls a release on its own."""
    keys = {"automatic": False, "update_mode": pointer.get("update_mode", "automatic"),
            "update_mode_chosen": bool(pointer.get("update_mode_chosen", False))}
    if keys["update_mode"] not in ("automatic", "manual"):
        keys["update_mode"] = "automatic"
    return keys


def configure(root=None, mode=None, rollback=False):
    """Set the update mode (``automatic``/``manual``, persisted and chosen) or roll back.

    An explicit rollback sets ``manual``, so a pushed target does not immediately
    reinstall the version the operator just left."""
    root = Path(root or default_root()).expanduser().resolve()
    lock = Queue(str(root / "update-lock"))
    lock.acquire_run_lock()
    try:
        current = read_pointer(root)
        if not current:
            raise ConfigError("install a managed release first")
        keys = mode_keys(current)
        if rollback:
            previous = current.get("previous")
            if not previous or not Path(previous.get("python", "")).is_file():
                raise ConfigError("no previous managed release is available")
            current = {**previous, "previous": {k: current[k] for k in ("tag", "commit", "python")}}
            mode = "manual"
        if mode is not None:
            keys.update(update_mode=mode, update_mode_chosen=True)
        current.update(keys)
        if rollback:
            sync_launcher(root, current["python"])  # before the pointer, as in install
        write_pointer(root, current)
        return {"tag": current["tag"], "update_mode": current["update_mode"]}
    finally:
        lock.release_run_lock()


MODE_NOTICE = ("raincli: automatic updates are now on for this managed install. The runtime installs the "
               "client version your team's operator sets, from the canonical GitHub releases only. "
               "Opt out with: raincli runtime update --manual")


def migrate_mode(root=None):
    """First run of a target-aware client: a v0.2 pointer's ``automatic: false`` was
    never a choice, so turn automatic on once. Returns the notice, or None."""
    root = Path(root or default_root()).expanduser().resolve()
    if not (root / "current.json").exists():
        return None
    lock = Queue(str(root / "update-lock"))
    lock.acquire_run_lock()
    try:
        current = read_pointer(root)
        if not current or current.get("update_mode_chosen"):
            return None
        # v0.2 wrote automatic: true only when the operator opted in; then it
        # stays automatic without a notice.
        notice = None if current.get("automatic") is True else MODE_NOTICE
        current.update(automatic=False, update_mode="automatic", update_mode_chosen=True)
        write_pointer(root, current)
        return notice
    finally:
        lock.release_run_lock()


def update_mode(root=None):
    try:
        return mode_keys(read_pointer(Path(root or default_root()).expanduser().resolve()))["update_mode"]
    except ConfigError:
        return "automatic"


# -- pushed-update state (read by the runtime and the launcher) -----------------

STATE_FILE = "update-state.json"


def read_update_state(root):
    try:
        data = json.loads(read_private_file(Path(root) / STATE_FILE, "update state"))
    except (ConfigError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_update_state(root, data):
    atomic_write_json(Path(root) / STATE_FILE, data)
