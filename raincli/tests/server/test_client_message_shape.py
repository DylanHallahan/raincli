"""The client reads the real server's message JSON (real PostgreSQL): the C17 frame shows the owner
from ``from_owner`` (§18.1 item 7), and the test fake renders the same keys as messaging.message_json."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

from api_helpers import auth

from raincli_agent.connector.runner import frame_record


def _fake_state():
    path = Path(__file__).resolve().parents[1] / "agent" / "fake_server.py"
    spec = importlib.util.spec_from_file_location("raincli_test_fake_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FakeState()


def received(client, world):
    sent = client.post("/api/v1/messages", headers=auth(world["tokens"]["alice"]),
                       json={"id": str(uuid.uuid4()), "to": "bob-agent", "body": "please review"})
    assert sent.status_code == 201, sent.text
    [message] = client.get("/api/v1/inbox", headers=auth(world["tokens"]["bob"])).json()["messages"]
    return message


def test_the_frame_shows_the_owner_the_server_sends(client, world):
    message = received(client, world)
    assert message["from_owner"] == "Alice" and "owner" not in message["from_endpoint"]
    record = {"id": message["id"], "sender": message["from"], "message": message, "target": None}
    text = frame_record(record, {"handle": "bob-agent", "team": "acme"})
    assert text.split("\n")[1] == 'From: machine alice-agent (owner "Alice") | team acme'


def test_a_person_sender_never_gets_an_owner(client, world):
    message = received(client, world)
    message = {**message, "from": "@alice@example.test", "from_owner": "Mallory",
               "from_endpoint": {"person": "alice@example.test", "display_name": "Alice"}}
    record = {"id": message["id"], "sender": message["from"], "message": message, "target": None}
    line = frame_record(record, {"handle": "bob-agent", "team": "acme"}).split("\n")[1]
    assert line == 'From: "Alice" <alice@example.test> | team acme'


def test_the_fake_server_renders_the_real_message_keys(client, world):
    real = received(client, world)
    state = _fake_state()
    alice, bob = state.add_agent("alice"), state.add_agent("bob")
    body = {"id": str(uuid.uuid4()), "to": "bob", "body": "please review"}
    with state.lock:  # as the fake's HTTP handler holds it
        status, reply = state.send(state.agents[state.tokens[alice]], body)
    fake = reply["message"]
    assert set(fake) == set(real), {"only in the fake": set(fake) - set(real), "only real": set(real) - set(fake)}
    assert fake["from_owner"] is None and set(fake["from_endpoint"]) == set(real["from_endpoint"])
    del bob
