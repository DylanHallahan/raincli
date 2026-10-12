"""The client reads the real server's message JSON (real PostgreSQL): the C17 frame shows the owner
from ``from_owner`` (§18.1 item 7), and the test fake renders the same keys as messaging.message_json."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
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
                       json={"id": str(uuid.uuid4()), "to": "bob@example.test", "body": "please review"})
    assert sent.status_code == 201, sent.text
    [message] = client.get("/api/v1/inbox", headers=auth(world["tokens"]["bob"])).json()["messages"]
    return message


def test_the_frame_shows_a_teammate_machine_as_its_person(client, world):
    """§21 C5.2: a teammate's machine reaches the reader as its person; no handle, owner or agent name."""
    message = received(client, world)
    assert message["from_owner"] is None and message["from_agent"] is None
    assert message["from"] == "@alice@example.test" and message["from_kind"] == "agent"
    assert message["from_endpoint"] == {"person": "alice@example.test", "display_name": "Alice"}
    # The reading machine's own endpoint keeps its machine form, so the connector sees its own target.
    assert message["to"] == "bob-agent" and message["to_endpoint"] == {"machine": "bob-agent"}
    assert "alice-agent" not in str(message)
    record = {"id": message["id"], "sender": message["from"], "message": message, "target": None}
    text = frame_record(record, {"handle": "bob-agent", "team": "acme"})
    assert text.split("\n")[1] == 'From: "Alice" <alice@example.test> | team acme'


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
    # The real server shows a teammate's machine as its person (§21 C5.2). The client tests' fake still renders the
    # machine form the v0.5 client was written against; the v0.6.0 client work moves it (and tightens this) along.
    assert set(real["from_endpoint"]) == {"person", "display_name"}
    assert fake["from_owner"] is None and real["from_owner"] is None
    assert set(fake["from_endpoint"]) in ({"machine"}, {"machine", "agent"}, {"person", "display_name"})
    del bob
