"""Native runtime/write-back/update smoke using disposable local test fixtures."""
import io
import json
import os
from pathlib import Path
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
from raincli_agent.fsutil import atomic_write_json
from raincli_agent.runtime import updates
from raincli_agent.runtime.service import request_stop
from raincli_agent.runtime.startup import systemd_unit


def wait_for(action, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = action()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("timed out waiting for runtime state")


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
        process = subprocess.Popen([sys.executable, "-m", "raincli_agent", "runtime", "run", "--config", str(config)])
        try:
            # First tick correctly reports offline until the child confirms it
            # authenticated and acquired the queue. Next tick sees unavailable Herdr.
            wait_for(lambda: server.state.presence.get("runtime-test") == "unknown")
            def published_report():
                try:
                    value = json.loads((state / "status.json").read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    return None
                entries = value.get("connectors", [])
                return value if entries and entries[0].get("status") == "unknown" else None
            report = wait_for(published_report)
            assert report["connectors"][0]["reported"]
            assert report["connectors"][0]["process_running"]
            duplicate = subprocess.run([sys.executable, "-m", "raincli_agent", "runtime", "run", "--config", str(config), "--once"],
                                       capture_output=True, timeout=15)
            assert duplicate.returncode != 0
            request_stop(config)
            assert process.wait(timeout=30) == 0
            assert server.state.presence["runtime-test"] == "offline"
            q = Queue(str(root / "queue"))
            q.acquire_run_lock()
            q.release_run_lock()  # no orphan child holding the delivery queue
            print("PASS: real runtime/connector processes, authenticated presence write-back, singleton and graceful stop", flush=True)
        finally:
            if process.poll() is None:
                try:
                    request_stop(config)
                    process.wait(timeout=30)
                except Exception:
                    process.kill()
                    process.wait(timeout=10)
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

        def archive_for(url, limit):
            # Like GitHub's codeload archive: one root named after the commit.
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
                for name in files:
                    path = ROOT / name
                    if path.is_file():
                        archive.write(path, "raincli-" + url.rsplit("/", 1)[1] + "/" + name)
            return buf.getvalue()
        original_fetch = updates.fetch
        updates.fetch = archive_for
        managed = root / "managed"
        try:
            release = {"tag": "v" + __version__, "commit": "a" * 40}
            assert updates.install(managed, release)["status"] == "installed"
            before = updates.read_pointer(managed)
            assert before["automatic"] is False
            launcher = managed / "launch.py"
            result = subprocess.run([sys.executable, str(launcher), "--version"], capture_output=True, text=True, check=True, timeout=20)
            assert result.stdout.strip() == "raincli " + __version__
            assert updates.configure(managed, automatic=True)["automatic"] is True
            updates.configure(managed, automatic=False)
            # Exercise the installed launcher and both installed environments as
            # real processes, including queue ownership across two handoffs.
            managed_process = subprocess.Popen([sys.executable, str(launcher), "runtime", "run", "--config", str(config)])
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
                wait_for(lambda: replacement(second), timeout=75)
                request_stop(config)
                assert managed_process.wait(timeout=45) == 0
                assert server.state.presence["runtime-test"] == "offline"
                q.acquire_run_lock()
                q.release_run_lock()
            finally:
                if managed_process.poll() is None:
                    try:
                        request_stop(config)
                        managed_process.wait(timeout=45)
                    except Exception:
                        managed_process.kill()
                        managed_process.wait(timeout=10)
            print("PASS: staged venv installation, live managed update/rollback handoff, opt-in updates and released queue", flush=True)
        finally:
            updates.fetch = original_fetch
    print("Runtime smoke passed. Fake relay, unavailable Herdr; no production messages, login restart or GitHub release publication tested.")


if __name__ == "__main__":
    main()
