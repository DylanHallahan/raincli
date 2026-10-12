"""Old clients against the v0.6 server (protocol §21 C10, C13 H1, M1, M2): the real v0.5.1 and v0.5.6 client code,
taken from the tags, on the server's own responses. Skipped where the tags are absent (a shallow checkout)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import io
import uuid
from pathlib import Path

import pytest

from api_helpers import auth
from conftest_v06 import v06_seed  # noqa: F401  (fixture)
from raincli_server import identity
from raincli_server.models import MachineAgent

REPO = Path(__file__).resolve().parents[3]
TAGS = ("v0.5.1", "v0.5.6")


def tagged_client(tag, into: Path) -> Path:
    """``raincli_agent`` as tagged, unpacked under ``into``; skips when the tag is not here."""
    found = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}"], cwd=REPO,
                           capture_output=True, text=True)
    if found.returncode != 0:
        pytest.skip(f"the {tag} tag is not available here (shallow checkout)")
    archive = subprocess.run(["git", "archive", tag, "raincli/raincli_agent"], cwd=REPO, capture_output=True,
                             check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into / "raincli"


def run_old(tag, tmp_path, script, payload):
    root = tagged_client(tag, tmp_path / tag)
    (tmp_path / "payload.json").write_text(json.dumps(payload))
    code = f"""
