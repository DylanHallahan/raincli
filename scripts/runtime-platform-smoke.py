"""Native runtime/write-back/update smoke using disposable local test fixtures."""
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "raincli/tests/agent"))
from fake_server import FakeApi
from raincli_agent import __version__
from raincli_agent.config import write_config
from raincli_agent.connector.queue import Queue
from raincli_agent.errors import ConfigError
from raincli_agent.fsutil import atomic_write_json, read_file_bytes, read_private_file
from raincli_agent.runtime import pushed, sessions, updates
from raincli_agent.runtime.service import request_stop, status
from raincli_agent.runtime.startup import systemd_unit


def wait_for(action, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = action()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("timed out waiting for runtime state")


def scrub(text):
    # Nothing here should hold a credential; redact token-shaped text regardless.
    return re.sub(r"rca_[A-Za-z0-9_-]{8,}", "rca_<redacted>", text)


def show(label, path, limit=6000):
    try:
        text = read_file_bytes(path).decode("utf-8", errors="replace")
    except OSError as exc:
        text = f"<unreadable: {type(exc).__name__}>"
    print(f"--- {label}: {path} ---\n{scrub(text[-limit:])}", flush=True)


def diagnose(label, config, state, managed=None, logs=()):
    """Print local runtime evidence on failure: paths, statuses, logs, processes.

    Only processes whose command line names this smoke's temporary root are
    listed, so unrelated sessions on the machine are never printed."""
    root = config.parent
    markers = {str(root), str(root.resolve())}
    print(f"===== DIAGNOSTICS ({label}) =====", flush=True)
    try:
        print("runtime status:", scrub(json.dumps(status(config), indent=2)), flush=True)
    except Exception as exc:
        print("runtime status unavailable:", type(exc).__name__, exc, flush=True)
    for path in sorted(state.glob("*.json")) + sorted(state.glob("*.stop")) + sorted(state.glob("connector-*.log*")):
        show("state file", path)
    if managed is not None:
        for name in ("current.json", "update.log"):
            show("managed", managed / name)
        for path in sorted(managed.glob("versions/*")):
            print("managed version:", path.name, sorted(p.name for p in path.iterdir()), flush=True)
    for path in logs:
        show("process output", path)
    if os.name == "nt":
        listing = ["powershell", "-NoProfile", "-Command",
                   "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | "
                   "ForEach-Object { '{0} {1} {2}' -f $_.ProcessId, $_.ParentProcessId, $_.CommandLine }"]
    else:
        listing = ["ps", "-eo", "pid,ppid,args"]
    try:
        output = subprocess.run(listing, capture_output=True, text=True, timeout=30).stdout
        fold = str.casefold if os.name == "nt" else str
        ours = [line for line in output.splitlines() if any(fold(m) in fold(line) for m in markers)]
        print("--- processes started by this smoke (pid ppid command) ---", flush=True)
        print(scrub("\n".join(ours)), flush=True)
    except Exception as exc:
        print("process listing unavailable:", type(exc).__name__, flush=True)
    print("===== END DIAGNOSTICS =====", flush=True)


def kill_tree(process):
    if os.name == "nt":  # include the runtime and connectors behind the launcher
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
    else:
        process.kill()
    process.wait(timeout=10)


PASSWORD = "smoke password, typed on a pty only"


def login_on_pty(argv, password, timeout=60):
    """Run ``raincli login`` on a pseudo-terminal, typing the password at its prompt."""
    import pty
    import select
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(argv[0], argv)
    transcript, typed = b"", False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if select.select([fd], [], [], 0.2)[0]:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                break
            transcript += chunk
            if not typed and transcript.rstrip().endswith(b"Password:"):
                os.write(fd, password.encode() + b"\n")
                typed = True
    _, raw = os.waitpid(pid, 0)
    os.close(fd)
    return os.waitstatus_to_exitcode(raw), transcript.decode(errors="replace")


def headless_login_and_machine_mode(root, server):
    """Headless sign-in (15.2) and a machine-mode runtime (15.4) as real processes."""
    machine = root / "machine"
    agent = machine / "agent.json"
    runtime = machine / "runtime.json"
    server.state.add_user("smoke@example.test", PASSWORD)
    if sys.platform.startswith("linux"):
        status, transcript = login_on_pty([sys.executable, "-m", "raincli_agent", "--config", str(agent), "login",
                                           "--email", "smoke@example.test", "--machine-name", "smoke-machine",
                                           "--api-url", server.url], PASSWORD)
        assert status == 0, scrub(transcript)
        assert PASSWORD not in transcript and "signed in as smoke-machine" in transcript
        refused = subprocess.run([sys.executable, "-m", "raincli_agent", "--config", str(agent), "login", "--email",
                                  "smoke@example.test", "--api-url", server.url], input=PASSWORD + "\n",
                                 capture_output=True, text=True, timeout=30)
        assert refused.returncode == 2 and "interactive terminal" in refused.stderr
        for path in machine.rglob("*"):
            assert not path.is_file() or PASSWORD.encode() not in path.read_bytes()
        print("PASS: headless login on a pty (no echo, no argv/env/file password); no-TTY refused", flush=True)
    else:
        from raincli_agent import login
        from raincli_agent.config import Secret
        login.login("smoke@example.test", Secret(PASSWORD), api_url=server.url, config_path=str(agent),
                    machine_name="smoke-machine")
    assert json.loads(runtime.read_text()) == {"machine_config": str(agent), "state_dir": "runtime-state"}
    log = root / "runtime-machine.log"
    with open(log, "wb") as output:
        process = subprocess.Popen([sys.executable, "-m", "raincli_agent", "runtime", "run", "--config", str(runtime)],
                                   stdout=output, stderr=subprocess.STDOUT)
    try:
        wait_for(lambda: server.state.presence.get("smoke-machine") == "ready")
        body = [b for h, b in server.state.presence_bodies if h == "smoke-machine"][-1]
        assert body["client"]["version"] == __version__ and "agents" in body
        assert all(a["role"] is None for a in body["agents"])
        request_stop(runtime)
        assert process.wait(timeout=30) == 0
        assert server.state.presence["smoke-machine"] == "offline"
    except BaseException:
        diagnose("machine-mode runtime", runtime, machine / "runtime-state", logs=[log])
        raise
    finally:
        if process.poll() is None:
            kill_tree(process)
    print("PASS: machine mode: ready, client block and directory with no inbox; graceful stop clears it", flush=True)
    return agent, runtime


def herdr_stage(server):
    """Real Herdr delivery (Phase 2): see scripts/herdr_smoke.py."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import herdr_smoke
    return herdr_smoke.run_stage(ROOT, server, wait_for, show, kill_tree)


def main():
    if "--herdr-only" in sys.argv[1:]:
        with FakeApi() as server:
            herdr_stage(server)
        return
    with FakeApi() as server:
        herdr_stage(server)
    with tempfile.TemporaryDirectory(prefix="raincli-runtime-") as tmp, FakeApi() as server:
        root = Path(tmp)
        identity = root / "agent.json"
        write_config(identity, server.url, server.state.add_agent("runtime-test"))
        connector = root / "connector.json"
        state = root / "state"
        atomic_write_json(connector, {"agent_config": str(identity), "herdr_agent": "test-inbox",
                                     "herdr_bin": "intentionally-missing-herdr", "state_dir": str(root / "queue"),
                                     "poll_wait": 1})
        config = root / "runtime.json"
        atomic_write_json(config, {"connectors": [str(connector)], "state_dir": str(state)})
        direct_log = root / "runtime-direct.log"
        with open(direct_log, "wb") as output:
            process = subprocess.Popen([sys.executable, "-m", "raincli_agent", "runtime", "run", "--config", str(config)],
                                       stdout=output, stderr=subprocess.STDOUT)
        try:
            # First tick correctly reports offline until the child confirms it
            # authenticated and acquired the queue. Next tick sees unavailable Herdr.
            wait_for(lambda: server.state.presence.get("runtime-test") == "unknown")
            def published_report():
                try:
                    # The product's reader: retries a Windows sharing violation
                    # while the runtime replaces status.json.
                    value = json.loads(read_private_file(state / "status.json", "runtime status"))
                except (ConfigError, ValueError):
                    return None
                entries = value.get("connectors", [])
                return value if entries and entries[0].get("status") == "unknown" else None
            report = wait_for(published_report)
            assert report["connectors"][0]["reported"]
            assert report["connectors"][0]["process_running"]
            duplicate = subprocess.run([sys.executable, "-m", "raincli_agent", "runtime", "run", "--config", str(config), "--once"],
                                       capture_output=True, timeout=15)
            assert duplicate.returncode != 0
            # Directory (protocol 14.3): a real hook process records a Claude Code
            # session; the runtime publishes it with its salted key and basename only.
            assert (state / "machine-salt").is_file()
            hook_payload = json.dumps({"session_id": "smoke-session", "cwd": str(root / "private" / "smoke-project"),
                                       "prompt": "private prompt"}).encode()
            hooked = subprocess.run([sys.executable, "-m", "raincli_agent", "hook", "claude", "SessionStart",
                                     "--state-dir", str(state)], input=hook_payload, capture_output=True, timeout=15)
            assert (hooked.returncode, hooked.stderr) == (0, b""), hooked
            salt = sessions.read_salt(str(state))

            def listed():
                agents = server.state.directory.get("runtime-test") or []
                return [a for a in agents if a["source"] == "hook"]
            [session] = wait_for(listed, timeout=60)
            assert (session["name"], session["type"], session["status"]) == ("smoke-project", "claude", "idle")
            assert session["key"] == sessions.agent_key(salt, "claude:smoke-session")
            inbox = [a for a in server.state.directory["runtime-test"] if a["role"] == "inbox"]
            assert [(a["name"], a["reachability"]) for a in inbox] == [("test-inbox", "instant")]
            published = json.dumps(server.state.presence_bodies)
            assert str(root) not in published and "private prompt" not in published
            client = server.state.clients["runtime-test"]
            assert client["version"] == __version__ and client["update_mode"] in ("manual", "automatic")
            request_stop(config)
            assert process.wait(timeout=30) == 0
            assert server.state.presence["runtime-test"] == "offline"
            assert server.state.presence_bodies[-1][1] == {"status": "offline", "agents": []}
            print("PASS: agent directory via a real hook process, salted keys, no paths; stop clears it", flush=True)
            q = Queue(str(root / "queue"))
            q.acquire_run_lock()
            q.release_run_lock()  # no orphan child holding the delivery queue
            print("PASS: real runtime/connector processes, authenticated presence write-back, singleton and graceful stop", flush=True)
        except BaseException:
            diagnose("direct runtime", config, state, logs=[direct_log])
            raise
        finally:
            if process.poll() is None:
                try:
                    request_stop(config)
                    process.wait(timeout=30)
                except Exception:
                    kill_tree(process)
        # R4-M1: an atomic replace while another handle holds the file (a plain
        # reader, or the share-DELETE state reader) must succeed once it is released.
        import threading
        from raincli_agent.fsutil import open_read_nofollow
        probe = root / "sharing-probe.json"
        atomic_write_json(probe, {"n": 0})
        for n, hold in enumerate((lambda p: open(p, "rb"), lambda p: os.fdopen(open_read_nofollow(p), "rb")), 1):
            handle = hold(probe)
            releaser = threading.Timer(0.3, handle.close)
            releaser.start()
            atomic_write_json(probe, {"n": n})
            releaser.join()
            assert json.loads(read_private_file(probe)) == {"n": n}
        print("PASS: atomic replace and private reads ride out a concurrently held file", flush=True)
        # Hook-session liveness (protocol 14.9): the real hook, run by an "agent"
        # process, records that process's pid and start time; the session stays
        # live while it runs and is gone once it exits. Windows: a copy of cmd.exe
        # named claude.exe (parent walk over a Toolhelp snapshot, GetProcessTimes).
        # Linux: a copy of bash at a native-install path (…/claude/versions/<n>).
        live_state = root / "liveness-state"
        live_state.mkdir(mode=0o700)
        live_salt = sessions.ensure_salt(str(live_state))
        sessions.sessions_dir(str(live_state), create=True)
        payload = root / "liveness-payload.json"
        payload.write_text(json.dumps({"session_id": "live-1", "cwd": str(root / "live-project")}))
        hook_argv = f'"{sys.executable}" -m raincli_agent hook claude SessionStart --state-dir "{live_state}" < "{payload}"'
        if os.name == "nt":
            import shutil
            agent = root / "agent-bin" / "claude.exe"
            agent.parent.mkdir()
            shutil.copy(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"), agent)
            # A raw command line: cmd.exe does not understand the \" escapes of an argv list.
            agent_process = subprocess.Popen(f'"{agent}" /d /s /c "{hook_argv} & ping -n 60 127.0.0.1 >nul"')
        else:
            import shutil
            agent = root / "share" / "claude" / "versions" / "2.1.119"
            agent.parent.mkdir(parents=True)
            shutil.copy(os.path.realpath(shutil.which("bash")), agent)
            agent_process = subprocess.Popen([str(agent), "-c", f"sh -c '{hook_argv}'; sleep 60"])
        try:
            key = sessions.agent_key(live_salt, "claude:live-1")
            record = wait_for(lambda: sessions.load_record(str(live_state), key), timeout=60)
            assert record.get("pid") == agent_process.pid, (record.get("pid"), agent_process.pid)
            assert sessions.process_state(record) == "alive"
            assert len(sessions.live_sessions(str(live_state), "claude", "live-project", now=time.time() + 5 * 3600)) == 1
        finally:
            kill_tree(agent_process)
        wait_for(lambda: sessions.process_state(record) == "dead", timeout=30)
        assert sessions.live_sessions(str(live_state), "claude", "live-project") == []
        print("PASS: hook-session liveness: the agent's pid is recorded, live however long it idles, gone once it exits", flush=True)
        machine_config = machine_runtime = None
        machine_config, machine_runtime = headless_login_and_machine_mode(root, server)
        if os.name == "nt":
            import winreg
            from raincli_agent.runtime import startup
            # Exercise real HKCU APIs, isolated from the actual Run key.
            original_key = startup.REGISTRY_KEY
            original_root = updates.default_root
            startup.REGISTRY_KEY = r"Software\RainCLI-test-" + root.name
            updates.default_root = lambda: root / "startup-managed"
            try:
                startup.install(config)
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, startup.REGISTRY_KEY) as key:
                    value, kind = winreg.QueryValueEx(key, startup.REGISTRY_VALUE)
                # The product stores the resolved long path; the runner's TEMP may
                # be an 8.3 short path (RUNNER~1), so compare against config.resolve().
                expected = startup.windows_command_line(startup.command(config))
                resolved = '"' + str(config.resolve()) + '"'
                # The value holds paths and fixed words only, never a credential.
                assert kind == winreg.REG_SZ and value == expected and resolved in value and " \"runtime\" \"run\" " in value, (
                    f"Run value kind={kind} (REG_SZ={winreg.REG_SZ}) value={value!r} expected={expected!r} config={resolved}")
                startup.remove()
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, startup.REGISTRY_KEY) as key:
                    try:
                        winreg.QueryValueEx(key, startup.REGISTRY_VALUE)
                    except FileNotFoundError:
                        pass
                    else:
                        raise AssertionError("Windows logon value remains after removal")
            finally:
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, startup.REGISTRY_KEY)
                except FileNotFoundError:
                    pass
                startup.REGISTRY_KEY = original_key
                updates.default_root = original_root
            print("PASS: Windows login entry install/remove using an isolated test registry key", flush=True)
        else:
            unit = root / "raincli-runtime.service"
            unit.write_text(systemd_unit(config))
            subprocess.run(["systemd-analyze", "--user", "verify", str(unit)], check=True)
            print("PASS: Linux systemd unit syntax (not installed)", flush=True)
        # Use precisely this checkout as a synthetic release archive. Network
        # release lookup is excluded; venv creation and installation are real.
        files = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, text=True).splitlines()

        # Synthetic later releases: the same code with another version, and one
        # whose runtime fails its first start (to exercise the launcher's rollback).
        base = tuple(int(x) for x in __version__.split("."))
        good = "%d.%d.%d" % (base[0], base[1], base[2] + 1)
        broken = "%d.%d.%d" % (base[0], base[1], base[2] + 2)
        # Machine mode accepts v0.4.0 or later only (15.8 H8).
        machine_good = "%d.%d.%d" % max((base[0], base[1], base[2] + 3), (0, 4, 0))
        variants = {"c" * 40: (good, False), "d" * 40: (broken, True), "e" * 40: (machine_good, False)}

        def archive_for(url, limit):
            # Like GitHub's codeload archive: one root named after the commit.
            commit = url.rsplit("/", 1)[1]
            version, crash = variants.get(commit, (None, False))
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
                for name in files:
                    path = ROOT / name
                    if not path.is_file():
                        continue
                    data = path.read_bytes()
                    # A Windows checkout has CRLF line endings (core.autocrlf): match
                    # either, and fail loudly if a patch does not apply exactly once.
                    if version and name == "raincli/raincli_agent/__init__.py":
                        data, count = re.subn(rb'__version__ = "[0-9.]+"', ('__version__ = "%s"' % version).encode(), data)
                        assert count == 1, "synthetic version patch did not apply"
                    if crash and name == "raincli/raincli_agent/runtime/service.py":
                        data, count = re.subn(rb"(def run\(path, once=False, pushed=None\):(\r?\n))",
                                              rb"\1    raise SystemExit(3)\2", data)
                        assert count == 1, "synthetic first-start failure patch did not apply"
                    archive.writestr("raincli-" + commit + "/" + name, data)
            return buf.getvalue()
        original_fetch = updates.fetch
        updates.fetch = archive_for
        managed = root / "managed"
        try:
            release = {"tag": "v" + __version__, "commit": "a" * 40}
            assert updates.install(managed, release)["status"] == "installed"
            before = updates.read_pointer(managed)
            assert (before["automatic"], before["update_mode"]) == (False, "automatic")
            launcher = managed / "launch.py"
            result = subprocess.run([sys.executable, str(launcher), "--version"], capture_output=True, text=True, check=True, timeout=20)
            assert result.stdout.strip() == "raincli " + __version__
            assert updates.configure(managed, mode="automatic")["update_mode"] == "automatic"
            # Manual for the live runs below: the runtime never reaches GitHub here.
            # The pushed install is driven from this process through the same path.
            updates.configure(managed, mode="manual")
            assert updates.read_pointer(managed)["automatic"] is False
            # Exercise the installed launcher and both installed environments as
            # real processes, including queue ownership across two handoffs.
            launcher_log = root / "launcher.log"
            with open(launcher_log, "wb") as output:
                managed_process = subprocess.Popen([sys.executable, str(launcher), "runtime", "run", "--config", str(config)],
                                                   stdout=output, stderr=subprocess.STDOUT)
            try:
                first = wait_for(published_report)
                assert updates.install(managed, {**release, "commit": "b" * 40})["status"] == "installed"
                assert updates.read_pointer(managed)["previous"]["commit"] == "a" * 40

                def replacement(previous):
                    report = published_report()
                    if not report or report["instance"] == previous["instance"]:
                        return None
                    assert report["pid"] != previous["pid"]
                    assert report["connectors"][0]["child_pid"] != previous["connectors"][0]["child_pid"]
                    assert report["connectors"][0]["reported"]
                    return report

                second = wait_for(lambda: replacement(first), timeout=75)
                updates.configure(managed, rollback=True)
                assert updates.read_pointer(managed)["commit"] == "a" * 40
                assert updates.read_pointer(managed)["automatic"] is False
                third = wait_for(lambda: replacement(second), timeout=75)
                print("PASS: staged venv installation, live managed update/rollback handoff and released queue", flush=True)

                # Pushed update (14.5): the team's target, installed immediately through
                # the updater, handed over by the live launcher; the new version reports
                # current. Then a target whose runtime fails its first start is rolled
                # back by the launcher and reported rolled_back.
                team = "alpha"

                def push(version, commit):
                    target = {"version": "v" + version, "allow_downgrade": False}
                    server.state.targets[team] = target
                    driver = pushed.PushedUpdates(managed, python=updates.read_pointer(managed)["python"],
                                                  resolve=lambda tag: {"tag": tag, "commit": commit},
                                                  log=lambda text: None)
                    key = pushed.target_key(target)
                    driver._save(state="updating", error=None, target=key, blocked=None)
                    driver._run(key)
                    assert driver.data["state"] == "updating", driver.data
                    return key

                push(good, "c" * 40)
                assert updates.read_pointer(managed)["tag"] == "v" + good

                def client_is(version, state_name):
                    client = server.state.clients.get("runtime-test") or {}
                    report = published_report()
                    return (client.get("version") == version and client.get("update_state") == state_name
                            and report and report["instance"] != third["instance"])
                wait_for(lambda: client_is(good, "current"), timeout=90)
                fourth = published_report()
                key = push(broken, "d" * 40)
                wait_for(lambda: updates.read_pointer(managed)["tag"] == "v" + good
                         and updates.read_update_state(managed).get("state") == "rolled_back", timeout=90)
                assert updates.read_update_state(managed)["blocked"] == key
                assert updates.read_update_state(managed)["error"] == "first_start_failed"
                assert updates.read_pointer(managed)["update_mode"] == "manual"  # unchanged by the rollback
                wait_for(lambda: (server.state.clients.get("runtime-test") or {}).get("update_state") == "rolled_back"
                         and (published_report() or fourth)["instance"] != fourth["instance"], timeout=90)
                assert server.state.clients["runtime-test"]["version"] == good
                assert server.state.clients["runtime-test"]["error"] == "first_start_failed"
                print("PASS: pushed target installed and handed over; a failing first start rolled back", flush=True)
                request_stop(config)
                assert managed_process.wait(timeout=45) == 0
                assert server.state.presence["runtime-test"] == "offline"
                # Machine mode under the managed launcher: a pushed update (15.4, 15.5).
                machine_log = root / "launcher-machine.log"
                with open(machine_log, "wb") as output:
                    machine_process = subprocess.Popen([sys.executable, str(launcher), "runtime", "run", "--config",
                                                        str(machine_runtime)], stdout=output, stderr=subprocess.STDOUT)
                try:
                    wait_for(lambda: (server.state.clients.get("smoke-machine") or {}).get("version") == good, timeout=60)
                    push(machine_good, "e" * 40)
                    wait_for(lambda: (server.state.clients.get("smoke-machine") or {}).get("version") == machine_good
                             and server.state.clients["smoke-machine"]["update_state"] == "current", timeout=90)
                    body = [b for h, b in server.state.presence_bodies if h == "smoke-machine"][-1]
                    assert body["status"] == "ready" and all(a["role"] is None for a in body.get("agents", []))
                    request_stop(machine_runtime)
                    assert machine_process.wait(timeout=45) == 0
                    assert server.state.presence["smoke-machine"] == "offline"
                except BaseException:
                    diagnose("machine-mode managed runtime", machine_runtime, root / "machine" / "runtime-state",
                             managed, logs=[machine_log])
                    raise
                finally:
                    if machine_process.poll() is None:
                        kill_tree(machine_process)
                print("PASS: machine mode under the managed launcher: pushed update installed and reported current", flush=True)
                q.acquire_run_lock()
                q.release_run_lock()
            except BaseException:
                diagnose("managed launcher runtime", config, state, managed, logs=[launcher_log])
                raise
            finally:
                if managed_process.poll() is None:
                    try:
                        request_stop(config)
                        managed_process.wait(timeout=45)
                    except Exception:
                        kill_tree(managed_process)
        finally:
            updates.fetch = original_fetch
    print("Runtime smoke passed. Fake relay; a pinned real Herdr with a self-reporting fake agent (no real Claude Code "
          "or Codex pane); no production messages, login restart or GitHub release publication tested.")


if __name__ == "__main__":
    main()
