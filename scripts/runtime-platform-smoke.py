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


def main():
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
        variants = {"c" * 40: (good, False), "d" * 40: (broken, True)}

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
                    if version and name == "raincli/raincli_agent/__init__.py":
                        data = data.replace(('"%s"' % __version__).encode(), ('"%s"' % version).encode())
                    if crash and name == "raincli/raincli_agent/runtime/service.py":
                        data = data.replace(b"def run(path, once=False, pushed=None):\n",
                                            b"def run(path, once=False, pushed=None):\n    raise SystemExit(3)\n")
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
    print("Runtime smoke passed. Fake relay, unavailable Herdr; no production messages, login restart or GitHub release publication tested.")


if __name__ == "__main__":
    main()
