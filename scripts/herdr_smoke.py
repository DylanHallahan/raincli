"""Real Herdr delivery stage for scripts/runtime-platform-smoke.py (Phase 2).

A pinned Herdr release (sha256 from https://herdr.dev/latest.json, v0.9.3) runs a
headless server in a throwaway session ("raincli-smoke") with its own HOME/config
directories, so no live Herdr session or pane is ever touched; the server is
stopped at the end. A fake agent runs in a pane: Herdr detects it as Claude
because it is a Python script named "claude" (Herdr's wrapped-runtime
detection), it self-reports through `pane report-agent`, and it appends every
byte it receives to a file. The real runtime and connector processes deliver to
it through the fake RainCLI API.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

HERDR_VERSION = "0.9.3"
RELEASE = f"https://github.com/herdrdev/herdr/releases/download/v{HERDR_VERSION}/"
PINNED = {  # from https://herdr.dev/latest.json (version 0.9.3)
    "windows-x86_64": ("herdr-windows-x86_64.zip",
                       "c75b1fa49f7a3ba4b8b11789912a6147e4214a3b6fd3556d0f80076c8887d795"),
    "linux-x86_64": ("herdr-linux-x86_64",
                     "18a8dc65f1c2fa485884344356dea1cfd911c6f06cf46fa78e193f4087f4dba7"),
}
SESSION = "raincli-smoke"
SOURCE = "custom:raincli-smoke"

FAKE_AGENT = r'''
import os, subprocess, sys, time
out_dir = sys.argv[1]
received = os.path.join(out_dir, "received.bin")
herdr = os.environ["HERDR_BIN_PATH"]
pane = os.environ["HERDR_PANE_ID"]

def note(text):
    with open(os.path.join(out_dir, "fake.log"), "a", encoding="utf-8") as fh:
        fh.write(text + "\n")

def report(state):
    flags = 0x08000000 if os.name == "nt" else 0
    r = subprocess.run([herdr, "pane", "report-agent", pane, "--source", "custom:raincli-smoke",
                        "--agent", "claude", "--state", state], capture_output=True, creationflags=flags)
    note("report %s -> %s" % (state, r.returncode))

if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetStdHandle.restype = wintypes.HANDLE
    inp, outp = k.GetStdHandle(-10), k.GetStdHandle(-11)
    k.SetConsoleMode(inp, 0x0200)  # ENABLE_VIRTUAL_TERMINAL_INPUT only: raw VT input, no echo
    mode = wintypes.DWORD()
    k.GetConsoleMode(outp, ctypes.byref(mode))
    k.SetConsoleMode(outp, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING

    def read_chunk():
        buf = ctypes.create_unicode_buffer(16384)
        n = wintypes.DWORD()
        if not k.ReadConsoleW(inp, buf, 16384, ctypes.byref(n), None):
            raise OSError(ctypes.get_last_error())
        return buf[:n.value].encode("utf-8", "surrogatepass")
else:
    import tty
    tty.setraw(0)

    def read_chunk():
        return os.read(0, 65536)

sys.stdout.write("\x1b[?2004h")  # bracketed paste on, as a real agent does
sys.stdout.write("fake agent ready\r\n")
sys.stdout.flush()
report("idle")
pending = b""
while True:
    chunk = read_chunk()
    if not chunk:
        break
    with open(received, "ab") as fh:
        fh.write(chunk)
    pending += chunk
    if b"\x1b[201~" in pending and pending.rstrip(b"\n").endswith(b"\r"):
        pending = b""
        report("working")
        time.sleep(0.5)
        report("idle")
'''


def target():
    machine = platform.machine().lower()
    if os.name == "nt" and machine in ("amd64", "x86_64"):
        return "windows-x86_64"
    if sys.platform.startswith("linux") and machine in ("x86_64", "amd64"):
        return "linux-x86_64"
    return None


def fetch_herdr(work):
    """The pinned Herdr executable, checked against its pinned sha256."""
    key = target()
    if key is None:
        local = shutil.which("herdr")
        return Path(local) if local else None
    name, sha = PINNED[key]
    with urllib.request.urlopen(RELEASE + name, timeout=120) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha:
        raise AssertionError(f"pinned Herdr {name} sha256 mismatch: {digest}")
    directory = work / "herdr-bin"
    directory.mkdir()
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            archive.extractall(directory)
        [exe] = [p for p in directory.rglob("herdr.exe")]
        return exe
    exe = directory / "herdr"
    exe.write_bytes(data)
    exe.chmod(0o755)
    return exe


def isolated_env(home):
    """No inherited Herdr variables and a private HOME/config, so only our session is reachable.
    PYTHONUTF8 is off: the adapter must decode Herdr's UTF-8 itself."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    env["PYTHONUTF8"] = "0"
    if os.name == "nt":
        env.update(USERPROFILE=str(home), APPDATA=str(home / "AppData" / "Roaming"),
                   LOCALAPPDATA=str(home / "AppData" / "Local"))
        for d in ("AppData/Roaming", "AppData/Local"):
            (home / d).mkdir(parents=True, exist_ok=True)
    else:
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), XDG_STATE_HOME=str(home / ".state"),
                   XDG_DATA_HOME=str(home / ".data"), XDG_RUNTIME_DIR=str(home / "run"))
        (home / "run").mkdir(parents=True, exist_ok=True)
        os.chmod(home / "run", 0o700)
    return env


NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}


class Herdr:
    def __init__(self, exe, env):
        self.exe, self.env = exe, env

    def __call__(self, *args, timeout=30):
        result = subprocess.run([str(self.exe), "--session", SESSION, *args], env=self.env, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=timeout,
                                stdin=subprocess.DEVNULL, **NO_WINDOW)
        return result

    def json(self, *args):
        result = self(*args)
        if result.returncode != 0:
            raise AssertionError(f"herdr {' '.join(args[:3])} failed: {result.stderr.strip()[:400]}")
        return json.loads(result.stdout)


def body_of(text):
    """The framed body of a connector prompt (lines between the label and the end marker)."""
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if line.endswith('every line starts with "| ":')) + 1
    end = next(i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith("[end of RainCLI "))
    return "\n".join(line[2:] for line in lines[start:end])


def pastes(raw):
    """Each bracketed paste the fake agent received, decoded."""
    out = []
    for part in raw.split(b"\x1b[200~")[1:]:
        if b"\x1b[201~" in part:
            out.append(part.split(b"\x1b[201~", 1)[0].decode("utf-8"))
    return out


def run_stage(root, server, wait_for, show, kill_tree):
    """Returns False when skipped (no Herdr for this platform)."""
    from raincli_agent.config import write_config
    from raincli_agent.fsutil import atomic_write_json
    from raincli_agent.runtime.service import request_stop

    work = Path(tempfile.mkdtemp(prefix="rch-", dir=None if os.name == "nt" else "/tmp"))  # short: socket paths
    try:
        exe = fetch_herdr(work)
    except OSError as exc:
        shutil.rmtree(work, ignore_errors=True)
        print(f"SKIP: Herdr stage, cannot download the pinned release: {exc}", flush=True)
        return False
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)  # a hash mismatch fails the smoke, leaving nothing behind
        raise
    if exe is None:
        shutil.rmtree(work, ignore_errors=True)
        print("SKIP: Herdr stage, no pinned Herdr for this platform and none on PATH", flush=True)
        return False
    env = isolated_env(work / "home")
    herdr = Herdr(exe, env)
    out = work / "agent"
    out.mkdir()
    server_log = work / "herdr-server.log"
    with open(server_log, "wb") as log:
        herdr_server = subprocess.Popen([str(exe), "--session", SESSION, "server"], env=env, stdout=log,
                                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **NO_WINDOW)
    runtime_process = None
    config = work / "runtime.json"
    try:
        wait_for(lambda: herdr("workspace", "list").returncode == 0, timeout=60)
        created = herdr.json("workspace", "create", "--cwd", str(out), "--label", "raincli-smoke", "--no-focus")
        pane = created["result"]["root_pane"]["pane_id"]
        fake = out / "claude"  # a Python script named "claude": Herdr's wrapped-runtime detection
        fake.write_text(FAKE_AGENT, encoding="utf-8")
        for path in (sys.executable, str(fake), str(out)):
            assert " " not in path, f"the smoke needs space-free paths for `pane run`: {path}"
        run = herdr("pane", "run", pane, f"{sys.executable} {fake} {out}")
        assert run.returncode == 0, run.stderr
        wait_for(lambda: (out / "fake.log").exists() and "report idle -> 0" in (out / "fake.log").read_text(),
                 timeout=60)
        renamed = herdr("agent", "rename", pane, "smoke-inbox")
        assert renamed.returncode == 0, renamed.stderr
        wait_for(lambda: herdr("agent", "get", "smoke-inbox").returncode == 0, timeout=30)

        # Two connectors in one runtime: the fake agent, and a name nothing runs (offline).
        connectors = []
        for handle, agent in (("herdr-inbox", "smoke-inbox"), ("herdr-absent", "no-such-agent")):
            identity = work / f"{handle}.json"
            write_config(identity, server.url, server.state.add_agent(handle))
            connector = work / f"{handle}-connector.json"
            atomic_write_json(connector, {"agent_config": str(identity), "herdr_agent": agent,
                                          "herdr_bin": str(exe), "herdr_session": SESSION,
                                          "expect_pane_id": pane if agent == "smoke-inbox" else "",
                                          "state_dir": str(work / f"queue-{handle}"), "poll_wait": 1,
                                          "trusted_senders": ["herdr-sender"], "prompt_timeout": 30})
            connectors.append(str(connector))
        sender = server.state.add_agent("herdr-sender")
        atomic_write_json(config, {"connectors": connectors, "state_dir": str(work / "runtime-state")})
        runtime_log = work / "runtime.log"
        with open(runtime_log, "wb") as log:
            runtime_process = subprocess.Popen([sys.executable, "-m", "raincli_agent", "runtime", "run", "--config",
                                                str(config)], stdout=log, stderr=subprocess.STDOUT, env=env)

        def send(to, body):
            from raincli_agent.api import ApiClient
            return ApiClient(server.url, sender).send(to, body)[0]["id"]

        def state_of(mid):
            return server.state.messages[mid]["delivery_state"]

        def received():
            path = out / "received.bin"
            return pastes(path.read_bytes()) if path.exists() else []

        # 1. Exact bytes: non-ASCII, quotes, cmd/PowerShell metacharacters, newlines and CRLF-free text.
        tricky = ("héllo wörld ✓ 日本語 😀\n\"double\" 'single' `back` \\\\share\\path\\\n"
                  "%PATH% $env:HOME ^& | < > && ; $(x)\n\n  indented line\nlast")
        first = send("herdr-inbox", tricky)
        wait_for(lambda: state_of(first) == "submitted", timeout=90)
        wait_for(lambda: len(received()) >= 1, timeout=30)
        assert body_of(received()[0]) == tricky, repr(received()[0][-400:])
        print("PASS: Herdr: non-ASCII, quotes, metacharacters and newlines delivered byte-exact; received -> submitted",
              flush=True)

        # 2. A near-cap body (the 16,000-character body limit) within the command-line bound.
        near = ("abcdefghij é ✓ " * 1200)[:15900]
        second = send("herdr-inbox", near)
        wait_for(lambda: state_of(second) == "submitted", timeout=90)
        wait_for(lambda: len(received()) >= 2, timeout=30)
        assert body_of(received()[1]) == near
        print("PASS: Herdr: a 15,900-character body delivered byte-exact", flush=True)

        # 3. Blocked -> held, then delivered once idle again.
        wait_for(lambda: json.loads(herdr("agent", "get", "smoke-inbox").stdout or "{}")
                 .get("result", {}).get("agent", {}).get("agent_status") == "idle", timeout=30)
        herdr("pane", "report-agent", pane, "--source", SOURCE, "--agent", "claude", "--state", "blocked")
        third = send("herdr-inbox", "while blocked")
        wait_for(lambda: state_of(third) == "held"
                 and any(e[0] == third and e[1] == "held" and "blocked" in (e[2] or "") for e in server.state.events),
                 timeout=60)
        herdr("pane", "report-agent", pane, "--source", SOURCE, "--agent", "claude", "--state", "idle")
        wait_for(lambda: state_of(third) == "submitted", timeout=90)
        print("PASS: Herdr: a blocked agent holds the message (blocked), delivered once idle", flush=True)

        # 4. An agent name nothing runs -> held offline.
        fourth = send("herdr-absent", "nobody home")
        wait_for(lambda: state_of(fourth) == "held"
                 and any(e[0] == fourth and e[1] == "held" and "offline" in (e[2] or "") for e in server.state.events),
                 timeout=60)
        print("PASS: Herdr: an unknown agent name holds the message (offline)", flush=True)

        # 5. The directory lists the inbox as instant.
        def inbox_listed():
            entries = server.state.directory.get("herdr-inbox") or []
            return [a for a in entries if a["role"] == "inbox" and a["name"] == "smoke-inbox"
                    and a["reachability"] == "instant" and a["source"] == "herdr"]
        wait_for(inbox_listed, timeout=90)
        print("PASS: Herdr: the runtime's directory shows smoke-inbox as the instant inbox", flush=True)

        # 6. An oversize command line is held, never truncated or sent.
        before = len(received())
        quotes = '"' * 15500  # list2cmdline doubles each quote: over the 30,000 bound
        fifth = send("herdr-inbox", quotes)
        wait_for(lambda: state_of(fifth) == "held" and any(
            e[0] == fifth and e[1] == "held" and "too_large_for_command_line" in (e[2] or "")
            for e in server.state.events), timeout=60)
        time.sleep(3)
        assert len(received()) == before, "an oversize prompt reached the pane"
        print("PASS: Herdr: an oversize command line is held as too_large_for_command_line, nothing sent", flush=True)
        request_stop(config)
        assert runtime_process.wait(timeout=60) == 0
        return True
    except BaseException:
        for path in [server_log, work / "runtime.log", out / "fake.log", *sorted((work / "runtime-state").glob("connector-*.log"))]:
            show("herdr stage", path)
        raise
    finally:
        if runtime_process is not None and runtime_process.poll() is None:
            try:
                request_stop(config)
                runtime_process.wait(timeout=60)
            except Exception:
                kill_tree(runtime_process)
        herdr("server", "stop")  # the throwaway session's server only
        try:
            herdr_server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            kill_tree(herdr_server)
        shutil.rmtree(work, ignore_errors=True)
