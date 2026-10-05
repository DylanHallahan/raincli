"""Send-to-any-agent routing (protocol §16.1, §16.2, §16.5, §16.12 C7, C8, C10, C11, C12), real PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from api_helpers import auth, err
from raincli_server import identity
from raincli_server.models import Agent, KnownAgent, Message

ALICE, BOB = "alice-agent", "bob-agent"


def entry(name, key, reachability="instant", role=None, source="herdr", status="idle", **extra):
    return {"key": key, "name": name, "type": "claude", "status": status, "role": role,
            "reachability": reachability, "source": source, **extra}


def report(client, token, agents):
    r = client.put("/api/v1/presence", headers=auth(token), json={"status": "ready", "agents": agents})
    assert r.status_code == 200, r.text


def send(client, token, to, body="hello", **extra):
    payload = {"id": str(extra.pop("id", uuid.uuid4())), "to": to, "body": body, **extra}
    return client.post("/api/v1/messages", headers=auth(token), json=payload)


def poll(client, token, routing=True):
    q = "?routing=1" if routing else ""
    return client.get(f"/api/v1/inbox{q}", headers=auth(token)).json()["messages"]


def to_agent(name, machine=BOB):
    return {"machine": machine, "agent": name}


@pytest.fixture
def bob_reports(client, world):
    """bob's runtime reports a Herdr agent, a hook session, a listed scan entry and an ambiguous name."""
    report(client, world["tokens"]["bob"], [
        entry("reviewer", "a" * 32),
        entry("notes", "b" * 32, reachability="next-turn", source="hook"),
        entry("claude", "c" * 32, reachability="listed", source="scan", status="unknown"),
        entry("twin", "d" * 32, reachability="listed", ambiguous=True),
        entry("twin", "e" * 32, reachability="listed", ambiguous=True, source="hook"),
    ])
    return world


# §16.2 order ---------------------------------------------------------------------------

def test_live_deliverable_agents_are_accepted(client, bob_reports):
    for name in ("reviewer", "notes"):
        r = send(client, bob_reports["tokens"]["alice"], to_agent(name))
        assert r.status_code == 201, r.text
        m = r.json()["message"]
        assert m["to_endpoint"] == {"machine": BOB, "agent": name} and m["to"] == BOB
        assert m["from_endpoint"] == {"machine": ALICE} and m["kind"] == "message"


def test_listed_and_ambiguous_agents_are_not_deliverable(client, bob_reports):
    for name, reason in (("claude", "listed_only"), ("twin", "ambiguous")):
        r = send(client, bob_reports["tokens"]["alice"], to_agent(name))
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["code"] == "not_deliverable" and body["reason"] == reason


def test_known_offline_agent_is_accepted_within_14_days(client, bob_reports, session):
    report(client, bob_reports["tokens"]["bob"], [])  # reviewer goes away but stays known
    assert send(client, bob_reports["tokens"]["alice"], to_agent("reviewer")).status_code == 201
    session.execute(update(KnownAgent).values(last_seen_at=identity.now() - timedelta(days=15)))
    session.commit()
    r = send(client, bob_reports["tokens"]["alice"], to_agent("reviewer"))
    assert r.status_code == 400 and err(r) == "unknown_agent"


def test_unknown_agent(client, world):
    r = send(client, world["tokens"]["alice"], to_agent("ghost"))
    assert r.status_code == 400 and err(r) == "unknown_agent"


def test_inbox_only_machine_refuses_agent_endpoints(client, bob_reports):
    r = client.put("/api/v1/routing", headers=auth(bob_reports["tokens"]["bob"]), json={"routing": "inbox-only"})
    assert r.status_code == 200 and r.json() == {"routing": "inbox-only"}
    assert client.get("/api/v1/routing", headers=auth(bob_reports["tokens"]["bob"])).json() == {"routing": "inbox-only"}
    r = send(client, bob_reports["tokens"]["alice"], to_agent("reviewer"))
    assert r.status_code == 400 and err(r) == "routing_inbox_only"
    assert send(client, bob_reports["tokens"]["alice"], BOB).status_code == 201  # the machine endpoint still works
    entry_ = next(a for a in client.get("/api/v1/agents", headers=auth(bob_reports["tokens"]["alice"])).json()["agents"]
                  if a["handle"] == BOB)
    assert entry_["routing"] == "inbox-only"
    for bad in ({"routing": "some"}, {}, {"routing": "all", "x": 1}, []):
        assert client.put("/api/v1/routing", headers=auth(bob_reports["tokens"]["bob"]), json=bad).status_code == 400


