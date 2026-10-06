"""Migrating an existing install to the app's credential storage and runtime
(protocol 15.6, amended by 15.8 H6, M7, M8 and L4).

It keeps the handle, the credential, the connector configs and their queues: it
never signs in, never creates a machine and never changes a handle. In order:

1. detect the managed v0.2/v0.3 install (``~/.raincli/client`` and a Run value
   naming it), an older pip/venv client's ``agent.json`` and the connector
   configs that use it; normalize the connector configs (explicit
   ``agent_config``, ``prompt_timeout`` clamped) and validate the resulting
   runtime config with ``load_runtime``. Nothing is written before this passes;
2. stop the old runtime through its stop request, and wait until every queue
   run lock is free (a foreground connector: "close the old RainCLI window");
3. while holding those locks, write the normalized configs and the runtime
   config, and convert every token to the DPAPI form (Windows), atomically;
4. start the new runtime (the caller's callback) and confirm it is ready;
5. only then disable the old Run value, recording the original.

Every step is recorded, without secrets, in ``<state_dir>/migration.log``.
Migration takes a lock and is idempotent.
"""
import json
import os
from pathlib import Path
import re
import time
import uuid

from . import filelock
from . import config as config_mod
from .config import default_config_path, load_config, standard_config_path, stored_form
from .connector.config import default_state_dir
from .connector.queue import ConnectorBusy, Queue
from .errors import ConfigError
from .fsutil import atomic_write_json, ensure_private_dir, read_private_file
from .login import _same, connector_agent_config, json_files, runtime_mode, scan_directories

