"""Person sessions and the person API (protocol §16.3, §16.4, §16.12 C6, C9, C11, C15), real PostgreSQL."""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from api_helpers import auth, err
from raincli_server import identity
from raincli_server.models import Agent, PersonSession

PASSWORD = "correct horse battery"


def app_login(client, email="alice@example.test", machine_name="alice-laptop", **extra):
    return client.post("/api/v1/app/login", json={"email": email, "password": PASSWORD,
                                                  "machine_name": machine_name, **extra})


@pytest.fixture
def alice(client, world):
    """alice signs in a machine with a person session (§16.12 C15)."""
    body = app_login(client, person_session=True).json()
    return {"token": body["token"], "person": body["person_session"], "world": world}


def psend(client, person, to, body="hello", **extra):
    payload = {"id": str(extra.pop("id", uuid.uuid4())), "to": to, "body": body, **extra}
    return client.post("/api/v1/person/send", headers=auth(person), json=payload)


def person_of(client, email, machine_name):
    return app_login(client, email=email, machine_name=machine_name, person_session=True).json()["person_session"]


# Issuing ---------------------------------------------------------------------------------

def test_person_session_only_when_asked(client, world, session):
    plain = app_login(client).json()
    assert "person_session" not in plain
    body = app_login(client, machine_name="alice-two", person_session=True).json()
    assert body["person_session"].startswith("rps_") and body["token"].startswith("rca_")
    row = session.scalar(select(PersonSession))
    assert row.scopes == ["person:read", "person:send"] and row.token_hash != body["person_session"]
    assert session.get(Agent, row.machine_agent_id).handle == "alice-two"


def test_person_only_adds_a_session_without_rotation(client, world, session):
    token = app_login(client).json()["token"]
    r = app_login(client, person_only=True, previous_token=token)
    assert r.status_code == 200 and set(r.json()) == {"person_session"}
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 200  # not rotated
    assert client.get("/api/v1/person/me", headers=auth(r.json()["person_session"])).status_code == 200
    # No machine name needed, and no machine created or renamed.
    r = client.post("/api/v1/app/login", json={"email": "alice@example.test", "password": PASSWORD,
                                               "person_only": True, "previous_token": token})
    assert r.status_code == 200
    assert session.scalar(select(Agent.handle).where(Agent.handle == "alice-laptop")) == "alice-laptop"
    for bad in ({"previous_token": world["tokens"]["bob"]}, {"previous_token": "rca_nope"}, {},
                {"previous_token": token, "replace": True}):
        r = client.post("/api/v1/app/login", json={"email": "alice@example.test", "password": PASSWORD,
                                                   "person_only": True, **bad})
        assert r.status_code == 400 and err(r) == "invalid", bad


def test_person_only_counts_like_any_sign_in(client, world, app):
    token = app_login(client).json()["token"]
    for _ in range(8):  # a correct password is a limiter success, whatever the outcome
        client.post("/api/v1/app/login", json={"email": "alice@example.test", "password": PASSWORD,
                                               "person_only": True, "previous_token": world["tokens"]["bob"]})
    r = client.post("/api/v1/app/login", json={"email": "alice@example.test", "password": "wrong password!!",
                                               "person_only": True, "previous_token": token})
    assert r.status_code == 401 and err(r) == "invalid_credentials"


# Authentication boundaries ------------------------------------------------------------------

def test_credentials_stay_on_their_side(client, alice):
    person, token = alice["person"], alice["token"]
    assert client.get("/api/v1/me", headers=auth(person)).status_code == 401
    assert client.get("/api/v1/inbox", headers=auth(person)).status_code == 401
    assert client.get("/api/v1/person/me", headers=auth(token)).status_code == 401
    assert client.get("/api/v1/person/inbox", headers=auth(token)).status_code == 401
    me = client.get("/api/v1/person/me", headers=auth(person)).json()
    assert me["user"] == {"display_name": "Alice", "email": "alice@example.test"}
    assert me["teams"] == [{"slug": "acme", "name": "Acme"}] and set(me["session"]) == {"created_at", "expires_at"}


def test_lifetime_idle_and_absolute(client, alice, session):
    person = alice["person"]
    session.execute(update(PersonSession).values(last_used_at=identity.now() - timedelta(days=31)))
    session.commit()
    assert client.get("/api/v1/person/me", headers=auth(person)).status_code == 401
    session.execute(update(PersonSession).values(last_used_at=identity.now(),
                                                 created_at=identity.now() - timedelta(days=181)))
    session.commit()
    assert client.get("/api/v1/person/me", headers=auth(person)).status_code == 401
    session.execute(update(PersonSession).values(created_at=identity.now() - timedelta(days=179)))
    session.commit()
    assert client.get("/api/v1/person/me", headers=auth(person)).status_code == 200


