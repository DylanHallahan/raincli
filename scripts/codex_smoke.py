"""Codex hooks stage for scripts/runtime-platform-smoke.py (Phase 2).

No credentialed Codex session runs: Codex skips untrusted hooks, and we never write
trust or bypass it. Instead:

1. A pinned, sha256-checked Codex release (0.160.0, digests from the GitHub release
   API) gives the version gate its input and parses our hooks.json through
   ``codex app-server`` ``hooks/list`` with a throwaway CODEX_HOME: our entries must
   load with no warnings or errors, use our Windows command, keep
   additionalContextLimit, and stay ``untrusted``.
2. Each hook is then run exactly as codex-rs/hooks/src/engine/command_runner.rs
   (build_command) runs it: ``%COMSPEC% /C "<command>"`` (raw, Windows) or
   ``$SHELL -lc <command>`` (POSIX), with the documented stdin JSON, from a parent
   process named ``codex``. The profile path contains a space.
"""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import uuid

CODEX_VERSION = "0.160.0"
RELEASE = f"https://github.com/openai/codex/releases/download/rust-v{CODEX_VERSION}/"
PINNED = {  # GitHub release API asset digests for rust-v0.160.0
    "windows-x86_64": ("codex-x86_64-pc-windows-msvc.exe.tar.gz",
                       "3927169c5f287cbb0fac256c43972e987b7383b5d661efd3d7727df18a95ec5a"),
    "linux-x86_64": ("codex-x86_64-unknown-linux-musl.tar.gz",
                     "306865417d4ee7a927785852910a527f41e1e159add390ac5ae3accb67d44a13"),
}
SESSION = "019a7f2e-4c1d-7b3e-9a21-0f6c5d4e3b2a"
NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}

# The parent process: an interpreter named "codex" that launches the hook command
# exactly as Codex's command runner does, then stays alive until told to stop.
FAKE_CODEX = r'''
import json, os, subprocess, sys, time
spec = json.load(open(sys.argv[1], encoding="utf-8"))
with open(spec["out"] + ".pid", "w") as fh:
    fh.write(str(os.getpid()))
for step in spec["steps"]:
    payload = json.dumps(step["payload"]).encode("utf-8")
    if os.name == "nt":
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        # Command::new(COMSPEC).arg("/C").raw_arg(format!("\"{command_line}\"")), CREATE_NO_WINDOW
        line = '"%s" /C "%s"' % (comspec, step["command"]) if " " in comspec else '%s /C "%s"' % (comspec, step["command"])
        proc = subprocess.run(line, input=payload, capture_output=True, creationflags=0x08000000)
    else:
        shell = os.environ.get("SHELL", "/bin/sh")
        proc = subprocess.run([shell, "-lc", step["command"]], input=payload, capture_output=True)
    with open(step["out"], "wb") as fh:
        fh.write(proc.stdout)
    with open(step["out"] + ".status", "w") as fh:
        fh.write(str(proc.returncode))
while not os.path.exists(spec["out"] + ".stop"):
    time.sleep(0.2)
'''


def target():
    machine = platform.machine().lower()
    if os.name == "nt" and machine in ("amd64", "x86_64"):
        return "windows-x86_64"
    if sys.platform.startswith("linux") and machine in ("x86_64", "amd64"):
        return "linux-x86_64"
    return None


def fetch_codex(work):
    key = target()
    if key is None:
        return None
    name, sha = PINNED[key]
    with urllib.request.urlopen(RELEASE + name, timeout=300) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha:
        raise AssertionError(f"pinned Codex {name} sha256 mismatch: {digest}")
    directory = work / "codex-bin"
    directory.mkdir()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        [member] = [m for m in archive.getmembers() if m.isfile()]
        member.name = os.path.basename(member.name)
        archive.extract(member, directory, filter="data")
    exe = directory / ("codex.exe" if os.name == "nt" else "codex")
    (directory / member.name).rename(exe)
    exe.chmod(0o755)
    return exe


