"""The Herdr boundary (protocol section 5).

The connector only talks to Herdr through ``HerdrBoundary``: ``get_agent(name)``
and ``prompt(name, text, timeout)``. The real implementation runs the ``herdr``
CLI with argv lists (never a shell) and a timeout. There is deliberately no
way to address the focused or current pane: every call names its target.
"""

import json
import os
import subprocess
import threading
from dataclasses import dataclass, field, replace

READY_STATUSES = ("idle", "done")


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

    def __init__(self, reason, message=""):
        super().__init__(message or reason)
        self.reason = reason  # hold reason: "blocked" or "offline"


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
    """Real boundary: ``herdr agent get`` and ``herdr agent prompt``."""

    def __init__(self, binary="herdr", timeout=10.0, own_session=False):
        self.binary = binary
        self.timeout = timeout
        # POSIX only: keep a terminal Ctrl-C from reaching a prompt in flight; the
        # supervised connector finishes it and then stops. Windows is unchanged.
        self.own_session = own_session and os.name != "nt"

    def _run(self, argv, timeout):
        try:
            return subprocess.run([self.binary, *argv], capture_output=True, text=True,
                                  timeout=timeout, shell=False, stdin=subprocess.DEVNULL,
                                  start_new_session=self.own_session)
        except subprocess.TimeoutExpired:
            raise HerdrTimeout(f"herdr {argv[0]} {argv[1]} timed out after {timeout}s") from None
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
        if proc.returncode == 2 and not proc.stdout:
            # Clap usage error: the command was never executed.
            raise HerdrRejected("offline", "herdr rejected the prompt arguments")
        raise HerdrError(f"herdr agent prompt failed ({code or proc.returncode}): {message}")

    def notify(self, title, body):
        # --body=<value>: a value starting with "-" can never be parsed as a flag.
        proc = self._run(["notification", "show", title, f"--body={body}"], self.timeout)
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