def test_endpoint_forms_and_bad_recipients(client, world):
    tok = world["tokens"]["alice"]
    assert send(client, tok, {"machine": BOB}).status_code == 201
    for bad in ({"machine": "eve-agent"}, {"machine": "nobody"}, {"person": "eve@example.test"},
                {"person": "nobody@example.test"}, {"machine": BOB, "agent": ""}, {"machine": BOB, "agent": "a\nb"},
                {"machine": BOB, "agent": "x" * 65}, {"agent": "x"}, {"machine": BOB, "x": 1}, 7):
        r = send(client, tok, bad)
        assert r.status_code == 400 and err(r) == "invalid", bad


# C8 derived holds and the capability gate -------------------------------------------------

def test_capability_gate_and_derived_holds(client, bob_reports, session):
    alice, bob = bob_reports["tokens"]["alice"], bob_reports["tokens"]["bob"]
    to_inbox = send(client, alice, BOB, body="inbox").json()["message"]
    to_agent_ = send(client, alice, to_agent("reviewer"), body="agent").json()["message"]
    assert to_inbox["delivery_state"] == "stored" and to_inbox["hold_reason"] is None
    # bob has never polled with routing=1: the agent message is held client_update_needed.
    assert to_agent_["delivery_state"] == "held" and to_agent_["hold_reason"] == "client_update_needed"
    old = poll(client, bob, routing=False)
    assert [m["body"] for m in old] == ["inbox"]
    seen = client.get(f"/api/v1/messages/{to_agent_['id']}", headers=auth(alice)).json()["message"]
    assert seen["hold_reason"] == "client_update_needed"
    new = poll(client, bob, routing=True)
    assert [m["body"] for m in new] == ["inbox", "agent"]
    assert session.scalar(select(Agent.routing_capable_at).where(Agent.handle == BOB)) is not None
    seen = client.get(f"/api/v1/messages/{to_agent_['id']}", headers=auth(alice)).json()["message"]
    assert seen["delivery_state"] == "stored" and seen["hold_reason"] is None  # reviewer is live
    report(client, bob, [])  # reviewer goes offline
    seen = client.get(f"/api/v1/messages/{to_agent_['id']}", headers=auth(alice)).json()["message"]
    assert seen["delivery_state"] == "held" and seen["hold_reason"] == "offline"
    # Once acked, only recipient events apply.
    assert client.post(f"/api/v1/messages/{to_agent_['id']}/ack", headers=auth(bob)).status_code == 200
    seen = client.get(f"/api/v1/messages/{to_agent_['id']}", headers=auth(alice)).json()["message"]
    assert seen["delivery_state"] == "received" and seen["hold_reason"] is None
    stored = session.scalar(select(Message.delivery_state).where(Message.id == uuid.UUID(to_agent_["id"])))
    assert stored == "received"  # derived holds are never stored


# C7 sending to its own agents ---------------------------------------------------------------

def test_a_machine_may_message_its_own_agents_but_not_its_inbox(client, world):
    tok = world["tokens"]["alice"]
    report(client, tok, [entry("reviewer", "a" * 32), entry("writer", "b" * 32)])
    assert send(client, tok, to_agent("reviewer", ALICE)).status_code == 201
    assert send(client, tok, to_agent("reviewer", ALICE), from_agent="writer").status_code == 201
    for to, extra in ((ALICE, {}), (ALICE, {"from_agent": "writer"}), (to_agent("reviewer", ALICE), {"from_agent": "reviewer"})):
        r = send(client, tok, to, **extra)
        assert r.status_code == 400 and err(r) == "invalid", (to, extra)


