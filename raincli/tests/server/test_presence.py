from datetime import timedelta

import pytest
from api_helpers import auth
from raincli_server import identity
from raincli_server.models import AgentPresence


def test_presence_is_owned_team_scoped_and_expires(client, world, session):
    alice = auth(world["tokens"]["alice"])
    bob = auth(world["tokens"]["bob"])
    eve = auth(world["tokens"]["eve"])
    r = client.put("/api/v1/presence", headers=alice, json={"status": "ready"})
    assert r.status_code == 200 and r.json()["presence"]["expires_at"]
    rows = client.get("/api/v1/agents", headers=bob).json()["agents"]
    assert next(a for a in rows if a["handle"] == "alice-agent")["presence"]["status"] == "ready"
    assert next(a for a in rows if a["handle"] == "bob-agent")["presence"]["status"] == "unknown"
    assert all(a["handle"] != "alice-agent" for a in client.get("/api/v1/agents", headers=eve).json()["agents"])
    row = session.get(AgentPresence, world["agents"]["alice"].id)
    row.seen_at -= timedelta(seconds=121)
    session.commit()
    rows = client.get("/api/v1/agents", headers=bob).json()["agents"]
    assert next(a for a in rows if a["handle"] == "alice-agent")["presence"]["status"] == "offline"
    client.put("/api/v1/presence", headers=alice, json={"status": "busy"})
    identity.revoke_agent(session, world["agents"]["alice"])
    session.commit()
    assert client.put("/api/v1/presence", headers=alice, json={"status": "ready"}).status_code == 401
    rows = client.get("/api/v1/agents", headers=bob).json()["agents"]
    assert next(a for a in rows if a["handle"] == "alice-agent")["presence"]["status"] == "offline"


@pytest.mark.parametrize("body", [{}, [], {"status": []}, {"status": True}, {"status": "bogus"},
                                    {"status": "ready", "agent": "bob-agent"},
                                    {"status": "ready", "seen_at": "2099-01-01"}])
def test_presence_rejects_invalid_and_spoofed_fields(client, world, body):
    assert client.put("/api/v1/presence", headers=auth(world["tokens"]["alice"]), json=body).status_code == 400


def test_presence_requires_ack_scope(client, world, session):
    _, token = identity.register_agent(session, world["teams"]["acme"], world["users"]["alice"], "reader", scopes=("messages:read",))
    session.commit()
    assert client.put("/api/v1/presence", headers=auth(token), json={"status": "ready"}).status_code == 403
    assert client.put("/api/v1/presence", json={"status": "ready"}).status_code == 401
