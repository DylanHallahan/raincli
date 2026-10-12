"""Routing (protocol §16.1, §16.2, §16.5, §16.12 C7, C8, C10, C11, C12) under v0.6 person addressing (§21 C2-C5,
C8, C13), real PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from api_helpers import auth, err
from raincli_server import identity, messaging
from raincli_server.models import Agent, Conversation, KnownAgent, Message, PersonInbox, User

ALICE, BOB = "alice-agent", "bob-agent"
BOB_EMAIL = "bob@example.test"
ADDRESS_BOB = "Address people: send to Bob (bob@example.test)"


def entry(name, key, reachability="instant", role=None, source="herdr", status="idle", **extra):
    return {"key": key, "name": name, "type": "claude", "status": status, "role": role,
            "reachability": reachability, "source": source, **extra}


# A machine's own inbox agent (role=inbox). The world fixture makes it each person's inbox (the inbox-mode path,
# §21 C3.1a); a presence report replaces the machine's directory rows, so reports keep it to stay on that path.
INBOX = entry("inbox", "9" * 32, role="inbox", reachability="next-turn")

CLIENT_050 = {"version": "0.5.0", "update_mode": "manual", "update_state": "current"}


def report(client, token, agents, client_block=CLIENT_050):
    body = {"status": "ready", "agents": agents}
    if client_block is not None:
        body["client"] = client_block
    r = client.put("/api/v1/presence", headers=auth(token), json=body)
    assert r.status_code == 200, r.text


def send(client, token, to, body="hello", **extra):
    payload = {"id": str(extra.pop("id", uuid.uuid4())), "to": to, "body": body, **extra}
    if to is None:
        del payload["to"]
    return client.post("/api/v1/messages", headers=auth(token), json=payload)


def poll(client, token, routing=True):
    q = "?routing=1" if routing else ""
    return client.get(f"/api/v1/inbox{q}", headers=auth(token)).json()["messages"]


def to_agent(name, machine=BOB):
    return {"machine": machine, "agent": name}


def pick_inbox(client, token, agent):
    """§21 C8.1: a machine credential picks an agent on its own machine as its owner's inbox."""
    return client.put("/api/v1/me/inbox", headers=auth(token), json={"agent": agent})


def stored(session, message_id):
    session.expire_all()
    return session.get(Message, uuid.UUID(message_id))


@pytest.fixture
def bob_reports(client, world):
    """bob's runtime reports its inbox, a Herdr agent, a hook session, a listed scan entry and an ambiguous name."""
    report(client, world["tokens"]["bob"], [
        INBOX,
        entry("reviewer", "a" * 32),
        entry("notes", "b" * 32, reachability="next-turn", source="hook"),
        entry("claude", "c" * 32, reachability="listed", source="scan", status="unknown"),
        entry("twin", "d" * 32, reachability="listed", ambiguous=True),
        entry("twin", "e" * 32, reachability="listed", ambiguous=True, source="hook"),
    ])
    return world


# §16.2 order, now applied to inbox picks (§21 C3.5): teammates address people, never agents ----------------------

def test_live_deliverable_agents_are_accepted_as_an_inbox_and_reached_through_the_person(client, bob_reports,
                                                                                        session):
    alice, bob = bob_reports["tokens"]["alice"], bob_reports["tokens"]["bob"]
    for name in ("reviewer", "notes"):
        r = pick_inbox(client, bob, name)
        assert r.status_code == 200 and r.json()["inbox"]["agent"] == name, r.text
        r = send(client, alice, BOB_EMAIL)
        assert r.status_code == 201, r.text
        m = r.json()["message"]
        # The sender sees the person; its own machine keeps its handle (§21 C5.2).
        assert m["to_endpoint"] == {"person": BOB_EMAIL, "display_name": "Bob"} and m["to"] == "@" + BOB_EMAIL
        assert m["from_endpoint"] == {"machine": ALICE} and m["from"] == ALICE and m["kind"] == "message"
        msg = stored(session, m["id"])
        assert (msg.recipient_agent_id, msg.recipient_agent_name, msg.route) == (
            bob_reports["agents"]["bob"].id, name, "inbox")  # the named path (C3.1b)
        # The recipient machine sees its own target.
        seen = client.get(f"/api/v1/messages/{m['id']}", headers=auth(bob)).json()["message"]
        assert seen["to_endpoint"] == {"machine": BOB, "agent": name} and seen["to"] == BOB


