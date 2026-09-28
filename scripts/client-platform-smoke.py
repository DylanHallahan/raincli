"""One-off native client checks. Uses a loopback fake relay and fake Herdr.

Run with a Python environment containing the installed --no-deps client.
No production configuration or credentials are read. No pytest dependency.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raincli/tests/agent"))
from fake_server import FakeApi
from raincli_agent.attachments import load_for_send, prepare_dir, AttachmentError
from raincli_agent.api import ApiClient
from raincli_agent.config import load_config
from raincli_agent.connector.config import load_connector_config
from raincli_agent.connector.herdr import FakeHerdr
from raincli_agent.connector.queue import Queue
from raincli_agent.connector.runner import Connector
from raincli_agent.errors import ConfigError
from raincli_agent.fsutil import atomic_write_json, read_private_file

checks = []


def passed(label):
    checks.append(label)
    print(f"PASS: {label}", flush=True)


def child_lock():
    queue = Queue(sys.argv[2])
    from raincli_agent.connector.queue import ConnectorBusy
    if sys.argv[3] == "short":
        with queue.lock():
            print("acquired", flush=True)
    else:
        try:
            queue.acquire_run_lock()
        except ConnectorBusy:
            sys.exit(23)
        queue.release_run_lock()


def main():
    executable = shutil.which("raincli")
    assert executable, "installed raincli executable missing from PATH"
    with tempfile.TemporaryDirectory(prefix="raincli smoke ü ") as tmp, FakeApi() as server:
        root = Path(tmp)
        env = dict(os.environ, PYTHONUTF8="1")
        env.pop("RAINCLI_CONFIG", None)
        # CLI gets an explicit disposable config every time.
        def cli(handle, *args, code=0):
            result = subprocess.run([executable, "--config", str(root / f"{handle}.json"), *map(str, args)],
                                    env=env, capture_output=True, text=True, encoding="utf-8", timeout=25)
            assert result.returncode == code, (args, result.returncode, result.stdout, result.stderr)
            return result.stdout
        for handle in ("alice", "bob"):
            token = server.state.add_agent(handle)
            source = root / f"download-{handle}.json"
            source.write_text(json.dumps({"api_url": server.url, "token": token}), encoding="utf-8")
            cli(handle, "config", "init", "--api-url", server.url, "--token-file", source)
            source.unlink()
            assert json.loads(cli(handle, "whoami", "--json"))["agent"]["handle"] == handle
        passed("installed CLI, config import, protected credential read, identity")
        original = read_private_file(root / "alice.json")
        atomic_write_json(root / "alice.json", json.loads(original))
        assert read_private_file(root / "alice.json") == original
        passed("atomic credential replacement preserves protection")
        skill = subprocess.run([executable, "--skill"], capture_output=True, check=True).stdout
        assert skill == (Path(__file__).resolve().parents[1] / "raincli/raincli_agent/skill/SKILL.md").read_bytes()
        passed("packaged skill from installed executable")
        data = "# Report\r\nCafé — context\n".encode("utf-8")
        report = root / "report.md"
        report.write_bytes(data)
        mid = str(uuid.uuid4())
        send_args = ("send", "--to", "bob", "--body", "Please review", "--attach", report, "--id", mid, "--json")
        sent = json.loads(cli("alice", *send_args))
        assert sent["created"] and sent["message"]["id"] == mid
        assert not json.loads(cli("alice", *send_args))["created"]
        cli("bob", "inbox", "--all", "--json")
        dest = root / "download with spaces"
        cli("bob", "fetch", mid, "--dir", dest)
        assert (dest / "report.md").read_bytes() == data
        assert "already present" in cli("bob", "fetch", mid, "--dir", dest)
        (dest / "report.md").write_bytes(b"keep my changes")
        cli("bob", "fetch", mid, "--dir", dest, code=3)
        assert (dest / "report.md").read_bytes() == b"keep my changes"
        passed("send, idempotent retry, inbox, exact UTF-8/CRLF attachment, no overwrite")
        reply = json.loads(cli("bob", "reply", mid, "--body", "Reviewed", "--json"))["message"]
        assert reply["in_reply_to"] == mid and reply["to"] == "alice"
        passed("linked reply between two client identities")
        shareable = root / "shareable"
        shareable.mkdir()
        conf = root / "connector.json"
        atomic_write_json(conf, {"agent_config": str(root / "bob.json"), "herdr_agent": "bob-inbox",
                                "expect_pane_id": "w9:p1", "state_dir": str(root / "queue"),
                                "mode": "inbox", "shareable_context": [str(shareable)], "trust_mode": "team"})
        cfg = load_connector_config(conf)
        bob = load_config(root / "bob.json")
        herdr = FakeHerdr()
        herdr.add("bob-inbox", status="idle", pane_id="w9:p1")
        queue = Queue(cfg.state_dir)
        conn = Connector(cfg, ApiClient(bob.api_url, bob.token), herdr, queue, log=lambda line: None)
        conn.run_once()
        assert queue.get(mid)["state"] == "submitted" and len(herdr.prompts) == 1
        assert server.state.messages[mid]["acked_at"] is not None
        conn2 = Connector(cfg, ApiClient(bob.api_url, bob.token), herdr, Queue(cfg.state_dir), log=lambda line: None)
        conn2.run_once()
        assert len(herdr.prompts) == 1
        passed("connector stores, fetches, acknowledges, submits to FAKE Herdr; restart does not duplicate")
        lock_cmd = [sys.executable, str(Path(__file__).resolve()), "--lock-child", cfg.state_dir]
        queue.acquire_run_lock()
        assert subprocess.run([*lock_cmd, "run"], timeout=10).returncode == 23
        queue.release_run_lock()
        assert subprocess.run([*lock_cmd, "run"], timeout=10).returncode == 0
        with queue.lock():
            child = subprocess.Popen([*lock_cmd, "short"], stdout=subprocess.PIPE, text=True)
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            else:
                raise AssertionError("second process bypassed short lock")
        output, _ = child.communicate(timeout=10)
        assert child.returncode == 0 and "acquired" in output
        # OS must release the run lock when its owning process is killed.
        holder_code = "import sys,time; from raincli_agent.connector.queue import Queue; q=Queue(sys.argv[1]); q.acquire_run_lock(); print('ready',flush=True); time.sleep(60)"
        holder = subprocess.Popen([sys.executable, "-c", holder_code, cfg.state_dir], stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "ready"
            assert subprocess.run([*lock_cmd, "run"], timeout=10).returncode == 23
        finally:
            holder.kill()
            holder.wait(timeout=10)
            holder.stdout.close()
        assert subprocess.run([*lock_cmd, "run"], timeout=10).returncode == 0
        passed("cross-process run/short locks, release and killed-process recovery")
        if os.name == "nt":
            # Grant Everyone read access: this MUST make credential loading fail.
            result = subprocess.run(["icacls", str(root / "alice.json"), "/grant", "*S-1-1-0:(R)"], capture_output=True)
            assert result.returncode == 0, result.stderr
            try:
                load_config(root / "alice.json")
            except ConfigError:
                pass
            else:
                raise AssertionError("world-readable Windows credential accepted")
            passed("Windows ACL rejects Everyone-readable credential")
            # Junction creation needs no symlink privilege.
            junction = root / "junction"
            subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(shareable)], check=True, capture_output=True)
            try:
                prepare_dir(root, "junction")
            except AttachmentError:
                pass
            else:
                raise AssertionError("junction accepted beneath attachment base")
            junction.rmdir()
            passed("Windows junction refused under managed attachment path")
        link = root / "linked.md"
        try:
            link.symlink_to(report)
        except OSError:
            if os.name == "nt":
                raise AssertionError("Windows runner must permit symlink test; cannot silently skip")
            raise
        try:
            load_for_send([str(link)])
        except AttachmentError:
            pass
        else:
            raise AssertionError("symlink attachment accepted")
        passed("symlink attachment refused")
    print(f"{len(checks)}/{len(checks)} checks passed on {sys.platform}, Python {sys.version.split()[0]}")
    print("Boundary: fake loopback relay + fake Herdr; not a production or live Herdr Windows test.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--lock-child":
        child_lock()
    else:
        main()