# C10 replies and from_agent ------------------------------------------------------------------

def test_from_agent_and_replies(client, world):
    alice, bob = world["tokens"]["alice"], world["tokens"]["bob"]
    report(client, alice, [entry("reviewer", "a" * 32)])
    for bad in ("", "a\nb", "x" * 65, " pad"):
        r = send(client, alice, BOB, from_agent=bad)
        assert r.status_code == 400 and err(r) == "invalid", bad
    sent = send(client, alice, BOB, from_agent="reviewer").json()["message"]
    assert sent["from_agent"] == "reviewer" and sent["from_endpoint"] == {"machine": ALICE, "agent": "reviewer"}
    assert sent["from"] == ALICE
    # A reply goes to the agent the message came from, in the same conversation.
    r = send(client, bob, to_agent("reviewer", ALICE), in_reply_to=sent["id"])
    assert r.status_code == 201, r.text
    reply = r.json()["message"]
    assert reply["conversation_id"] == sent["conversation_id"]
    assert client.get(f"/api/v1/messages/{sent['id']}", headers=auth(alice)).json()["message"]["delivery_state"] == "replied"
    # Addressing anyone else is refused; the machine endpoint is the explicit fallback (no silent one).
    assert send(client, bob, {"machine": "alice-agent", "agent": "other"}, in_reply_to=sent["id"]).status_code == 400
    fallback = send(client, bob, ALICE, in_reply_to=sent["id"])
    assert fallback.status_code == 201 and fallback.json()["message"]["conversation_id"] != sent["conversation_id"]
    # When the §16.2 order refuses the agent, the reply is refused with that reason.
    report(client, alice, [entry("reviewer", "a" * 32, reachability="listed")])
    r = send(client, bob, to_agent("reviewer", ALICE), in_reply_to=sent["id"])
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_deliverable"


def test_idempotency_covers_endpoints(client, bob_reports):
    alice = bob_reports["tokens"]["alice"]
    mid = str(uuid.uuid4())
    assert send(client, alice, to_agent("reviewer"), id=mid).status_code == 201
    assert send(client, alice, to_agent("reviewer"), id=mid).status_code == 200
    r = send(client, alice, to_agent("notes"), id=mid)
    assert r.status_code == 409 and err(r) == "id_conflict"
    r = send(client, alice, BOB, id=mid)
    assert r.status_code == 409 and err(r) == "id_conflict"


# Person endpoints from a machine and escalations (C11) -------------------------------------------

def test_machine_to_person_and_escalation(client, world):
    alice, bob = world["tokens"]["alice"], world["tokens"]["bob"]
    r = send(client, alice, {"person": "BOB@example.test"})
    assert r.status_code == 201
    m = r.json()["message"]
    assert m["to"] == "@bob@example.test" and m["to_endpoint"]["person"] == "bob@example.test"
    # Escalations go only from a machine to its own owner.
    r = send(client, alice, {"person": "alice@example.test"}, kind="escalation")
    assert r.status_code == 201 and r.json()["message"]["kind"] == "escalation"
    for to in ({"person": "bob@example.test"}, BOB, to_agent("x")):
        r = send(client, alice, to, kind="escalation")
        assert r.status_code == 400 and err(r) in ("invalid", "unknown_agent"), to
    r = send(client, alice, BOB, kind="urgent")
    assert r.status_code == 400 and err(r) == "invalid"
    # A person-sent message never reaches a pre-routing client; it is held client_update_needed.
    assert poll(client, bob, routing=False) == []


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
    client.get("/api/v1/inbox?routing=1", headers=auth(world["tokens"]["bob"]))  # bob becomes routing-capable
    code, out, errs = run(database_url, "set-client-version", "--team", "acme", "v0.4.1")
    assert code == 0 and "1 machine(s) in acme already route messages to named agents" in errs
    assert "client target for acme is v0.4.1" in out
    code, out, errs = run(database_url, "set-client-version", "--team", "acme", "v0.5.0")
    assert code == 0 and "route messages" not in errs