def test_listed_and_ambiguous_agents_cannot_be_an_inbox_or_addressed(client, bob_reports):
    for name in ("claude", "twin"):
        r = pick_inbox(client, bob_reports["tokens"]["bob"], name)
        assert r.status_code == 400 and err(r) == "inbox_invalid", name
        r = send(client, bob_reports["tokens"]["alice"], to_agent(name))
        assert r.status_code == 400 and err(r) == "address_people" and r.json()["error"]["message"] == ADDRESS_BOB


def test_known_offline_agent_is_an_inbox_choice_within_14_days(client, bob_reports, session):
    bob = bob_reports["tokens"]["bob"]
    report(client, bob, [INBOX])  # reviewer and notes go away but stay known
    assert pick_inbox(client, bob, "reviewer").status_code == 200
    session.execute(update(KnownAgent).values(last_seen_at=identity.now() - timedelta(days=15)))
    session.commit()
    r = pick_inbox(client, bob, "notes")
    assert r.status_code == 400 and err(r) == "inbox_invalid"


def test_unknown_agent_is_indistinguishable_from_a_known_one(client, bob_reports):
    bob, alice = bob_reports["tokens"]["bob"], bob_reports["tokens"]["alice"]
    r = pick_inbox(client, bob, "ghost")
    assert r.status_code == 400 and err(r) == "inbox_invalid"
    ghost, known = send(client, alice, to_agent("ghost")), send(client, alice, to_agent("reviewer"))
    assert ghost.status_code == known.status_code == 400 and ghost.json() == known.json()
    assert err(ghost) == "address_people" and ghost.json()["error"]["message"] == ADDRESS_BOB


def test_inbox_only_machine_takes_messages_at_its_inbox(client, bob_reports, session):
    alice, bob = bob_reports["tokens"]["alice"], bob_reports["tokens"]["bob"]
    assert pick_inbox(client, bob, "reviewer").status_code == 200  # named, while the machine routes to all
    r = client.put("/api/v1/routing", headers=auth(bob), json={"routing": "inbox-only"})
    assert r.status_code == 200 and r.json() == {"routing": "inbox-only"}
    assert client.get("/api/v1/routing", headers=auth(bob)).json() == {"routing": "inbox-only"}
    # An inbox-only machine takes a message to its person at the machine endpoint (C3.1a).
    r = send(client, alice, BOB_EMAIL)
    assert r.status_code == 201
    msg = stored(session, r.json()["message"]["id"])
    assert (msg.recipient_agent_id, msg.recipient_agent_name) == (bob_reports["agents"]["bob"].id, None)
    # No agent other than its inbox can be picked there now, and agent forms are refused to everyone.
    r = pick_inbox(client, bob, "notes")
    assert r.status_code == 400 and err(r) == "inbox_invalid"
    r = send(client, alice, to_agent("reviewer"))
    assert r.status_code == 400 and err(r) == "address_people"
    own = client.get("/api/v1/agents", headers=auth(bob)).json()["agents"]
    assert [(a["handle"], a["routing"]) for a in own] == [(BOB, "inbox-only")]
    assert BOB not in client.get("/api/v1/agents", headers=auth(alice)).text  # teammates' machines never appear
    for bad in ({"routing": "some"}, {}, {"routing": "all", "x": 1}, []):
        assert client.put("/api/v1/routing", headers=auth(bob), json=bad).status_code == 400


def test_endpoint_forms_and_bad_recipients(client, world, session):
    tok = world["tokens"]["alice"]
    for to in (BOB_EMAIL, "@BOB@example.test", "Bob", " bob ", {"person": BOB_EMAIL}, {"person_name": "BOB"}):
        r = send(client, tok, to)
        assert r.status_code == 201, (to, r.text)
        msg = stored(session, r.json()["message"]["id"])
        assert (msg.recipient_agent_id, msg.recipient_agent_name) == (world["agents"]["bob"].id, None), to
        assert client.post(f"/api/v1/messages/{msg.id}/ack", headers=auth(world["tokens"]["bob"])).status_code == 200
    # A teammate's machine, in any form, is mapped to its person and refused.
    for to in (BOB, f"{BOB}/x", {"machine": BOB}, {"machine": BOB, "agent": "x"}, {"machine": BOB, "agent": ""}):
        r = send(client, tok, to)
        assert r.status_code == 400 and err(r) == "address_people", to
        assert r.json()["error"]["message"] == ADDRESS_BOB, to
    # Another team's machine or person, an unknown one and your own handle are indistinguishable.
    generic = {send(client, tok, to).text for to in (
        {"machine": "eve-agent"}, "eve-agent", {"machine": "nobody"}, "nobody", {"person": "eve@example.test"},
        {"person": "nobody@example.test"}, "eve@example.test", "Eve", ALICE, {"machine": ALICE, "agent": "x"})}
    assert len(generic) == 1 and '"address_people"' in generic.pop()
    for bad in ({"agent": "x"}, {"machine": BOB, "x": 1}, {"person": BOB_EMAIL, "x": 1}, {"person": 7}, 7, "",
                "   ", []):
        r = send(client, tok, bad)
        assert r.status_code == 400 and err(r) == "invalid", bad


