"""Resource limits and agent names (protocol §18.1 items 5 and 6), real PostgreSQL."""

from __future__ import annotations

import io
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from api_helpers import auth, err
from raincli_server import admin, identity, messaging, quotas
from raincli_server.app import sweep
from raincli_server.models import Agent, Attachment, HandoffCode, KnownAgent, MachineAgent, Message, Team, User
from raincli_server.presence import KNOWN_AGENTS_MAX
from test_api_attachments import att
from test_routing import entry, report, send
from test_web_app import app_csrf, login

KIB = 1024


def machine(session, world, name):
    return session.get(Agent, world["agents"][name].id)


# The machine cap on every path ----------------------------------------------------------------

def test_the_machine_cap_applies_to_adding_a_machine_on_the_website(client, world, session):
    login(client)
    for i in range(identity.MACHINE_LIMIT - 1):  # alice-agent is the first
        r = client.post("/app/agents", data={"csrf_token": app_csrf(client), "team": "acme", "handle": f"web-{i:02d}"})
        assert r.status_code == 200, r.text
    r = client.post("/app/agents", data={"csrf_token": app_csrf(client), "team": "acme", "handle": "one-too-many"})
    assert r.status_code == 400 and f"{identity.MACHINE_LIMIT} active machines" in r.text
    assert session.scalar(select(Agent).where(Agent.handle == "one-too-many")) is None
    r = client.post("/api/v1/app/login", json={"email": "alice@example.test", "password": "correct horse battery",
                                               "machine_name": "via-app"})
    assert r.status_code == 409 and err(r) == "machine_limit"  # one cap, whichever way machines were added


# Directory history ----------------------------------------------------------------------------

def test_a_machine_remembers_at_most_500_agent_names_dropping_the_oldest(client, world, session):
    bob = machine(session, world, "bob")
    old = identity.now() - timedelta(days=2)
    session.execute(KnownAgent.__table__.insert(), [
        {"agent_id": bob.id, "name": f"old-{i:03d}", "type": "claude", "reachability": "instant",
         "last_seen_at": old + timedelta(seconds=i)} for i in range(KNOWN_AGENTS_MAX)])
    session.commit()
    report(client, world["tokens"]["bob"], [entry(f"new-{i}", f"{i:032x}") for i in range(3)])
    session.expire_all()
    names = set(session.scalars(select(KnownAgent.name).where(KnownAgent.agent_id == bob.id)))
    assert len(names) == KNOWN_AGENTS_MAX and {"new-0", "new-1", "new-2"} <= names
    assert not {"old-000", "old-001", "old-002"} & names and "old-003" in names


# Handoff codes ---------------------------------------------------------------------------------

def handoff(client, person):
    r = client.post("/api/v1/app/handoff", headers=auth(person), json={"app_install_hash": "a" * 64})
    assert r.status_code == 200, r.text


def test_used_and_expired_handoff_codes_are_deleted(client, world, session, app):
    r = client.post("/api/v1/app/login", json={"email": "alice@example.test", "password": "correct horse battery",
                                               "machine_name": "alice-pc", "person_session": True})
    person = r.json()["person_session"]
    handoff(client, person)
    handoff(client, person)
    session.execute(update(HandoffCode).values(expires_at=identity.now() - timedelta(seconds=1)))
    session.commit()
    handoff(client, person)  # creating a code clears the expired ones
    assert session.scalar(select(func.count()).select_from(HandoffCode)) == 1
    session.execute(update(HandoffCode).values(used_at=identity.now()))
    session.commit()
    sweep(app)  # the hourly sweep
    assert session.scalar(select(func.count()).select_from(HandoffCode)) == 0


# Attachment quotas -----------------------------------------------------------------------------

def files(n_kib, name="a.md"):
    return [(name, b"x" * (n_kib * KIB))]


def send_files(session, sender, to, n_kib, q, **kw):
    msg, _ = messaging.send_message(session, sender, id=str(uuid.uuid4()), to=to, body="with a file",
                                    attachments=files(n_kib), max_pending=1000, quotas=q, **kw)
    return msg


def test_two_sessions_and_machines_of_one_user_share_the_daily_quota(client, world, session):
    q = quotas.Quotas(user_daily=500 * KIB, team_window=10_000 * KIB)
    alice, second = machine(session, world, "alice"), identity.register_agent(
        session, session.get(Team, world["teams"]["acme"].id), session.get(User, world["users"]["alice"].id),
        "alice-two")[0]
    send_files(session, alice, "bob-agent", 200, q)
    messaging.send_as_person(session, session.get(User, world["users"]["alice"].id), world["teams"]["acme"].id,
                             id=str(uuid.uuid4()), to={"person": "bob@example.test"}, body="as alice",
                             attachments=files(200), max_pending=1000, quotas=q)
    session.commit()
    with pytest.raises(messaging.MessagingError) as refused:
        send_files(session, second, "bob-agent", 200, q)  # 600 KiB > 500 KiB, from another machine of hers
    session.rollback()
    assert refused.value.status == 413 and refused.value.code == "quota_exceeded"
    assert refused.value.extra["quota"] == "user_daily" and refused.value.extra["resets_at"].endswith("T00:00:00Z")
    # Bob is not affected, and a message without attachments is never refused.
    send_files(session, machine(session, world, "bob"), "alice-agent", 200, q)
    messaging.send_message(session, alice, id=str(uuid.uuid4()), to="bob-agent", body="text only",
                           max_pending=1000, quotas=q)
    session.commit()


