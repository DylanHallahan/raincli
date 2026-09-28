"""Opt-in releases from the canonical repository, staged in separate environments."""
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
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


class ReleaseNotFound(ConfigError):
    pass


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
        raise ConfigError(f"update request failed: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise ConfigError(f"update request failed: {getattr(exc, 'reason', exc)}") from None
    if len(raw) > limit:
        raise ConfigError("update response exceeds the download limit")
    return raw


def version_key(tag):
    return tuple(int(part) for part in tag[1:].split("."))


def latest():
    try:
        release = json.loads(fetch(API + "/releases/latest", 1024 * 1024))
    except ReleaseNotFound:
        return None
    tag = release.get("tag_name", "")
    if release.get("draft") or release.get("prerelease") or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ConfigError("latest release is not a stable vMAJOR.MINOR.PATCH release")
    # Resolve the release tag to an immutable commit before fetching its archive.
    commit = json.loads(fetch(API + "/commits/" + tag, 1024 * 1024)).get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ConfigError("GitHub returned an invalid release commit")
    return {"tag": tag, "commit": commit}


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
    # On Windows a replace fails while the launcher briefly has the pointer open.
    for attempt in range(50):
        try:
            return atomic_write_json(root / "current.json", pointer)
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            time.sleep(0.1)


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
            raise ConfigError("release archive does not match the resolved commit")
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
            raise ConfigError("installed client version does not match the release tag")
        subprocess.run([str(python), "-m", "raincli_agent", "runtime", "--help"],
                       check=True, capture_output=True, timeout=15)
        pointer = {**release, "python": str(python), "automatic": old.get("automatic", False),
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


def configure(root=None, automatic=None, rollback=False):
    root = Path(root or default_root()).expanduser().resolve()
    lock = Queue(str(root / "update-lock"))
    lock.acquire_run_lock()
    try:
        current = read_pointer(root)
        if not current:
            raise ConfigError("install a managed release first")
        if rollback:
            previous = current.get("previous")
            if not previous or not Path(previous.get("python", "")).is_file():
                raise ConfigError("no previous managed release is available")
            current = {**previous, "automatic": False, "previous": {k: current[k] for k in ("tag", "commit", "python")}}
        if automatic is not None:
            current["automatic"] = automatic
        if rollback:
            sync_launcher(root, current["python"])  # before the pointer, as in install
        write_pointer(root, current)
        return {"tag": current["tag"], "automatic": current["automatic"]}
    finally:
        lock.release_run_lock()
