"""The one listing rule (protocol §20.6 M3): listed exactly when routing.check_agent_route accepts, real PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select, update

from api_helpers import auth
from raincli_server import identity, routing
from raincli_server.messaging import Endpoint
from raincli_server.messaging_errors import MessagingError
from raincli_server.models import Agent, KnownAgent, MachineAgent
from raincli_server.web.listing import listed_agents

CLIENT = {"version": "0.5.5", "update_mode": "automatic", "update_state": "current"}


def entry(name, key, reachability="instant", status="idle", **extra):
    return {"key": key, "name": name, "type": "claude", "status": status, "role": None,
            "reachability": reachability, "source": "herdr", **extra}


def report(client, token, agents):
    r = client.put("/api/v1/presence", headers=auth(token), json={"status": "ready", "agents": agents, "client": CLIENT})
    assert r.status_code == 200, r.text


def accepted(session, machine, name):
    try:
        routing.check_agent_route(session, Endpoint("agent", agent=machine, name=name))
        return True
    except MessagingError:
        return False


def every_name(session, machines):
    ids = [m.id for m in machines]
    names = {(r.agent_id, r.name) for r in session.scalars(select(MachineAgent).where(MachineAgent.agent_id.in_(ids)))}
    names |= {(r.agent_id, r.name) for r in session.scalars(select(KnownAgent).where(KnownAgent.agent_id.in_(ids)))}
    return names


def assert_same_as_routing(session, machines):
    session.expire_all()
    listed = {(a.machine.id, a.name) for a in listed_agents(session, machines)}
    by_id = {m.id: m for m in machines}
    for machine_id, name in every_name(session, machines):
        assert ((machine_id, name) in listed) == accepted(session, by_id[machine_id], name), name
    return listed


def test_listed_exactly_when_routing_accepts(client, world, session):
    bob = world["agents"]["bob"]
    report(client, world["tokens"]["bob"], [
        entry("reviewer", "a" * 32), entry("notes", "b" * 32, reachability="next-turn", status="working"),
        entry("scan", "c" * 32, reachability="listed"),
        entry("twin", "d" * 32, reachability="listed", ambiguous=True),
        entry("twin", "e" * 32, reachability="listed", ambiguous=True, source="hook"),
    ])
    listed = assert_same_as_routing(session, [bob])
    assert {name for _, name in listed} == {"reviewer", "notes"}


def test_an_ambiguous_live_name_with_a_known_row_is_not_listed(client, world, session):
    bob = world["agents"]["bob"]
    report(client, world["tokens"]["bob"], [entry("twin", "a" * 32)])  # deliverable once: now a known name
    report(client, world["tokens"]["bob"], [entry("twin", "a" * 32, reachability="listed", ambiguous=True),
                                            entry("twin", "b" * 32, reachability="listed", ambiguous=True)])
    assert session.scalar(select(KnownAgent).where(KnownAgent.agent_id == bob.id, KnownAgent.name == "twin"))
    assert assert_same_as_routing(session, [bob]) == set()


def test_an_inbox_only_machine_lists_no_named_agents(client, world, session):
    bob = world["agents"]["bob"]
    report(client, world["tokens"]["bob"], [entry("reviewer", "a" * 32)])
    session.execute(update(Agent).where(Agent.id == bob.id).values(routing="inbox-only"))
    session.commit()
    assert assert_same_as_routing(session, [session.get(Agent, bob.id)]) == set()


def test_an_offline_known_agent_is_listed_as_offline(client, world, session):
    bob = world["agents"]["bob"]
    report(client, world["tokens"]["bob"], [entry("reviewer", "a" * 32)])
    report(client, world["tokens"]["bob"], [])  # gone from the snapshot, still known
    [a] = listed_agents(session, [bob])
    assert (a.name, a.status, a.reachability) == ("reviewer", "offline", "instant")
    assert_same_as_routing(session, [bob])
    session.execute(update(MachineAgent).values(seen_at=identity.now() - timedelta(minutes=5)))
    session.execute(update(KnownAgent).values(last_seen_at=identity.now() - timedelta(days=15)))
    session.commit()
    assert assert_same_as_routing(session, [bob]) == set()


def test_a_stale_snapshot_falls_back_to_known(client, world, session):
    bob = world["agents"]["bob"]
    report(client, world["tokens"]["bob"], [entry("reviewer", "a" * 32, reachability="listed")])
    report(client, world["tokens"]["bob"], [entry("writer", "b" * 32)])
    session.execute(update(MachineAgent).values(seen_at=identity.now() - timedelta(minutes=3)))
    session.commit()
    listed = assert_same_as_routing(session, [bob])
    assert {name for _, name in listed} == {"writer"}  # writer known; reviewer was never deliverable
    assert {a.status for a in listed_agents(session, [bob])} == {"offline"}


def test_presence_words_and_revoked_machines(client, world, session):
    bob = world["agents"]["bob"]
    report(client, world["tokens"]["bob"], [entry("a1", "a" * 32), entry("a2", "b" * 32, status="working"),
                                            entry("a3", "c" * 32, status="blocked"), entry("a4", "d" * 32, status="unknown")])
    assert [(a.name, a.status) for a in listed_agents(session, [bob])] == [
        ("a1", "ready"), ("a2", "busy"), ("a3", "blocked"), ("a4", "unknown")]
    later = identity.now() + timedelta(seconds=121)
    assert {a.status for a in listed_agents(session, [bob], now=later)} == {"offline"}
    identity.revoke_agent(session, bob)
    session.commit()
    assert listed_agents(session, [bob]) == []


def test_every_listed_agent_sends(client, world, session):
    report(client, world["tokens"]["bob"], [entry("reviewer", "a" * 32), entry("notes", "b" * 32, reachability="next-turn")])
    report(client, world["tokens"]["bob"], [entry("notes", "b" * 32, reachability="next-turn")])  # reviewer: known only
    for a in listed_agents(session, [world["agents"]["bob"]]):
        r = client.post("/api/v1/messages", headers=auth(world["tokens"]["alice"]),
                        json={"id": str(uuid.uuid4()), "to": {"machine": a.machine.handle, "agent": a.name}, "body": "hi"})
        assert r.status_code == 201, (a.name, r.text)
