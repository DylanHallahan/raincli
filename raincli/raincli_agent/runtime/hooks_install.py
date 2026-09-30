"""``raincli hooks install --claude|--codex [--remove]`` (protocol 14.3, 14.7 M8).

Verified hook APIs (see the directory-client report for the evidence):

* Claude Code 2.1.x: ``~/.claude/settings.json`` ``hooks.<Event>[{matcher?, hooks:
  [{type: "command", command, timeout (s), statusMessage}]}]``; SessionStart and
  UserPromptSubmit accept ``hookSpecificOutput.additionalContext``; Notification
  carries ``notification_type``.
* Codex 0.159: ``~/.codex/hooks.json`` in the same shape (feature ``hooks`` is
  stable), events SessionStart, UserPromptSubmit, Stop, PermissionRequest and
  SessionEnd. Codex asks the user to review new hooks before they run.

Entries this command owns carry ``statusMessage: "raincli"``; nothing else is
touched. Edits are refused when the existing file does not parse, are written
atomically with the file's mode kept, and leave a 0600 backup.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import time

from ..errors import ConfigError
from ..fsutil import atomic_write_bytes

MARKER = "raincli"
TIMEOUT = 5  # seconds; the hook itself exits within about 2 s
EVENTS = {
    "claude": ("SessionStart", "UserPromptSubmit", "Stop", "Notification", "SessionEnd"),
    "codex": ("SessionStart", "UserPromptSubmit", "Stop", "PermissionRequest", "SessionEnd"),
}


def config_path(kind, home=None):
    home = Path(home) if home else Path.home()
    return home / ".claude" / "settings.json" if kind == "claude" else home / ".codex" / "hooks.json"


def launcher_prefix():
    """The stable command for the hook (14.7 M8): the managed launcher, or the
    ``raincli`` entry point; never a versioned environment's interpreter."""
    from .updates import default_root
    managed = default_root() / "launch.py"
    if managed.is_file():
        base = Path(getattr(sys, "_base_executable", sys.executable))
        return [str(base), str(managed)], "managed launcher"
    entry = shutil.which("raincli")
    if entry:
        return [str(Path(entry).absolute())], "raincli entry point"
    # Never a (possibly versioned) interpreter path: it would break at the next update.
    raise ConfigError("no stable raincli command: install the managed client (SETUP.md) or put the "
                      "raincli entry point on PATH, then run hooks install again")


def handler(kind, event, prefix, state_dir):
    argv = [*prefix, "hook", kind, event, "--state-dir", str(state_dir)]
    entry = {"type": "command", "timeout": TIMEOUT, "statusMessage": MARKER}
    if os.name == "nt":
        if kind != "claude":
            raise ConfigError("Codex hooks are installed on Linux and macOS only; Windows Codex sessions stay scan-only")
        # Claude Code's exec form: no shell parses the paths.
        entry.update(command=argv[0], args=argv[1:])
    else:
        # Output to stderr is discarded and the status is always 0: a missing
        # interpreter or any failure can never block the agent (exit 2 would).
        entry["command"] = " ".join(shlex.quote(a) for a in argv) + " 2>/dev/null || true"
    return entry


def owned(entry):
    return isinstance(entry, dict) and entry.get("statusMessage") == MARKER


def strip_owned(hooks):
    """Remove only our entries; drop groups and events we emptied."""
    out = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            out[event] = groups
            continue
        kept = []
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list) and any(owned(h) for h in group["hooks"]):
                rest = [h for h in group["hooks"] if not owned(h)]
                if not rest:
                    continue
                group = {**group, "hooks": rest}
            kept.append(group)
        if kept or not groups:
            out[event] = kept
    return out


def load(path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ConfigError(f"{path} does not parse as JSON; fix it first (nothing was changed)") from None
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ConfigError(f"{path} has an unexpected shape; nothing was changed")
    return raw, data


def codex_support(run=subprocess.run):
    """Feature probe (14.7 M8): ``codex features list`` must show ``hooks`` enabled."""
    binary = shutil.which("codex")
    if not binary:
        return False, "codex not found on PATH"
    try:
        version = run([binary, "--version"], capture_output=True, text=True, timeout=120).stdout.strip()
        features = run([binary, "features", "list"], capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"codex probe failed: {type(exc).__name__}"
    for line in features.splitlines():
        parts = line.split()
        if parts and parts[0] == "hooks":
            enabled = parts[-1] == "true"
            return enabled, f"{version or 'codex'}: feature hooks {' '.join(parts[1:])}"
    return False, f"{version or 'codex'}: no hooks feature"


def install(kind, state_dir, remove=False, home=None, prefix=None, probe=codex_support):
    if kind not in EVENTS:
        raise ConfigError("hooks install supports --claude or --codex")
    state_dir = os.path.abspath(state_dir)
    link = config_path(kind, home)
    path = Path(os.path.realpath(link)) if link.is_symlink() else link  # edit a dotfile-managed target in place
    result = {"agent": kind, "config": str(path)}
    if kind == "codex" and not remove:
        supported, evidence = probe()
        result["codex_hooks"] = evidence
        if not supported:
            result.update(status="unsupported", note="Codex sessions stay scan-only (listed, status unknown)")
            return result
    raw, data = load(path)
    hooks = strip_owned(data.get("hooks", {}))
    if not remove:
        prefix, how = prefix or launcher_prefix()
        result["command"] = how
        for event in EVENTS[kind]:
            hooks.setdefault(event, [])
            if not isinstance(hooks[event], list):
                raise ConfigError(f"{path}: hooks.{event} is not a list; nothing was changed")
            hooks[event].append({"hooks": [handler(kind, event, prefix, state_dir)]})
    new = {**data, "hooks": hooks}
    if not hooks and "hooks" not in data:
        new.pop("hooks")
    if new == data:
        result["status"] = "unchanged"
    else:
        backup = write(path, raw, new, backup_dir=link.parent)
        result["status"] = "removed" if remove else "installed"
        if backup is not None:
            result["backup"] = str(backup)
    if kind == "codex" and not remove:
        result["note"] = "Codex asks you to review new hooks (/hooks) before they run"
    return result


KEEP_BACKUPS = 3


def write(path, raw, data, backup_dir=None):
    """Atomic write keeping the file's mode; returns the 0600 backup, if any.

    The backup goes beside the agent's own config path (not into a symlink's
    target, which may be a dotfiles repository), and only the newest few are kept."""
    text = (json.dumps(data, indent=2) + "\n").encode("utf-8")
    mode, backup = 0o600, None
    if raw is not None:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        directory = Path(backup_dir or path.parent)
        prefix = f"{path.name}.raincli-backup-"
        backup = directory / f"{prefix}{stamp}"
        n = 1
        while backup.exists():
            backup = directory / f"{prefix}{stamp}-{n}"
            n += 1
        atomic_write_bytes(str(backup), raw, 0o600)
        old = sorted((p for p in directory.glob(prefix + "*") if p.is_file()), key=lambda p: p.stat().st_mtime)
        for stale in old[:-KEEP_BACKUPS]:
            try:
                stale.unlink()
            except OSError:
                pass
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(str(path), text, mode)
    os.chmod(path, mode)
    return backup


def state_dir_from_runtime(config):
    from .service import _read_runtime
    return _read_runtime(config)[-1]

