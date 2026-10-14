"""Archived conversations (protocol §17.2, §17.3) on real PostgreSQL: who may archive, the shared,
position-based state, the list filters, a newer message leaving the archive, and delivery never affected."""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text

from api_helpers import auth, err, send
from raincli_server import identity

PASSWORD = "correct horse battery"
# §21 (v0.6): teammates are addressed as people; each person's inbox is their own machine (conftest). New sends
# land in the one person-pair conversation of (team, alice, bob), which either side's machine may read and archive.
ALICE, BOB = "alice@example.test", "bob@example.test"
P_ALICE = {"person": ALICE, "display_name": "Alice"}
P_BOB = {"person": BOB, "display_name": "Bob"}


def conv_of(client, token, to=BOB, body="hello"):
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
    assert conv["archived_by"] == P_ALICE and conv["archived_through_seq"] > 0
    assert conv["peer_endpoint"] == P_BOB and conv["peer"] == "Bob"
    for token in (alice, bob):  # one shared state: gone from both main lists
        assert cid not in listed(client, token)
        assert cid in listed(client, token, "only") and cid in listed(client, token, "include")
        assert listed(client, token, "only")[cid]["archived_by"] == P_ALICE
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
    assert again.json()["conversation"]["archived_by"] == P_ALICE
    assert archive(client, alice, cid, undo=True).status_code == 200
    r = archive(client, alice, cid, undo=True)
    assert r.status_code == 200 and r.json()["conversation"]["archived_at"] is None


@pytest.mark.parametrize("sender", ["alice", "bob"])
def test_a_new_message_from_either_side_unarchives(client, world, sender):
    alice, bob = world["tokens"]["alice"], world["tokens"]["bob"]
    cid, _ = conv_of(client, alice)
    archive(client, alice, cid)
    assert cid not in listed(client, alice)
    to = BOB if sender == "alice" else ALICE
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
    carol = identity.create_user(session, "carol@example.test", "Carol", PASSWORD)
    identity.add_member(session, world["teams"]["acme"], carol)
    _, other_token = identity.register_agent(session, world["teams"]["acme"], carol, "carol-agent")
    session.commit()
    for token in (eve, other_token):  # another team; the same team but neither side's machine
        for undo in (False, True):
            r = archive(client, token, cid, undo=undo)
            assert r.status_code == 404 and err(r) == "not_found"
    for bogus in (str(uuid.uuid4()), "not-a-uuid"):
        r = archive(client, alice, bogus)
        assert r.status_code == 404 and err(r) == "not_found"
    assert listed(client, alice)[cid]["archived_at"] is None  # nothing changed
    # §21 C13 M1: by contrast, any machine of a side's owner may read and archive the pair conversation
    _, bob_two = identity.register_agent(session, world["teams"]["acme"], world["users"]["bob"], "bob-two")
    session.commit()
    assert cid in listed(client, bob_two)
    assert archive(client, bob_two, cid).json()["conversation"]["archived_by"] == P_BOB
    assert archive(client, bob_two, cid, undo=True).status_code == 200
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
    # Their machines' messages land in the same pair conversation (§21), which the people archive as its sides.
    mid, _ = conv_of(client, world["tokens"]["alice"])
    assert mid == pid
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
    conv_of(client, bob, to=ALICE, body="two, not yet seen by alice")
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


class Background:
    """Run ``fn`` in its own thread; ``blocked()`` says it is still waiting (on a row lock) after a pause."""

    def __init__(self, fn):
        self.result, self.error = None, None
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self.thread.start()

    def _run(self, fn):
        try:
            self.result = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
            self.error = exc

    def blocked(self, pause=0.7):
        self.thread.join(pause)
        return self.thread.is_alive()

    def join(self):
        self.thread.join(10)
        assert not self.thread.is_alive(), "still waiting after 10 s"
        if self.error is not None:
            raise self.error
        return self.result


def sessions(app, n):
    out = [app.state.sessionmaker() for _ in range(n)]
    for s in out:  # a wait we did not expect fails instead of hanging
        s.execute(text("SET lock_timeout = '10s'"))
    return out


