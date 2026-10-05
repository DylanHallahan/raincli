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
import re
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


# Codex hooks (Phase 2): the per-handler `additionalContextLimit` (camelCase in hooks.json;
# codex-rs/config/src/hook_config.rs, HookHandlerConfig::Command, from 0.145.0) is an
# approximate token threshold, counted as UTF-8 bytes / 4 (codex-rs/utils/string,
# approx_token_count; hooks/src/output_spill.rs spills above it, 2,500 when unset). Our
# Codex claim cap is CLAIM_CAP_BYTES (32 KiB = 8,192 tokens), so 9,000 keeps every claim inline.
CODEX_CONTEXT_LIMIT = 9000
# Codex accepts additionalContextLimit only where a hook can emit additionalContext, and
# clamps SessionEnd timeouts to 3 s (hooks/src/engine/discovery.rs, normalize_command_hook,
# SESSION_END_MAX_TIMEOUT_SEC); real codex 0.160.0 warns about both otherwise.
CODEX_CONTEXT_EVENTS = ("SessionStart", "UserPromptSubmit")
CODEX_SESSION_END_TIMEOUT = 3
# Windows: quoted hook paths (#33926) and additionalContextLimit (#34393) arrived in 0.145.0.
CODEX_MIN_WINDOWS = (0, 145, 0)
# cmd.exe interprets these inside `cmd /C "…"` (Codex runs Windows hooks that way:
# codex-rs/hooks/src/engine/command_runner.rs, build_command), so no path may contain them.
CMD_SPECIAL = set('%^&|<>"!')  # "!": cmd delayed expansion (§16.14 K3)
TRUST_NOTE = ("Codex runs these hooks only after you trust them once in Codex: open /hooks and trust the "
              "raincli hooks. Trust again after any reinstall that changes the command (a new state "
              "directory or install path).")


def codex_home(home=None):
    """$CODEX_HOME, else ~/.codex (with ``home``, always <home>/.codex)."""
    if home is None and os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"])
    return (Path(home) if home else Path.home()) / ".codex"


def config_path(kind, home=None):
    if kind == "claude":
        return (Path(home) if home else Path.home()) / ".claude" / "settings.json"
    return codex_home(home) / "hooks.json"


def codex_version(evidence):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", evidence or "")
    return tuple(int(part) for part in match.groups()) if match else None


def launcher_prefix():
    """The stable command for the hook (14.7 M8): the managed launcher, or the
    ``raincli`` entry point; never a versioned environment's interpreter."""
    from .updates import default_root
    from .winapp import SHIM, app_root
    root = app_root()
    if root is not None and (root / SHIM).is_file():
        return [str(root / SHIM)], "app PATH shim"
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


def windows_command_line(argv):
    """The command line cmd.exe receives as ``cmd /C "<line>"``: every argument quoted.
    Paths with cmd-special characters are refused (they would be expanded or split)."""
    for arg in argv:
        bad = sorted(set(arg) & CMD_SPECIAL) or [ch for ch in arg if ord(ch) < 32]
        if not bad and arg.endswith("\\"):
            bad = ["a trailing backslash"]  # it would escape the closing quote
        if bad:
            raise ConfigError(f"cannot install Codex hooks: the path {arg!r} contains characters cmd.exe "
                              f"interprets ({' '.join(bad)}); use a state directory and install path without them")
    return " ".join(f'"{arg}"' if (" " in arg or not arg or "\\" in arg or "/" in arg) else arg for arg in argv)


def handler(kind, event, prefix, state_dir, windows=None):
    windows = os.name == "nt" if windows is None else windows
    argv = [*prefix, "hook", kind, event, "--state-dir", str(state_dir)]
    entry = {"type": "command", "timeout": TIMEOUT, "statusMessage": MARKER}
    if kind == "codex":
        if event in CODEX_CONTEXT_EVENTS:
            entry["additionalContextLimit"] = CODEX_CONTEXT_LIMIT
        if event == "SessionEnd":
            entry["timeout"] = CODEX_SESSION_END_TIMEOUT
    if windows:
        if kind == "claude":
            # Claude Code's exec form: no shell parses the paths.
            entry.update(command=argv[0], args=argv[1:])
        else:
            # Codex runs %COMSPEC% /C "<command>"; commandWindows wins on Windows.
            line = windows_command_line(argv)
            entry.update(command=line, commandWindows=line)
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


