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


def fetch(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "RainCLI-updater", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        # No credentials are sent. Reject unexpected final origins.
        from urllib.parse import urlsplit
        if urlsplit(response.url).hostname not in {"api.github.com", "codeload.github.com"}:
            raise ConfigError("update download redirected outside GitHub's release hosts")
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ConfigError("update response exceeds the download limit")
    return raw


def latest():
    try:
        release = json.loads(fetch(API + "/releases/latest", 1024 * 1024))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
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


def install(root=None, release=None):
    root = Path(root or default_root()).expanduser().resolve()
    ensure_private_dir(root)
    lock = Queue(str(root / "update-lock"))
    lock.acquire_run_lock()
    stage = None
    try:
        release = release or latest()
        if release is None:
            return {"status": "no_release"}
        old = read_pointer(root)
        if old.get("commit") == release["commit"]:
            return {"status": "current", **release}
        raw = fetch(f"https://codeload.github.com/{REPO}/zip/{release['commit']}", MAX_ARCHIVE)
        stage = root / "versions" / (release["commit"][:12] + "-" + uuid.uuid4().hex[:8])
        ensure_private_dir(stage)
        source = unpack(raw, stage)
        # Build at its final path: console entry-point paths remain correct.
        env = stage / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(env)], check=True, timeout=90)
        python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([str(python), "-m", "pip", "install", "--no-deps", str(source / "raincli")],
                       check=True, timeout=240)
        check = subprocess.run([str(python), "-m", "raincli_agent", "--version"],
                               check=True, capture_output=True, text=True, timeout=15)
        if check.stdout.strip() != "raincli " + release["tag"][1:]:
            raise ConfigError("installed client version does not match the release tag")
        subprocess.run([str(python), "-m", "raincli_agent", "runtime", "--help"],
                       check=True, capture_output=True, timeout=15)
        bootstrap = Path(__file__).with_name("launcher.py").read_bytes()
        # The launcher is operator-installed infrastructure; automatic updates
        # switch its target but do not rewrite the running launcher.
        if not (root / "launch.py").exists():
            atomic_write_bytes(root / "launch.py", bootstrap)
        pointer = {**release, "python": str(python), "automatic": old.get("automatic", False),
                   "previous": {k: old[k] for k in ("tag", "commit", "python") if k in old}}
        atomic_write_json(root / "current.json", pointer)
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
        atomic_write_json(root / "current.json", current)
        return {"tag": current["tag"], "automatic": current["automatic"]}
    finally:
        lock.release_run_lock()
