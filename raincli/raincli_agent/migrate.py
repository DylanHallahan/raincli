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
                 sleep=time.sleep, clock=time.monotonic):
        from .runtime import updates, winapp
        self.env = os.environ if env is None else env
        self.registry = winapp.WindowsRegistry() if registry is None else registry
        self.app_root = Path(app_root) if app_root else None
        self.managed_root = Path(managed_root) if managed_root else updates.default_root()
        self.sleep, self.clock = sleep, clock
        self.home = home

    # -- 1. detect, normalize and validate (nothing written) ---------------------------------

    def detect(self):
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

    # -- 2. stop the old runtime, hold every queue ----------------------------------------------

    def queue_dirs(self, plan):
        dirs = [Path(plan.state_dir)]
        for data in plan.connectors.values():
            state = data.get("state_dir")
            if isinstance(state, str) and state:
                dirs.append(Path(state).expanduser())
            else:
                # A queue at the default location is named after its handle: check them all.
                root = Path(default_state_dir("x")).parent
                dirs.extend(sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else [])
        unique = []
        for d in dirs:
            if d.is_dir() and all(not _same(d, u) for u in unique):
                unique.append(d)
        return unique

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

    def request_old_stop(self, plan):
        """A managed (or other) old runtime is stopped through its own stop request (15.8 M7)."""
        from .runtime.service import request_stop
        results = {}
        for config in plan.old_runtimes:
            try:
                results[config] = request_stop(config)["status"]
            except (ConfigError, OSError) as exc:
                results[config] = "error:" + type(exc).__name__
        return results

    # -- 3-5 -----------------------------------------------------------------------------------------

    def run(self, start_runtime=None, *, notify=lambda message: None, cancelled=lambda: False, wait=600):
        """Migrate. ``start_runtime(runtime_config)`` starts the new runtime and returns
        True once it is ready (step 4); without it the old Run value is left alone.
        ``notify`` receives user-facing messages; ``cancelled`` ends the wait for an
        old window. Returns a result dict with ``status``."""
        lock = self._lock()
        if lock is None:
            return {"status": "busy", "message": "another migration is running"}
        try:
            plan = self.detect()
            if plan is None:
                return {"status": "nothing_to_migrate"}
            log = MigrationLog(plan.state_dir)
            log.write("detected", **plan.summary())
            for path in plan.clamped:
                log.write("prompt_timeout_clamped", connector=path)
            held = self.acquire(plan)
            if held is None:
                stops = self.request_old_stop(plan)
                log.write("old_runtime_stop_requested", results=stops)
                notify(CLOSE_OLD)
                deadline = self.clock() + wait
                while held is None:
                    if cancelled():
                        log.write("cancelled")
                        return {"status": "cancelled", "message": CLOSE_OLD}
                    if self.clock() >= deadline:
                        log.write("old_runtime_still_running")
                        return {"status": "waiting", "message": CLOSE_OLD}
                    self.sleep(1)
                    held = self.acquire(plan)
            try:
                converted = self.write(plan, log)
            finally:
                for queue in held:
                    queue.release_run_lock()
            result = {"status": "migrated", **plan.summary(), "converted": converted}
            if config_mod.protects_tokens() and converted:
                result["notice"] = PIP_NOTICE
                notify(PIP_NOTICE)
            if self.app_root is not None:
                from .runtime import winapp
                winapp.write_settings(self.app_root, {"agent_config": plan.agent_configs[0],
                                                      "runtime_config": plan.runtime_config})
            if start_runtime is None:
                result["run_value"] = "unchanged"
                return result
            if not start_runtime(plan.runtime_config):
                log.write("new_runtime_not_ready")
                result.update(status="runtime_not_ready", run_value="unchanged")
                return result
            log.write("new_runtime_ready")
            result["run_value"] = self.disable_old_run_value(plan, log)
            return result
        finally:
            self._unlock(lock)

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

    def disable_old_run_value(self, plan, log):
        from .runtime import winapp
        if not plan.run_value:
            return "none"
        log.write("old_run_value_disabled", original=plan.run_value)
        if self.app_root is not None:
            self.registry.set(winapp.RUN_VALUE, winapp.run_value(self.app_root))
            return "replaced_by_app"
        self.registry.delete(winapp.RUN_VALUE)
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
