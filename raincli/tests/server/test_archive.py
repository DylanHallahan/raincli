"""Archived conversations (protocol §17.2, §17.3) on real PostgreSQL: who may archive, the shared,
position-based state, the list filters, a newer message leaving the archive, and delivery never affected."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from api_helpers import auth, err, send
from raincli_server import identity

PASSWORD = "correct horse battery"


def conv_of(client, token, to="bob-agent", body="hello"):
    r = send(client, token, to, body)
    assert r.status_code == 201, r.text
    return r.json()["message"]["conversation_id"], r.json()["message"]["id"]


def listed(client, token, archived="exclude", person=False):
    path = "/api/v1/person/conversations" if person else "/api/v1/conversations"
    r = client.get(path + (f"?archived={archived}" if archived else ""), headers=auth(token))
    assert r.status_code == 200, r.text
    return {c["id"]: c for c in r.json()["conversations"]}


def archive(client, token, cid, undo=False, person=False, through=None):
    base = "/api/v1/person/conversations" if person else "/api/v1/conversations"
    body = {"json": {"archived_through_seq": through}} if through is not None else {}
    return client.post(f"{base}/{cid}/{'unarchive' if undo else 'archive'}", headers=auth(token), **body)


def person_session(client, email, machine):
    r = client.post("/api/v1/app/login", json={"email": email, "password": PASSWORD, "machine_name": machine,
                                               "person_session": True})
    assert r.status_code == 201, r.text
    return r.json()["person_session"]


def psend(client, person, to, body="hello"):
    r = client.post("/api/v1/person/send", headers=auth(person),
                    json={"id": str(uuid.uuid4()), "to": to, "body": body})
    assert r.status_code == 201, r.text
    return r.json()["message"]


def test_either_machine_archives_for_both_and_either_unarchives(client, world):
    alice, bob = world["tokens"]["alice"], world["tokens"]["bob"]
    cid, _ = conv_of(client, alice)
    assert listed(client, alice)[cid]["archived_at"] is None and listed(client, alice)[cid]["archived_by"] is None
    r = archive(client, alice, cid)
    assert r.status_code == 200
    conv = r.json()["conversation"]
    assert conv["id"] == cid and conv["archived"] is True and conv["archived_at"].endswith("Z")
    assert conv["archived_by"] == {"machine": "alice-agent"} and conv["archived_through_seq"] > 0
    assert conv["peer_endpoint"] == {"machine": "bob-agent"}
    for token in (alice, bob):  # one shared state: gone from both main lists
        assert cid not in listed(client, token)
        assert cid in listed(client, token, "only") and cid in listed(client, token, "include")
        assert listed(client, token, "only")[cid]["archived_by"] == {"machine": "alice-agent"}
    r = archive(client, bob, cid, undo=True)  # the other side restores it for both
    assert r.status_code == 200 and r.json()["conversation"]["archived_at"] is None
    for token in (alice, bob):
        assert cid in listed(client, token) and cid not in listed(client, token, "only")


def test_archive_and_unarchive_are_idempotent(client, world):
    alice, bob = world["tokens"]["alice"], world["tokens"]["bob"]
    cid, _ = conv_of(client, alice)
    first = archive(client, alice, cid).json()["conversation"]
    again = archive(client, bob, cid)  # already archived: 200 with the current state, unchanged
    assert again.status_code == 200 and again.json()["conversation"]["archived_at"] == first["archived_at"]
    assert again.json()["conversation"]["archived_by"] == {"machine": "alice-agent"}
    assert archive(client, alice, cid, undo=True).status_code == 200
    r = archive(client, alice, cid, undo=True)
    assert r.status_code == 200 and r.json()["conversation"]["archived_at"] is None


@pytest.mark.parametrize("sender", ["alice", "bob"])
def test_a_new_message_from_either_side_unarchives(client, world, sender):
    alice, bob = world["tokens"]["alice"], world["tokens"]["bob"]
    cid, _ = conv_of(client, alice)
    archive(client, alice, cid)
    assert cid not in listed(client, alice)
    to = "bob-agent" if sender == "alice" else "alice-agent"
    new_cid, _ = conv_of(client, world["tokens"][sender], to=to, body="back again")
    assert new_cid == cid
    for token in (alice, bob):
        row = listed(client, token)[cid]
        assert row["archived_at"] is None and row["archived_by"] is None
    thread = client.get(f"/api/v1/conversations/{cid}/messages", headers=auth(bob)).json()["messages"]
    assert [m["body"] for m in thread] == ["hello", "back again"]  # the full history comes back


def test_delivery_is_never_affected(client, world):
    """Messages in an archived conversation are delivered, acked and evented exactly as in an active one."""
    alice, bob = world["tokens"]["alice"], world["tokens"]["bob"]
    cid, mid = conv_of(client, alice, body="before the archive")
    archive(client, bob, cid)
    inbox = client.get("/api/v1/inbox", headers=auth(bob)).json()["messages"]
    assert [m["id"] for m in inbox] == [mid]
    r = client.post(f"/api/v1/messages/{mid}/ack", headers=auth(bob))
    assert r.status_code == 200 and r.json()["message"]["delivery_state"] == "received"
    r = client.post(f"/api/v1/messages/{mid}/events", headers=auth(bob), json={"state": "submitted"})
    assert r.status_code == 200 and r.json()["message"]["delivery_state"] == "submitted"
    assert cid in listed(client, alice, "only")  # acks and events never unarchive; only a new message does
    r = client.get(f"/api/v1/messages/{mid}", headers=auth(alice))
    assert r.status_code == 200 and r.json()["message"]["delivery_state"] == "submitted"
    later_cid, later = conv_of(client, alice, body="sent while archived")  # stored and delivered as usual
    inbox = client.get("/api/v1/inbox", headers=auth(bob)).json()["messages"]
    assert later_cid == cid and later in [m["id"] for m in inbox]


def test_outsiders_get_404_with_no_hint(client, world, session):
    alice, eve = world["tokens"]["alice"], world["tokens"]["eve"]
    cid, _ = conv_of(client, alice)
    other_agent, other_token = identity.register_agent(session, world["teams"]["acme"], world["users"]["bob"],
                                                       "bob-two")
    session.commit()
    for token in (eve, other_token):  # another team; the same team but not an endpoint
        for undo in (False, True):
            r = archive(client, token, cid, undo=undo)
            assert r.status_code == 404 and err(r) == "not_found"
    for bogus in (str(uuid.uuid4()), "not-a-uuid"):
        r = archive(client, alice, bogus)
        assert r.status_code == 404 and err(r) == "not_found"
    assert listed(client, alice)[cid]["archived_at"] is None  # nothing changed
    eve_person = person_session(client, "eve@example.test", "eve-laptop")
    r = archive(client, eve_person, cid, person=True)
    assert r.status_code == 404 and err(r) == "not_found"


def test_persons_archive_as_endpoints_and_as_owners(client, world):
    alice_p = person_session(client, "alice@example.test", "alice-laptop")
    bob_p = person_session(client, "bob@example.test", "bob-laptop")
    # A person endpoint: alice (person) and bob (person).
    pid = psend(client, alice_p, {"person": "bob@example.test"})["conversation_id"]
    r = archive(client, alice_p, pid, person=True)
    assert r.status_code == 200
    assert r.json()["conversation"]["archived_by"] == {"person": "alice@example.test", "display_name": "Alice"}
    assert pid not in listed(client, bob_p, person=True) and pid in listed(client, bob_p, "only", person=True)
    assert archive(client, bob_p, pid, undo=True, person=True).status_code == 200
    assert pid in listed(client, alice_p, person=True)
    # The person who owns a machine endpoint: alice owns alice-agent, bob owns bob-agent.
    mid, _ = conv_of(client, world["tokens"]["alice"])
    r = archive(client, alice_p, mid, person=True)
    assert r.status_code == 200 and r.json()["conversation"]["archived_by"]["person"] == "alice@example.test"
    assert mid not in listed(client, world["tokens"]["bob"])
    assert archive(client, bob_p, mid, undo=True, person=True).status_code == 200
    assert mid in listed(client, world["tokens"]["alice"])


def test_membership_is_checked_on_every_request(client, world, session):
    alice_p = person_session(client, "alice@example.test", "alice-laptop")
    bob_p = person_session(client, "bob@example.test", "bob-laptop")
    pid = psend(client, alice_p, {"person": "bob@example.test"})["conversation_id"]
    identity.remove_member(session, world["teams"]["acme"], world["users"]["bob"])
    session.commit()
    r = archive(client, bob_p, pid, person=True)
    assert r.status_code in (401, 404)  # removed from the team: never allowed
    assert listed(client, alice_p, person=True)[pid]["archived_at"] is None


def test_the_archived_filter_is_validated(client, world):
    for path, token in (("/api/v1/conversations", world["tokens"]["alice"]),):
        r = client.get(path + "?archived=maybe", headers=auth(token))
        assert r.status_code == 400 and err(r) == "invalid"


def test_person_conversation_shows_the_archive_state(client, world):
    alice_p = person_session(client, "alice@example.test", "alice-laptop")
    pid = psend(client, alice_p, {"person": "bob@example.test"})["conversation_id"]
    archive(client, alice_p, pid, person=True)
    conv = client.get(f"/api/v1/person/conversations/{pid}", headers=auth(alice_p)).json()["conversation"]
    assert conv["archived_at"] and conv["archived_by"]["person"] == "alice@example.test"



# -- §17.3 ------------------------------------------------------------------------------------------------

def test_a_machine_naming_no_filter_sees_everything_and_a_person_the_main_list(client, world):
    """A4: v0.5.1 and older clients send no ``archived`` and must never lose a conversation."""
    alice_p = person_session(client, "alice@example.test", "alice-laptop")
    cid, _ = conv_of(client, world["tokens"]["alice"])
    pid = psend(client, alice_p, {"person": "bob@example.test"})["conversation_id"]
    archive(client, world["tokens"]["alice"], cid)
    archive(client, alice_p, pid, person=True)
    assert cid in listed(client, world["tokens"]["alice"], archived=None)  # machine: include
    assert pid not in listed(client, alice_p, archived=None, person=True)  # person: exclude


def test_archived_through_seq_is_what_the_archiver_saw(client, world):
    """A1: archived only while no message is newer than archived_through_seq, which is capped."""
    alice, bob = world["tokens"]["alice"], world["tokens"]["bob"]
    cid, _ = conv_of(client, alice, body="one")
    seen = listed(client, alice)[cid]["last_seq"]
    conv_of(client, bob, to="alice-agent", body="two, not yet seen by alice")
    r = archive(client, alice, cid, through=seen)  # archived through what alice displayed
    assert r.status_code == 200 and r.json()["conversation"]["archived"] is False  # a newer one exists
    assert cid in listed(client, alice)
    newest = listed(client, alice)[cid]["last_seq"]
    r = archive(client, alice, cid, through=newest + 1000)  # capped at the newest message
    assert r.json()["conversation"]["archived"] is True and r.json()["conversation"]["archived_through_seq"] == newest
    assert archive(client, alice, cid, through=-1).status_code == 400
    assert client.post(f"/api/v1/conversations/{cid}/archive", headers=auth(alice),
                       json={"other": 1}).status_code == 400


def test_archiving_never_touches_inboxes_or_counts(client, world):
    """A3: the inbox and the unread count are unchanged; the Archived view shows the unread count."""
    alice, bob = world["tokens"]["alice"], world["tokens"]["bob"]
    cid, mid = conv_of(client, alice, body="unread")
    before = client.get("/api/v1/inbox", headers=auth(bob)).json()["messages"]
    archive(client, bob, cid)
    after = client.get("/api/v1/inbox", headers=auth(bob)).json()["messages"]
    assert [m["id"] for m in after] == [m["id"] for m in before] == [mid]
    assert listed(client, bob, "only")[cid]["unacked"] == 1


@pytest.mark.parametrize("order", ["archive_commits_first", "send_commits_first"])
def test_a_send_racing_an_archive_always_leaves_the_conversation_listed(client, world, app, order):
    """A1: no send-path write; whichever transaction commits first, the newer message keeps it listed."""
    from raincli_server import messaging
    from raincli_server.models import Agent

    alice, bob = world["tokens"]["alice"], world["tokens"]["bob"]
    cid, _ = conv_of(client, alice)
    make = app.state.sessionmaker
    s_archive, s_send = make(), make()
    for s in (s_archive, s_send):  # a lock wait would mean the send path contends with archive: fail, don't hang
        s.execute(text("SET lock_timeout = '5s'"))
    try:
        bob_agent = s_archive.get(Agent, world["agents"]["bob"].id)
        alice_agent = s_send.get(Agent, world["agents"]["alice"].id)
        if order == "archive_commits_first":
            messaging.archive_as_machine(s_archive, bob_agent, cid, True)
            msg, _ = messaging.send_message(s_send, alice_agent, to="bob-agent", body="raced", id=str(uuid.uuid4()),
                                              max_pending=1000)
            s_archive.commit()
            s_send.commit()
        else:
            msg, _ = messaging.send_message(s_send, alice_agent, to="bob-agent", body="raced", id=str(uuid.uuid4()),
                                              max_pending=1000)
            messaging.archive_as_machine(s_archive, bob_agent, cid, True)  # cannot see the uncommitted message
            s_send.commit()
            s_archive.commit()
    finally:
        s_archive.close()
        s_send.close()
    row = listed(client, bob)[cid]
    assert row["archived"] is False and row["last_seq"] == msg.seq
