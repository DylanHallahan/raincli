"""A small supervisor; the connector remains responsible for durable delivery."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

from .. import filelock
from ..api import ApiClient
from ..config import load_config
from ..connector.config import default_state_dir, load_connector_config
from ..connector.herdr import HerdrCli, HerdrError, READY_STATUSES
from ..connector.queue import Queue
from ..errors import ConfigError
from ..fsutil import atomic_write_json, ensure_private_dir, read_private_file

LOG_LIMIT = 1024 * 1024  # per connector log; one rotated generation is kept


def file_sha256(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def fingerprint(config_path, agent_config):
    """What a connector mapping was loaded from: exact paths and content hashes.

    The agent config hash stands for the credential; the token itself is never
    copied into readiness or status records."""
    return {"config": str(config_path), "config_sha256": file_sha256(config_path),
            "agent_config": os.path.abspath(agent_config), "agent_config_sha256": file_sha256(agent_config)}


def load_bound(config_path):
    """Load one connector mapping and bind it to the bytes it was loaded from."""
    config_sha = file_sha256(config_path)
    cfg = load_connector_config(config_path)
    if not cfg.agent_config:
        raise ConfigError("each runtime connector must name its agent_config explicitly")
    agent_sha = file_sha256(cfg.agent_config)
    identity = load_config(cfg.agent_config)
    binding = fingerprint(config_path, cfg.agent_config)
    if (config_sha, agent_sha) != (binding["config_sha256"], binding["agent_config_sha256"]) or None in (config_sha, agent_sha):
        raise ConfigError("connector config changed while loading; retry")
    return cfg, identity, binding


def _read_runtime(path):
    path = Path(path).expanduser().resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read runtime config: {exc}") from None
    if not isinstance(data, dict) or set(data) - {"connectors", "state_dir"}:
        raise ConfigError("runtime config supports only connectors and state_dir")
    entries = data.get("connectors")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 16 or not all(isinstance(p, str) and p for p in entries):
        raise ConfigError("runtime connectors must contain 1-16 explicit connector config paths")
    def absolute(p):
        return (path.parent / Path(p).expanduser()).resolve()
    state = data.get("state_dir", "runtime-state")
    if not isinstance(state, str) or not state:
        raise ConfigError("runtime state_dir must be a path")
    return path, entries, absolute, absolute(state)


def load_runtime(path):
    path, entries, absolute, state = _read_runtime(path)
    paths = [str(absolute(p)) for p in entries]
    if len(set(map(os.path.normcase, paths))) != len(paths):
        raise ConfigError("runtime connector paths must be unique")
    configs, identities, states = [], set(), set()
    for config_path in paths:
        cfg, identity, binding = load_bound(config_path)
        key = (identity.api_url, identity.token)
        if key in identities:
            raise ConfigError("runtime connectors must have distinct credentials")
        identities.add(key)
        if cfg.state_dir:
            canonical = os.path.normcase(os.path.realpath(cfg.state_dir))
            if canonical in states or canonical == os.path.normcase(os.path.realpath(state)):
                raise ConfigError("runtime connectors must have distinct queue state directories")
            states.add(canonical)
        configs.append((config_path, cfg, identity, binding))
    return path, state, configs


def availability(cfg, herdr, running):
    if not running:
        return "offline"
    try:
        info = herdr.get_agent(cfg.herdr_agent)
    except HerdrError:
        return "unknown"
    if info is None:
        return "offline"
    if cfg.expect_pane_id and cfg.expect_pane_id != info.pane_id:
        return "blocked"
    if cfg.expect_cwd and os.path.normcase(os.path.normpath(cfg.expect_cwd)) != os.path.normcase(os.path.normpath(info.cwd)):
        return "blocked"
    if info.status in READY_STATUSES:
        return "ready"
    if info.status == "blocked":
        return "blocked"
    return "busy" if info.status == "working" else "unknown"


class Worker:
    """Supervises one connector child for one exact, loaded mapping.

    Presence is published only with the credential this mapping was loaded
    with, and ``ready`` only when the child confirms it serves the same
    config bytes as the same handle. Once the files on disk differ from that
    binding the worker retires; the runtime revalidates before any new write.
    """

    def __init__(self, path, cfg, identity, state, binding):
        self.path, self.cfg, self.binding = path, cfg, binding
        self.api = ApiClient.from_config(identity, timeout=5, max_attempts=1)
        self.herdr = HerdrCli(cfg.herdr_bin, timeout=5)
        self.process = None
        self.next_start = 0
        self.failures = 0
        self.started = 0
        self.handle = None  # confirmed by the server for this credential
        self.retired = False
        self.report = {"connector": path, "status": "offline", "reported": False}
        self.state = state
        self.ready_path = state / ("ready-" + uuid.uuid4().hex + ".json")
        self.stop_path = Path(str(self.ready_path) + ".stop")
        self.log_path = state / ("connector-" + hashlib.sha256(path.encode()).hexdigest()[:12] + ".log")
        self.owner_fd = None  # per-queue lock: one runtime per connector, whatever its state_dir

    def changed(self):
        return fingerprint(self.path, self.cfg.agent_config) != self.binding

    def retire(self):
        if not self.retired:
            self.retired = True
            self.stop()

    def _retired_report(self):
        self.report = {"connector": self.path, "status": "offline", "reported": False,
                       "process_running": False, "child_pid": None, "error": "config_changed"}
        return dict(self.report)

    def _claim(self):
        """Own the connector's queue directory across runtimes, so two runtime
        configs naming one connector cannot both publish for its credential."""
        if self.owner_fd is not None:
            return True
        directory = self.cfg.state_dir or default_state_dir(self.handle)
        ensure_private_dir(directory)
        fd = os.open(os.path.join(directory, "runtime-owner.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            filelock.lock(fd, blocking=False)
        except BlockingIOError:
            os.close(fd)
            return False
        self.owner_fd = fd
        return True

    def _release(self):
        if self.owner_fd is not None:
            filelock.unlock(self.owner_fd)
            os.close(self.owner_fd)
            self.owner_fd = None

    def _spawn(self):
        # Connector output (escaped log lines, no credentials) goes to a
        # private, size-capped log in the runtime state directory.
        try:
            if self.log_path.stat().st_size > LOG_LIMIT:
                os.replace(self.log_path, str(self.log_path) + ".1")
        except FileNotFoundError:
            pass
        log = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            return subprocess.Popen([sys.executable, "-m", "raincli_agent", "connector", "run", "--config", self.path, "--runtime-ready", str(self.ready_path)],
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        finally:
            os.close(log)

    def _confirmed_ready(self):
        try:
            record = json.loads(read_private_file(self.ready_path))
        except (ConfigError, ValueError, UnicodeDecodeError):
            return False
        return (self.handle is not None and isinstance(record, dict)
                and record == {"pid": self.process.pid, "handle": self.handle, **self.binding})

    def tick(self, now):
        if self.retired or self.changed():
            self.retire()
            return self._retired_report()
        if self.process is not None and self.process.poll() is not None:
            self.failures = 0 if now - self.started > 60 else self.failures + 1
            self.next_start = now + min(60, 2 ** min(self.failures, 6))
            self.process = None
            self.ready_path.unlink(missing_ok=True)
            self.stop_path.unlink(missing_ok=True)
        self.report = {"connector": self.path, "status": "offline", "reported": False}
        if self.handle is None:
            try:
                self.handle = self.api.me()["agent"]["handle"]
            except Exception as exc:
                # Nothing is started or published until the server confirms this credential.
                self.report["error"] = type(exc).__name__
                return dict(self.report)
        try:
            claimed = self._claim()
        except OSError as exc:
            self.report["error"] = type(exc).__name__
            return dict(self.report)
        if not claimed:
            self.report["error"] = "connector_owned_by_another_runtime"
            return dict(self.report)
        if self.process is None and now >= self.next_start:
            try:
                self.process = self._spawn()
                self.started = now
            except OSError:
                self.next_start = now + 30
        running = self.process is not None and self.process.poll() is None
        state = availability(self.cfg, self.herdr, running and self._confirmed_ready())
        self.report.update(status=state, process_running=running, child_pid=self.process.pid if running else None)
        if self.changed():  # edited during this tick: never publish under a stale binding
            self.retire()
            return self._retired_report()
        try:
            presence = self.api.publish_presence(state)
            self.report.update(reported=True, expires_at=presence["expires_at"])
        except Exception as exc:
            # Do not persist server text or arbitrary exception details: no secrets
            # or local tool output belong in status records.
            self.report["error"] = type(exc).__name__
        return dict(self.report)

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            # Ask the connector to finish its current iteration and release its
            # queue lock; terminate only if it does not exit in time.
            try:
                atomic_write_json(self.stop_path, {"pid": self.process.pid})
                self.process.wait(timeout=self.cfg.poll_wait + 10)
            except (OSError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        self.process = None
        self.ready_path.unlink(missing_ok=True)
        self.stop_path.unlink(missing_ok=True)
        self._release()
        if self.handle is not None:
            try:
                self.api.publish_presence("offline")
            except Exception:
                pass  # server expiry handles shutdown while disconnected


class Supervisor:
    def __init__(self, path, state, configs, runtime_sha):
        self.path, self.state = path, state
        self.workers = [Worker(p, cfg, identity, state, binding) for p, cfg, identity, binding in configs]
        self.seen = (runtime_sha, tuple(w.binding for w in self.workers))
        self.error = None

    def _snapshot(self):
        return file_sha256(self.path), tuple(fingerprint(w.path, w.cfg.agent_config) for w in self.workers)

    def changed(self):
        return self._snapshot() != self.seen

    def refresh(self, pool):
        """Revalidate every mapping after any config edit, before further presence writes."""
        if not self.changed():
            return
        snapshot = self._snapshot()
        list(pool.map(lambda w: w.retire(), [w for w in self.workers if w.changed()]))
        try:
            _, state, configs = load_runtime(self.path)
            if state != self.state:
                raise ConfigError("runtime state_dir changes require a restart")
        except ConfigError:
            # Changed mappings stay retired (offline, unpublished) until a
            # further edit makes the whole runtime config valid again.
            self.seen, self.error = snapshot, "config_invalid"
            return
        current = {w.path: w for w in self.workers if not w.retired}
        workers = []
        for p, cfg, identity, binding in configs:
            worker = current.pop(p, None)
            if worker is None or worker.binding != binding:
                if worker is not None:
                    worker.retire()
                worker = Worker(p, cfg, identity, state, binding)
            workers.append(worker)
        list(pool.map(lambda w: w.retire(), current.values()))  # removed from the runtime config
        self.workers, self.error = workers, None
        self.seen = (snapshot[0], tuple(w.binding for w in workers))


def stop_requested(state, instance):
    try:
        request = json.loads(read_private_file(state / "stop.json"))
    except (ConfigError, ValueError, UnicodeDecodeError):
        return False
    return isinstance(request, dict) and request.get("instance") == instance


def run(path, once=False):
    path = Path(path).expanduser().resolve()
    runtime_sha = file_sha256(path)
    path, state, configs = load_runtime(path)
    queue = Queue(str(state))
    queue.acquire_run_lock()
    stop = threading.Event()
    previous = {}
    supervisor = None
    instance = uuid.uuid4().hex

    def record(status, connectors):
        data = {"pid": os.getpid(), "instance": instance, "updated_at": time.time(), "status": status,
                "connectors": connectors}
        if supervisor is not None and supervisor.error:
            data["error"] = supervisor.error
        atomic_write_json(state / "status.json", data)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
        # Publish this instance before any slow first tick, so a stop request
        # made during startup targets it rather than a previous run.
        record("starting", [])
        supervisor = Supervisor(path, state, configs, runtime_sha)
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            while not stop.is_set():
                supervisor.refresh(pool)
                futures = [pool.submit(w.tick, time.monotonic()) for w in supervisor.workers]
                while concurrent.futures.wait(futures, timeout=1).not_done:
                    if stop_requested(state, instance):
                        stop.set()  # honoured as soon as the in-flight ticks return
                if stop_requested(state, instance):
                    stop.set()
                record("running", [f.result() for f in futures])
                if once:
                    break
                for _ in range(30):
                    if stop.is_set() or stop.wait(1):
                        break
                    if stop_requested(state, instance):
                        stop.set()
                        break
                    if supervisor.changed():
                        break
    finally:
        workers = supervisor.workers if supervisor else []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(workers))) as pool:
            list(pool.map(lambda w: w.retire(), workers))
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        record("stopped", [])
        queue.release_run_lock()


def status(path):
    state = _read_runtime(path)[-1]
    try:
        data = json.loads(read_private_file(state / "status.json", "runtime status"))
    except (ConfigError, ValueError, UnicodeDecodeError):
        return {"status": "not_observed"}
    if not isinstance(data, dict) or not isinstance(data.get("updated_at"), (int, float)):
        return {"status": "not_observed"}
    data["stale"] = time.time() - data["updated_at"] > 120
    return data


def request_stop(path):
    # Only the state directory is needed: stopping must work while a mapping is invalid.
    state = _read_runtime(path)[-1]
    try:
        data = json.loads(read_private_file(state / "status.json", "runtime status"))
    except (ConfigError, ValueError, UnicodeDecodeError):
        return {"status": "not_running"}
    if not isinstance(data, dict) or data.get("status") == "stopped" or not isinstance(data.get("instance"), str):
        return {"status": "not_running"}
    atomic_write_json(state / "stop.json", {"instance": data["instance"]})
    return {"status": "stop_requested"}
