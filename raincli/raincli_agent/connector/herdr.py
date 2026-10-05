"""The Herdr boundary (protocol section 5).

The connector only talks to Herdr through ``HerdrBoundary``: ``get_agent(name)``
and ``prompt(name, text, timeout)``. The real implementation runs the ``herdr``
CLI with argv lists (never a shell) and a timeout. There is deliberately no
way to address the focused or current pane: every call names its target.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field, replace

READY_STATUSES = ("idle", "done")
# A Herdr session name: passed as `--session NAME` on every call. It never starts
# with "-", so it can't be read as an option.
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Windows limits a command line to 32,767 UTF-16 units. A prompt whose whole
# command line (as subprocess.list2cmdline quotes it) would exceed this is held,
# never truncated. The same bound applies on every platform, so behaviour is
# predictable.
CMDLINE_CAP = 30_000
# Herdr's install.ps1 keeps this directory a junction to the active release
# (`$defaultVisibleBinDir = Join-Path $env:LOCALAPPDATA "Programs\Herdr\bin"`,
# Set-ManagedJunction; the release holds herdr.exe), so the path survives
# `herdr update` while the versioned release directory on PATH does not.
WINDOWS_ALIAS = ("Programs", "Herdr", "bin", "herdr.exe")


MIN_VERSION = (0, 9, 3)  # agent_not_ready is a pre-send refusal from this version (src/app/api/agents.rs)
SCRIPT_SUFFIXES = (".bat", ".cmd")


def windows_script(path):
    """A batch file runs through cmd.exe, whose parsing argv quoting cannot protect
    (a teammate's message would reach cmd.exe): never accepted as Herdr."""
    return str(path).lower().endswith(SCRIPT_SUFFIXES)


def resolve_herdr_bin(configured="herdr", env=None, windows=None):
    """The absolute path of the Herdr executable to run (Phase 2, review H1/H2).

    - An explicit absolute path wins; on Windows it must be a ``.exe``.
    - On Windows the default name prefers the stable alias
      %LOCALAPPDATA%\\Programs\\Herdr\\bin\\herdr.exe when it exists.
    - Otherwise the absolute PATH entries are searched, in order, for exactly
      ``herdr.exe`` (Windows; never through PATHEXT) or an executable ``herdr``.
      Empty, ``.`` and relative entries are skipped: the current directory is never searched.

    Raises ``HerdrError`` when nothing is found (the message is then held offline);
    there is no bare-name fallback."""
    env = os.environ if env is None else env
    windows = os.name == "nt" if windows is None else windows
    configured = configured or "herdr"
    expanded = os.path.expanduser(configured)
    if windows and windows_script(expanded):
        raise HerdrError(f"herdr_bin {configured!r} is a batch file; on Windows only a .exe is run")
    if os.path.isabs(expanded):
        if windows and not expanded.lower().endswith(".exe"):
            raise HerdrError(f"herdr_bin {configured!r} is not a .exe; on Windows only a .exe is run")
        return expanded
    if os.sep in configured or (os.altsep and os.altsep in configured) or "/" in configured:
        raise HerdrError(f"herdr_bin {configured!r} must be an absolute path or a command name")
    name = configured
    if windows:
        stem = name[:-4] if name.lower().endswith(".exe") else name
        if "." in stem and not name.lower().endswith(".exe"):
            raise HerdrError(f"herdr_bin {configured!r}: on Windows only a .exe is run")
        name = stem + ".exe"
        if stem.lower() == "herdr" and env.get("LOCALAPPDATA") and os.path.isabs(env["LOCALAPPDATA"]):
            alias = Path(env["LOCALAPPDATA"]).joinpath(*WINDOWS_ALIAS)
            if alias.is_file():
                return str(alias)
    for entry in (env.get("PATH") or "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or entry == "." or not os.path.isabs(entry):
            continue
        candidate = os.path.join(entry, name)
        if os.path.isfile(candidate) and (windows or os.access(candidate, os.X_OK)):
            return candidate
    raise HerdrError(f"herdr executable {name!r} not found on PATH (absolute entries only)")


def parse_version(text):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def command_line_length(argv):
    """UTF-16 length of ``argv`` as Windows receives it (subprocess.list2cmdline)."""
    line = subprocess.list2cmdline([str(a) for a in argv])
    return len(line) + sum(1 for ch in line if ord(ch) > 0xFFFF)


@dataclass(frozen=True)
class AgentInfo:
    name: str
    status: str
    pane_id: str = ""
    cwd: str = ""


class HerdrError(Exception):
    """Herdr failed in a way that does not prove whether input was sent."""


class HerdrTimeout(HerdrError):
    """A Herdr call exceeded its timeout. A prompt may or may not have landed."""


class HerdrRejected(HerdrError):
    """Herdr refused a prompt before sending any input (definitely not delivered)."""

    def __init__(self, reason, message="", code=""):
        super().__init__(message or reason)
        self.code = code  # Herdr's error code, when there is one
        self.reason = reason  # hold reason: "blocked", "offline" or "too_large_for_command_line"


class HerdrBoundary:
    def get_agent(self, name):
        """Return ``AgentInfo`` for the named live agent, or None if it does not exist."""
        raise NotImplementedError

    def prompt(self, name, text, timeout):
        """Submit ``text`` to the named agent. Raise HerdrRejected, HerdrTimeout or HerdrError."""
        raise NotImplementedError

    def notify(self, title, body):
        """Show a visible Herdr notification. Raise HerdrError on failure."""
        raise NotImplementedError

    def list_agents(self):
        """Every live agent as ``{name, kind, status, terminal_id, cwd}``. Raise HerdrError."""
        raise NotImplementedError

    def command_line_fits(self, name, text):
        """Whether a prompt of ``text`` to ``name`` fits the command-line bound."""
        return True


def _find_agent_dict(obj):
    if isinstance(obj, dict):
        if "agent_status" in obj:
            return obj
        for value in obj.values():
            found = _find_agent_dict(value)
            if found is not None:
                return found
    return None


def _error_code(stderr):
    """Best-effort extraction of Herdr's JSON error code from stderr."""
    try:
        data = json.loads(stderr)
    except (ValueError, TypeError):
        return "", (stderr or "").strip()[:200]
    if isinstance(data, dict):
        err = data.get("error", data)
        if isinstance(err, dict):
            return str(err.get("code") or ""), str(err.get("message") or "")[:200]
        if isinstance(err, str):
            return err, ""
    return "", ""


NOT_FOUND_CODE = "agent_not_found"  # verified: `herdr agent get <missing>` exits 1 with this code


def _is_not_found(code, message):
    return code == NOT_FOUND_CODE


class HerdrCli(HerdrBoundary):
    """Real boundary: ``herdr agent get`` and ``herdr agent prompt``.

    Output is decoded as UTF-8 (undecodable bytes replaced), never the ANSI code
    page. On Windows each call runs without a console window and in its own
    process group, so a console Ctrl-C never reaches a prompt in flight."""

    def __init__(self, binary="herdr", timeout=10.0, own_session=False, session=None, resolve=True):
        self.configured = binary or "herdr"
        self.binary = None if resolve else binary
        if resolve:
            self.resolve()  # once per connector start; a miss is retried at the next call
        self.timeout = timeout
        if session is not None and not SESSION_RE.fullmatch(session):
            raise ValueError("invalid herdr session name")
        self.session = session or None
        self._version = None
        # POSIX only: keep a terminal Ctrl-C from reaching a prompt in flight; the
        # supervised connector finishes it and then stops. Windows uses a new process group.
        self.own_session = own_session and os.name != "nt"

    def resolve(self):
        """Resolve the executable again (each connector start). Returns the error, if any."""
        try:
            self.binary = resolve_herdr_bin(self.configured)
            self._version = None
            return None
        except HerdrError as exc:
            self.binary = None
            return exc

    def version(self):
        """``herdr --version`` as a tuple, cached per connector start (None if unknown)."""
        if self._version is None:
            try:
                proc = self._run(["--version"], self.timeout, plain=True)
                self._version = parse_version(proc.stdout) or ()
            except HerdrError:
                return None
        return self._version or None

    def argv(self, args, plain=False):
        session = ["--session", self.session] if self.session and not plain else []
        return [self.binary or self.configured, *session, *args]

    def command_line_fits(self, name, text):
        return command_line_length(self.argv(["agent", "prompt", name, text])) <= CMDLINE_CAP

    def _run(self, argv, timeout, plain=False):
        if self.binary is None:
            error = self.resolve()
            if error is not None:
                raise error
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            return subprocess.run(self.argv(argv, plain), capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout, shell=False, stdin=subprocess.DEVNULL,
                                  start_new_session=self.own_session, creationflags=flags)
        except subprocess.TimeoutExpired:
            raise HerdrTimeout(f"herdr {' '.join(argv[:2])} timed out after {timeout}s") from None
        except (UnicodeDecodeError, ValueError) as exc:
            raise HerdrError(f"herdr {' '.join(argv[:2])} output could not be read: {type(exc).__name__}") from None
        except OSError as exc:
            raise HerdrError(f"cannot run herdr: {exc.strerror}") from None

    def get_agent(self, name):
        proc = self._run(["agent", "get", name], self.timeout)
        if proc.returncode != 0:
            code, message = _error_code(proc.stderr)
            if _is_not_found(code, message):
                return None
            raise HerdrError(f"herdr agent get failed ({code or proc.returncode}): {message}")
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            raise HerdrError("herdr agent get returned non-JSON output") from None
        agent = _find_agent_dict(data)
        if agent is None:
            return None
        return AgentInfo(name=name, status=str(agent.get("agent_status") or "unknown"),
                         pane_id=str(agent.get("pane_id") or ""), cwd=str(agent.get("cwd") or ""))

    def list_agents(self):
        proc = self._run(["agent", "list"], self.timeout)
        if proc.returncode != 0:
            code, message = _error_code(proc.stderr)
            raise HerdrError(f"herdr agent list failed ({code or proc.returncode}): {message}")
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            raise HerdrError("herdr agent list returned non-JSON output") from None
        agents = data.get("result", {}).get("agents") if isinstance(data, dict) else None
        if not isinstance(agents, list):
            raise HerdrError("herdr agent list returned no agent list")
        out = []
        for agent in agents:
            if isinstance(agent, dict):
                # "name" is absent for an unnamed agent; "agent" is its kind (claude, codex, ...).
                out.append({"name": agent.get("name") if isinstance(agent.get("name"), str) else None,
                            "kind": str(agent.get("agent") or ""), "status": str(agent.get("agent_status") or ""),
                            "terminal_id": str(agent.get("terminal_id") or ""), "cwd": str(agent.get("cwd") or "")})
        return out

    def prompt(self, name, text, timeout):
        if not self.command_line_fits(name, text):
            raise HerdrRejected("too_large_for_command_line",
                                f"the prompt's command line would exceed {CMDLINE_CAP} characters")
        proc = self._run(["agent", "prompt", name, text], timeout)
        if proc.returncode == 0:
            return
        # Only exact, documented pre-submission refusals count as "not sent".
        # Anything else may have delivered input, so it becomes uncertain.
        code, message = _error_code(proc.stderr)
        if proc.returncode == 1 and code == "agent_blocked":
            raise HerdrRejected("blocked", "herdr rejected the prompt: agent_blocked")
        if proc.returncode == 1 and code == NOT_FOUND_CODE:
            raise HerdrRejected("offline", "herdr rejected the prompt: agent_not_found")
        if proc.returncode == 1 and code == "agent_not_ready":
            version = self.version()
            if version is not None and version >= MIN_VERSION:
                # Herdr 0.9.3 src/app/api/agents.rs (handle_agent_prompt): returned before any
                # input is sent, when the pane's foreground process is not the named agent.
                raise HerdrRejected("offline", "herdr rejected the prompt: agent_not_ready", code="agent_not_ready")
            # Older or unknown Herdr: not verified to be a pre-send refusal.
            raise HerdrError(f"herdr agent prompt failed (agent_not_ready, herdr {version or 'unknown'}): {message}")
        if proc.returncode == 2 and not proc.stdout:
            # Clap usage error: the command was never executed.
            raise HerdrRejected("offline", "herdr rejected the prompt arguments")
        raise HerdrError(f"herdr agent prompt failed ({code or proc.returncode}): {message}")

    def notify(self, title, body):
        # Herdr rejects "--body=<value>" ("unknown option"). It takes the argument after
        # --body as the value even when it starts with "-", so keep them separate.
        proc = self._run(["notification", "show", title, "--body", body], self.timeout)
        if proc.returncode != 0:
            code, message = _error_code(proc.stderr)
            raise HerdrError(f"herdr notification show failed ({code or proc.returncode}): {message}")


class SimulatedCrash(BaseException):
    """Raised by FakeHerdr to simulate the connector process dying mid-submission."""


@dataclass
class FakeHerdr(HerdrBoundary):
    """In-memory boundary for tests. Records every call; never touches a real pane.

    ``prompt_results`` is a queue of outcomes for the next prompts: ``"ok"``,
    ``"timeout"``, ``"blocked"``, ``"offline"``, ``"error"`` or ``"crash"``.
    Default outcome is ``"ok"``, after which the agent becomes ``working`` when
    ``busy_after_prompt`` is true.
    """

    agents: dict = field(default_factory=dict)
    prompt_results: list = field(default_factory=list)
    busy_after_prompt: bool = False
    get_error: Exception = None
    notify_error: Exception = None
    prompts: list = field(default_factory=list)
    notifications: list = field(default_factory=list)
    get_calls: list = field(default_factory=list)
    max_prompt_chars: int = None  # simulate the command-line bound

    def command_line_fits(self, name, text):
        return self.max_prompt_chars is None or len(text) <= self.max_prompt_chars

    def __post_init__(self):
        self._lock = threading.Lock()

    list_error: Exception = None

    def add(self, name, status="idle", pane_id="w9:p1", cwd="/work", focused=False, kind="claude"):
        self.agents[name] = {"info": AgentInfo(name, status, pane_id, cwd), "focused": focused, "kind": kind}

    def list_agents(self):
        with self._lock:
            if self.list_error is not None:
                raise self.list_error
            return [{"name": name, "kind": entry.get("kind", "claude"), "status": entry["info"].status,
                     "terminal_id": "t-" + name, "cwd": entry["info"].cwd} for name, entry in self.agents.items()]

    def set_status(self, name, status):
        self.agents[name]["info"] = replace(self.agents[name]["info"], status=status)

    def remove(self, name):
        self.agents.pop(name, None)

    def get_agent(self, name):
        with self._lock:
            self.get_calls.append(name)
            if self.get_error is not None:
                raise self.get_error
            entry = self.agents.get(name)
            return entry["info"] if entry else None

    def prompt(self, name, text, timeout):
        with self._lock:
            outcome = self.prompt_results.pop(0) if self.prompt_results else "ok"
            if outcome in ("blocked", "offline"):
                raise HerdrRejected(outcome)
            if name not in self.agents:
                raise HerdrRejected("offline")
            self.prompts.append((name, text, timeout))
            if outcome == "timeout":
                raise HerdrTimeout("fake timeout")
            if outcome == "error":
                raise HerdrError("fake error")
            if outcome == "crash":
                raise SimulatedCrash()
            if self.busy_after_prompt:
                self.set_status(name, "working")

    def notify(self, title, body):
        with self._lock:
            self.notifications.append((title, body))
            if self.notify_error is not None:
                raise self.notify_error