def hooks_list(codex, codex_home, cwd, env):
    """``hooks/list`` over ``codex app-server`` stdio. No credentials are involved."""
    proc = subprocess.Popen([str(codex), "app-server"], cwd=cwd, env={**env, "CODEX_HOME": str(codex_home)},
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8", **NO_WINDOW)
    replies, ready = {}, threading.Event()

    def reader():
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "id" in message:
                replies[message["id"]] = message
                if message["id"] == 2:
                    ready.set()
    threading.Thread(target=reader, daemon=True).start()

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "raincli-smoke", "version": "0"}}})
        deadline = time.monotonic() + 60
        while 1 not in replies and time.monotonic() < deadline:
            time.sleep(0.1)
        send({"jsonrpc": "2.0", "method": "initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "hooks/list", "params": {"cwds": [str(cwd)]}})
        if not ready.wait(60):
            raise AssertionError("codex app-server did not answer hooks/list")
        return replies[2]["result"]["data"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()


class MockResponses:
    """A loopback stand-in for the Responses API, as Codex's own tests use (core/tests/common:
    CODEX_API_KEY=dummy, ``-c openai_base_url=...``). Every POST gets one short assistant reply."""

    def __init__(self):
        import http.server

        events = [
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.output_item.done", "item": {"type": "message", "role": "assistant", "id": "msg_1",
                                                           "content": [{"type": "output_text", "text": "done"}]}},
            {"type": "response.completed", "response": {"id": "resp_1", "usage": {
                "input_tokens": 0, "input_tokens_details": None, "output_tokens": 0,
                "output_tokens_details": None, "total_tokens": 0}}},
        ]
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def real_exec(codex, project, env, base_url, state=None):
    """One credential-free ``codex exec`` turn. Trust comes only from Codex's own switch, set
    here in the test job (``--dangerously-bypass-hook-trust``, as codex-rs/exec/tests/suite/hooks.rs
    uses it); shipped code never writes or bypasses trust. With ``state``, returns also every
    codex session record seen while it ran (our SessionEnd hook removes it at the end)."""
    seen, done = {}, threading.Event()

    def watch():
        while not done.is_set():
            for record in codex_records(state, 0):
                seen[record["key"]] = record
            time.sleep(0.05)
    if state is not None:
        threading.Thread(target=watch, daemon=True).start()
    try:
        ran = subprocess.run([str(codex), "exec", "--skip-git-repo-check", "--dangerously-bypass-hook-trust",
                              "-c", f"openai_base_url={json.dumps(base_url)}", "say done"],
                             cwd=project, env={**env, "CODEX_API_KEY": "dummy"}, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=240, stdin=subprocess.DEVNULL, **NO_WINDOW)
    finally:
        time.sleep(0.2)
        done.set()
    return ran, list(seen.values())


def codex_records(state, since):
    from raincli_agent.runtime import sessions
    return [r for r in sessions.all_records(str(state))  # live or not: codex exec has ended by now
            if r.get("type") == "codex" and r.get("updated_at", 0) >= since]


def fake_codex_interpreter(work):
    """An interpreter whose process is named codex: a venv launcher copy (Windows) or a
    symlink to this Python (Linux, where the process comm is the link's name)."""
    if os.name == "nt":
        venv = work / "fakecodex"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=180)
        launcher = venv / "Scripts" / "python.exe"
        exe = venv / "Scripts" / "codex.exe"
        shutil.copy(launcher, exe)
        return exe
    link = work / "fakecodex" / "codex"
    link.parent.mkdir()
    link.symlink_to(sys.executable)
    return link


def approx_tokens(text):
    return math.ceil(len(text.encode("utf-8")) / 4)