# Revocation (C6) -----------------------------------------------------------------------------

def test_person_sign_out_revokes_only_that_session(client, alice):
    other = person_of(client, "alice@example.test", "alice-two")
    assert client.post("/api/v1/person/sign-out", headers=auth(alice["person"])).json() == {"signed_out": True}
    assert client.get("/api/v1/person/me", headers=auth(alice["person"])).status_code == 401
    assert client.get("/api/v1/person/me", headers=auth(other)).status_code == 200
    assert client.get("/api/v1/me", headers=auth(alice["token"])).status_code == 200


@pytest.mark.parametrize("trigger", ["app_sign_out", "revoke_machine", "rotate_website", "rotate_operator",
                                     "app_re_sign_in", "password_change", "deactivate"])
def test_every_trigger_revokes(client, alice, session, trigger):
    person, token, world = alice["person"], alice["token"], alice["world"]
    machine = session.scalar(select(Agent).where(Agent.handle == "alice-laptop"))
    if trigger == "app_sign_out":
        assert client.post("/api/v1/app/sign-out", headers=auth(token)).status_code == 200
    elif trigger == "revoke_machine":
        identity.revoke_agent(session, machine, world["users"]["alice"])
    elif trigger == "rotate_website":
        identity.rotate_agent_credential(session, machine, world["users"]["alice"])
    elif trigger == "rotate_operator":
        identity.rotate_agent_credential(session, machine)
    elif trigger == "app_re_sign_in":
        assert app_login(client, previous_token=token).status_code == 200
    elif trigger == "password_change":
        from test_web_app import app_csrf, login
        login(client)
        r = client.post("/app/account/password", data={
            "csrf_token": app_csrf(client), "current_password": PASSWORD, "password": "a brand new passphrase",
            "password_confirm": "a brand new passphrase"}, follow_redirects=False)
        assert r.status_code == 303
    elif trigger == "deactivate":
        identity.set_user_active(session, world["users"]["alice"], False)
    session.commit()
    assert client.get("/api/v1/person/me", headers=auth(person)).status_code == 401


def test_member_removal_keeps_the_session_but_rechecks_membership(client, world, session):
    """C6: removal from a team doesn't revoke the session; every request re-checks membership."""
    identity.add_member(session, world["teams"]["globex"], world["users"]["alice"])
    session.commit()
    person = app_login(client, team="acme", person_session=True).json()["person_session"]
    sent = psend(client, person, "eve-agent", team="globex").json()["message"]
    identity.remove_member(session, world["teams"]["globex"], world["users"]["alice"])
    session.commit()
    assert client.get("/api/v1/person/me", headers=auth(person)).json()["teams"] == [{"slug": "acme", "name": "Acme"}]
    assert client.get(f"/api/v1/person/messages/{sent['id']}", headers=auth(person)).status_code == 404
    r = psend(client, person, "eve-agent", team="globex")
    assert r.status_code == 400 and err(r) == "invalid"
    assert psend(client, person, "bob-agent").status_code == 201  # acme still works


def test_removal_from_the_machines_team_ends_the_session(client, world, session):
    bob = person_of(client, "bob@example.test", "bob-laptop")
    identity.remove_member(session, world["teams"]["acme"], world["users"]["bob"])
    session.commit()
    # bob's machines in acme are revoked with the membership (§11.5), and so are their sessions (C6).
    assert client.get("/api/v1/person/me", headers=auth(bob)).status_code == 401


# The person API ----------------------------------------------------------------------------