# C8 derived holds and the capability gate (the named path) ---------------------------------------------------------

def test_capability_gate_and_derived_holds(client, bob_reports, session):
    alice, bob = bob_reports["tokens"]["alice"], bob_reports["tokens"]["bob"]
    to_inbox = send(client, alice, BOB_EMAIL, body="inbox").json()["message"]  # the inbox-mode path
    # Bob's inbox becomes his reviewer agent (set directly, so the first message stays at the machine endpoint).
    session.execute(update(PersonInbox).where(PersonInbox.user_id == bob_reports["users"]["bob"].id)
                    .values(agent_name="reviewer"))
    session.commit()
    to_agent_ = send(client, alice, BOB_EMAIL, body="agent").json()["message"]
    assert stored(session, to_agent_["id"]).recipient_agent_name == "reviewer"
    assert to_inbox["delivery_state"] == "stored" and to_inbox["hold_reason"] is None
    # bob has never polled with routing=1: the named message is held; its sender sees the coarse code (C13 M3).
    assert to_agent_["delivery_state"] == "held" and to_agent_["hold_reason"] == "update_needed"
    old = poll(client, bob, routing=False)
    assert [m["body"] for m in old] == ["inbox"]
    seen = client.get(f"/api/v1/messages/{to_agent_['id']}", headers=auth(alice)).json()["message"]
    assert seen["hold_reason"] == "update_needed"
    own = client.get(f"/api/v1/messages/{to_agent_['id']}", headers=auth(bob)).json()["message"]
    assert own["hold_reason"] == "client_update_needed"  # raw for the recipient machine's owner
    new = poll(client, bob, routing=True)
    assert [m["body"] for m in new] == ["inbox", "agent"]
    assert session.scalar(select(Agent.routing_capable_at).where(Agent.handle == BOB)) is not None
    seen = client.get(f"/api/v1/messages/{to_agent_['id']}", headers=auth(alice)).json()["message"]
    assert seen["delivery_state"] == "stored" and seen["hold_reason"] is None  # reviewer is live
    report(client, bob, [INBOX])  # reviewer goes offline
    seen = client.get(f"/api/v1/messages/{to_agent_['id']}", headers=auth(alice)).json()["message"]
    assert seen["delivery_state"] == "held" and seen["hold_reason"] == "inbox_offline"
    own = client.get(f"/api/v1/messages/{to_agent_['id']}", headers=auth(bob)).json()["message"]
    assert own["delivery_state"] == "held" and own["hold_reason"] == "offline"
    # Once acked, only recipient events apply.
    assert client.post(f"/api/v1/messages/{to_agent_['id']}/ack", headers=auth(bob)).status_code == 200
    seen = client.get(f"/api/v1/messages/{to_agent_['id']}", headers=auth(alice)).json()["message"]
    assert seen["delivery_state"] == "received" and seen["hold_reason"] is None
    state = session.scalar(select(Message.delivery_state).where(Message.id == uuid.UUID(to_agent_["id"])))
    assert state == "received"  # derived holds are never stored


# C13 H2: own traffic -----------------------------------------------------------------------------------------------

def test_a_machine_may_message_its_owner_but_not_its_own_machine_or_agents(client, world, session):
    tok = world["tokens"]["alice"]
    report(client, tok, [INBOX, entry("reviewer", "a" * 32), entry("writer", "b" * 32)])
    for extra in ({}, {"from_agent": "writer"}):
        r = send(client, tok, "alice@example.test", **extra)
        assert r.status_code == 201, r.text
        msg = stored(session, r.json()["message"]["id"])
        assert (msg.route, msg.recipient_user_id, msg.recipient_agent_id) == ("self", world["users"]["alice"].id,
                                                                              None)
        assert session.get(Conversation, msg.conversation_id).kind == "self"
    for to, extra in ((ALICE, {}), (ALICE, {"from_agent": "writer"}), (to_agent("reviewer", ALICE), {}),
                      (to_agent("reviewer", ALICE), {"from_agent": "reviewer"})):
        r = send(client, tok, to, **extra)
        assert r.status_code == 400 and err(r) == "address_people", (to, extra)
        assert r.json()["error"]["message"] == "Address people: send to a teammate's name or email"


