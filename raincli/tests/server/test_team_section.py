"""The app-mode Team section's data (protocol §19.5, §19.3), real PostgreSQL."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import event, update

from api_helpers import auth
from raincli_server import identity
from raincli_server.models import AgentPresence, KnownAgent
from raincli_server.web.team import team_agents

CLIENT = {"version": "0.5.4", "update_mode": "automatic", "update_state": "current"}


def entry(name, key, reachability="instant", status="idle", **extra):
    return {"key": key, "name": name, "type": "claude", "status": status, "role": None,
            "reachability": reachability, "source": "herdr", **extra}


def report(client, token, agents):
    r = client.put("/api/v1/presence", headers=auth(token), json={"status": "ready", "agents": agents, "client": CLIENT})
    assert r.status_code == 200, r.text


def viewer(world, name="alice"):
    return SimpleNamespace(user=world["users"][name], teams=[(t, "member") for t in world["teams"].values()
                                                             if name != "eve" and t.slug == "acme"
                                                             or name == "eve" and t.slug == "globex"])


def age(session, agent, minutes):
    session.execute(update(AgentPresence).where(AgentPresence.agent_id == agent.id)
                    .values(seen_at=AgentPresence.seen_at - timedelta(minutes=minutes)))
    session.commit()


def test_teammates_deliverable_agents_with_presence_words(client, world, session):
    report(client, world["tokens"]["bob"], [
        entry("reviewer", "a" * 32), entry("writer", "b" * 32, status="working"),
        entry("stuck", "c" * 32, reachability="next-turn", status="blocked"),
        entry("gone", "d" * 32, status="offline"), entry("odd", "e" * 32, status="unknown"),
        entry("scan", "f" * 32, reachability="listed"),
        entry("twin", "1" * 32, reachability="listed", ambiguous=True),
    ])
    report(client, world["tokens"]["alice"], [entry("mine", "9" * 32)])  # the viewer's own: not listed
    rows = team_agents(session, viewer(world))
    assert [(r["agent"], r["status"]) for r in rows] == [
        ("gone", "offline"), ("odd", "offline"), ("reviewer", "ready"), ("stuck", "busy"), ("writer", "busy")]
    assert rows[2] == {"person": "Bob", "agent": "reviewer", "handle": "bob-agent", "to": "bob-agent/reviewer",
                       "status": "ready", "team": "Acme"}


def test_every_agent_is_offline_once_its_machine_has_not_reported_for_two_minutes(client, world, session):
    report(client, world["tokens"]["bob"], [entry("reviewer", "a" * 32), entry("writer", "b" * 32, status="working")])
    age(session, world["agents"]["bob"], 1)
    assert {r["status"] for r in team_agents(session, viewer(world))} == {"ready", "busy"}
    age(session, world["agents"]["bob"], 2)
    assert {r["status"] for r in team_agents(session, viewer(world))} == {"offline"}


def test_a_known_agent_that_left_the_snapshot_stays_listed_offline_within_the_window(client, world, session):
    report(client, world["tokens"]["bob"], [entry("reviewer", "a" * 32)])
    report(client, world["tokens"]["bob"], [])  # the session went away; the name is still deliverable (§16.2)
    assert [(r["agent"], r["status"]) for r in team_agents(session, viewer(world))] == [("reviewer", "offline")]
    session.execute(update(KnownAgent).values(last_seen_at=identity.now() - timedelta(days=15)))
    session.commit()
    assert team_agents(session, viewer(world)) == []


def test_other_teams_revoked_machines_and_dash_names_are_not_listed(client, world, session):
    report(client, world["tokens"]["eve"], [entry("spy", "a" * 32)])
    report(client, world["tokens"]["bob"], [entry("reviewer", "b" * 32)])
    assert [r["agent"] for r in team_agents(session, viewer(world))] == ["reviewer"]
    assert [r["agent"] for r in team_agents(session, viewer(world, "eve"))] == []  # eve's own agent only
    identity.revoke_agent(session, world["agents"]["bob"])
    session.commit()
    assert team_agents(session, viewer(world)) == []


def test_owner_names_are_disambiguated_like_everywhere_else(client, world, session):
    twin = identity.create_user(session, "bob2@example.test", "Bob", "correct horse battery")
    identity.add_member(session, world["teams"]["acme"], twin)
    _, token = identity.register_agent(session, world["teams"]["acme"], twin, "bob2-agent")
    session.commit()
    report(client, world["tokens"]["bob"], [entry("reviewer", "a" * 32)])
    report(client, token, [entry("reviewer", "b" * 32)])
    people = sorted(r["person"] for r in team_agents(session, viewer(world)))
    assert people == ["Bob (bob)", "Bob (bob2)"]


def test_the_query_count_does_not_grow_with_agents(client, world, session, engine):
    def count(fn):
        seen = []
        listener = lambda *a, **k: seen.append(1)  # noqa: E731
        event.listen(engine, "before_cursor_execute", listener)
        try:
            fn()
        finally:
            event.remove(engine, "before_cursor_execute", listener)
        return len(seen)

    report(client, world["tokens"]["bob"], [entry("a1", "a" * 32)])
    one = count(lambda: team_agents(session, viewer(world)))
    report(client, world["tokens"]["bob"], [entry(f"a{i}", f"{i:032d}") for i in range(40)])
    many = count(lambda: team_agents(session, viewer(world)))
    assert len(team_agents(session, viewer(world))) == 40
    assert many == one <= 5