def test_person_to_person_states(client, alice):
    bob = person_of(client, "bob@example.test", "bob-laptop")
    sent = psend(client, alice["person"], {"person": "bob@example.test"}, body="**hi** bob").json()["message"]
    assert sent["from"] == "@alice@example.test" and sent["to"] == "@bob@example.test"
    assert sent["delivery_state"] == "stored" and sent["kind"] == "message"
    inbox = client.get("/api/v1/person/inbox", headers=auth(bob)).json()
    assert [m["id"] for m in inbox["messages"]] == [sent["id"]]
    acked = client.post(f"/api/v1/person/messages/{sent['id']}/ack", headers=auth(bob)).json()
    assert acked["acked"] is True and acked["message"]["delivery_state"] == "received"
    assert client.post(f"/api/v1/person/messages/{sent['id']}/ack", headers=auth(bob)).json()["acked"] is False
    assert client.post(f"/api/v1/person/messages/{sent['id']}/ack", headers=auth(alice["person"])).status_code == 403
    reply = psend(client, bob, {"person": "alice@example.test"}, in_reply_to=sent["id"]).json()["message"]
    assert reply["conversation_id"] == sent["conversation_id"]
    seen = client.get(f"/api/v1/person/messages/{sent['id']}", headers=auth(alice["person"])).json()["message"]
    assert seen["delivery_state"] == "replied"
    convs = client.get("/api/v1/person/conversations", headers=auth(alice["person"])).json()["conversations"]
    assert [c["peer"] for c in convs] == ["@bob@example.test"]
    thread = client.get(f"/api/v1/person/conversations/{sent['conversation_id']}", headers=auth(alice["person"])).json()
    assert [m["id"] for m in thread["messages"]] == [sent["id"], reply["id"]]


def test_person_to_machine_and_reply_to_person(client, alice):
    world = alice["world"]
    sent = psend(client, alice["person"], "bob-agent").json()["message"]
    # bob's v0.5 connector sees it; a v0.4 connector would not (§16.2 gate).
    assert client.get("/api/v1/inbox", headers=auth(world["tokens"]["bob"])).json()["messages"] == []
    got = client.get("/api/v1/inbox?routing=1", headers=auth(world["tokens"]["bob"])).json()["messages"]
    assert [m["id"] for m in got] == [sent["id"]] and got[0]["from_endpoint"]["person"] == "alice@example.test"
    r = client.post("/api/v1/messages", headers=auth(world["tokens"]["bob"]), json={
        "id": str(uuid.uuid4()), "to": {"person": "alice@example.test"}, "body": "back", "in_reply_to": sent["id"]})
    assert r.status_code == 201
    assert [m["body"] for m in client.get("/api/v1/person/inbox", headers=auth(alice["person"])).json()["messages"]] \
        == ["back"]


def test_person_sees_messages_of_their_machines(client, alice):
    world = alice["world"]
    r = client.post("/api/v1/messages", headers=auth(world["tokens"]["bob"]),
                    json={"id": str(uuid.uuid4()), "to": "alice-agent", "body": "to the machine"})
    mid = r.json()["message"]["id"]
    assert client.get(f"/api/v1/person/messages/{mid}", headers=auth(alice["person"])).status_code == 200
    eve = person_of(client, "eve@example.test", "eve-laptop")
    assert client.get(f"/api/v1/person/messages/{mid}", headers=auth(eve)).status_code == 404


def test_person_send_rules(client, alice, session):
    world, person = alice["world"], alice["person"]
    r = psend(client, person, {"person": "alice@example.test"})
    assert r.status_code == 400 and err(r) == "invalid"  # never to yourself
    r = psend(client, person, {"person": "alice@example.test"}, kind="escalation")
    assert r.status_code == 400 and err(r) == "invalid"  # C11: escalations come from machines
    r = psend(client, person, {"person": "eve@example.test"})
    assert r.status_code == 400 and err(r) == "invalid"  # another team
    r = client.post("/api/v1/person/send", headers=auth(person), json={"id": str(uuid.uuid4()), "to": "bob-agent",
                                                                        "body": "x", "from_agent": "spoof"})
    assert r.status_code == 400 and err(r) == "invalid"
    # C9: several teams need "team".
    identity.add_member(session, world["teams"]["globex"], world["users"]["alice"])
    session.commit()
    r = psend(client, person, "bob-agent")
    assert r.status_code == 400 and r.json()["error"]["code"] == "team_required"
    assert {t["slug"] for t in r.json()["teams"]} == {"acme", "globex"}
    assert psend(client, person, "bob-agent", team="acme").status_code == 201
    assert psend(client, person, "eve-agent", team="globex").status_code == 201
    r = psend(client, person, "eve-agent", team="acme")
    assert r.status_code == 400 and err(r) == "invalid"
    r = psend(client, person, "bob-agent", team="nope")
    assert r.status_code == 400 and err(r) == "invalid"


def test_person_attachments(client, alice):
    bob = person_of(client, "bob@example.test", "bob-laptop")
    data = b"# Notes\n\nhello\n"
    att = {"filename": "notes.md", "content_b64": base64.b64encode(data).decode(),
           "sha256": hashlib.sha256(data).hexdigest()}
    sent = psend(client, alice["person"], {"person": "bob@example.test"}, attachments=[att]).json()["message"]
    att_id = sent["attachments"][0]["id"]
    for ref in ("1", att_id):
        r = client.get(f"/api/v1/person/messages/{sent['id']}/attachments/{ref}", headers=auth(bob))
        assert r.status_code == 200 and r.content == data
        assert r.headers["content-disposition"] == 'attachment; filename="notes.md"'
        assert r.headers["x-raincli-sha256"] == hashlib.sha256(data).hexdigest()
    assert client.get(f"/api/v1/person/messages/{sent['id']}/attachments/2", headers=auth(bob)).status_code == 404