def run_stage(root, server, wait_for, show, kill_tree):
    """Returns False when skipped."""
    from herdr_smoke import body_of  # the framed body, as the Herdr stage reads it
    from raincli_agent.api import ApiClient
    from raincli_agent.config import write_config
    from raincli_agent.connector.config import load_connector_config
    from raincli_agent.connector.herdr import FakeHerdr
    from raincli_agent.connector.queue import Queue
    from raincli_agent.connector.runner import Connector
    from raincli_agent.fsutil import atomic_write_json
    from raincli_agent.runtime import hooks_install, sessions

    work = Path(tempfile.mkdtemp(prefix="rcx-"))
    try:
        try:
            codex = fetch_codex(work)
        except OSError as exc:
            print(f"SKIP: Codex stage, cannot download the pinned release: {exc}", flush=True)
            return False
        if codex is None:
            print("SKIP: Codex stage, no pinned Codex for this platform", flush=True)
            return False
        profile = work / "Raincli Smoke User"  # a profile path with a space, as on many Windows PCs
        codex_home = profile / ".codex"  # a throwaway CODEX_HOME; the real ~/.codex is never read or written
        project = profile / "work" / "smoke project"
        for d in (codex_home, project):
            d.mkdir(parents=True)
        venv = profile / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=300)
        scripts = venv / ("Scripts" if os.name == "nt" else "bin")
        vpython = scripts / ("python.exe" if os.name == "nt" else "python")
        subprocess.run([str(vpython), "-m", "pip", "install", "--quiet", "--no-deps", str(root / "raincli")],
                       check=True, timeout=600)
        state = profile / ".config" / "raincli" / "runtime-state"
        identity = profile / ".config" / "raincli" / "agent.json"
        write_config(identity, server.url, server.state.add_agent("codex-inbox"))
        runtime = identity.parent / "runtime.json"
        atomic_write_json(runtime, {"machine_config": str(identity), "state_dir": str(state)})
        state.mkdir(mode=0o700)
        salt = sessions.ensure_salt(str(state))
        sessions.sessions_dir(str(state), create=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("CODEX")}
        env.update(CODEX_HOME=str(codex_home), PYTHONUTF8="0",
                   PATH=os.pathsep.join([str(scripts), str(codex.parent), env.get("PATH", "")]))

        # 1. The real CLI installs the hooks: version gate from the real codex, quoted command, limit.
        installed = subprocess.run([str(scripts / ("raincli.exe" if os.name == "nt" else "raincli")), "hooks",
                                    "install", "--codex", "--config", str(runtime)], env=env, capture_output=True,
                                   text=True, encoding="utf-8", timeout=180)
        assert installed.returncode == 0, installed.stderr
        result = json.loads(installed.stdout)
        assert result["status"] == "installed" and CODEX_VERSION in result["codex_hooks"], result
        assert "/hooks" in result["note"], result
        hooks_file = codex_home / "hooks.json"
        data = json.loads(hooks_file.read_text(encoding="utf-8"))
        assert "trusted_hash" not in hooks_file.read_text(encoding="utf-8")
        assert not list(codex_home.glob("config.toml")), "the installer must not write Codex trust state"
        print(f"PASS: Codex: raincli hooks install --codex against real codex {CODEX_VERSION} "
              f"(throwaway CODEX_HOME, profile path with a space)", flush=True)

        # 2. Real Codex parses our entries: our command, the limit, untrusted.
        listed = hooks_list(codex, codex_home, project, env)
        [entry_set] = listed
        assert entry_set["warnings"] == [] and entry_set["errors"] == [], entry_set
        ours = [h for h in entry_set["hooks"] if h.get("statusMessage") == "raincli"]
        assert len(ours) == 5, ours
        for hook_entry in ours:
            event = hook_entry["key"].split(":")[-3]
            configured = data["hooks"][{"session_start": "SessionStart", "user_prompt_submit": "UserPromptSubmit",
                                        "stop": "Stop", "permission_request": "PermissionRequest",
                                        "session_end": "SessionEnd"}[event]][0]["hooks"][0]
            expected = configured["commandWindows"] if os.name == "nt" else configured["command"]
            assert hook_entry["command"] == expected, (hook_entry["command"], expected)
            if event in ("session_start", "user_prompt_submit"):
                assert hook_entry["additionalContextLimit"] == hooks_install.CODEX_CONTEXT_LIMIT, hook_entry
            assert hook_entry["timeoutSec"] <= 5 and hook_entry["trustStatus"] == "untrusted"
        print("PASS: Codex: real codex app-server lists our 5 hooks with our command, additionalContextLimit "
              f"{hooks_install.CODEX_CONTEXT_LIMIT}, no warnings, and trust status untrusted", flush=True)

        # 2b. §16.19 item 5: REAL Codex executes our installed hook, in its own session shell
        # (PowerShell on Windows, the user's shell elsewhere), with paths that contain a space.
        mock = MockResponses()
        keep = {r["key"] for r in codex_records(state, 0)}
        try:
            if os.name == "nt":
                # Evidence for the bug: the v0.5.0 form, a line starting with a quoted path.
                new_text = hooks_file.read_text(encoding="utf-8")
                old_line = data["hooks"]["SessionStart"][0]["hooks"][0]["commandWindows"].split(" /d /c call ", 1)[1]
                old = json.loads(new_text)
                for groups in old["hooks"].values():  # every event in the v0.5.0 form
                    for entry in groups[0]["hooks"]:
                        entry["command"] = entry["commandWindows"] = entry["commandWindows"].split(" /d /c call ", 1)[1]
                # Evidence only (job-only probes, trusted by the job's bypass switch): which shell
                # Codex runs hooks in. "ver" works only in cmd, $PSVersionTable only in PowerShell.
                probe_cmd, probe_ps = work / "shell-cmd.txt", work / "shell-ps.txt"
                old["hooks"]["SessionStart"].append({"hooks": [
                    {"type": "command", "command": f'ver > "{probe_cmd}"', "commandWindows": f'ver > "{probe_cmd}"'},
                    {"type": "command", "command": f"$PSVersionTable.PSEdition > '{probe_ps}'",
                     "commandWindows": f"$PSVersionTable.PSEdition > '{probe_ps}'"}]})
                hooks_file.write_text(json.dumps(old, indent=2), encoding="utf-8")
                before = {r["key"] for r in codex_records(state, 0)}
                ran, seen = real_exec(codex, project, env, mock.url, state)
                old_recorded = [r for r in seen if r["key"] not in before]
                hooks_file.write_text(new_text, encoding="utf-8")
                shell = ("cmd" if probe_cmd.exists() else "") + ("PowerShell" if probe_ps.exists() else "")
                print(f"EVIDENCE: Codex {CODEX_VERSION} exec runs hooks in: {shell or 'neither probe ran'}; "
                      f"with the v0.5.0 hook form: exit {ran.returncode}, {len(old_recorded)} session(s) recorded",
                      flush=True)
                assert shell == "PowerShell" and not old_recorded, (shell, old_recorded)  # the bug, reproduced
                for label, argv in (("powershell -Command", ["powershell", "-NoProfile", "-Command", old_line]),
                                    ("pwsh -Command", ["pwsh", "-NoProfile", "-Command", old_line])):
                    exe = shutil.which(argv[0])
                    if exe:
                        before = {r["key"] for r in codex_records(state, 0)}
                        payload = json.dumps({"session_id": str(uuid.uuid4()), "cwd": str(project),
                                              "hook_event_name": "SessionStart", "source": "startup"}).encode()
                        proc = subprocess.run(subprocess.list2cmdline([exe] + argv[1:]), input=payload,
                                              capture_output=True, timeout=60, **NO_WINDOW)
                        got = [r for r in codex_records(state, 0) if r["key"] not in before]
                        print(f"EVIDENCE: the v0.5.0 line under {label}: exit {proc.returncode}, {len(got)} recorded; "
                              f"{proc.stderr.decode(errors='replace').strip()[:300]!r}", flush=True)
                comspec = os.environ.get("COMSPEC", "cmd.exe")
                before = {r["key"] for r in codex_records(state, 0)}
                proc = subprocess.run(f"{comspec} /C {old_line}", input=b"{}", capture_output=True, timeout=60,
                                      **NO_WINDOW)
                print(f"EVIDENCE: the v0.5.0 line under cmd /C unwrapped: exit {proc.returncode}; "
                      f"{proc.stderr.decode(errors='replace').strip()[:300]!r}", flush=True)
            before = {r["key"] for r in codex_records(state, 0)}
            ran, seen = real_exec(codex, project, env, mock.url, state)
            recorded = [r for r in seen if r["key"] not in before]
            assert recorded and recorded[0]["name"] == "smoke project", (ran.returncode, ran.stderr[-2000:])
            assert "hook: SessionStart Completed" in ran.stderr, ran.stderr[-2000:]
            print(f"PASS: Codex: real codex {CODEX_VERSION} exec ran our installed SessionStart hook in its session "
                  f"shell (exit {ran.returncode}); the session was recorded under a profile path with a space",
                  flush=True)
        finally:
            mock.close()
        if os.name == "nt":
            line = data["hooks"]["SessionStart"][0]["hooks"][0]["commandWindows"]
            payload = json.dumps({"session_id": "019a7f2e-4c1d-7b3e-9a21-0f6c5d4e3b2b", "cwd": str(project),
                                  "hook_event_name": "SessionStart", "source": "startup"}).encode()
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            shells = {"cmd /C wrapped": f'{comspec} /C "{line}"', "cmd /C unwrapped": f"{comspec} /C {line}"}
            for name in ("powershell", "pwsh"):
                exe = shutil.which(name)
                if exe:
                    shells[f"{name} -Command"] = subprocess.list2cmdline([exe, "-NoProfile", "-Command", line])
            for label, cmdline in shells.items():
                since = time.time()
                proc = subprocess.run(cmdline, input=payload, capture_output=True, timeout=60, **NO_WINDOW)
                assert proc.returncode == 0 and codex_records(state, since), (label, proc.stderr[-1000:])
            print(f"PASS: Codex: the hook line runs the same under {', '.join(shells)}", flush=True)
        for record in codex_records(state, 0):  # the evidence sessions above end here
            if record["key"] not in keep:
                sessions.remove_record(str(state), record["key"])

        # 3. A connector maps the inbox to this Codex session and hands over a long message.
        agent_cfg = profile / "bob-agent.json"
        bob_token = server.state.add_agent("codex-bob")
        write_config(agent_cfg, server.url, bob_token)
        sender = server.state.add_agent("codex-alice")
        connector_path = profile / "connector.json"
        connector_path.write_text(json.dumps({"agent_config": str(agent_cfg),
                                              "inbox": {"hook": "codex", "name": "smoke project"},
                                              "state_dir": str(profile / "queue"),
                                              "trusted_senders": ["codex-alice"]}), encoding="utf-8")
        cfg = load_connector_config(str(connector_path))
        connector = Connector(cfg, ApiClient(server.url, bob_token), FakeHerdr(), Queue(cfg.state_dir), log=lambda line: None, sleep=lambda s: None,
                              sessions_state=str(state))

        def command(event):
            entry = data["hooks"][event][0]["hooks"][0]
            return entry["commandWindows"] if os.name == "nt" else entry["command"]

        base = {"session_id": SESSION, "transcript_path": None, "cwd": str(project), "model": "gpt-5.5-codex",
                "permission_mode": "default"}
        out = work / "hook-out"
        spec = {"out": str(out), "steps": [
            {"command": command("SessionStart"), "out": str(out) + ".start",
             "payload": {**base, "hook_event_name": "SessionStart", "source": "startup"}}]}
        fake = work / "fake_codex.py"
        fake.write_text(FAKE_CODEX, encoding="utf-8")
        spec_path = work / "spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        parent = subprocess.Popen([str(fake_codex_interpreter(work)), str(fake), str(spec_path)], env=env,
                                  **NO_WINDOW)
        try:
            wait_for(lambda: Path(str(out) + ".start.status").exists(), timeout=120)
            assert Path(str(out) + ".start.status").read_text() == "0"
            key = sessions.agent_key(salt, f"codex:{SESSION}")
            record = sessions.load_record(str(state), key)
            assert record and (record["type"], record["name"], record["status"]) == ("codex", "smoke project", "idle")
            # Windows: the venv launcher named codex.exe runs the real interpreter as its child;
            # the hook's walk passes python.exe, raincli.exe, cmd.exe and python.exe to reach it.
            # Linux: the interpreter itself runs with the comm "codex".
            assert record.get("pid") == parent.pid, (record.get("pid"), parent.pid)
            assert sessions.process_state(record) == "alive"
            print("PASS: Codex: SessionStart through the exact Codex launch recorded the session; liveness "
                  "reached the codex process through the shell and raincli", flush=True)

            body = ("Ünïcödé ✓ 日本 next-turn report line for the Codex inbox. " * 300)[:15000]
            message = ApiClient(server.url, sender).send("codex-bob", body)[0]["id"]
            connector.run_once()
            assert connector.queue.get(message)["state"] == "handed_over", connector.queue.get(message)
            Path(str(out) + ".stop").write_text("")
            parent.wait(timeout=60)
        finally:
            if parent.poll() is None:
                kill_tree(parent)
        wait_for(lambda: sessions.process_state(record) == "dead", timeout=60)
        print("PASS: Codex: once the codex process exits, its session is no longer live", flush=True)

        # 4. The next prompt claims the message as additionalContext above 2,500 tokens.
        spec = {"out": str(out) + "2", "steps": [
            {"command": command("SessionStart"), "out": str(out) + "2.start",
             "payload": {**base, "hook_event_name": "SessionStart", "source": "resume"}},
            {"command": command("UserPromptSubmit"), "out": str(out) + "2.prompt",
             "payload": {**base, "hook_event_name": "UserPromptSubmit", "turn_id": "t-1", "prompt": "continue"}}]}
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        Path(str(out) + "2.stop").write_text("")  # exit after the steps
        parent = subprocess.Popen([str(fake_codex_interpreter_cached(work)), str(fake), str(spec_path)], env=env,
                                  **NO_WINDOW)
        parent.wait(timeout=180)
        claimed = Path(str(out) + "2.start").read_bytes() or Path(str(out) + "2.prompt").read_bytes()
        output = json.loads(claimed)
        specific = output["hookSpecificOutput"]
        context = specific["additionalContext"]
        assert specific["hookEventName"] in ("SessionStart", "UserPromptSubmit")
        assert body_of(context) == body
        assert approx_tokens(context) > 2500 and approx_tokens(context) <= hooks_install.CODEX_CONTEXT_LIMIT
        connector.run_once()
        assert connector.queue.get(message)["state"] == "submitted"
        print(f"PASS: Codex: the next turn claimed the message as additionalContext of {approx_tokens(context)} "
              f"tokens (over the 2,500 default, under the {hooks_install.CODEX_CONTEXT_LIMIT} limit), "
              "byte-exact; handed_over -> submitted", flush=True)
        return True
    except BaseException:
        for path in [state / "hook.log"] if "state" in locals() else []:
            show("codex stage", path)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def fake_codex_interpreter_cached(work):
    path = work / "fakecodex" / ("Scripts/codex.exe" if os.name == "nt" else "codex")
    return path if path.exists() else fake_codex_interpreter(work)
