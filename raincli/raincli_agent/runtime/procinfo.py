"""Local process facts for hook liveness and the process scan.

Only an executable's name or path, the kernel's short process name (comm), the
parent pid and the start time are read; a command line never is (14.7 M5).
Nothing here leaves the machine. Linux uses /proc, Windows a Toolhelp snapshot
and GetProcessTimes, macOS and other POSIX systems ``ps``.
"""
import os
import subprocess
import sys

TYPES_BY_NAME = {
    "claude": "claude", "codex": "codex", "gemini": "gemini", "cursor-agent": "cursor", "opencode": "opencode",
}
# Processes a hook may run under between the agent and itself: shells, the Python of
# the managed launcher or the raincli entry point, and raincli's own executables (the
# Windows app's bin\raincli.exe shim, the pip entry-point launcher). Codex on Windows
# runs a hook as codex.exe -> cmd.exe /C -> raincli.exe -> raincli.exe.
PASS_THROUGH = {"sh", "bash", "dash", "zsh", "fish", "ksh", "mksh", "tcsh", "csh", "busybox", "env", "nu",
                "pwsh", "powershell", "cmd", "conhost", "raincli"}


def plain(name):
    """Lowercase basename without a Windows ``.exe`` suffix."""
    name = os.path.basename((name or "").replace("\\", "/")).lower()
    return name[:-4] if name.endswith(".exe") else name


def exe_name(path):
    """The executable's name, with a native Claude Code install (…/claude/versions/<n>)
    counted as ``claude`` (review 2, O1)."""
    parts = (path or "").replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 3 and parts[-2] == "versions" and parts[-3] == "claude":
        return "claude"
    name = plain(parts[-1] if parts else "")
    if name.startswith("codex-"):
        return "codex"  # a release binary as shipped, e.g. codex-x86_64-pc-windows-msvc.exe (§16.14 K2)
    return name


def kind_of(exe, comm=""):
    """The agent type of a process from its executable name or its comm, else None."""
    return TYPES_BY_NAME.get(exe_name(exe)) or TYPES_BY_NAME.get(plain(comm))


def passes_through(exe, comm=""):
    names = {exe_name(exe), plain(comm)} - {""}
    return any(n in PASS_THROUGH or n.startswith("python") for n in names)


# -- Linux -------------------------------------------------------------------------

def linux_info(pid, proc="/proc"):
    """(exe name, parent pid, comm). Raises OSError or ValueError."""
    with open(f"{proc}/{pid}/stat", "rb") as fh:
        parent = int(fh.read().rsplit(b")", 1)[1].split()[1])
    try:
        exe = exe_name(os.readlink(f"{proc}/{pid}/exe"))
    except OSError:
        exe = ""
    try:
        with open(f"{proc}/{pid}/comm", "rb") as fh:
            comm = fh.read(64).decode("utf-8", "replace").strip()
    except OSError:
        comm = ""
    return exe, parent, comm


