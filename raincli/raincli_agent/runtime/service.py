"""A small supervisor; the connector remains responsible for durable delivery."""
import concurrent.futures
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

from ..api import ApiClient
from ..config import load_config
from ..connector.config import load_connector_config
from ..connector.herdr import HerdrCli, HerdrError, READY_STATUSES
from ..connector.queue import Queue
from ..errors import ConfigError
from ..fsutil import atomic_write_json, read_private_file


def load_runtime(path):
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
    paths = [str(absolute(p)) for p in entries]
    if len(set(map(os.path.normcase, paths))) != len(paths):
        raise ConfigError("runtime connector paths must be unique")
    configs, identities, states = [], set(), set()
    for config_path in paths:
        cfg = load_connector_config(config_path)
        if not cfg.agent_config:
            raise ConfigError("each runtime connector must name its agent_config explicitly")
        identity = load_config(cfg.agent_config)
        key = (identity.api_url, identity.token)
        if key in identities:
            raise ConfigError("runtime connectors must have distinct credentials")
        identities.add(key)
        if cfg.state_dir:
            canonical = os.path.normcase(os.path.realpath(cfg.state_dir))
            if canonical in states:
                raise ConfigError("runtime connectors must have distinct queue state directories")
            states.add(canonical)
        configs.append((config_path, cfg, identity))
    return path, absolute(state), configs


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
    def __init__(self, path, cfg, identity, state):
        self.path, self.cfg = path, cfg
        self.api = ApiClient.from_config(identity, timeout=5, max_attempts=1)
        self.herdr = HerdrCli(cfg.herdr_bin, timeout=5)
        self.process = None
        self.next_start = 0
        self.failures = 0
        self.started = 0
        self.report = {"connector": path, "status": "offline", "reported": False}
        self.state = state
        self.ready_path = state / ("ready-" + uuid.uuid4().hex + ".json")

    def tick(self, now):
        if self.process is not None and self.process.poll() is not None:
            self.failures = 0 if now - self.started > 60 else self.failures + 1
            self.next_start = now + min(60, 2 ** min(self.failures, 6))
            self.process = None
            self.ready_path.unlink(missing_ok=True)
        if self.process is None and now >= self.next_start:
            # Child output is redirected by the supervisor to its local log.
            try:
                self.process = subprocess.Popen([sys.executable, "-m", "raincli_agent", "connector", "run", "--config", self.path, "--runtime-ready", str(self.ready_path)],
                                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.started = now
            except OSError:
                self.next_start = now + 30
        running = self.process is not None and self.process.poll() is None
        ready = False
        if running:
            try:
                ready = json.loads(read_private_file(self.ready_path))["pid"] == self.process.pid
            except (ConfigError, ValueError, KeyError):
                pass
        state = availability(self.cfg, self.herdr, running and ready)
        self.report = {"connector": self.path, "status": state, "reported": False,
                       "process_running": running, "child_pid": self.process.pid if running else None}
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
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.ready_path.unlink(missing_ok=True)
        try:
            self.api.publish_presence("offline")
        except Exception:
            pass  # server expiry handles shutdown while disconnected


def run(path, once=False):
    path, state, configs = load_runtime(path)
    queue = Queue(str(state))
    queue.acquire_run_lock()
    stop = threading.Event()
    previous = {}
    workers = []
    instance = uuid.uuid4().hex
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
        workers = [Worker(p, cfg, identity, state) for p, cfg, identity in configs]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(workers)) as pool:
            while not stop.is_set():
                reports = list(pool.map(lambda w: w.tick(time.monotonic()), workers))
                atomic_write_json(state / "status.json", {"pid": os.getpid(), "instance": instance, "updated_at": time.time(), "connectors": reports})
                if once:
                    break
                for _ in range(30):
                    if stop.wait(1):
                        break
                    try:
                        request = json.loads(read_private_file(state / "stop.json"))
                        if request.get("instance") == instance:
                            stop.set()
                            break
                    except (ConfigError, ValueError):
                        pass
    finally:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(workers))) as pool:
            list(pool.map(lambda w: w.stop(), workers))
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        atomic_write_json(state / "status.json", {"pid": os.getpid(), "instance": instance,
                          "updated_at": time.time(), "status": "stopped", "connectors": []})
        queue.release_run_lock()


def status(path):
    _, state, _ = load_runtime(path)
    try:
        data = json.loads(read_private_file(state / "status.json", "runtime status"))
    except ConfigError:
        return {"status": "not_observed"}
    data["stale"] = time.time() - data["updated_at"] > 120
    return data


def request_stop(path):
    _, state, _ = load_runtime(path)
    data = json.loads(read_private_file(state / "status.json", "runtime status"))
    atomic_write_json(state / "stop.json", {"instance": data["instance"]})
    return {"status": "stop_requested"}