import json, sys
sys.path.insert(0, {str(root)!r})
import raincli_agent
assert raincli_agent.__version__ == {tag[1:]!r}, raincli_agent.__version__
payload = json.load(open({str(tmp_path / 'payload.json')!r}))
{script}
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                            env={**os.environ, "PYTHONPATH": "", "HOME": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


POLL_TWICE = """
import os
from raincli_agent.connector.config import load_connector_config
from raincli_agent.connector.herdr import FakeHerdr
from raincli_agent.connector.queue import Queue
from raincli_agent.connector.runner import Connector

class Api:
    def __init__(self):
        self.acks, self.polls = [], []
    def inbox(self, after, wait=0, limit=100, routing=False, **kw):
        self.polls.append(after)
        return payload["messages"], payload["cursor"]
    def ack(self, message_id):
        self.acks.append(message_id)
        return {}

tmp = payload["tmp"]
agent_cfg = os.path.join(tmp, "agent.json")
open(agent_cfg, "w").write(json.dumps({"api_url": "http://127.0.0.1:9", "token": "rc_x"}))
os.chmod(agent_cfg, 0o600)
cfg_path = os.path.join(tmp, "connector.json")
open(cfg_path, "w").write(json.dumps({"agent_config": agent_cfg, "herdr_agent": "inbox-agent",
                                      "state_dir": os.path.join(tmp, "queue"), "trusted_senders": []}))
cfg = load_connector_config(cfg_path)
queue = Queue(cfg.state_dir)
with queue.lock():
    queue.set_cursor(payload["start_cursor"])
api = Api()
sessions = os.path.join(tmp, "sessions"); os.makedirs(sessions, exist_ok=True)
conn = Connector(cfg, api, FakeHerdr(), queue, identity={"handle": "zq-handle-sam-app", "team": "northwind"},
                 log=lambda line: None, sleep=lambda s: None, sessions_state=sessions)
conn.poll()
after_first = queue.cursor()
conn.poll()  # the server sends it again (as if the ack were lost): still one record, cursor unchanged
records = [r["id"] for r in queue.all()]
print(json.dumps({"records": records, "cursor": [after_first, queue.cursor()], "acks": api.acks,
                  "targets": [r.get("target") for r in queue.all()]}))
"""


@pytest.mark.parametrize("tag", TAGS)
def test_a_redelivered_message_below_the_cursor_is_ingested_once(tag, client, session, v06_seed, tmp_path):  # noqa: F811
    """H1: Sam picks an inbox; the waiting message comes below the cursor of his machine's poll."""
    sam = v06_seed["machine_tokens"]["sam"]
    session.add(MachineAgent(agent_id=v06_seed["machines"]["sam"].id, key="herdr:zq-agent-sam", name="zq-agent-sam",
                             type="claude", status="idle", reachability="instant", source="herdr",
                             seen_at=identity.now()))
    session.commit()
    high = 10_000
    assert client.put("/api/v1/me/inbox", headers=auth(sam), json={"agent": "zq-agent-sam"}).json()[
        "delivered_waiting"] == 1
    page = client.get("/api/v1/inbox", headers=auth(sam), params={"routing": 1, "after": high}).json()
    hello = str(v06_seed["messages"]["to_sam"].id)
    assert [m["id"] for m in page["messages"]] == [hello] and page["cursor"] == high
    out = run_old(tag, tmp_path, POLL_TWICE, {"messages": page["messages"], "cursor": page["cursor"],
                                              "start_cursor": high, "tmp": str(tmp_path)})
    assert out["records"] == [hello] and out["cursor"] == [high, high]
    assert out["acks"][:1] == [hello] and out["targets"] == ["zq-agent-sam"]
    # The person-shaped sender is framed as a person by the old client: no handle of Maya's machine.
    assert page["messages"][0]["from_endpoint"] == {"person": "maya@example.test", "display_name": "Maya"}


AGENTS_V051 = """
import types, io, contextlib
from raincli_agent import cli
args = types.SimpleNamespace(json=False)
buf = io.StringIO()
cli.client = lambda a: types.SimpleNamespace(agents=lambda: payload["agents"])
with contextlib.redirect_stdout(buf):
    code = cli.cmd_agents(args)
print(json.dumps({"code": code, "out": buf.getvalue()}))
"""

STATUS_V056 = """
import types
from raincli_agent.app import services
me = payload["me"]
agents = payload["agents"]
found = next((m for m in agents if m.get("handle") == me["handle"]), {})
shown = [{k: a.get(k) for k in ("name", "type", "status", "reachability")} for a in found.get("agents") or []]
print(json.dumps({"shown": shown}))
"""


def test_old_agents_parsers_on_the_new_agents_response(client, v06_seed, tmp_path):  # noqa: F811
    """M2: /agents lists only the caller's own machines, in the old shape; v0.5.1 ``raincli agents`` prints them and
    the v0.5.6 This computer status still finds its own row."""
    agents = client.get("/api/v1/agents", headers=auth(v06_seed["machine_tokens"]["maya"])).json()["agents"]
    out = run_old("v0.5.1", tmp_path, AGENTS_V051, {"agents": agents})
    assert out["code"] == 0 and out["out"].startswith("zq-handle-maya")
    assert "zq-handle-jon" not in out["out"]
    status = run_old("v0.5.6", tmp_path, STATUS_V056, {"agents": agents, "me": {"handle": "zq-handle-maya"}})
    assert {a["name"] for a in status["shown"]} == {"zq-agent-maya-laptop", "zq-agent-maya-notes"}


def test_old_client_request_shapes(client, session, v06_seed):  # noqa: F811
    """C10.1 and M1: what a v0.5.1 client sends still works: a send to {person: email}, a reply to the person (or to
    the machine it saw before), /conversations, /conversations/{id}/messages, archive, and a send naming an old id."""
    maya, jon = v06_seed["machine_tokens"]["maya"], v06_seed["machine_tokens"]["jon"]
    sent = client.post("/api/v1/messages", headers=auth(maya), json={
        "id": str(uuid.uuid4()), "to": {"person": "jon@example.test"}, "body": "hello", "from_agent": "zq-agent-maya-laptop"})
    assert sent.status_code == 201, sent.text
    mid = sent.json()["message"]["id"]
    # The old client's `raincli reply` sends to the from_endpoint it saw: now the person.
    seen = client.get(f"/api/v1/messages/{mid}", headers=auth(jon)).json()["message"]
    assert seen["from_endpoint"] == {"person": "maya@example.test", "display_name": "Maya"}
    reply = client.post("/api/v1/messages", headers=auth(jon), json={
        "id": str(uuid.uuid4()), "to": {"person": seen["from_endpoint"]["person"]}, "body": "answer",
        "in_reply_to": mid})  # what v0.5.1 cli_me.reply_endpoint sends for a person
    assert reply.status_code == 201, reply.text
    conv = seen["conversation_id"]
    rows = client.get("/api/v1/conversations", headers=auth(jon)).json()["conversations"]
    assert conv in {r["id"] for r in rows}
    assert client.get(f"/api/v1/conversations/{conv}/messages", headers=auth(jon)).status_code == 200
    assert client.post(f"/api/v1/conversations/{conv}/archive", headers=auth(jon)).status_code == 200
    assert client.post(f"/api/v1/conversations/{conv}/unarchive", headers=auth(jon)).status_code == 200
    # A send naming an id that was merged away resolves to where it went.
    from raincli_server.models import Conversation
    old = Conversation(id=uuid.uuid4(), team_id=v06_seed["teams"]["northwind"].id,
                       agent_a_id=v06_seed["machines"]["maya"].id, a_key=f"m:{v06_seed['machines']['maya'].id}",
                       agent_b_id=v06_seed["machines"]["jon"].id, b_key=f"m:{v06_seed['machines']['jon'].id}",
                       is_default=False, merged_into=uuid.UUID(conv))
    if old.a_key > old.b_key:
        old.a_key, old.b_key, old.agent_a_id, old.agent_b_id = old.b_key, old.a_key, old.agent_b_id, old.agent_a_id
    session.add(old)
    session.commit()
    again = client.post("/api/v1/messages", headers=auth(maya), json={
        "id": str(uuid.uuid4()), "to": {"person": "jon@example.test"}, "body": "x", "conversation_id": str(old.id)})
    assert again.status_code == 201 and again.json()["message"]["conversation_id"] == conv, again.text
