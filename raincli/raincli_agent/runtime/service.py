"""A small supervisor; the connector remains responsible for durable delivery."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
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
from ..fsutil import atomic_write_json, ensure_private_dir, read_file_bytes, read_private_file
from . import discovery, sessions

LOG_LIMIT = 1024 * 1024  # per connector log; one rotated generation is kept
# Graceful-stop budget. A supervised connector long-polls in slices of at most
# SUPERVISED_POLL_WAIT and starts no submission once asked to stop, so it exits
# within one slice plus one in-flight prompt. Capping prompt_timeout bounds that:
# per connector <= 5 + 60 + 15 = 80 s; the runtime adds an in-flight tick and an
# offline publish (<= 100 s). The launcher (120 s) and the systemd unit (150 s)
# allow more.
SUPERVISED_POLL_WAIT = 5
MAX_PROMPT_TIMEOUT = 60
STOP_MARGIN = 15


def file_sha256(path):
    try:
        return hashlib.sha256(read_file_bytes(path)).hexdigest()
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
    if cfg.prompt_timeout > MAX_PROMPT_TIMEOUT:
        raise ConfigError(f"runtime connectors need prompt_timeout <= {MAX_PROMPT_TIMEOUT} s, "
                          "so a stop can let an in-flight delivery finish")
    if not cfg.agent_config:
        raise ConfigError("each runtime connector must name its agent_config explicitly")
    agent_sha = file_sha256(cfg.agent_config)
    identity = load_config(cfg.agent_config)
    binding = fingerprint(config_path, cfg.agent_config)
    if (config_sha, agent_sha) != (binding["config_sha256"], binding["agent_config_sha256"]) or None in (config_sha, agent_sha):
        raise ConfigError("connector config changed while loading; retry")
    return cfg, identity, binding


def _read_runtime(path):
    """Parse a runtime config: ``connectors`` (connector mode) or ``machine_config``
    (machine mode, 15.4), exactly one of them, and ``state_dir``. Returns
    ``(path, connector entries, resolver, machine config path or None, state_dir)``."""
    path = Path(path).expanduser().resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read runtime config: {exc}") from None
    machine_only = {"herdr_bin", "herdr_session", "trust_mode", "trusted_senders", "blocked_senders", "owner_email"}
    if not isinstance(data, dict) or set(data) - {"connectors", "machine_config", "state_dir"} - machine_only:
        raise ConfigError("runtime config supports only connectors, machine_config, state_dir, "
                          "herdr_bin, herdr_session, trust_mode, trusted_senders, blocked_senders and owner_email")
    if (machine_only & set(data)) and "machine_config" not in data:
        raise ConfigError("runtime herdr_bin, herdr_session and trust settings apply to machine mode; "
                          "connectors set their own")
    def absolute(p):
        return (path.parent / Path(p).expanduser()).resolve()
    entries, machine = data.get("connectors"), data.get("machine_config")
    if ("connectors" in data) == ("machine_config" in data):
        raise ConfigError("runtime config needs exactly one of machine_config or a non-empty connectors list")
    if machine is not None:
        if not isinstance(machine, str) or not machine:
            raise ConfigError("runtime machine_config must be the path of the machine's agent config")
        entries, machine = [], absolute(machine)
    elif not isinstance(entries, list) or not 1 <= len(entries) <= 16 or not all(isinstance(p, str) and p for p in entries):
        raise ConfigError("runtime connectors must contain 1-16 explicit connector config paths")
    state = data.get("state_dir", "runtime-state")
    if not isinstance(state, str) or not state:
        raise ConfigError("runtime state_dir must be a path")
    return path, entries, absolute, machine, absolute(state)


def machine_options(runtime_path):
    """Machine mode's Herdr options from runtime.json: ``{"herdr_bin", "herdr_session"}``."""
    from ..connector.config import _herdr_session
    try:
        data = json.loads(Path(runtime_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read runtime config: {exc}") from None
    data = data if isinstance(data, dict) else {}
    from ..connector.config import _herdr_bin
    return {"herdr_bin": _herdr_bin(data, "runtime config"), "herdr_session": _herdr_session(data, "runtime config")}


def load_machine(runtime_path):
    """Machine mode (15.4, §16.7): a connector with no inbox mapping, built from the
    runtime config itself and supervised like any other (it delivers to named agents)."""
    return (str(runtime_path), *load_bound(str(runtime_path)))


def load_runtime(path):
    """``(path, state_dir, configs)``; ``configs`` holds ``(path, connector config,
    identity, binding)`` per connector, or one entry with no connector config in
    machine mode."""
    path, entries, absolute, machine, state = _read_runtime(path)
    if machine is not None:
        machine_options(path)  # validated with the rest of the runtime config
        return path, state, [load_machine(path)]
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


def machine_mode(configs):
    return len(configs) == 1 and (configs[0][1] is None or getattr(configs[0][1], "machine", False))


HOOK_PRESENCE = {"idle": "ready", "working": "busy", "blocked": "blocked"}


def inbox_spec(cfg):
    """How the directory marks this connector's inbox (14.3); None in machine mode."""
    if cfg is None or not cfg.has_inbox:
        return None
    return ("hook", *cfg.inbox_hook) if cfg.inbox_hook else ("herdr", cfg.herdr_agent)


def availability(cfg, herdr, running, state_dir=None):
    if not running:
        return "offline"
    if not cfg.has_inbox:
        return "ready"  # machine mode: the connector runs; named agents report their own status
    if cfg.inbox_hook:
        # Next-turn inbox: the single live hook session of that type and name.
        live = sessions.live_sessions(state_dir, *cfg.inbox_hook) if state_dir else []
        if len(live) != 1:
            return "offline" if not live else "blocked"  # none, or ambiguous (held target_ambiguous)
        return HOOK_PRESENCE.get(live[0]["status"], "unknown")
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


def kill_tree(process):
    """Last resort. On Windows include descendants: a venv python.exe redirector
    would otherwise die alone and leave the interpreter holding the queue lock."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        process.terminate()
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            process.kill()
    process.wait(timeout=10)


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
        self.herdr = HerdrCli(cfg.herdr_bin, timeout=5, session=cfg.herdr_session or None)
        self.process = None
        self.next_start = 0
        self.failures = 0
        self.started = 0
        self.handle = None  # confirmed by the server for this credential
        self.retired = False
        self.reply = None  # the last presence reply (with the team's update target)
        self.report = {"connector": path, "status": "offline", "reported": False}
        self.state = state
        self._new_handshake()
        self.log_path = state / ("connector-" + hashlib.sha256(path.encode()).hexdigest()[:12] + ".log")
        self.owner_fd = None  # per-queue lock: one runtime per connector, whatever its state_dir

    def _new_handshake(self):
        # A fresh path per spawn: only the child started with it can confirm
        # readiness. Pids cannot be compared, because on Windows a venv's
        # python.exe is a redirector whose pid differs from the interpreter's.
        self.ready_path = self.state / ("ready-" + uuid.uuid4().hex + ".json")
        self.stop_path = Path(str(self.ready_path) + ".stop")

    def stop_budget(self):
        return min(self.cfg.poll_wait, SUPERVISED_POLL_WAIT) + self.cfg.prompt_timeout + STOP_MARGIN

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
        # Best-effort: on Windows a reader holding the log open (an editor, a tail)
        # blocks rotation or opening; that must never keep a connector from starting.
        try:
            if self.log_path.stat().st_size > LOG_LIMIT:
                os.replace(self.log_path, str(self.log_path) + ".1")
        except OSError:
            pass
        self._new_handshake()
        # Re-resolve the Herdr executable at each connector start (an update moves it).
        if isinstance(self.herdr, HerdrCli):
            self.herdr.resolve()  # a miss holds offline until Herdr is found
        try:
            log = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        except OSError:
            log = os.open(os.devnull, os.O_WRONLY)
        try:
            from .winapp import self_command
            return subprocess.Popen(self_command("connector", "run", "--config", self.path, "--runtime-ready", str(self.ready_path)),
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        finally:
            os.close(log)

    def _confirmed_ready(self):
        try:
            record = json.loads(read_private_file(self.ready_path))
        except (ConfigError, ValueError, UnicodeDecodeError):
            return False
        return (self.handle is not None and isinstance(record, dict) and isinstance(record.get("pid"), int)
                and {k: v for k, v in record.items() if k != "pid"} == {"handle": self.handle, **self.binding})

    def tick(self, now, agents=None, client=None):
        """One supervision step. ``agents`` is this handle's directory snapshot and
        ``client`` the runtime's version block (protocol 14.1); None omits them."""
        self.reply = None
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
        state = availability(self.cfg, self.herdr, running and self._confirmed_ready(), self.state)
        self.report.update(status=state, process_running=running, child_pid=self.process.pid if running else None)
        if self.changed():  # edited during this tick: never publish under a stale binding
            self.retire()
            return self._retired_report()
        try:
            self.reply = self.api.report_presence(state, agents, client)
            self.report.update(reported=True, expires_at=self.reply["presence"]["expires_at"],
                               agents=len(agents) if agents is not None else None)
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
                self.process.wait(timeout=self.stop_budget())
            except (OSError, subprocess.TimeoutExpired):
                kill_tree(self.process)
        self.process = None
        self.ready_path.unlink(missing_ok=True)
        self.stop_path.unlink(missing_ok=True)
        self._release()
        if self.handle is not None:
            try:
                # The directory empties at once rather than expiring (14.7 L11).
                self.api.report_presence("offline", [])
            except Exception:
                pass  # server expiry handles shutdown while disconnected


class MachineWorker:
    """Machine mode (15.4): no connector and no inbox. Publishes ``ready``, the
    client block and the directory under the machine credential, and carries the
    reply's update target. Messages to the machine stay stored on the server."""

    def __init__(self, path, identity, state, binding, options=None):
        self.path, self.cfg, self.binding, self.state = path, None, binding, state
        self.options = options or {"herdr_bin": "herdr", "herdr_session": ""}
        self.api = ApiClient.from_config(identity, timeout=5, max_attempts=1)
        self.herdr = HerdrCli(self.options["herdr_bin"], timeout=5,
                              session=self.options["herdr_session"] or None)
        self.handle = None
        self.retired = False
        self.reply = None
        self.report = {"machine_config": path, "status": "offline", "reported": False}

    def changed(self):
        return fingerprint(self.path, self.path) != self.binding

    def retire(self):
        if not self.retired:
            self.retired = True
            self.stop()

    def tick(self, now, agents=None, client=None):
        self.reply = None
        if self.retired or self.changed():
            self.retire()
            self.report = {"machine_config": self.path, "status": "offline", "reported": False,
                           "error": "config_changed"}
            return dict(self.report)
        self.report = {"machine_config": self.path, "status": "offline", "reported": False}
        if self.handle is None:
            try:
                self.handle = self.api.me()["agent"]["handle"]
            except Exception as exc:
                self.report["error"] = type(exc).__name__
                return dict(self.report)
        self.report.update(status="ready", handle=self.handle)
        try:
            self.reply = self.api.report_presence("ready", agents, client)
            self.report.update(reported=True, expires_at=self.reply["presence"]["expires_at"],
                               agents=len(agents) if agents is not None else None)
        except Exception as exc:
            self.report["error"] = type(exc).__name__
        return dict(self.report)

    def stop(self):
        if self.handle is not None:
            try:
                self.api.report_presence("offline", [])
            except Exception:
                pass


def make_worker(path, cfg, identity, state, binding, options=None):
    if cfg is None:
        return MachineWorker(path, identity, state, binding, options)
    return Worker(path, cfg, identity, state, binding)


class Supervisor:
    def __init__(self, path, state, configs, runtime_sha, salt=None):
        self.path, self.state, self.salt = path, state, salt
        options = machine_options(path) if machine_mode(configs) else None
        self.workers = [make_worker(p, cfg, identity, state, binding, options)
                        for p, cfg, identity, binding in configs]
        self.seen = (runtime_sha, tuple(w.binding for w in self.workers))
        self.error = self.reason = None

    def _snapshot(self):
        return file_sha256(self.path), tuple(
            fingerprint(w.path, w.cfg.agent_config if w.cfg else w.path) for w in self.workers)

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
        except ConfigError as exc:
            # Changed mappings stay retired (offline, unpublished) until a
            # further edit makes the whole runtime config valid again. Config
            # errors name paths and limits only; redact token-shaped text anyway.
            self.seen, self.error = snapshot, "config_invalid"
            self.reason = re.sub(r"rca_[A-Za-z0-9_-]+", "rca_<redacted>", str(exc))[:300]
            return
        current = {w.path: w for w in self.workers if not w.retired}
        workers = []
        options = machine_options(self.path) if machine_mode(configs) else None
        for p, cfg, identity, binding in configs:
            worker = current.pop(p, None)
            if worker is None or worker.binding != binding or getattr(worker, "options", None) != options:
                if worker is not None:
                    worker.retire()
                worker = make_worker(p, cfg, identity, state, binding, options)
            workers.append(worker)
        list(pool.map(lambda w: w.retire(), current.values()))  # removed from the runtime config
        self.workers, self.error, self.reason = workers, None, None
        self.seen = (snapshot[0], tuple(w.binding for w in workers))

    def directories(self):
        """Each live worker's directory snapshot, or None when there is no salt.

        The first connector's credential is the machine credential: it publishes
        every session found on this machine. Any further connector publishes only
        its own inbox, so a session is never listed twice under this machine."""
        live = [w for w in self.workers if not w.retired]
        if self.salt is None or not live:
            return {}
        out = {}
        for index, worker in enumerate(live):
            spec = inbox_spec(worker.cfg)
            try:
                found = discovery.discover(str(self.state), self.salt, worker.herdr, spec, include_scan=index == 0)
            except Exception:
                found = None  # discovery is advisory; the report then leaves the directory unchanged
            if found is not None and index:
                found = [a for a in found if a["role"] == "inbox"]
            out[id(worker)] = found
        return out


HANDSHAKE = re.compile(r"ready-[0-9a-f]{32}\.json(\.stop)?")


def clean_handshakes(state):
    """Remove handshake files left by a crashed runtime. Called while holding the
    state directory's run lock, before any worker exists, so none is live."""
    for entry in os.scandir(state):
        if HANDSHAKE.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
            try:
                os.unlink(entry.path)
            except FileNotFoundError:
                pass


def stop_requested(state, instance):
    try:
        request = json.loads(read_private_file(state / "stop.json"))
    except (ConfigError, ValueError, UnicodeDecodeError):
        return False
    return isinstance(request, dict) and request.get("instance") == instance


def repair_hooks(state):
    """§16.19 item 6: on the first start of a new client version, bring our hook entries to the
    current form. Never fatal; the log gets each change (paths only) and any notice."""
    from .. import __version__
    from . import hooks_install
    log = lambda text: print(text, file=sys.stderr, flush=True)  # noqa: E731 - the runtime log
    try:
        hooks_install.repair_on_version_change(str(state), __version__, log=log)
        notice = _hooks_notice(state)
        if notice:
            log(f"notice: {notice}")
    except Exception as exc:  # noqa: BLE001 - a repair never stops the runtime
        log(f"hooks: repair failed ({type(exc).__name__})")


def notification_feed(configs):
    """Machine mode with a person session: long-poll the person inbox into the app's
    notification queue (§16.10). The feed idles while there is no session."""
    cfg = configs[0][1] if configs else None
    if cfg is None or not getattr(cfg, "machine", False) or not cfg.agent_config:
        return None
    from .. import person
    feed = person.NotificationFeed(cfg.agent_config, log=lambda text: print(text, file=sys.stderr, flush=True))
    feed.start()
    return feed


def run(path, once=False, pushed=None):
    from .pushed import PushedUpdates
    from . import updates
    path = Path(path).expanduser().resolve()
    runtime_sha = file_sha256(path)
    path, state, configs = load_runtime(path)
    queue = Queue(str(state))
    queue.acquire_run_lock()
    # The per-machine salt and the hook sessions directory (14.7 H3, M7).
    salt = sessions.ensure_salt(str(state))
    sessions.sessions_dir(str(state), create=True)
    repair_hooks(state)
    pushed = pushed or PushedUpdates()
    pushed.machine_mode = machine_mode(configs)
    pushed.state_dir = state
    if pushed.managed():
        notice = updates.migrate_mode(pushed.root)
        if notice:
            print(notice, file=sys.stderr, flush=True)  # the runtime log or journal keeps it
    stop = threading.Event()
    previous = {}
    supervisor = feed = None
    instance = uuid.uuid4().hex

    def record(status, connectors):
        data = {"pid": os.getpid(), "instance": instance, "updated_at": time.time(), "status": status,
                "connectors": connectors}
        if supervisor is not None and supervisor.error:
            data["error"], data["error_reason"] = supervisor.error, supervisor.reason
        data["client"] = pushed.client()  # version, update mode and state, as reported
        try:
            atomic_write_json(state / "status.json", data)
        except OSError:
            pass  # status is advisory: never stop supervising over it; the next tick rewrites it
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
        clean_handshakes(state)
        # Publish this instance before any slow first tick, so a stop request
        # made during startup targets it rather than a previous run.
        record("starting", [])
        supervisor = Supervisor(path, state, configs, runtime_sha, salt)
        feed = None if once else notification_feed(configs)
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            while not stop.is_set():
                supervisor.refresh(pool)
                pushed.machine_mode = bool(supervisor.workers) and all(
                    w.cfg is None or getattr(w.cfg, "machine", False) for w in supervisor.workers)
                directories = supervisor.directories()
                client = pushed.client()
                futures = [pool.submit(w.tick, time.monotonic(), directories.get(id(w)), client)
                           for w in supervisor.workers]
                while concurrent.futures.wait(futures, timeout=1).not_done:
                    if stop_requested(state, instance):
                        stop.set()  # honoured as soon as the in-flight ticks return
                if stop_requested(state, instance):
                    stop.set()
                record("running", [f.result() for f in futures])
                pushed.started()
                # The machine credential's team target (14.5); acted on in the background.
                first = next((w for w in supervisor.workers if not w.retired), None)
                if first is not None and first.reply is not None and not once:
                    pushed.consider(first.reply.get("target"))
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
        if feed is not None:
            feed.stop.set()
        if stop.is_set() and not once and pushed.managed():
            # Asked to stop (a signal or `runtime stop`): tell the launcher this
            # exit is not a failed first start (review 2, O10).
            try:
                atomic_write_json(pushed.root / "stop-requested.json", {"at": time.time()})
            except OSError:
                pass
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
    notice = _hooks_notice(state)
    if notice:
        data["notice"] = notice  # §16.19 item 6: "trust them again" in Codex
    return data


def _hooks_notice(state):
    try:
        data = json.loads((Path(state) / "hooks-notice.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("message") if isinstance(data, dict) else None


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