def find_codex(env=None, windows=None):
    """The first ``codex`` in the absolute PATH entries, as an absolute path, or None
    (§16.14 K1). The current directory is never searched. On Windows ``codex.exe``,
    then the npm shim ``codex.cmd`` (its arguments are constant), per entry."""
    env = os.environ if env is None else env
    windows = os.name == "nt" if windows is None else windows
    names = ("codex.exe", "codex.cmd") if windows else ("codex",)
    for entry in (env.get("PATH") or "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or entry == "." or not os.path.isabs(entry):
            continue
        for name in names:
            candidate = os.path.join(entry, name)
            if os.path.isfile(candidate) and (windows or os.access(candidate, os.X_OK)):
                return candidate
    return None


def codex_support(run=subprocess.run, find=find_codex):
    """Feature probe (14.7 M8): ``codex features list`` must show ``hooks`` enabled."""
    binary = find()
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


def install(kind, state_dir, remove=False, home=None, prefix=None, probe=codex_support, windows=None):
    windows = os.name == "nt" if windows is None else windows
    if kind not in EVENTS:
        raise ConfigError("hooks install supports --claude or --codex")
    state_dir = state_dir if windows and os.name != "nt" else os.path.abspath(state_dir)
    link = config_path(kind, home)
    path = Path(os.path.realpath(link)) if link.is_symlink() else link  # edit a dotfile-managed target in place
    result = {"agent": kind, "config": str(path)}
    if kind == "codex" and not remove:
        supported, evidence = probe()
        result["codex_hooks"] = evidence
        if not supported:
            result.update(status="unsupported", note="Codex sessions stay scan-only (listed, status unknown)")
            return result
        version = codex_version(evidence)
        if windows and (version is None or version < CODEX_MIN_WINDOWS):
            raise ConfigError(f"Codex hooks on Windows need Codex 0.145.0 or later ({evidence}); update Codex "
                              "(npm install -g @openai/codex) and run raincli hooks install --codex again. Until "
                              "then Codex sessions are listed by the process scan only")
        if version is not None and version < CODEX_MIN_WINDOWS:
            result["warning"] = ("this Codex ignores additionalContextLimit (0.145.0 or later): a next-turn "
                                 "message over about 10,000 characters reaches the session only as a preview")
    raw, data = load(path)
    hooks = strip_owned(data.get("hooks", {}))
    if not remove:
        prefix, how = prefix or launcher_prefix()
        result["command"] = how
        for event in EVENTS[kind]:
            hooks.setdefault(event, [])
            if not isinstance(hooks[event], list):
                raise ConfigError(f"{path}: hooks.{event} is not a list; nothing was changed")
            hooks[event].append({"hooks": [handler(kind, event, prefix, state_dir, windows)]})
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
        result["note"] = TRUST_NOTE
    return result


KEEP_BACKUPS = 3


def own_backups(directory, name):
    """[(stamp, counter, path)] for backups this command wrote, and nothing else."""
    pattern = re.compile(re.escape(name) + r"\.raincli-backup-([0-9]{8}-[0-9]{6})-([0-9]+)")
    out = []
    for path in Path(directory).glob(name + ".raincli-backup-*"):
        match = pattern.fullmatch(path.name)
        if match and path.is_file():
            out.append((match.group(1), int(match.group(2)), path))
    return out


def next_backup(directory, name):
    """A new backup name whose counter only goes up, so names sort in write order."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    counter = 1 + max((c for _, c, _ in own_backups(directory, name)), default=0)
    return Path(directory) / f"{name}.raincli-backup-{stamp}-{counter}"


def write(path, raw, data, backup_dir=None):
    """Atomic write keeping the file's mode; returns the 0600 backup, if any.

    The backup goes beside the agent's own config path (not into a symlink's
    target, which may be a dotfiles repository), and only the newest few are kept."""
    text = (json.dumps(data, indent=2) + "\n").encode("utf-8")
    mode, backup = 0o600, None
    if raw is not None:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        directory = Path(backup_dir or path.parent)
        backup = next_backup(directory, path.name)
        atomic_write_bytes(str(backup), raw, 0o600)
        # Prune only our own backups, oldest first by the counter alone, which only
        # grows: a clock step back can never make the new backup look oldest, and
        # it is never a candidate anyway (review 3, N2).
        older = sorted((b for b in own_backups(directory, path.name) if b[2] != backup), key=lambda b: b[1])
        for stale in older[:max(0, len(older) - (KEEP_BACKUPS - 1))]:
            try:
                stale[2].unlink()
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