def linux_start(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            return int(fh.read().rsplit(b")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def pid_namespace():
    """This process's pid namespace (Linux), so pids from another namespace are
    never compared with this /proc (review 2, O6)."""
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


# -- macOS and other POSIX: ps ---------------------------------------------------------

def ps_env():
    """A fixed locale and time zone: ``lstart`` is printed in local time and the
    local language, and the hook (a terminal's TZ and LANG) and the runtime (a
    login service's) must read the same start time (review 3, N1)."""
    return {"LC_ALL": "C", "LANG": "C", "TZ": "UTC", "PATH": os.environ.get("PATH") or "/bin:/usr/bin"}


def _ps(pid, fields):
    out = subprocess.run(["ps", "-o", fields, "-p", str(int(pid))], capture_output=True, text=True, timeout=2,
                         stdin=subprocess.DEVNULL, env=ps_env())
    return out.stdout.strip() if out.returncode == 0 else None


def parse_ps_info(line):
    """``ps -o ppid=,comm=``: the parent pid, then the executable (a full path on macOS)."""
    parent, _, command = (line or "").strip().partition(" ")
    command = command.strip()
    return exe_name(command), int(parent), plain(command)


def ps_info(pid):
    line = _ps(pid, "ppid=,comm=")
    if not line:
        raise OSError("no such process")
    return parse_ps_info(line)


def ps_start(pid):
    return _ps(pid, "lstart=") or None


# -- Windows -----------------------------------------------------------------------------

def _windows():
    import ctypes as c
    from ctypes import wintypes as w

    class Entry(c.Structure):
        _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD), ("heap", c.c_void_p),
                    ("module", w.DWORD), ("threads", w.DWORD), ("parent", w.DWORD), ("priority", c.c_long),
                    ("flags", w.DWORD), ("exe", w.WCHAR * 260)]
    k = c.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes, k.CreateToolhelp32Snapshot.restype = [w.DWORD, w.DWORD], w.HANDLE
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [w.HANDLE, c.POINTER(Entry)]
    k.OpenProcess.argtypes, k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    k.GetProcessTimes.argtypes = [w.HANDLE] + [c.POINTER(w.FILETIME)] * 4
    k.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
    k.CloseHandle.argtypes = [w.HANDLE]
    return c, w, k, Entry


def windows_snapshot():
    """{pid: (exe name, parent pid)} for every process, from one Toolhelp snapshot."""
    c, w, k, Entry = _windows()
    snap = k.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if snap in (None, w.HANDLE(-1).value):
        raise OSError("process snapshot failed")
    try:
        out, entry = {}, Entry()
        entry.size = c.sizeof(Entry)
        ok = k.Process32FirstW(snap, c.byref(entry))
        while ok:
            out[entry.pid] = (plain(entry.exe), entry.parent)
            ok = k.Process32NextW(snap, c.byref(entry))
        return out
    finally:
        k.CloseHandle(snap)


def windows_start(pid, alive_only=True):
    """The process creation time, or None if it no longer runs."""
    c, w, k, _ = _windows()
    handle = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        code = w.DWORD()
        if alive_only and (not k.GetExitCodeProcess(handle, c.byref(code)) or code.value != 259):  # STILL_ACTIVE
            return None
        times = [w.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(handle, *[c.byref(t) for t in times]):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        k.CloseHandle(handle)


# -- portable ------------------------------------------------------------------------------

def process_start(pid):
    """An identity for a live process: its start time, or None."""
    if not pid:
        return None
    try:
        if sys.platform.startswith("linux"):
            return linux_start(pid)
        if os.name == "nt":
            return windows_start(pid)
        return ps_start(pid)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def agent_pid(agent_type, start=None):
    """The agent process that runs this hook, or None.

    Starts at the parent and passes only through shells and the launcher's
    Python; the first other process decides. It is the agent if it is of this
    type, and otherwise there is none: an unrelated ancestor further up (an
    editor, another agent) is never adopted (review 2, O1)."""
    pid = os.getppid() if start is None else start
    snapshot = None
    for _ in range(8):
        if not pid or pid <= 1:
            return None
        try:
            if sys.platform.startswith("linux"):
                exe, parent, comm = linux_info(pid)
            elif os.name == "nt":
                snapshot = snapshot if snapshot is not None else windows_snapshot()
                if pid not in snapshot:
                    return None
                exe, parent = snapshot[pid]
                comm = ""
            else:
                exe, parent, comm = ps_info(pid)
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return None
        kind = kind_of(exe, comm)
        if kind is not None:
            return pid if kind == agent_type else None
        if not passes_through(exe, comm):
            return None
        pid = parent
    return None


def process_state(record):
    """``alive``, ``dead``, or None when the record names no determinable process."""
    pid, start = record.get("pid"), record.get("pid_start")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or start is None:
        return None
    if sys.platform.startswith("linux"):
        namespace = record.get("pid_ns")
        if namespace is not None and namespace != pid_namespace():
            return None  # recorded in another pid namespace: this /proc cannot tell
    if not sys.platform.startswith("linux") and os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "dead"
        except PermissionError:
            pass
        except OSError:
            return None
        try:
            current = ps_start(pid)
        except (OSError, subprocess.SubprocessError):
            return None  # ps unavailable: not determinable, never "dead"
        if current is None:
            return None
        return "alive" if current == start else "dead"
    current = process_start(pid)
    return "alive" if current is not None and current == start else "dead"
