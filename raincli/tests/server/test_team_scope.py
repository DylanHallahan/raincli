"""Conversations belong to exactly one team (protocol §18.1 item 3), real PostgreSQL."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from api_helpers import auth
from raincli_server import identity, messaging
from raincli_server.models import Conversation, Message
from test_archive import PASSWORD
from test_web_app import login


@pytest.fixture
def shared(client, world, session):
    """Alice and Bob share acme and globex; each has a person session per team, from a machine in that team (an app
    sign-in covers its machine's team, §21.13)."""
    globex = world["teams"]["globex"]
    for name in ("alice", "bob"):
        identity.add_member(session, globex, world["users"][name])
    session.commit()

    def person(name):
        sessions = {}
        for team in ("globex", "acme"):
            r = client.post("/api/v1/app/login", json={"email": f"{name}@example.test", "password": PASSWORD,
                                                       "machine_name": f"{name}-{team[:2]}", "team": team,
                                                       "person_session": True})
            assert r.status_code == 201, r.text
            sessions[team] = r.json()["person_session"]
        return sessions
    return {"alice": person("alice"), "bob": person("bob")}


def psend(client, person, team, body, **extra):
    """``person``: their sessions by team; the one for ``team`` sends."""
    r = client.post("/api/v1/person/send", headers=auth(person[team]),
                    json={"id": str(uuid.uuid4()), "to": {"person": "bob@example.test"}, "body": body, "team": team,
                          **extra})
    assert r.status_code == 201, r.text
    return r.json()["message"]


def test_a_pair_in_two_teams_has_one_conversation_per_team(client, world, shared, session):
    in_globex = psend(client, shared["alice"], "globex", "about globex")
    in_acme = psend(client, shared["alice"], "acme", "about acme")
    assert in_globex["conversation_id"] != in_acme["conversation_id"]
    teams = dict(session.execute(select(Conversation.id, Conversation.team_id)).all())
    assert teams[uuid.UUID(in_acme["conversation_id"])] == world["teams"]["acme"].id
    assert teams[uuid.UUID(in_globex["conversation_id"])] == world["teams"]["globex"].id
    assert psend(client, shared["alice"], "acme", "again")["conversation_id"] == in_acme["conversation_id"]


def test_a_removed_member_cannot_read_the_other_teams_messages(client, app, world, shared, session):
    keep = psend(client, shared["alice"], "globex", "still shared")
    gone = psend(client, shared["alice"], "acme", "acme only")
    identity.remove_member(session, world["teams"]["acme"], world["users"]["bob"])
    session.commit()
    bob = shared["bob"]["globex"]
    r = client.get(f"/api/v1/person/conversations/{gone['conversation_id']}", headers=auth(bob))
    assert r.status_code == 404
    r = client.get(f"/api/v1/person/conversations/{keep['conversation_id']}", headers=auth(bob))
    assert r.status_code == 200 and "acme only" not in r.text and "still shared" in r.text
    listed = client.get("/api/v1/person/conversations?archived=include", headers=auth(bob)).json()["conversations"]
    assert {c["id"] for c in listed} == {keep["conversation_id"]}
    with TestClient(app) as browser:
        assert login(browser, "bob@example.test").status_code == 303
        assert browser.get(f"/app/conversations/{gone['conversation_id']}").status_code == 404
        page = browser.get(f"/app/conversations/{keep['conversation_id']}").text
        assert "still shared" in page and "acme only" not in page
        assert "acme only" not in browser.get("/app/inbox").text


def test_a_reply_cannot_cross_teams(client, world, shared, session):
    parent = psend(client, shared["alice"], "acme", "parent in acme")
    # The person API files a reply in the parent's team, whatever team the request names.
    r = client.post("/api/v1/person/send", headers=auth(shared["bob"]["acme"]),
                    json={"id": str(uuid.uuid4()), "to": {"person": "alice@example.test"}, "body": "reply",
                          "team": "globex", "in_reply_to": parent["id"]})
    assert r.status_code == 201 and r.json()["message"]["conversation_id"] == parent["conversation_id"]
    # Sending into another team with that parent is refused outright.
    bob = messaging.Endpoint("person", user=session.get(type(world["users"]["bob"]), world["users"]["bob"].id))
    with pytest.raises(messaging.MessagingError) as refused:
        messaging.send(session, bob, world["teams"]["globex"].id, id=str(uuid.uuid4()),
                       to={"person": "alice@example.test"}, body="cross", in_reply_to=parent["id"], max_pending=100)
    assert refused.value.status == 404
    session.rollback()


def test_the_database_refuses_a_message_in_another_teams_conversation(client, world, shared, session):
    acme = psend(client, shared["alice"], "acme", "acme")
    globex = psend(client, shared["alice"], "globex", "globex")
    with pytest.raises(IntegrityError):
        session.execute(update(Message).where(Message.id == uuid.UUID(globex["id"]))
                        .values(conversation_id=uuid.UUID(acme["conversation_id"])))
        session.flush()
    session.rollback()


def test_a_retried_send_naming_a_conversation_it_was_moved_from_is_the_same_message(client, world, shared, session):
    first = psend(client, shared["alice"], "acme", "moved once")
    old = psend(client, shared["alice"], "globex", "other thread")["conversation_id"]
    session.execute(update(Message).where(Message.id == uuid.UUID(first["id"]))
                    .values(moved_from_conversation_id=uuid.UUID(old)))
    session.commit()
    r = client.post("/api/v1/person/send", headers=auth(shared["alice"]["acme"]),
                    json={"id": first["id"], "to": {"person": "bob@example.test"}, "body": "moved once",
                          "team": "acme", "conversation_id": old})
    assert r.status_code == 200 and r.json()["created"] is False, r.text
    assert r.json()["message"]["conversation_id"] == first["conversation_id"]