CLOSE_OLD = "Close the old RainCLI window to finish moving this machine to the app."
PIP_NOTICE = ("The old pip-installed raincli command can no longer read this machine's credential; "
              "use the app's raincli command instead.")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_command_line(value):
    """Split a Run value (CommandLineToArgvW rules for the quoted values RainCLI wrote)."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        argc = ctypes.c_int()
        parse = ctypes.windll.shell32.CommandLineToArgvW
        parse.argtypes, parse.restype = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)], ctypes.POINTER(wintypes.LPWSTR)
        argv = parse(value, ctypes.byref(argc))
        try:
            return [argv[i] for i in range(argc.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(argv)
    # Elsewhere (tests): the always-quoted form startup.windows_command_line writes.
    return [quoted if quoted or not bare else bare for quoted, bare in re.findall(r'"([^"]*)"|(\S+)', value)]


class Plan:
    def __init__(self):
        self.managed = None  # {"root", "run_value", "runtime_config"}
        self.agent_configs = []  # every credential to keep and convert
        self.connectors = {}  # path -> normalized connector config dict
        self.clamped = []  # connector paths whose prompt_timeout was clamped
        self.runtime_config = None
        self.runtime = None  # the runtime config to write (dict), or None to keep the existing one
        self.state_dir = None
        self.old_runtimes = []  # runtime configs whose runtime may still be running
        self.run_value = None

    def summary(self):
        return {"managed_install": self.managed is not None, "agent_configs": list(self.agent_configs),
                "connectors": sorted(self.connectors), "runtime_config": self.runtime_config,
                "mode": "connector" if self.connectors else "machine", "state_dir": str(self.state_dir)}


class Migration:
    def __init__(self, *, env=None, registry=None, app_root=None, managed_root=None, home=None,
                 sleep=time.sleep, clock=time.monotonic, connector_configs=(), own_runtime=None,
                 stop_own=None, restart_own=None, own_running=None, spawn=None, inbox=None, check=None):
        """``own_runtime`` is the app's runtime config; ``own_running()`` tells whether
        the tray's runtime host is running it now, and ``stop_own``/``restart_own``
        pause and resume it. ``connector_configs``
        are connector configs the user names explicitly (``--connector-config``)."""
        from .runtime import updates, winapp
        self.connector_configs = [str(Path(p).absolute()) for p in connector_configs]
        self.own_runtime = own_runtime
        self.stop_own, self.restart_own = stop_own, restart_own
        self.own_running = own_running or (lambda: False)
        self.spawn = spawn or self._spawn
        self.inbox = inbox or self.inbox_role
        self.check = check or (lambda agent: _check_credential(agent))  # §16.17 3
        self.env = os.environ if env is None else env
        self.registry = winapp.WindowsRegistry() if registry is None else registry
        self.app_root = Path(app_root) if app_root else None
        self.managed_root = Path(managed_root) if managed_root else updates.default_root()
        self.sleep, self.clock = sleep, clock
        self.home = home

    # -- 1. detect, normalize and validate (nothing written) ---------------------------------

    def detect(self, validate=True):
        from .runtime import winapp
        plan = Plan()
        plan.run_value = self.registry.get(winapp.RUN_VALUE)
        if self.app_root is not None and plan.run_value == winapp.run_value(self.app_root):
            plan.run_value = None  # already the app's
        runtime_candidates = []
        if (self.managed_root / "current.json").is_file() and plan.run_value and \
                os.path.normcase(str(self.managed_root)) in os.path.normcase(plan.run_value):
            argv = parse_command_line(plan.run_value)
            config = argv[argv.index("--config") + 1] if "--config" in argv[:-1] else None
            plan.managed = {"root": str(self.managed_root), "runtime_config": config}
            if config:
                runtime_candidates.append(Path(config))
        agent = Path(default_config_path(self.env)).absolute()
        directories = self._directories(agent)
        for directory in directories:
            runtime_candidates.append(directory / "runtime.json")
        # The connector-mode runtime config to keep, if any.
        for candidate in runtime_candidates:
            if runtime_mode(candidate) == "connector":
                plan.runtime_config = str(candidate.absolute())
                break
        connectors = []
        if plan.runtime_config:
            data = json.loads(Path(plan.runtime_config).read_text(encoding="utf-8"))
            base = Path(plan.runtime_config).parent
            connectors = [str((base / Path(p).expanduser()).absolute()) for p in data["connectors"]]
            state = data.get("state_dir", "runtime-state")
            plan.state_dir = (base / Path(state).expanduser()).absolute()
            plan.old_runtimes.append(plan.runtime_config)
        agents = []
        if agent.is_file():
            agents.append(str(agent))
        for path, data in json_files(directories):
            target = connector_agent_config(path, data)
            if target is not None and (_same(target, agent) or any(_same(target, a) for a in agents)) \
                    and all(not _same(path, c) for c in connectors):
                connectors.append(str(path.absolute()))
        for connector in self.connector_configs:
            if all(not _same(connector, c) for c in connectors):
                connectors.append(connector)
        for connector in connectors:
            data = self._read_json(connector)
            target = connector_agent_config(connector, data)
            if target is None:
                raise ConfigError(f"migration: {connector} is not a connector config")
            normalized = dict(data)
            normalized["agent_config"] = str(Path(target).absolute())  # 15.8 M8: written explicitly
            from .runtime.service import MAX_PROMPT_TIMEOUT
            timeout = normalized.get("prompt_timeout")
            if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > MAX_PROMPT_TIMEOUT:
                normalized["prompt_timeout"] = MAX_PROMPT_TIMEOUT
                plan.clamped.append(connector)
            plan.connectors[connector] = normalized
            if all(not _same(target, a) for a in agents):
                agents.append(str(Path(target).absolute()))
        plan.agent_configs = agents
        if not agents:
            return None
        if plan.runtime_config is None:
            plan.runtime_config = str(agent.parent / "runtime.json")
            mode = runtime_mode(plan.runtime_config)
            if mode not in (None, "machine"):
                raise ConfigError(f"migration: {plan.runtime_config} is not a runtime config it can use")
            plan.state_dir = agent.parent / "runtime-state"
            if mode == "machine" and not connectors:
                # Signed in already (machine mode): keep it as it is.
                data = self._read_json(plan.runtime_config)
                plan.state_dir = (agent.parent / Path(data.get("state_dir", "runtime-state")).expanduser()).absolute()
                plan.old_runtimes.append(plan.runtime_config)
            elif connectors:
                plan.runtime = {"connectors": sorted(plan.connectors), "state_dir": str(plan.state_dir)}
            else:
                plan.runtime = {"machine_config": agents[0], "state_dir": str(plan.state_dir)}
            if plan.runtime is not None and mode == "machine":
                plan.old_runtimes.append(plan.runtime_config)
        if validate:
            self.validate(plan)
        return plan

    def _directories(self, agent):
        dirs = scan_directories(str(agent))
        standard = Path(standard_config_path()).parent
        if self.home is not None:
            dirs = [Path(self.home) / ".config" / "raincli" if _same(d, standard) else d for d in dirs]
        return dirs

    @staticmethod
    def _read_json(path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise ConfigError(f"migration: cannot read {path}: {type(exc).__name__}") from None
        if not isinstance(data, dict):
            raise ConfigError(f"migration: {path} is not a JSON object")
        return data

    def validate(self, plan):
        """The new runtime config, as it will be written, must pass ``load_runtime``.
        It is checked on scratch copies beside the originals, removed afterwards."""
        from .runtime.service import load_runtime
        scratch = []
        tag = uuid.uuid4().hex[:8]
        try:
            mapping = {}
            for path, data in plan.connectors.items():
                copy = Path(path).with_name(f".{Path(path).name}.migrate-{tag}.json")
                atomic_write_json(copy, data)
                scratch.append(copy)
                mapping[path] = str(copy)
            if plan.runtime is not None:
                runtime = dict(plan.runtime)
            else:
                runtime = self._read_json(plan.runtime_config)
                base = Path(plan.runtime_config).parent
                if "connectors" in runtime:
                    runtime["connectors"] = [str((base / Path(p).expanduser()).absolute()) for p in runtime["connectors"]]
                if isinstance(runtime.get("machine_config"), str):
                    runtime["machine_config"] = str((base / Path(runtime["machine_config"]).expanduser()).absolute())
                runtime["state_dir"] = str(plan.state_dir)
            if "connectors" in runtime:
                runtime["connectors"] = [mapping.get(p, p) for p in runtime["connectors"]]
            copy = Path(plan.runtime_config).with_name(f".runtime.json.migrate-{tag}.json")
            atomic_write_json(copy, runtime)
            scratch.append(copy)
            load_runtime(copy)
        except ConfigError as exc:
            raise ConfigError(f"migration aborted, nothing changed: {exc}") from None
        finally:
            for path in scratch:
                try:
                    path.unlink()
                except OSError:
                    pass

    # -- 2. who holds the queues; stop only runtimes we can restart ------------------------------

    @staticmethod
    def connector_queues(path, data):
        """A connector's queue directory: its ``state_dir`` resolved from the connector
        config's own directory, as the connector resolves it (review 1a F2), or every
        queue at the default location, which is named after a handle."""
        state = data.get("state_dir") if isinstance(data, dict) else None
        if isinstance(state, str) and state:
            return [(Path(path).absolute().parent / Path(state).expanduser()).absolute()]
        root = Path(default_state_dir("x")).parent
        return sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []

    def runtime_dirs(self, runtime_config):
        """The runtime state directory and connector queues a runtime config's runtime holds."""
        try:
            data = self._read_json(runtime_config)
        except ConfigError:
            return []
        base = Path(runtime_config).absolute().parent
        dirs = [(base / Path(data.get("state_dir") or "runtime-state").expanduser()).absolute()]
        for entry in data.get("connectors") or []:
            if isinstance(entry, str) and entry:
                connector = base / Path(entry).expanduser()
                try:
                    dirs.extend(self.connector_queues(connector, self._read_json(connector)))
                except ConfigError:
                    continue
        return dirs

    def queue_dirs(self, plan):
        dirs = [Path(plan.state_dir)]
        for path, data in plan.connectors.items():
            dirs.extend(self.connector_queues(path, data))
        for runtime in plan.old_runtimes:
            dirs.extend(self.runtime_dirs(runtime))
        unique = []
        for d in dirs:
            if d.is_dir() and all(not _same(d, u) for u in unique):
                unique.append(d)
        return unique

    @staticmethod
    def _busy(directory):
        queue = Queue(str(directory))
        try:
            queue.acquire_run_lock()
        except ConnectorBusy:
            return True
        queue.release_run_lock()
        return False

    def held(self, plan):
        """The queue directories whose run lock someone holds, probed without keeping any."""
        return [d for d in self.queue_dirs(plan) if self._busy(d)]

    def acquire(self, plan):
        """Every existing queue run lock, or None when one is held (an old runtime or connector)."""
        held = []
        for directory in self.queue_dirs(plan):
            queue = Queue(str(directory))
            try:
                queue.acquire_run_lock()
            except ConnectorBusy:
                for q in held:
                    q.release_run_lock()
                return None
            held.append(queue)
        return held

    def stoppable(self, plan):
        """The running runtimes migration may stop, because it can start them again:
        the app's own runtime (``own_runtime``; the tray restarts it) and an old
        runtime its Run value starts. ``[(kind, runtime config, dirs)]``."""
        out = []
        # "own" only while the tray's host really runs that config; a lock on the same
        # config held by anything else (the old managed launcher) goes through the old
        # Run value's stop request (review 2, R1).
        own = self.own_runtime is not None and self.own_running()
        if own:
            out.append(("own", str(self.own_runtime), self.runtime_dirs(self.own_runtime)))
        command = self.old_command(plan)
        if command is not None:
            config = command[command.index("--config") + 1]
            if not (own and _same(config, self.own_runtime)):
                out.append(("old", config, self.runtime_dirs(config)))
        return [(kind, config, dirs) for kind, config, dirs in out if dirs and self._busy(dirs[0])]

    @staticmethod
    def old_command(plan):
        """The old Run value's argv when it runs ``runtime run --config``, else None."""
        if not plan.run_value:
            return None
        argv = parse_command_line(plan.run_value)
        if "runtime" in argv and "run" in argv and "--config" in argv[:-1]:
            return argv
        return None

    def stop_runtime(self, kind, config, log):
        """A graceful stop through the runtime's own stop request (15.8 M7)."""
        from .runtime.service import request_stop
        if kind == "own" and self.stop_own is not None:
            self.stop_own()
            log.write("app_runtime_paused", runtime_config=config)
            return
        try:
            result = request_stop(config)["status"]
        except (ConfigError, OSError) as exc:
            result = "error:" + type(exc).__name__
        log.write("old_runtime_stop_requested", runtime_config=config, result=result)

    def restart(self, stopped, plan, log):
        """After a cancel or timeout: start again what migration stopped (review 1a F4)."""
        for kind, config in stopped:
            try:
                if kind == "own":
                    if self.restart_own is not None:
                        self.restart_own()
                else:
                    self.spawn(self.old_command(plan))
                log.write("runtime_restarted", runtime_config=config, kind=kind)
            except OSError as exc:
                log.write("runtime_restart_failed", runtime_config=config, error=type(exc).__name__)

    @staticmethod
    def _spawn(argv):
        import subprocess
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=flags)

    def free_queues(self, plan, log, notify, cancelled, wait):
        """Wait until no queue lock is held. A runtime is stopped only when the
        remaining holders are all runtimes migration can restart; anything else
        (a connector in an old window) is waited for with a notice. Returns the
        held locks, or a result dict when cancelled or timed out."""
        stopped, notified = [], False
        deadline = self.clock() + wait
        while True:
            held = self.held(plan)
            if not held:
                locks = self.acquire(plan)
                if locks is not None:
                    return locks
                continue
            owners = self.stoppable(plan)
            owned = [d for _, _, dirs in owners for d in dirs]
            if all(any(_same(h, d) for d in owned) for h in held):
                for kind, config, _ in owners:
                    if not any(_same(config, c) for _, c in stopped):  # each runtime config once
                        self.stop_runtime(kind, config, log)
                        stopped.append((kind, config))
            elif not notified:
                log.write("old_window_holds_queue", queues=[str(h) for h in held])
                notify(CLOSE_OLD)
                notified = True
            if cancelled() or self.clock() >= deadline:
                self.restart(stopped, plan, log)
                status = "cancelled" if cancelled() else "waiting"
                log.write(status)
                return {"status": status, "message": CLOSE_OLD}
            self.sleep(1)

    # -- 3-5 -----------------------------------------------------------------------------------------

    def pending(self):
        """Whether a migration has anything left to do: a token to convert, a config
        to write or normalize, or an old Run value (review 1a F5). False when nothing
        is installed or the plan does not validate (``run`` then reports why)."""
        try:
            plan = self.detect()
        except ConfigError:
            return True
        if plan is None:
            return False
        if plan.runtime is not None or plan.run_value:
            return True
        for path, data in plan.connectors.items():
            if self._read_json(path) != data:
                return True
        if config_mod.protects_tokens():
            for agent in plan.agent_configs:
                if "token_dpapi" not in json.loads(read_private_file(agent, "agent config")):
                    return True
        return False

    @staticmethod
    def any_protected(plan):
        for agent in plan.agent_configs:
            try:
                if "token_dpapi" in json.loads(read_private_file(agent, "agent config")):
                    return True
            except (ConfigError, ValueError):
                continue
        return False

    def old_run_value(self):
        """The current Run value when it is not the app's stub, else None."""
        from .runtime import winapp
        value = self.registry.get(winapp.RUN_VALUE)
        if self.app_root is not None and value == winapp.run_value(self.app_root):
            return None
        return value

    def inbox_role(self, agent_config):
        """Whether this credential's handle has delivery history, from ``GET /me``'s
        ``delivery_history`` (review 2 R4: an inbox role ever published, or a message
        recipient): True, False, or None when it cannot be told (offline, or a server
        without the field)."""
        from .api import ApiClient
        from .errors import ApiError
        try:
            client = ApiClient.from_config(load_config(agent_config), timeout=15, max_attempts=2)
            history = client.me().get("delivery_history")
        except (ApiError, ConfigError, KeyError, TypeError, AttributeError):
            return None
        return history if isinstance(history, bool) else None

    def run(self, start_runtime=None, *, notify=lambda message: None, cancelled=lambda: False, wait=600):
        """Migrate. ``start_runtime(runtime_config)`` starts the new runtime and returns
        True once it is ready (step 4). ``notify`` receives user-facing messages;
        ``cancelled`` ends the wait for an old window. Returns a result dict with ``status``."""
        lock = self._lock()
        if lock is None:
            return {"status": "busy", "message": "another migration is running"}
        try:
            stale = self.set_aside_stale()
            plan = self.detect()
            if plan is None:
                if stale:  # §16.18 V2: no valid credential remains
                    return {"status": "fresh_sign_in_needed", "set_aside": stale}
                return {"status": "nothing_to_migrate"}
            log = MigrationLog(plan.state_dir)
            log.write("detected", **plan.summary())
            for path in plan.clamped:
                log.write("prompt_timeout_clamped", connector=path)
            if plan.runtime is not None and "machine_config" in plan.runtime:
                # An existing handle about to become machine mode: if it has served as an
                # inbox, its connector config lives somewhere migration did not look.
                role = self.inbox(plan.agent_configs[0])
                log.write("delivery_history_checked", result=role)  # null: offline, cannot tell
                if role:
                    message = (f"This machine delivered messages through a connector, but its connector config was "
                               f"not found. Run: raincli migrate --connector-config PATH")
                    notify(message)
                    return {"status": "connector_config_required", "message": message, **plan.summary()}
            locks = self.free_queues(plan, log, notify, cancelled, wait)
            if isinstance(locks, dict):
                return locks
            try:
                converted = self.write(plan, log)
            finally:
                for queue in locks:
                    queue.release_run_lock()
            result = {"status": "migrated_with_stale_set_aside" if stale else "migrated", **plan.summary(),
                      "converted": converted, "run_value": "unchanged"}
            if stale:
                result["set_aside"] = stale
            if config_mod.protects_tokens() and converted:
                result["notice"] = PIP_NOTICE
                notify(PIP_NOTICE)
            if self.app_root is not None:
                from .runtime import winapp
                winapp.write_settings(self.app_root, {"agent_config": plan.agent_configs[0],
                                                      "runtime_config": plan.runtime_config})
                if converted or (plan.run_value and self.any_protected(plan)):
                    # The old Run value cannot read a converted token, converted now or by an
                    # earlier, interrupted run: the app starts at logon from now on, even if
                    # step 4 fails (review 1a F1, review 2 R2, 15.9).
                    result["run_value"] = self.point_run_value_at_app(plan, log)
            if start_runtime is None:
                return result
            if not start_runtime(plan.runtime_config):
                log.write("new_runtime_not_ready")
                result["status"] = "runtime_not_ready"
                return result
            log.write("new_runtime_ready")
            if result["run_value"] == "unchanged":
                result["run_value"] = self.point_run_value_at_app(plan, log)
            return result
        finally:
            self._unlock(lock)

    def set_aside_stale(self):
        """§16.17 3, §16.18 V2, V4: before adopting anything, check every credential migration
        would keep. An ``invalid`` or ``unreadable`` one is never adopted: its setup is set
        aside and logged ``stale_credential_set_aside``. ``unknown`` (offline, an older server)
        is adopted as before; migration has no signing-in user, so it never decides ``not_owner``.
        Returns the set-aside results."""
        from .setaside import set_aside
        try:
            plan = self.detect(validate=False)
        except ConfigError:
            return []
        if plan is None:
            return []
        log = MigrationLog(Path(default_config_path(self.env)).absolute().parent / "runtime-state")
        out = []
        for agent in plan.agent_configs:
            try:
                outcome = self.check(agent)
            except ConfigError:
                continue
            if outcome not in ("invalid", "unreadable"):
                continue
            result = set_aside(agent, migration=self, locked=True, log=log,
                               stop_own=self.stop_own if self.own_running() else None)
            log.write("stale_credential_set_aside", agent_config=agent, result=outcome, backup=result["backup"])
            out.append({"agent_config": agent, "result": outcome, **result})
        return out

    def write(self, plan, log):
        for path, data in plan.connectors.items():
            if self._read_json(path) != data:
                atomic_write_json(path, data)
                log.write("connector_config_normalized", connector=path)
        if plan.runtime is not None:
            atomic_write_json(plan.runtime_config, plan.runtime)
            log.write("runtime_config_written", path=plan.runtime_config, mode=next(iter(plan.runtime)))
        else:
            log.write("runtime_config_kept", path=plan.runtime_config)
        converted = []
        for agent in plan.agent_configs:
            raw = json.loads(read_private_file(agent, "agent config"))
            if not config_mod.protects_tokens() or "token_dpapi" in raw:
                continue
            config = load_config(agent)
            atomic_write_json(agent, stored_form(config.api_url, config.token))
            converted.append(agent)
            log.write("token_protected", agent_config=agent)
        return converted

    def point_run_value_at_app(self, plan, log):
        """Record the old Run value, then replace it with the stub (app) or remove it."""
        from .runtime import winapp
        if plan.run_value:
            log.write("old_run_value_disabled", original=plan.run_value)
        if self.app_root is not None:
            if self.registry.get(winapp.RUN_VALUE) != winapp.run_value(self.app_root):
                self.registry.set(winapp.RUN_VALUE, winapp.run_value(self.app_root))
                log.write("run_value_set_to_app")
            plan.run_value = None
            return "app"
        if not plan.run_value:
            return "none"
        self.registry.delete(winapp.RUN_VALUE)
        plan.run_value = None
        return "removed"

    def _lock(self):
        directory = Path(default_config_path(self.env)).absolute().parent
        ensure_private_dir(str(directory))
        fd = os.open(directory / ".migration.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            filelock.lock(fd, blocking=False)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd

    @staticmethod
    def _unlock(fd):
        try:
            filelock.unlock(fd)
        finally:
            os.close(fd)


def _check_credential(agent):
    from .setaside import check_credential
    return check_credential(agent)


class MigrationLog:
    """``<state_dir>/migration.log``: one JSON line per step, paths and codes only."""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / "migration.log"

    def write(self, event, **fields):
        ensure_private_dir(str(self.path.parent))
        line = json.dumps({"at": _now(), "event": event, **fields}, sort_keys=True, default=str)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)