# C10 from_agent, and replies (§21 C4) ------------------------------------------------------------------------------

def test_from_agent_and_replies(client, world, session):
    alice, bob = world["tokens"]["alice"], world["tokens"]["bob"]
    report(client, alice, [INBOX, entry("reviewer", "a" * 32)])
    for bad in ("", "a\nb", "x" * 65, " pad"):
        r = send(client, alice, BOB_EMAIL, from_agent=bad)
        assert r.status_code == 400 and err(r) == "invalid", bad
    sent = send(client, alice, BOB_EMAIL, from_agent="reviewer").json()["message"]
    assert sent["from_agent"] == "reviewer" and sent["from_endpoint"] == {"machine": ALICE, "agent": "reviewer"}
    assert sent["from"] == ALICE and sent["from_kind"] == "agent"
    # The recipient sees the person, never the sending agent (C5.2).
    seen = client.get(f"/api/v1/messages/{sent['id']}", headers=auth(bob)).json()["message"]
    assert seen["from_agent"] is None and seen["from"] == "@alice@example.test"
    assert seen["from_endpoint"] == {"person": "alice@example.test", "display_name": "Alice"}
    # A reply goes to the agent the message came from while it is live, in the same conversation (C4.1); its `to`
    # may be omitted, name the person, or (an old client) a machine of theirs: it never chooses the target.
    for to in (None, "alice@example.test", {"machine": ALICE, "agent": "other"}):
        r = send(client, bob, to, in_reply_to=sent["id"])
        assert r.status_code == 201, (to, r.text)
        reply = r.json()["message"]
        assert reply["conversation_id"] == sent["conversation_id"]
        msg = stored(session, reply["id"])
        assert (msg.recipient_agent_id, msg.recipient_agent_name, msg.route) == (
            world["agents"]["alice"].id, "reviewer", "reply"), to
        assert reply["to_endpoint"] == {"person": "alice@example.test", "display_name": "Alice"}
    assert client.get(f"/api/v1/messages/{sent['id']}", headers=auth(alice)).json()["message"]["delivery_state"] == \
        "replied"
    # Addressing anyone else in a reply is refused.
    r = send(client, bob, BOB_EMAIL, in_reply_to=sent["id"])
    assert r.status_code == 400 and err(r) == "invalid"
    # A new message to the person is in the same pair conversation (there is one per pair of people).
    fresh = send(client, bob, "alice@example.test")
    assert fresh.status_code == 201 and fresh.json()["message"]["conversation_id"] == sent["conversation_id"]
    # When the agent is no longer deliverable, the reply falls back to the person's inbox (C13 M4).
    report(client, alice, [INBOX, entry("reviewer", "a" * 32, reachability="listed")])
    r = send(client, bob, None, in_reply_to=sent["id"])
    assert r.status_code == 201, r.text
    msg = stored(session, r.json()["message"]["id"])
    assert (msg.recipient_agent_id, msg.recipient_agent_name, msg.route) == (world["agents"]["alice"].id, None,
                                                                             "inbox")


def test_idempotency_covers_recipients(client, bob_reports):
    alice = bob_reports["tokens"]["alice"]
    mid = str(uuid.uuid4())
    assert send(client, alice, BOB_EMAIL, id=mid).status_code == 201
    for same in (BOB_EMAIL, "Bob", {"person": BOB_EMAIL}, "@bob@example.test"):  # the same person, any form
        assert send(client, alice, same, id=mid).status_code == 200, same
    for other, body in (("alice@example.test", "hello"), (BOB_EMAIL, "changed")):
        r = send(client, alice, other, body, id=mid)
        assert r.status_code == 409 and err(r) == "id_conflict", other
    # An old client retrying with Bob's machine handle names the same person (as C2.5 tolerates for replies): the
    # retry is the stored message, not a new send, so it is not refused as address_people.
    assert send(client, alice, BOB, id=mid).status_code == 200
    r = send(client, alice, "eve-agent", id=mid)  # nobody in the team
    assert r.status_code == 409 and err(r) == "id_conflict"


