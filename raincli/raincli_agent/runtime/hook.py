"""``raincli hook <claude|codex> <event>``: a fast, network-free agent hook.

It records the session's status for the runtime's directory and, for Claude
Code's SessionStart/UserPromptSubmit, emits pending next-turn inbox messages as
additional context. Whatever happens it exits 0 within about 2 s and writes
nothing to stderr; its own errors go to a private log as short codes only (never
prompt, cwd or transcript fields).
"""
import json
import os
import sys
import threading
import time

from . import procinfo, sessions

STDIN_LIMIT = 1024 * 1024
DEADLINE = 2.0
LOG_LIMIT = 256 * 1024
EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "Notification", "PermissionRequest", "SessionEnd")
CLAIM_EVENTS = ("SessionStart", "UserPromptSubmit")
# Notification types that mean the session waits for its user (Claude Code 2.1.x).
NEEDS_INPUT = ("permission_prompt", "elicitation_dialog")


def status_for(event, payload, current):
    """The new status for an event; None removes the record (session end)."""
    if event == "SessionEnd":
        return None
    if event == "UserPromptSubmit":
        return "working"
    if event in ("SessionStart", "Stop"):
        return "idle"
    if event == "PermissionRequest":
        return "blocked"
    kind = payload.get("notification_type")
    if kind in NEEDS_INPUT:
        return "blocked"
    if kind == "idle_prompt":
        return "idle"
    return current or "idle"  # other notifications leave the status as it was


def session_name(override, payload, agent_type):
    """--name, then RAINCLI_AGENT_NAME, then the basename of the project directory."""
    name = override or os.environ.get("RAINCLI_AGENT_NAME") or sessions.basename(payload.get("cwd"))
    return sessions.normalize_name(name, agent_type)


def agent_pid(agent_type):
    """The agent process behind this hook (local only, for liveness and scan
    deduplication): Linux /proc, a Windows snapshot, or ps on macOS."""
    return procinfo.agent_pid(agent_type)


def log_code(state_dir, code):
    """Append ``<time> <code>`` to the private hook log (rotated, one generation)."""
    try:
        path = os.path.join(state_dir, "hook.log")
        try:
            if os.path.getsize(path) > LOG_LIMIT:
                os.replace(path, path + ".1")
        except OSError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {code}\n".encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def read_payload(stdin):
    raw = stdin.read(STDIN_LIMIT + 1)
    if len(raw) > STDIN_LIMIT:
        raise ValueError("stdin_too_large")
    payload = json.loads(raw.decode("utf-8") or "{}")
    if not isinstance(payload, dict):
        raise ValueError("payload_not_object")
    return payload


def handle(agent_type, event, name, state_dir, stdin, stdout, now=None):
    """The hook's work. Raises on errors; ``main`` turns every error into a log code."""
    state_dir = os.path.abspath(state_dir)
    salt = sessions.read_salt(state_dir)
    if salt is None:
        return "no_salt"  # the runtime has not set this machine up: do nothing
    payload = read_payload(stdin)
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not 1 <= len(session_id) <= 256:
        return "no_session_id"
    key = sessions.agent_key(salt, f"{agent_type}:{session_id}")
    if sessions.sessions_dir(state_dir, create=True) is None:
        return "no_sessions_dir"
    current = sessions.load_record(state_dir, key)
    status = status_for(event, payload, current and current["status"])
    if status is None:
        sessions.remove_record(state_dir, key)
        return "ended"
    record = {"key": key, "type": agent_type, "name": session_name(name, payload, agent_type), "status": status,
              "updated_at": time.time() if now is None else now}
    # A resumed session (claude --resume) keeps its session id but runs in a new
    # process: reuse the recorded one only while it is alive (review 2, O2).
    if current is not None and sessions.process_state(current) == "alive":
        for field in ("pid", "pid_start", "pid_ns"):
            if current.get(field) is not None:
                record[field] = current[field]
    else:
        try:
            pid = agent_pid(agent_type)
            start = sessions.process_start(pid) if pid else None
        except Exception:  # noqa: BLE001 - liveness is best effort; the record is written anyway
            pid = start = None
        if pid and start is not None:
            record.update(pid=pid, pid_start=start)  # local only: never reported
            namespace = procinfo.pid_namespace()
            if namespace:
                record["pid_ns"] = namespace
    sessions.write_record(state_dir, record)
    if agent_type != "claude" or event not in CLAIM_EVENTS:
        return "recorded"
    texts, ids = sessions.claim(state_dir, key)
    if not ids:
        return "recorded"
    output = {"hookSpecificOutput": {"hookEventName": event,
                                     "additionalContext": sessions.SEPARATOR.join(texts)}}
    stdout.write(json.dumps(output).encode("utf-8") + b"\n")
    stdout.flush()
    sessions.write_receipts(state_dir, key, ids)
    return "claimed"


def main(agent_type, event, name, state_dir):
    """Entry point for ``raincli hook``. Always returns 0."""
    # Hard deadline: never hold up the agent, even on a stuck filesystem.
    timer = threading.Timer(DEADLINE, lambda: os._exit(0))
    timer.daemon = True
    timer.start()
    try:
        if agent_type not in sessions.HOOK_TYPES or event not in EVENTS:
            log_code(state_dir, "unsupported_event")
            return 0
        handle(agent_type, event, name, state_dir, sys.stdin.buffer, sys.stdout.buffer)
    except ValueError as exc:
        code = str(exc) if str(exc) in ("stdin_too_large", "payload_not_object") else "invalid_payload"
        log_code(state_dir, code)
    except Exception as exc:  # noqa: BLE001 - a hook never fails its agent
        log_code(state_dir, "error:" + type(exc).__name__)
    finally:
        timer.cancel()
    return 0
