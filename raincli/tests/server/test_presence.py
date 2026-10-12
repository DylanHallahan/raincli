from datetime import timedelta

import pytest
from api_helpers import auth
from raincli_server import identity
from raincli_server.models import AgentPresence


def _machine(client, headers, handle):
    """The caller's OWN machine entry (§21 C13 M2: /agents lists only the caller's machines)."""
    return next(a for a in client.get("/api/v1/agents", headers=headers).json()["agents"] if a["handle"] == handle)


def _person(client, headers, email):
    """A teammate as a person (§21 C5.3): presence comes from their inbox, never from a listed machine."""
    return next(p for p in client.get("/api/v1/people", headers=headers).json()["people"] if p["email"] == email)


def test_presence_is_owned_team_scoped_and_expires(client, world, session):
    alice = auth(world["tokens"]["alice"])
    bob = auth(world["tokens"]["bob"])
    eve = auth(world["tokens"]["eve"])
    r = client.put("/api/v1/presence", headers=alice, json={"status": "ready"})
    assert r.status_code == 200 and r.json()["presence"]["expires_at"]
    assert _machine(client, alice, "alice-agent")["presence"]["status"] == "ready"
    assert _machine(client, bob, "bob-agent")["presence"]["status"] == "unknown"
    # Bob sees Alice the person (her inbox is on that machine), never her machine.
    assert all(a["handle"] != "alice-agent" for a in client.get("/api/v1/agents", headers=bob).json()["agents"])
    assert _person(client, bob, "alice@example.test")["presence"] == "ready"
    assert _person(client, alice, "bob@example.test")["presence"] == "offline"  # Bob's inbox never reported
    # Another team sees neither the machine nor the person.
    for path in ("/api/v1/agents", "/api/v1/people"):
        text = client.get(path, headers=eve).text
        assert "alice-agent" not in text and "alice@example.test" not in text
    row = session.get(AgentPresence, world["agents"]["alice"].id)
    row.seen_at -= timedelta(seconds=121)
    session.commit()
    assert _machine(client, alice, "alice-agent")["presence"]["status"] == "offline"
    assert _person(client, bob, "alice@example.test") == {
        "display_name": "Alice", "email": "alice@example.test", "team": "Acme", "presence": "offline", "speed": None}
    client.put("/api/v1/presence", headers=alice, json={"status": "busy"})
    assert _machine(client, alice, "alice-agent")["presence"]["status"] == "busy"
    # Alice the person is back. Her inbox is her machine's own inbox agent (the inbox-mode path), so her presence
    # is the machine's (C5.4): busy.
    assert _person(client, bob, "alice@example.test")["presence"] == "busy"
    identity.revoke_agent(session, world["agents"]["alice"])
    session.commit()
    assert client.put("/api/v1/presence", headers=alice, json={"status": "ready"}).status_code == 401
    # Revoking the inbox machine removes the inbox (§21 C6.4): Alice is no longer reachable there.
    assert _person(client, bob, "alice@example.test")["presence"] == "no_inbox"
    assert all(a["handle"] != "alice-agent" for a in client.get("/api/v1/agents", headers=bob).json()["agents"])


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