# Person endpoints from a machine and escalations (C11) -------------------------------------------

def test_machine_to_person_and_escalation(client, world, session):
    alice, bob = world["tokens"]["alice"], world["tokens"]["bob"]
    r = send(client, alice, {"person": "BOB@example.test"})
    assert r.status_code == 201
    m = r.json()["message"]
    assert m["to"] == "@bob@example.test" and m["to_endpoint"]["person"] == "bob@example.test"
    # Escalations go only from a machine to its own owner.
    r = send(client, alice, {"person": "alice@example.test"}, kind="escalation")
    assert r.status_code == 201 and r.json()["message"]["kind"] == "escalation"
    for to, code in (({"person": "bob@example.test"}, "invalid"), (BOB, "address_people"),
                     (to_agent("x"), "address_people")):
        r = send(client, alice, to, kind="escalation")
        assert r.status_code == 400 and err(r) == code, to
    r = send(client, alice, BOB_EMAIL, kind="urgent")
    assert r.status_code == 400 and err(r) == "invalid"
    # A person-sent message never reaches a pre-routing client; a machine's message to Bob's inbox does.
    messaging.send_as_person(session, session.get(User, world["users"]["alice"].id), world["teams"]["acme"].id,
                             id=str(uuid.uuid4()), to=BOB_EMAIL, body="from alice in the app", max_pending=100)
    session.commit()
    assert [x["id"] for x in poll(client, bob, routing=False)] == [m["id"]]
    assert "from alice in the app" in [x["body"] for x in poll(client, bob, routing=True)]


# Presence: reachability everywhere and known agents ---------------------------------------------

def test_known_agents_are_upserted_and_pruned(client, world, session):
    tok = world["tokens"]["alice"]
    report(client, tok, [entry("reviewer", "a" * 32), entry("ghost", "b" * 32, reachability="listed"),
                         entry("twin", "c" * 32, reachability="listed", ambiguous=True)])
    names = set(session.scalars(select(KnownAgent.name)))
    assert names == {"reviewer"}
    session.execute(update(KnownAgent).values(last_seen_at=identity.now() - timedelta(days=31)))
    session.commit()
    report(client, tok, [entry("writer", "d" * 32, reachability="next-turn", source="hook")])
    session.expire_all()
    assert set(session.scalars(select(KnownAgent.name))) == {"writer"}
    # A v0.4 report (reachability only on the inbox) stays valid.
    report(client, tok, [entry("raincli-inbox", "e" * 32, role="inbox"), entry("other", "f" * 32, reachability=None)])


# C1: the operator is warned before pinning routing-capable machines below v0.5.0 -----------------

def test_set_client_version_warns_for_routing_capable_teams(client, world, database_url):
    from test_directory import run

    code, out, errs = run(database_url, "set-client-version", "--team", "acme", "v0.4.0")
    assert code == 0 and "route messages to named agents" not in errs
    report(client, world["tokens"]["bob"], [])  # a v0.5.0 runtime (§16.14 S1)
    client.get("/api/v1/inbox?routing=1", headers=auth(world["tokens"]["bob"]))  # bob becomes routing-capable
    code, out, errs = run(database_url, "set-client-version", "--team", "acme", "v0.4.1")
    assert code == 0 and "1 machine(s) in acme already route messages to named agents" in errs
    assert "client target for acme is v0.4.1" in out
    code, out, errs = run(database_url, "set-client-version", "--team", "acme", "v0.5.0")
    assert code == 0 and "route messages" not in errs



# §16.14 S1: only a machine whose last presence reported v0.5.0+ becomes routing-capable --------------

@pytest.mark.parametrize("presence", [None, {"version": "0.4.0", "update_mode": "automatic",
                                             "update_state": "current"}, "no-client-block"])
def test_routing_capable_needs_a_v050_presence(client, world, session, presence):
    bob = world["tokens"]["bob"]
    if presence == "no-client-block":
        report(client, bob, [], client_block=None)
    elif presence is not None:
        report(client, bob, [], client_block=presence)
    poll(client, bob, routing=True)
    session.expire_all()
    assert session.scalar(select(Agent.routing_capable_at).where(Agent.handle == BOB)) is None
    report(client, bob, [])  # now a v0.5.0 client
    poll(client, bob, routing=True)
    session.expire_all()
    assert session.scalar(select(Agent.routing_capable_at).where(Agent.handle == BOB)) is not None