def test_person_rate_limit_per_session(settings, world, engine):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from raincli_server.app import create_app

    app = create_app(replace(settings, rate_limit_per_min=3))
    try:
        with TestClient(app) as client:
            person = person_of(client, "alice@example.test", "alice-laptop")
            codes = [client.get("/api/v1/person/me", headers=auth(person)).status_code for _ in range(4)]
            assert codes == [200, 200, 200, 429]
    finally:
        app.state.engine.dispose()


def test_person_inbox_long_poll_returns_on_arrival(client, alice):
    import threading
    import time

    bob = person_of(client, "bob@example.test", "bob-laptop")
    timer = threading.Timer(1.0, lambda: psend(client, alice["person"], {"person": "bob@example.test"}, body="late"))
    started = time.monotonic()
    timer.start()
    got = client.get("/api/v1/person/inbox?wait=10", headers=auth(bob)).json()["messages"]
    timer.join()
    assert [m["body"] for m in got] == ["late"] and time.monotonic() - started < 9


# §16.16 lead decisions ---------------------------------------------------------------------

def test_me_names_the_machines_owner(client, alice):
    me = client.get("/api/v1/me", headers=auth(alice["token"])).json()
    assert me["owner"] == {"email": "alice@example.test", "display_name": "Alice"}
    bob = client.get("/api/v1/me", headers=auth(alice["world"]["tokens"]["bob"])).json()
    assert bob["owner"] == {"email": "bob@example.test", "display_name": "Bob"}


def test_from_same_owner_is_decided_by_the_server(client, alice):
    world = alice["world"]

    def send(token, to):
        r = client.post("/api/v1/messages", headers=auth(token), json={"id": str(uuid.uuid4()), "to": to, "body": "x"})
        assert r.status_code == 201, r.text
        return r.json()["message"]

    # alice-laptop (signed in above) and alice-agent are both alice's machines.
    assert send(alice["token"], "alice-agent")["from_same_owner"] is True
    assert send(world["tokens"]["bob"], "alice-agent")["from_same_owner"] is False
    assert send(alice["token"], "bob-agent")["from_same_owner"] is False
    assert send(world["tokens"]["bob"], {"person": "alice@example.test"})["from_same_owner"] is False  # not a machine
    assert psend(client, alice["person"], "alice-agent").json()["message"]["from_same_owner"] is True
    assert psend(client, alice["person"], "bob-agent").json()["message"]["from_same_owner"] is False
    seen = client.get("/api/v1/inbox?routing=1", headers=auth(world["tokens"]["alice"])).json()["messages"]
    assert sorted((m["from"], m["from_same_owner"]) for m in seen) == [
        ("@alice@example.test", True), ("alice-laptop", True), ("bob-agent", False)]


def test_person_reply_takes_the_parents_team(client, alice, session):
    world, person = alice["world"], alice["person"]
    identity.add_member(session, world["teams"]["globex"], world["users"]["alice"])
    session.commit()
    r = client.post("/api/v1/messages", headers=auth(world["tokens"]["bob"]),
                    json={"id": str(uuid.uuid4()), "to": {"person": "alice@example.test"}, "body": "question"})
    parent = r.json()["message"]
    r = psend(client, person, "bob-agent", "answer", in_reply_to=parent["id"])  # no team needed
    assert r.status_code == 201 and r.json()["message"]["conversation_id"] == parent["conversation_id"]
    r = psend(client, person, "bob-agent", "again", in_reply_to=parent["id"], team="globex")  # team is ignored
    assert r.status_code == 201 and r.json()["message"]["conversation_id"] == parent["conversation_id"]
    r = psend(client, person, "bob-agent", "new")  # a new conversation still needs it
    assert r.status_code == 400 and r.json()["error"]["code"] == "team_required"
    r = psend(client, person, "bob-agent", "x", in_reply_to=str(uuid.uuid4()))  # unknown or unseen: 404
    assert r.status_code == 404
    r = psend(client, person, "bob-agent", "x", in_reply_to="not-a-uuid")
    assert r.status_code == 400 and err(r) == "invalid"