def test_concurrent_uploads_do_not_overshoot_the_quota(client, world, app):
    q = quotas.Quotas(user_daily=1000 * KIB, team_window=10_000 * KIB)
    start = threading.Barrier(8)
    outcomes = []

    def upload():
        with app.state.sessionmaker() as s:
            sender = s.get(Agent, world["agents"]["alice"].id)
            start.wait()
            try:
                send_files(s, sender, "bob-agent", 200, q)
                s.commit()
                outcomes.append("stored")
            except messaging.MessagingError as exc:
                s.rollback()
                outcomes.append(exc.code)
    threads = [threading.Thread(target=upload) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(outcomes) == ["quota_exceeded"] * 3 + ["stored"] * 5  # 5 x 200 KiB = the 1000 KiB


def test_one_user_cannot_block_the_team_beyond_the_rolling_window(client, world, session):
    q = quotas.Quotas(user_daily=10_000 * KIB, team_window=400 * KIB)
    alice, bob = machine(session, world, "alice"), machine(session, world, "bob")
    first = send_files(session, alice, "bob-agent", 200, q)
    send_files(session, alice, "bob-agent", 200, q)
    session.commit()
    with pytest.raises(messaging.MessagingError) as refused:
        send_files(session, bob, "alice-agent", 100, q)
    session.rollback()
    assert refused.value.extra["quota"] == "team_30_days"
    frees = refused.value.extra["resets_at"]
    assert frees == (first.created_at + quotas.TEAM_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Once the oldest upload is 30 days old the team has room again.
    session.execute(update(Attachment).where(Attachment.message_id == first.id)
                    .values(created_at=identity.now() - timedelta(days=31)))
    session.commit()
    send_files(session, bob, "alice-agent", 100, q)
    session.commit()


def test_the_api_answers_413_naming_the_quota(client, world, session):
    session.execute(update(User).where(User.id == world["users"]["alice"].id).values(attachment_quota_bytes=10))
    session.commit()
    r = send(client, world["tokens"]["alice"], "bob-agent", "file", attachments=[att("a.md", b"hello world")])
    assert r.status_code == 413 and r.json()["error"]["code"] == "quota_exceeded"
    assert r.json()["quota"] == "user_daily" and "resets" in r.json()["error"]["message"]
    assert send(client, world["tokens"]["alice"], "bob-agent", "no file").status_code == 201


def test_the_operator_sees_and_raises_a_quota(client, world, database_url, session):
    send_files(session, machine(session, world, "alice"), "bob-agent", 2, quotas.Quotas())
    session.commit()

    def run(*argv):
        out = io.StringIO()
        code = admin.main(list(argv), env={"RAINCLI_DATABASE_URL": database_url}, stdout=out, stderr=io.StringIO())
        return code, out.getvalue()
    code, out = run("quota", "--user", "alice@example.test")
    assert code == 0 and f"{2 * KIB} of {quotas.USER_DAILY_DEFAULT} bytes" in out and "configured default" in out
    code, out = run("quota", "--team", "acme", "--set", str(20 * quotas.MIB))
    assert code == 0 and f"of {20 * quotas.MIB} bytes" in out and "set for this team" in out
    session.expire_all()
    assert session.get(Team, world["teams"]["acme"].id).attachment_quota_bytes == 20 * quotas.MIB
    code, out = run("quota", "--team", "acme", "--default")
    assert code == 0 and "configured default" in out


# Agent names starting with "-" -----------------------------------------------------------------

def test_names_starting_with_a_dash_are_never_targets_or_listed(client, world, session):
    report(client, world["tokens"]["bob"], [entry("-x", "a" * 32), entry("reviewer", "b" * 32),
                                            entry(" -y", "c" * 32)])
    session.expire_all()
    stored = set(session.scalars(select(MachineAgent.name).where(MachineAgent.agent_id == world["agents"]["bob"].id)))
    assert stored == {"reviewer"}  # only those entries are dropped; the report is accepted
    for name in ("-x", "-", "--help"):
        r = send(client, world["tokens"]["alice"], {"machine": "bob-agent", "agent": name})
        assert r.status_code == 400 and err(r) == "invalid", name
    directory = client.get("/api/v1/agents", headers=auth(world["tokens"]["alice"])).json()["agents"]
    bob = next(a for a in directory if a["handle"] == "bob-agent")
    assert [a["name"] for a in bob["agents"]] == ["reviewer"]


def test_existing_rows_with_a_leading_dash_are_ignored_and_pruned(client, world, session):
    bob = world["agents"]["bob"].id
    now = identity.now()
    session.execute(MachineAgent.__table__.insert(), [
        {"agent_id": bob, "key": "f" * 32, "name": "-old", "type": "claude", "status": "idle",
         "reachability": "instant", "source": "herdr", "ambiguous": False, "seen_at": now}])
    session.execute(KnownAgent.__table__.insert(), [
        {"agent_id": bob, "name": "-old", "type": "claude", "reachability": "instant", "last_seen_at": now}])
    session.commit()
    directory = client.get("/api/v1/agents", headers=auth(world["tokens"]["alice"])).json()["agents"]
    assert not [a for a in next(d for d in directory if d["handle"] == "bob-agent")["agents"] if a["name"] == "-old"]
    report(client, world["tokens"]["bob"], [entry("reviewer", "b" * 32)])
    session.expire_all()
    assert session.scalar(select(KnownAgent).where(KnownAgent.agent_id == bob, KnownAgent.name == "-old")) is None
    assert session.scalar(select(func.count()).select_from(Message)) == 0
