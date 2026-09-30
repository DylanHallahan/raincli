"""The machine agent directory (protocol 14.3 and 14.7): Herdr, hooks, then a scan.

Each entry is ``{key, name, type, status, role, reachability, source}`` and is
normalized here, because the server rejects a whole report over one bad entry.
Paths, cwds, titles, pane ids and pids never leave this module; only a
directory's basename may become a name.
"""
import csv
import io
import os
import subprocess
import sys

from ..connector.herdr import HerdrError
from . import sessions

MAX_AGENTS = 100
HERDR_STATUS = {"idle": "idle", "done": "idle", "working": "working", "blocked": "blocked"}
# Executable basenames per type for the process scan (basename only, 14.7 M5).
SCAN_EXECUTABLES = {
    "claude": ("claude", "claude.exe"),
    "codex": ("codex", "codex.exe"),
    "gemini": ("gemini", "gemini.exe"),
    "cursor": ("cursor-agent", "cursor-agent.exe"),
    "opencode": ("opencode", "opencode.exe"),
}
BY_EXECUTABLE = {exe: kind for kind, names in SCAN_EXECUTABLES.items() for exe in names}


def entry(salt, source_id, name, kind, status, source, role=None, reachability=None):
    kind = kind if kind in sessions.TYPES else "other"
    return {"key": sessions.agent_key(salt, source_id), "name": sessions.normalize_name(name, kind),
            "type": kind, "status": status, "role": role, "reachability": reachability, "source": source}


# -- Herdr ---------------------------------------------------------------------

def herdr_entries(salt, herdr, inbox):
    """(entries, ok). Every Herdr agent; the mapped inbox is marked instant."""
    inbox_name = inbox[1] if inbox and inbox[0] == "herdr" else None
    try:
        agents = herdr.list_agents()
    except HerdrError:
        agents, ok = [], False
    else:
        ok = True
    out, seen_inbox = [], False
    for agent in agents:
        name = agent.get("name")
        kind = agent.get("kind") or "other"
        status = HERDR_STATUS.get(agent.get("status"), "unknown")
        if name:
            source_id = "herdr:" + name
        elif agent.get("terminal_id"):
            # An unnamed agent: "#" never occurs in a Herdr name, so this id cannot collide.
            source_id = "herdr:#" + agent["terminal_id"]
            name = sessions.basename(agent.get("cwd")) or kind
        else:
            continue
        is_inbox = inbox_name is not None and name == inbox_name and not seen_inbox and agent.get("name")
        seen_inbox = seen_inbox or bool(is_inbox)
        out.append(entry(salt, source_id, name, kind, status, "herdr",
                         "inbox" if is_inbox else None, "instant" if is_inbox else None))
    if inbox_name and not seen_inbox:
        # The mapped inbox is not live (or Herdr could not be read): list it anyway.
        out.append(entry(salt, "herdr:" + inbox_name, inbox_name, "other", "offline" if ok else "unknown",
                         "herdr", "inbox", "instant"))
    return out, ok


# -- hooks ---------------------------------------------------------------------

def hook_entries(salt, state_dir, inbox, now=None):
    """(entries, claimed pids). A single live session of the mapped type and name
    is the next-turn inbox; with none live the inbox is listed offline."""
    records = sessions.read_sessions(state_dir, now)
    want = (inbox[1], inbox[2]) if inbox and inbox[0] == "hook" else None
    live = [r for r in records if want and (r["type"], r["name"]) == want and r["status"] != "offline"]
    out, pids = [], set()
    for record in records:
        if isinstance(record.get("pid"), int):
            pids.add(record["pid"])
        is_inbox = len(live) == 1 and record is live[0]
        out.append({"key": record["key"], "name": sessions.normalize_name(record["name"], record["type"]),
                    "type": record["type"], "status": record["status"], "role": "inbox" if is_inbox else None,
                    "reachability": "next-turn" if is_inbox else None, "source": "hook",
                    "_pid": record["pid"] if isinstance(record.get("pid"), int) else None})
    if want and not live:
        out.append(entry(salt, "hook-inbox:%s:%s" % want, want[1], want[0], "offline", "hook",
                         "inbox", "next-turn"))
    return out, pids


# -- process scan ------------------------------------------------------------------

def _read_stat_parent(proc, pid):
    with open(f"{proc}/{pid}/stat", "rb") as fh:
        return int(fh.read().rsplit(b")", 1)[1].split()[1])


def linux_processes(proc="/proc"):
    """Same-uid processes as {pid: (exe basename, parent pid, comm)}.

    Only the executable's basename and the kernel's short process name (comm)
    are read; the command line never is (14.7 M5)."""
    uid = os.getuid()
    out = {}
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            if os.stat(f"{proc}/{name}").st_uid != uid:
                continue
            parent = _read_stat_parent(proc, pid)
        except (OSError, ValueError, IndexError):
            continue
        try:
            exe = os.path.basename(os.readlink(f"{proc}/{name}/exe"))
        except OSError:
            exe = ""
        try:
            with open(f"{proc}/{name}/comm", "rb") as fh:
                comm = fh.read(64).decode("utf-8", "replace").strip()
        except OSError:
            comm = ""
        out[pid] = (exe, parent, comm)
    return out


