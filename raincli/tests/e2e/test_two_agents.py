"""End-to-end: real uvicorn server, real PostgreSQL, two agent CLI processes.

Runs only when RAINCLI_TEST_DATABASE_URL is set (see scripts/test-postgres.sh).
The PostgreSQL-restart check needs RAINCLI_E2E_PG_CONTAINER naming a *dedicated*
container that is safe to restart (scripts/raincli-demo.sh provides one).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PG_CONTAINER = os.environ.get("RAINCLI_E2E_PG_CONTAINER", "")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(database_url, tmp_path):
    port = _free_port()
    env = dict(
        os.environ,
        RAINCLI_DATABASE_URL=database_url,
        RAINCLI_SECRET_KEY="e2e-secret-" + "k" * 40,
        RAINCLI_PUBLIC_URL=f"http://127.0.0.1:{port}",
        RAINCLI_COOKIE_SECURE="0",
        RAINCLI_MAX_PENDING="50",
    )
    log = open(tmp_path / "server.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "raincli_server.app:app_from_env", "--factory",
         "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with urllib.request.urlopen(base + "/api/v1/health", timeout=1) as r:
                if r.status == 200:
                    break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("server did not become healthy: " + (tmp_path / "server.log").read_text())
    yield base
    proc.terminate()
    proc.wait(10)
    log.close()


@pytest.fixture
def agents(world, server, tmp_path):
    configs = {}
    for who in ("alice", "bob", "eve"):
        path = tmp_path / f"{who}.json"
        path.write_text(json.dumps({"api_url": server, "token": world["tokens"][who]}))
        path.chmod(0o600)
        configs[who] = path
    return configs


def cli(configs, who, *args, check=True, input=None):
    env = dict(os.environ, RAINCLI_CONFIG=str(configs[who]))
    proc = subprocess.run([sys.executable, "-m", "raincli_agent", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=90, input=input)
    if check and proc.returncode != 0:
        pytest.fail(f"raincli {args} as {who} -> {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc


def inbox(configs, who, *extra):
    out = cli(configs, who, "inbox", "--json", *extra).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_send_reply_ack_roundtrip(agents, world):
    mid = str(uuid.uuid4())
    cli(agents, "alice", "send", "--to", "bob-agent", "--body-file", "-", "--id", mid,
        input="Can you review PR 12?\nThanks")
    # Retry of the same command must not duplicate.
    cli(agents, "alice", "send", "--to", "bob-agent", "--body-file", "-", "--id", mid,
        input="Can you review PR 12?\nThanks")
    msgs = inbox(agents, "bob")
    assert [m["id"] for m in msgs] == [mid]
    assert msgs[0]["from"] == "alice-agent" and msgs[0]["body"] == "Can you review PR 12?\nThanks"

    # Sender cannot acknowledge; recipient can, idempotently.
    assert cli(agents, "alice", "ack", mid, check=False).returncode == 3
    cli(agents, "bob", "ack", mid)
    cli(agents, "bob", "ack", mid)
    assert inbox(agents, "bob") == []

    cli(agents, "bob", "reply", mid, "--body", "Done, LGTM")
    reply = inbox(agents, "alice")
    assert len(reply) == 1 and reply[0]["in_reply_to"] == mid and reply[0]["from"] == "bob-agent"
    shown = json.loads(cli(agents, "alice", "show", mid, "--json").stdout)["message"]
    assert shown["delivery_state"] == "replied" and shown["acked_at"]


def test_offline_catch_up_and_watch(agents):
    ids = [str(uuid.uuid4()) for _ in range(3)]
    for i, mid in enumerate(ids):
        cli(agents, "alice", "send", "--to", "bob-agent", "--body", f"note {i}", "--id", mid)
    out = cli(agents, "bob", "watch", "--json", "--once", "--timeout", "10").stdout
    got = [json.loads(line)["id"] for line in out.splitlines() if line.strip()]
    assert got == ids
    # watch never acks
    assert [m["id"] for m in inbox(agents, "bob")] == ids


def test_cross_team_and_spoofing_rejected(agents, world, server):
    assert cli(agents, "eve", "send", "--to", "bob-agent", "--body", "hi", check=False).returncode != 0
    mid = str(uuid.uuid4())
    cli(agents, "alice", "send", "--to", "bob-agent", "--body", "private", "--id", mid)
    assert cli(agents, "eve", "show", mid, check=False).returncode != 0
    assert cli(agents, "eve", "ack", mid, check=False).returncode != 0
    req = urllib.request.Request(
        server + "/api/v1/messages", method="POST",
        data=json.dumps({"id": str(uuid.uuid4()), "to": "bob-agent", "body": "x", "from": "bob-agent"}).encode(),
        headers={"Authorization": "Bearer " + world["tokens"]["alice"], "Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=5)
    assert err.value.code == 403
    err.value.close()


def test_revoked_and_rotated_credentials_rejected(agents, world, session):
    from raincli_server import identity

    identity.rotate_agent_credential(session, world["agents"]["bob"])
    session.commit()
    assert cli(agents, "bob", "whoami", check=False).returncode != 0
    identity.revoke_agent(session, world["agents"]["alice"])
    session.commit()
    assert cli(agents, "alice", "send", "--to", "bob-agent", "--body", "x", check=False).returncode != 0


@pytest.mark.skipif(not PG_CONTAINER, reason="needs a dedicated restartable PostgreSQL container")
def test_postgres_restart_preserves_and_clients_recover(agents, engine):
    mid = str(uuid.uuid4())
    cli(agents, "alice", "send", "--to", "bob-agent", "--body", "before restart", "--id", mid)
    subprocess.run(["docker", "restart", "-t", "5", PG_CONTAINER], check=True, capture_output=True, timeout=120)
    engine.dispose()  # the test harness's own pooled connections died with the server
    # The client retries transient 503/connection failures; the server reconnects (pool_pre_ping).
    mid2 = str(uuid.uuid4())
    cli(agents, "alice", "send", "--to", "bob-agent", "--body", "after restart", "--id", mid2)
    assert [m["id"] for m in inbox(agents, "bob")] == [mid, mid2]


def test_markdown_attachments_exact_bytes_and_safe_fetch(agents, tmp_path):
    report = tmp_path / "Q3 report.md"
    data = "# Q3 report\r\n\r\n- ✅ shipped\n- trailing spaces   \n".encode("utf-8")
    report.write_bytes(data)
    ctx = tmp_path / "context.md"
    ctx.write_bytes(b"context\n")
    mid = str(uuid.uuid4())
    for _ in range(2):  # retry with same id + same attachments is idempotent
        cli(agents, "alice", "send", "--to", "bob-agent", "--body", "Report attached", "--id", mid,
            "--attach", str(report), "--attach", str(ctx))
    [msg] = inbox(agents, "bob")
    names = [(a["filename"], a["size"]) for a in msg["attachments"]]
    assert names == [("Q3 report.md", len(data)), ("context.md", 8)]

    dest = tmp_path / "inbox"
    cli(agents, "bob", "fetch", mid, "--dir", str(dest))
    assert (dest / "Q3 report.md").read_bytes() == data
    # Fetch again: identical content is skipped, not overwritten.
    cli(agents, "bob", "fetch", mid, "--dir", str(dest))
    # A different local file with the same name is never overwritten.
    (dest / "context.md").write_bytes(b"local edits\n")
    assert cli(agents, "bob", "fetch", mid, "--dir", str(dest), check=False).returncode == 3
    assert (dest / "context.md").read_bytes() == b"local edits\n"
    # Non-participant cannot fetch.
    assert cli(agents, "eve", "fetch", mid, "--dir", str(tmp_path / "eve"), check=False).returncode != 0
    assert not (tmp_path / "eve" / "Q3 report.md").exists()


def test_body_file_is_not_an_attachment(agents, tmp_path):
    body = tmp_path / "body.md"
    body.write_text("# Body only\n")
    cli(agents, "alice", "send", "--to", "bob-agent", "--body-file", str(body))
    [msg] = inbox(agents, "bob")
    assert msg["body"] == "# Body only\n" and msg["attachments"] == []


def test_skill_is_packaged(agents):
    out = cli(agents, "alice", "--skill").stdout
    packaged = (ROOT / "raincli_agent" / "skill" / "SKILL.md").read_text(encoding="utf-8")
    assert out == packaged and out.startswith("---\nname: raincli\n")
