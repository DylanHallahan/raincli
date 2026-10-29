"""H1 (protocol §22, contract G12): a 0.6.1 connector ingests a group copy and holds it (its agent is busy); the
runtime then rolls back to the v0.6.0 or v0.5.1 tag on the SAME state directory. The old connector delivers
nothing (group records live in group-messages/, which it never lists), its poll (no groups=1) withdraws the copy on
the server, and the next post makes no copy for that member. A live server; skipped without the tags."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import pytest

from api_helpers import auth
from conftest_groups import groups_seed  # noqa: F401  (fixture)
from conftest_v06 import v06_seed  # noqa: F401  (fixture)
from raincli_server.models import Message
from test_v06_old_clients import tagged_client
from test_v060_cli import live  # noqa: F401  (fixture: a real uvicorn server)

OLD_RUN = """
import json, os, sys
sys.path.insert(0, payload["root"])
import raincli_agent
assert raincli_agent.__version__ == payload["version"], raincli_agent.__version__
from raincli_agent.api import ApiClient
from raincli_agent.connector.config import load_connector_config
from raincli_agent.connector.herdr import FakeHerdr
from raincli_agent.connector.queue import Queue
from raincli_agent.connector.runner import Connector

cfg = load_connector_config(payload["config"])
herdr = FakeHerdr()
herdr.add("inbox-agent", status="idle")  # ready to take anything it finds
sessions = os.path.join(payload["tmp"], "sessions"); os.makedirs(sessions, exist_ok=True)
conn = Connector(cfg, ApiClient(payload["url"], payload["token"]), herdr, Queue(cfg.state_dir), identity={},
                 log=lambda line: None, sleep=lambda s: None, sessions_state=sessions)
for _ in range(3):
    conn.run_once()
print(json.dumps({"prompts": [p[1] for p in herdr.prompts], "records": sorted(r["id"] for r in conn.queue.all())}))
"""


def _connector_config(tmp_path, url, token):
    agent = tmp_path / "agent.json"
    agent.write_text(json.dumps({"api_url": url, "token": token}))
    os.chmod(agent, 0o600)
    path = tmp_path / "connector.json"
    path.write_text(json.dumps({"agent_config": str(agent), "herdr_agent": "inbox-agent",
                                "state_dir": str(tmp_path / "queue"), "trusted_senders": ["maya@example.test"]}))
    return str(path)


@pytest.mark.parametrize("tag", ("v0.6.0", "v0.5.1"))
def test_a_rollback_never_delivers_a_held_group_copy(tag, live, client, session, groups_seed, tmp_path):  # noqa: F811
    from raincli_agent.api import ApiClient
    from raincli_agent.connector.config import load_connector_config
    from raincli_agent.connector.herdr import FakeHerdr
    from raincli_agent.connector.queue import Queue
    from raincli_agent.connector.runner import Connector

    s = groups_seed
    token = s["machine_tokens"]["jon"]
    config = _connector_config(tmp_path, live, token)
    cfg = load_connector_config(config)
    busy = FakeHerdr()
    busy.add("inbox-agent", status="working")
    new = Connector(cfg, ApiClient(live, token), busy, Queue(cfg.state_dir), identity={}, log=lambda line: None,
                    sleep=lambda s: None, sessions_state=str(tmp_path / "sessions-new"))
    os.makedirs(tmp_path / "sessions-new", exist_ok=True)
    pid = str(uuid.uuid4())
    client.post(f"/api/v1/groups/{s['groups']['checkout'].id}/posts", headers=auth(s["machine_tokens"]["maya"]),
                json={"id": pid, "body": "@Jon a group post"})
    new.run_once()
    session.expire_all()
    copy = session.query(Message).filter_by(post_id=uuid.UUID(pid), recipient_agent_id=s["machines"]["jon"].id).one()
    held = new.queue.get(str(copy.id))
    assert held["group"] is True and held["state"] in ("held", "agent_held") and copy.acked_at is not None
    assert (tmp_path / "queue" / "group-messages" / f"{copy.id}.json").is_file()
    assert copy.delivery_state == "held" and not busy.prompts

    # The runtime rolls back: the old connector runs on the same state directory.
    root = tagged_client(tag, tmp_path / "old")
    payload = {"root": str(root), "version": tag[1:], "config": config, "url": live, "token": token,
               "tmp": str(tmp_path)}
    result = subprocess.run([sys.executable, "-c", f"import json\npayload = json.loads({json.dumps(json.dumps(payload))})\n"
                             + OLD_RUN], capture_output=True, text=True, timeout=120,
                            env={**os.environ, "PYTHONPATH": "", "HOME": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["prompts"] == [] and str(copy.id) not in out["records"]  # nothing delivered, nothing listed
    session.expire_all()
    assert s["machines"]["jon"].groups_capable_at is None  # its poll had no groups=1: capability ended
    assert session.get(Message, copy.id).withdrawn_at is not None  # held, never to be delivered: withdrawn
    # The next post makes no copy for Jon while the old client runs.
    later = client.post(f"/api/v1/groups/{s['groups']['checkout'].id}/posts",
                        headers=auth(s["machine_tokens"]["maya"]),
                        json={"id": str(uuid.uuid4()), "body": "after the rollback"}).json()["post"]
    assert later["recipients_m"] == 1  # Priya only

    # Re-upgrade (the client review's C2): the 0.6.1 connector comes back on the same state directory, its agent
    # idle. The held record's copy is withdrawn on the server, so it is dropped, never handed over.
    ready = FakeHerdr()
    for name in ("inbox-agent", "zq-agent-jon-api"):
        ready.add(name, status="idle")
    again = Connector(cfg, ApiClient(live, token), ready, Queue(cfg.state_dir), identity={}, log=lambda line: None,
                      sleep=lambda s: None, sessions_state=str(tmp_path / "sessions-new"))
    again.run_once()
    again.run_once()
    assert not [p for p in ready.prompts if str(copy.id) in p[1]]
    assert not (tmp_path / "queue" / "group-messages" / f"{copy.id}.json").exists()
