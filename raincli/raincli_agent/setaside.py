"""Stale or foreign machine credentials (protocol §16.17, amended by §16.18).

- ``check_credential(agent_config, email=None)``: ``ok``, ``invalid``, ``not_owner``,
  ``unknown`` or ``unreadable``. It never raises for those cases and never logs the token.
- ``set_aside(agent_config, ...)``: moves the old setup into ``<config dir>/replaced-<UTC
  stamp>/`` so a fresh sign-in starts clean. It is the same for migration, the window and
  the CLI, and holds the migration lock (``.migration.lock``).

  **What moves** (``os.replace``):
  - ``agent.json``, ``person.json``, ``app-install.json`` and ``notifications/``;
  - every connector config naming this ``agent.json``, found as ``migrate.detect`` finds them
    (the scan directories, connector-mode runtime configs, the old Run value's ``--config``,
    ``app.json``'s runtime config, and explicit ``--connector-config`` paths);
  - a runtime config naming only this credential or its connectors, **with its state_dir**:
    the machine queue, ``sessions/`` (by-name handover boxes included), ``machine-salt``,
    ``routing-capable.json`` and ``status.json`` (V1).

  **Rewritten, not moved:** a runtime config that also names other credentials' connectors
  loses this credential's connectors, atomically; the original is copied into the backup (V2).

  **Stays in place:** connector queues with their own ``state_dir``. Nothing is deleted.

  **Safety:**
  - It stops the app's own runtime first and refuses while any other process holds a queue
    run lock (V3, V6).
  - A move that fails part-way is undone before it raises.
  - ``app.json`` is cleared, and a Run value naming a moved runtime config is pointed at the
    app's stub (app installs) or removed (V5).
  - The result lists every path it moved, rewrote or found, and never a token.
"""
import json
import os
from pathlib import Path
import shutil
import time

from . import filelock
from .config import default_config_path, load_config
from .errors import ApiError, ConfigError, RainError, Unauthorized
from .fsutil import atomic_write_bytes, atomic_write_json, ensure_private_dir, mkdir_private, read_file_bytes

OUTCOMES = ("ok", "invalid", "not_owner", "unknown", "unreadable")
CLOSE_OLD = "Close the old RainCLI window, then try again."
STOP_WAIT = 150  # a runtime stops within about 100 s (§11); the systemd unit allows 150


class SetAsideRefused(RainError):
    """Nothing was changed: another process holds a queue, or another migration is running."""


# -- 2. checking a credential ------------------------------------------------------------------------

def check_credential(agent_config=None, email=None, *, client_factory=None):
    """One of ``OUTCOMES`` for the machine credential in ``agent_config`` (§16.17 2, §16.18 V4):

    - ``unreadable``: a foreign or damaged DPAPI blob, a damaged file, or both token forms;
    - ``invalid``: ``GET /me`` answers 401;
    - ``not_owner``: ``/me``'s ``owner.email`` differs from ``email`` (only when ``email`` is given);
    - ``unknown``: unreachable, any other error, or an older server without ``owner`` (with ``email``);
    - ``ok``.

    A missing ``agent_config`` raises ``ConfigError`` (nothing to check)."""
    path = str(Path(agent_config or default_config_path()).absolute())
    if not os.path.lexists(path):
        raise ConfigError(f"this machine is not signed in ({path} does not exist)")
    try:
        config = load_config(path)
    except ConfigError:
        return "unreadable"
    try:
        if client_factory is not None:
            api = client_factory(config)
        else:
            from .api import ApiClient
            api = ApiClient.from_config(config, timeout=15, max_attempts=2)
        me = api.me()
    except Unauthorized:
        return "invalid"
    except (ApiError, OSError, ValueError, KeyError, TypeError):
        return "unknown"
    if email is None:
        return "ok"
    owner = me.get("owner") if isinstance(me, dict) else None
    if not isinstance(owner, dict) or not isinstance(owner.get("email"), str):
        return "unknown"
    return "ok" if owner["email"].strip().lower() == email.strip().lower() else "not_owner"


