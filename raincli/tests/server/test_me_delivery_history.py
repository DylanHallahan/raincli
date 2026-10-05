"""GET /api/v1/me reports delivery_history (review 2 R4): the §15.8 H2 replace rule's history."""

from __future__ import annotations

from tests.server.api_helpers import auth, send


def history(client, token):
    response = client.get("/api/v1/me", headers=auth(token))
    assert response.status_code == 200
    return response.json()["delivery_history"]


def test_new_machine_has_no_history(client, world):
    assert history(client, world["tokens"]["alice"]) is False


def test_a_message_recipient_has_history(client, world):
    tokens = world["tokens"]
    assert send(client, tokens["alice"], "bob-agent").status_code in (200, 201)
    assert history(client, tokens["bob"]) is True
    assert history(client, tokens["alice"]) is False  # sending is not delivery history


def test_a_published_inbox_role_is_history(client, world):
    token = world["tokens"]["alice"]
    body = {"status": "ready", "agents": [{"key": "a" * 16, "name": "inbox", "type": "claude", "status": "idle",
                                           "role": "inbox", "reachability": "instant", "source": "herdr"}]}
    assert client.put("/api/v1/presence", json=body, headers=auth(token)).status_code == 200
    # Still true after the live directory empties: the role was published once.
    assert client.put("/api/v1/presence", json={"status": "offline", "agents": []},
                      headers=auth(token)).status_code == 200
    assert history(client, token) is True