def kind_of(entry):
    """The agent type of a process: its executable basename, or its comm. A native
    Claude Code install runs ``…/versions/<n>`` but is named ``claude``; a Node
    agent that sets its process title shows it in comm (review 1, finding 10)."""
    exe, _parent, comm = (tuple(entry) + (None, None))[:3]
    return BY_EXECUTABLE.get(exe) or BY_EXECUTABLE.get(comm or "")


def _cwd_name(pid):
    try:
        return sessions.basename(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return ""


def herdr_descendant(pid, processes):
    ancestor = processes.get(pid, (None, None))[1]
    for _ in range(32):
        if ancestor not in processes:
            return False
        if processes[ancestor][0] in ("herdr", "herdr.exe") or processes[ancestor][2:3] == ("herdr",):
            return True
        ancestor = processes[ancestor][1]
    return False


def linux_scan(salt, claimed_pids, herdr_ok, processes=None, cwd_name=_cwd_name):
    """Known agent processes not already reported by a hook record or Herdr.

    A process under a ``herdr`` server process runs in a Herdr pane, and when
    Herdr could be read Herdr reports it. Child processes of an agent of the same
    type are part of that agent."""
    processes = linux_processes() if processes is None else processes
    out = []
    for pid in sorted(processes):
        kind = kind_of(processes[pid])
        if kind is None or pid in claimed_pids:
            continue
        parent = processes[pid][1]
        if parent in processes and kind_of(processes[parent]) == kind:
            continue
        ancestor, owned = parent, False
        for _ in range(32):
            if ancestor not in processes:
                break
            if ancestor in claimed_pids:
                owned = True
            ancestor = processes[ancestor][1]
        if owned or (herdr_ok and herdr_descendant(pid, processes)):
            continue
        out.append(entry(salt, f"scan:{kind}:{pid}", cwd_name(pid) or kind, kind, "unknown", "scan"))
    return out


def windows_scan(salt, claimed_pids, run=subprocess.run):
    """Type-only: tasklist image names for this user's processes; the name is the type."""
    user = os.environ.get("USERNAME", "")
    if not user:
        return []  # never list other users' processes
    argv = ["tasklist", "/FO", "CSV", "/NH", "/FI", f"USERNAME eq {user}"]
    try:
        proc = run(argv, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return []
    out = []
    for row in csv.reader(io.StringIO(proc.stdout or "")):
        if len(row) < 2 or not row[1].isdigit():
            continue
        kind = BY_EXECUTABLE.get(row[0].lower())
        pid = int(row[1])
        if kind and pid not in claimed_pids:
            out.append(entry(salt, f"scan:{kind}:{pid}", kind, kind, "unknown", "scan"))
    return out


def scan(salt, claimed_pids, herdr_ok, processes=None):
    try:
        if sys.platform.startswith("linux"):
            return linux_scan(salt, claimed_pids, herdr_ok, processes)
        if os.name == "nt":
            return windows_scan(salt, claimed_pids)
    except OSError:
        pass
    return []


# -- the report --------------------------------------------------------------------

def normalize(entries):
    """Unique keys (first wins), at most one inbox, inbox first, at most 100 entries."""
    seen, out, inbox = set(), [], None
    for item in entries:
        if item["key"] in seen:
            continue
        seen.add(item["key"])
        if item["role"] == "inbox":
            if inbox is not None:
                item = {**item, "role": None, "reachability": None}
            else:
                inbox = item
                continue
        out.append(item)
    return ([inbox] if inbox else []) + out[:MAX_AGENTS - (1 if inbox else 0)]


def without_duplicates(hooked, scanned, herdr_ok, processes, inbox):
    """One listing per session (review 1, finding 11).

    * A hook record whose process runs in a Herdr pane is Herdr's to list, while
      Herdr is readable (unless it is the mapped next-turn inbox).
    * Where hook records carry no process id (Windows), each live hook record of a
      type accounts for one scanned process of that type."""
    if processes and herdr_ok:
        hooked = [h for h in hooked if h["role"] == "inbox" or not (
            isinstance(h.get("_pid"), int) and herdr_descendant(h["_pid"], processes))]
    unmatched = {}
    for h in hooked if os.name == "nt" else ():
        if h.get("_pid") is None and h["status"] != "offline":
            unmatched[h["type"]] = unmatched.get(h["type"], 0) + 1
    kept = []
    for item in scanned:
        if unmatched.get(item["type"]):
            unmatched[item["type"]] -= 1
            continue
        kept.append(item)
    return hooked, kept


def discover(state_dir, salt, herdr, inbox, now=None, include_scan=True):
    """The directory for one report. ``inbox`` is ("herdr", name),
    ("hook", type, name) or None (list sessions without marking an inbox)."""
    hooked, pids = hook_entries(salt, state_dir, inbox, now)
    herdr_found, herdr_ok = herdr_entries(salt, herdr, inbox) if herdr is not None else ([], False)
    processes = None
    if sys.platform.startswith("linux"):
        try:
            processes = linux_processes()
        except OSError:
            processes = None
    scanned = scan(salt, pids, herdr_ok, processes) if include_scan else []
    hooked, scanned = without_duplicates(hooked, scanned, herdr_ok, processes, inbox)
    for h in hooked:
        h.pop("_pid", None)  # local only: never reported
    return normalize(herdr_found + hooked + scanned)