def raced_send(session, agent_id, to, body="raced"):
    from raincli_server import messaging
    from raincli_server.models import Agent

    msg, _ = messaging.send_message(session, session.get(Agent, agent_id), to=to, body=body, id=str(uuid.uuid4()),
                                    max_pending=1000)
    return msg


def raced_archive(session, agent_id, cid, through=None):
    from raincli_server import messaging
    from raincli_server.models import Agent

    return messaging.archive_as_machine(session, session.get(Agent, agent_id), cid, True, through)


def test_an_archive_in_flight_makes_a_send_wait_then_the_send_unarchives(client, world, app):
    """A1, archive commits first: the send waits for it, then its newer seq leaves the conversation listed."""
    alice, bob = world["agents"]["alice"].id, world["agents"]["bob"].id
    cid, _ = conv_of(client, world["tokens"]["alice"])
    s_archive, s_send = sessions(app, 2)
    try:
        raced_archive(s_archive, bob, cid)
        send = Background(lambda: (raced_send(s_send, alice, BOB), s_send.commit())[0])
        assert send.blocked()  # KEY SHARE waits for the archive's FOR UPDATE
        s_archive.commit()
        msg = send.join()
    finally:
        s_archive.close()
        s_send.close()
    row = listed(client, world["tokens"]["bob"])[cid]
    assert row["archived"] is False and row["last_seq"] == msg.seq


def test_a_send_in_flight_makes_an_archive_wait_and_a_stale_position_keeps_it_listed(client, world, app):
    """A1, send commits first: the archive waits for the send. Archived through what the archiver saw (before
    the send), the conversation stays listed; archived through "the newest", it covers the committed message."""
    alice, bob = world["agents"]["alice"].id, world["agents"]["bob"].id
    cid, _ = conv_of(client, world["tokens"]["alice"])
    seen = listed(client, world["tokens"]["bob"])[cid]["last_seq"]
    for through in (seen, None):
        s_send, s_archive = sessions(app, 2)
        try:
            msg = raced_send(s_send, alice, BOB)
            archived = Background(lambda: (raced_archive(s_archive, bob, cid, through), s_archive.commit()))
            assert archived.blocked()  # FOR UPDATE waits for the send's KEY SHARE
            s_send.commit()
            archived.join()
        finally:
            s_send.close()
            s_archive.close()
        row = listed(client, world["tokens"]["bob"], "include")[cid]
        if through is None:
            assert row["archived"] is True and row["archived_through_seq"] == msg.seq == row["last_seq"]
        else:
            assert row["archived"] is False and row["last_seq"] == msg.seq
            seen = msg.seq


def test_opposite_direction_sends_never_let_an_archive_skip_a_lower_seq(client, world, app):
    """L1: A->B takes the lower seq and stays uncommitted. B->A, in the same team, waits for it to commit before
    it takes the next number (§21.13: a team's numbers commit in order), so no lower number can commit after a
    higher one; an archive in between waits for A->B and covers only committed messages."""
    alice, bob = world["agents"]["alice"].id, world["agents"]["bob"].id
    cid, _ = conv_of(client, world["tokens"]["alice"])
    s_low, s_high, s_archive = sessions(app, 3)
    try:
        low = raced_send(s_low, alice, BOB, "lower seq")
        high = Background(lambda: (raced_send(s_high, bob, ALICE, "higher seq"), s_high.commit())[0])
        assert high.blocked()  # the team's next number waits for the uncommitted lower one
        archived = Background(lambda: (raced_archive(s_archive, bob, cid), s_archive.commit()))
        assert archived.blocked()  # and so does an archive: its max(seq) never passes the uncommitted low.seq
        s_low.commit()
        high = high.join()
        archived.join()
        assert low.seq < high.seq and low.conversation_id == high.conversation_id == uuid.UUID(cid)
    finally:
        for s in (s_low, s_high, s_archive):
            s.close()
    row = listed(client, world["tokens"]["bob"], "include")[cid]
    assert row["archived"] is True and row["archived_through_seq"] == high.seq == row["last_seq"]
    # Every message at or below the position was committed when the archive read it: none appears later.
    ids = {m["id"] for m in client.get(f"/api/v1/conversations/{cid}/messages",
                                       headers=auth(world["tokens"]["bob"])).json()["messages"]}
    assert {str(low.id), str(high.id)} <= ids