INVALID = "This computer's saved RainCLI setup belongs to a machine that was revoked."
NOT_OWNER = ("This computer's saved RainCLI setup belongs to a machine owned by another account. "
             "The other machine stays active for its owner until they revoke it.")
UNREADABLE = "This computer's saved RainCLI setup can't be read by this Windows account."
UNREADABLE_POSIX = "This computer's saved RainCLI setup can't be read."
OFFER = "Set up this computer as a new machine"


def stale_message(outcome):
    """The one plain sentence for ``invalid``, ``not_owner`` or ``unreadable`` (also for the
    ``machine_credential_invalid``/``not_machine_owner`` refusals), else None."""
    outcome = {"machine_credential_invalid": "invalid", "not_machine_owner": "not_owner"}.get(outcome, outcome)
    if outcome == "invalid":
        return INVALID
    if outcome == "not_owner":
        return NOT_OWNER
    if outcome == "unreadable":
        return UNREADABLE if os.name == "nt" else UNREADABLE_POSIX
    return None


# -- 5. setting the old setup aside ----------------------------------------------------------------------

def _same(a, b):
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _stamp():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def lock_migration(directory):
    """The migration lock in ``directory``, or None when another process holds it."""
    ensure_private_dir(str(directory))
    fd = os.open(Path(directory) / ".migration.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        filelock.lock(fd, blocking=False)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def unlock_migration(fd):
    try:
        filelock.unlock(fd)
    finally:
        os.close(fd)


class _Found:
    def __init__(self):
        self.connectors = []  # connector config paths naming this agent.json
        self.move_runtimes = []  # runtime configs to move, with their state_dir
        self.rewrite_runtimes = {}  # runtime config -> the data to write (other credentials stay)
        self.runtime_states = []  # state dirs of the moved runtime configs
        self.touched_runtimes = []  # every runtime config whose runtime must be stopped
        self.queue_dirs = []  # queue directories whose run lock must be free


def _add(items, path):
    if path is not None and all(not _same(path, p) for p in items):
        items.append(str(Path(path).absolute()))


def _state_dir(runtime_config, data):
    state = data.get("state_dir") or "runtime-state"
    return (Path(runtime_config).absolute().parent / Path(state).expanduser()).absolute()


def runtime_candidates(migration, agent):
    """Every runtime config ``migrate.detect`` would look at, plus the app's (§16.18 V3)."""
    from . import migrate
    from .runtime import winapp
    out = []
    for directory in migration._directories(agent):
        _add(out, directory / "runtime.json")
    value = migration.registry.get(winapp.RUN_VALUE)
    if value:
        argv = migrate.parse_command_line(value)
        if "--config" in argv[:-1]:
            _add(out, argv[argv.index("--config") + 1])
    if migration.app_root is not None:
        _add(out, winapp.read_settings(migration.app_root).get("runtime_config"))
    _add(out, migration.own_runtime)
    return [Path(p) for p in out if Path(p).is_file()]


def discover(agent_config, migration):
    """What ``set_aside`` moves, rewrites and must find free, without changing anything."""
    from .login import connector_agent_config, json_files, runtime_mode
    agent = Path(agent_config).absolute()
    found = _Found()
    for path, data in json_files(migration._directories(agent)):
        target = connector_agent_config(path, data)
        if target is not None and _same(target, agent):
            _add(found.connectors, path)
    for path in migration.connector_configs:
        target = connector_agent_config(path, _read_json(path) or {})
        if target is not None and _same(target, agent):
            _add(found.connectors, path)
    for runtime in runtime_candidates(migration, agent):
        data = _read_json(runtime) or {}
        mode = runtime_mode(runtime)
        base = runtime.absolute().parent
        if mode == "machine":
            if _same(base / Path(data["machine_config"]).expanduser(), agent):
                _add(found.move_runtimes, runtime)
        elif mode == "connector":
            ours, others = [], []
            for entry in data["connectors"]:
                connector = base / Path(str(entry)).expanduser()
                target = connector_agent_config(connector, _read_json(connector) or {})
                (ours if target is not None and _same(target, agent) else others).append(entry)
                if target is not None and _same(target, agent):
                    _add(found.connectors, connector)
            if ours and not others:
                _add(found.move_runtimes, runtime)
            elif ours:
                found.rewrite_runtimes[str(runtime.absolute())] = dict(data, connectors=others)
    for runtime in found.move_runtimes:
        state = _state_dir(runtime, _read_json(runtime) or {})
        if state.is_dir():
            _add(found.runtime_states, state)
    for runtime in found.move_runtimes + list(found.rewrite_runtimes):
        _add(found.touched_runtimes, runtime)
    for runtime in found.touched_runtimes:
        for d in migration.runtime_dirs(runtime):
            if Path(d).is_dir():
                _add(found.queue_dirs, d)
    for connector in found.connectors:
        for d in migration.connector_queues(connector, _read_json(connector) or {}):
            if Path(d).is_dir():
                _add(found.queue_dirs, d)
    return found


def _busy(directory):
    from .connector.queue import ConnectorBusy, Queue
    queue = Queue(str(directory))
    try:
        queue.acquire_run_lock()
    except ConnectorBusy:
        return True
    queue.release_run_lock()
    return False


def _stop_runtimes(found, stop_own, migration, clock, sleep, wait):
    """V6: stop the app's own runtime (the window's ``host.pause()``), or ask every touched
    runtime to stop (``request_stop``, the CLI), and wait for its state-dir lock. Nothing
    is resumed here: the fresh sign-in starts the new runtime."""
    from .runtime.service import request_stop
    if stop_own is not None:
        stop_own()
    else:
        for runtime in found.touched_runtimes:
            try:
                request_stop(runtime)
            except (ConfigError, OSError):
                continue
    states = [d for r in found.touched_runtimes for d in migration.runtime_dirs(r)[:1] if Path(d).is_dir()]
    deadline = clock() + wait
    while any(_busy(d) for d in states) and clock() < deadline:
        sleep(0.5)


def _backup_dir(config_dir):
    """``replaced-<UTC stamp>``, created exclusively; ``-2``, ``-3``... on a clash (V6)."""
    base = Path(config_dir) / f"replaced-{_stamp()}"
    for n in range(1, 1000):
        candidate = base if n == 1 else base.with_name(f"{base.name}-{n}")
        try:
            mkdir_private(str(candidate))
            return candidate
        except FileExistsError:
            continue
    raise ConfigError("cannot create a backup directory for the old setup")


def _target(backup, src, used):
    name = Path(src).name
    dst, n = backup / name, 2
    while dst.name.lower() in used:
        dst = backup / f"{n}-{name}"
        n += 1
    used.add(dst.name.lower())
    return dst


def set_aside(agent_config=None, *, migration=None, stop_own=None, locked=False, log=None, clock=time.monotonic,
              sleep=time.sleep, wait=STOP_WAIT, _replace=os.replace):
    """Move the old setup aside (§16.17 item 5, §16.18 V1-V3, V5, V6). Returns
    ``{"backup", "moved": [{"from", "to"}], "rewritten": [{"path", "original"}], "kept_queues",
    "run_value", "startup_entries", "app_settings"}`` (paths only). Raises ``SetAsideRefused``
    with nothing changed while another process holds a queue, and re-raises a failed move
    after moving everything back.

    ``migration`` supplies the scan directories, registry, app root, explicit connector configs
    and the app's own runtime (a ``migrate.Migration``; a default one otherwise). ``stop_own``
    is the window's ``host.pause``. ``locked`` is True when the caller (migration) already
    holds the migration lock."""
    from .migrate import Migration, MigrationLog
    from .runtime import winapp
    agent = Path(agent_config or default_config_path()).absolute()
    if migration is None:
        migration = Migration(app_root=winapp.app_root())
    config_dir = agent.parent
    lock = None
    if not locked:
        lock = lock_migration(config_dir)
        if lock is None:
            raise SetAsideRefused("another RainCLI migration or set-aside is running; try again shortly")
    try:
        found = discover(agent, migration)
        _stop_runtimes(found, stop_own, migration, clock, sleep, wait)
        held = [d for d in found.queue_dirs if _busy(d)]
        if held:
            raise SetAsideRefused(CLOSE_OLD)
        sources = [agent] + [config_dir / n for n in ("person.json", "app-install.json", "notifications")]
        sources += [Path(c) for c in found.connectors] + [Path(r) for r in found.move_runtimes]
        sources += [Path(s) for s in found.runtime_states]
        unique = []
        for src in sources:
            if os.path.lexists(src) and all(not _same(src, u) for u in unique):
                unique.append(src)
        backup = _backup_dir(config_dir)
        used, done, rewritten, originals, changed = set(), [], [], {}, []
        try:
            for src in unique:
                dst = _target(backup, src, used)
                _replace(str(src), str(dst))
                done.append((src, dst))
            for runtime in found.rewrite_runtimes:
                originals[runtime] = read_file_bytes(runtime)
                copy = _target(backup, runtime, used)
                atomic_write_bytes(str(copy), originals[runtime])
                rewritten.append({"path": runtime, "original": str(copy)})
            for runtime, data in found.rewrite_runtimes.items():
                changed.append(runtime)
                atomic_write_json(runtime, data)  # last: each is one atomic replace
        except BaseException:
            for runtime in changed:
                try:
                    atomic_write_bytes(runtime, originals[runtime])
                except OSError:
                    pass
            for src, dst in reversed(done):
                try:
                    _replace(str(dst), str(src))
                except OSError:
                    pass
            shutil.rmtree(backup, ignore_errors=True)
            raise
        moved = [{"from": str(src), "to": str(dst)} for src, dst in done]
        result = {"backup": str(backup), "moved": moved, "rewritten": rewritten,
                  "kept_queues": [d for d in found.queue_dirs if all(not _same(d, s) for s in found.runtime_states)],
                  "run_value": "unchanged", "startup_entries": [], "app_settings": "unchanged"}
        if migration.app_root is not None:
            winapp.write_settings(migration.app_root, {})  # the default paths from now on
            result["app_settings"] = "cleared"
            result["startup_entries"] = startup_entries(migration.app_root)
        log = log or MigrationLog(config_dir / "runtime-state")
        result["run_value"] = _old_run_value(migration, found, log)
        atomic_write_json(str(backup / "set-aside.json"), result)
        return result
    finally:
        if lock is not None:
            unlock_migration(lock)


def _old_run_value(migration, found, log):
    """V5: a Run value naming a moved runtime config is recorded, then pointed at the app's
    stub (app installs) or removed (the CLI). Any other Run value is kept."""
    from . import migrate
    from .runtime import winapp
    value = migration.registry.get(winapp.RUN_VALUE)
    if not value or (migration.app_root is not None and value == winapp.run_value(migration.app_root)):
        return "unchanged"
    argv = migrate.parse_command_line(value)
    config = argv[argv.index("--config") + 1] if "--config" in argv[:-1] else None
    if config is None or not any(_same(config, r) for r in found.move_runtimes):
        return "unchanged"
    log.write("old_run_value_disabled", original=value)
    if migration.app_root is not None:
        migration.registry.set(winapp.RUN_VALUE, winapp.run_value(migration.app_root))
        log.write("run_value_set_to_app")
        return "app"
    migration.registry.delete(winapp.RUN_VALUE)
    return "removed"


def startup_entries(app_root):
    """The Startup-folder and Scheduled Task entries the installer recorded (H7)."""
    try:
        lines = (Path(app_root) / "installer-record.log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if line.startswith(("startup-folder entry:", "scheduled task:")) and line not in out:
            out.append(line)
    return out
