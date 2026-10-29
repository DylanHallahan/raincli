"""Migration 0011 on a throwaway PostgreSQL database (protocol §21.13): per-team numbers that continue above every
number issued before, team foreign keys, an exact downgrade; then the real v0.5.1 and v0.6.0 connectors, from the
tags, polling a live server with the cursors they stored before the upgrade, and with one kept from another team."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from raincli_server.migrate import alembic_config, upgrade
from test_migration_0006 import scratch_db  # noqa: F401 - fixture
from test_migration_0010 import _conv, _msg, _seed, P, M
from test_v06_old_clients import tagged_client

TABLES = ("teams", "agents", "conversations", "messages", "person_inboxes")


def _schema(conn):
    """Columns, constraints, indexes, triggers and functions of the public schema."""
    q = lambda sql: sorted(tuple(r) for r in conn.execute(text(sql)))  # noqa: E731
    return {
        "columns": q("SELECT table_name, column_name, data_type, is_nullable, coalesce(column_default, '') "
                     "FROM information_schema.columns WHERE table_schema = 'public'"),
        "constraints": q("SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
                         "WHERE connamespace = 'public'::regnamespace"),
        "indexes": q("SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"),
        "triggers": q("SELECT tgrelid::regclass::text, tgname FROM pg_trigger WHERE NOT tgisinternal"),
        "functions": q("SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace"),
    }


def _rows(conn):
    return {t: sorted(tuple(r) for r in conn.execute(text(f"SELECT * FROM {t}"))) for t in TABLES}


def _at_0010(url, db):
    upgrade(url, "0009")
    with db.begin() as conn:
        seed = _seed(conn)
        nw, ct = seed["teams"]
        for team, person in ((nw, "maya"), (nw, "jon"), (nw, "priya"), (ct, "maya"), (ct, "lee")):
            conn.execute(text("INSERT INTO memberships (team_id, user_id, role) VALUES (:t, :u, 'member')"),
                         {"t": team, "u": seed["users"][person]})
    upgrade(url, "0010")
    return seed


def test_numbers_continue_per_team_and_the_downgrade_is_exact(scratch_db):  # noqa: F811
    url, db = scratch_db
    seed = _at_0010(url, db)
    nw, ct = seed["teams"]
    with db.connect() as conn:
        schema, rows = _schema(conn), _rows(conn)
        top = conn.execute(text("SELECT max(seq) FROM messages")).scalar()
        teams_seen = conn.execute(text("SELECT count(DISTINCT team_id) FROM messages")).scalar()
    assert top > 5 and teams_seen == 2  # both teams' messages, interleaved on the one global number

    upgrade(url, "0011")
    with db.begin() as conn:
        # Existing messages keep their number; every team continues above the highest one issued.
        assert conn.execute(text("SELECT count(*) FROM messages WHERE team_seq <> seq")).scalar() == 0
        assert {r[0] for r in conn.execute(text("SELECT next_seq FROM teams"))} == {top + 1}
        maya, lee = seed["users"]["maya"], seed["users"]["lee"]
        c_lee = seed["lee"]
        first_ct = _msg(conn, ct, c_lee, P(lee), P(maya), "after the upgrade, contoso")
        second_ct = _msg(conn, ct, c_lee, P(lee), P(maya), "again, contoso")
        c_jon = seed["jon"][0]
        first_nw = _msg(conn, nw, c_jon, P(seed["users"]["jon"]), P(maya), "after the upgrade, northwind")
        numbers = {mid: conn.execute(text("SELECT team_seq FROM messages WHERE id = :i"), {"i": mid}).scalar()
                   for mid, _ in (first_ct, second_ct, first_nw)}
        # Each team counts on its own: northwind's next number ignores contoso's two sends.
        assert numbers == {first_ct.id: top + 1, second_ct.id: top + 2, first_nw.id: top + 1}
        # A new team starts above every number issued before the upgrade too.
        new_team = conn.execute(text("INSERT INTO teams (id, slug, name) VALUES (gen_random_uuid(), 'new', 'New') "
                                     "RETURNING next_seq")).scalar()
        assert new_team == top + 1
    with pytest.raises(IntegrityError, match="fk_messages_recipient_agent_team"), db.begin() as conn:
        # The database now keeps a message's machines in its team.
        laptop = seed["machines"]["laptop"]
        maya_ct = conn.execute(text("SELECT id FROM agents WHERE handle = 'maya-ct'")).scalar()
        conv = _conv(conn, nw, M(laptop), P(seed["users"]["jon"]), created="2026-10-01", default=False)
        _msg(conn, nw, conv, P(seed["users"]["jon"]), M(maya_ct), "to a machine of another team")

    command.downgrade(alembic_config(url), "0010")
    with db.connect() as conn:
        assert _schema(conn) == schema
        after = _rows(conn)
    for table in ("agents", "conversations", "person_inboxes"):
        assert after[table] == rows[table], table
    before_ids = {r[0] for r in rows["messages"]}
    assert [r for r in after["messages"] if r[0] in before_ids] == rows["messages"]  # every earlier row exactly
    assert len(after["messages"]) == len(rows["messages"]) + 3  # and the ones sent after the upgrade stay
    assert sorted(r for r in after["teams"] if r[1] != "new") == rows["teams"]


def test_numbers_continue_above_the_highest_ever_issued_even_if_the_newest_were_deleted(scratch_db):  # noqa: F811
    """The newest messages are gone before the upgrade (a deleted team's, say), but a client may have stored their
    number as its cursor: every team continues above it, not above what is left."""
    url, db = scratch_db
    seed = _at_0010(url, db)
    nw, ct = seed["teams"]
    with db.begin() as conn:
        issued = conn.execute(text("SELECT max(seq) FROM messages")).scalar()
        conn.execute(text("DELETE FROM messages WHERE seq > :s"), {"s": issued - 2})
        assert conn.execute(text("SELECT max(seq) FROM messages")).scalar() == issued - 2
    upgrade(url, "0011")
    with db.begin() as conn:
        assert {r[0] for r in conn.execute(text("SELECT next_seq FROM teams"))} == {issued + 1}
        first = _msg(conn, nw, seed["jon"][0], P(seed["users"]["jon"]), P(seed["users"]["maya"]), "after")
        assert conn.execute(text("SELECT team_seq FROM messages WHERE id = :i"), {"i": first.id}).scalar() == issued + 1


def test_a_database_that_never_issued_a_number_starts_at_one(scratch_db):  # noqa: F811
    url, db = scratch_db
    upgrade(url, "0011")
    with db.begin() as conn:
        team = conn.execute(text("INSERT INTO teams (id, slug, name) VALUES (gen_random_uuid(), 'n', 'N') "
                                 "RETURNING next_seq")).scalar()
    assert team == 1


def test_the_upgrade_stops_on_a_row_whose_machine_is_in_another_team(scratch_db):  # noqa: F811
    url, db = scratch_db
    seed = _at_0010(url, db)
    nw, _ct = seed["teams"]
    with db.begin() as conn:  # before 0011 nothing in the database stops this
        maya_ct = conn.execute(text("SELECT id FROM agents WHERE handle = 'maya-ct'")).scalar()
        _msg(conn, nw, seed["jon"][0], P(seed["users"]["jon"]), M(maya_ct), "to a machine of another team")
    with pytest.raises(RuntimeError, match=r"fk_messages_recipient_agent_team \(1\)"):
        upgrade(url, "0011")
    with db.connect() as conn:  # nothing changed: one transaction
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0010"
        assert "team_seq" not in {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'messages'"))}


# Old clients ---------------------------------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def upgraded(scratch_db, settings):  # noqa: F811
    """0010 data with the cursors clients stored then, upgraded to 0011 and served live."""
    import uvicorn

    from raincli_server import identity
    from raincli_server.app import create_app
    from raincli_server.models import Agent, PersonInbox

    url, db = scratch_db
    seed = _at_0010(url, db)
    with db.connect() as conn:
        top = conn.execute(text("SELECT max(seq) FROM messages")).scalar()
    upgrade(url)  # 0011, and every later revision: today's server runs at head
    nw, ct = seed["teams"]
    with Session(db) as s:
        maya_ct = s.scalar(text("SELECT id FROM agents WHERE handle = 'maya-ct'"))
        machines = {"laptop": s.get(Agent, seed["machines"]["laptop"]), "jon": s.get(Agent, seed["machines"]["jon_laptop"]),
                    "priya": s.scalar(text("SELECT id FROM agents WHERE handle = 'priya-pc'")), "maya_ct": maya_ct}
        machines["priya"] = s.get(Agent, machines["priya"])
        machines["maya_ct"] = s.get(Agent, maya_ct)
        tokens = {k: identity.rotate_agent_credential(s, a) for k, a in machines.items()}
        for person, machine in (("jon", "jon"), ("priya", "priya")):  # their inbox: the machine, inbox-only
            s.add(PersonInbox(user_id=seed["users"][person], team_id=nw, agent_id=machines[machine].id,
                              agent_name="inbox", set_via="api"))
            machines[machine].routing = "inbox-only"
        s.commit()  # Lee has no inbox: contoso's sends to him wait in his app, numbered all the same
    port = _free_port()
    live = replace(settings, database_url=url, public_url=f"http://127.0.0.1:{port}", max_pending=1000)
    app = create_app(live)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    try:
        yield {"url": live.public_url, "db": db, "top": top, "tokens": tokens, "seed": seed}
    finally:
        server.should_exit = True
        thread.join(10)
        app.state.engine.dispose()


def _send(up, token, to, body="hello"):
    import urllib.request

    mid = str(uuid.uuid4())
    req = urllib.request.Request(f"{up['url']}/api/v1/messages", method="POST",
                                 data=json.dumps({"id": mid, "to": to, "body": body}).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        assert r.status == 201
    return mid


POLL = """
import os
from raincli_agent.api import ApiClient
from raincli_agent.connector.config import load_connector_config
from raincli_agent.connector.herdr import FakeHerdr
from raincli_agent.connector.queue import Queue
from raincli_agent.connector.runner import Connector

tmp = payload["tmp"]
agent_cfg = os.path.join(tmp, "agent.json")
open(agent_cfg, "w").write(json.dumps({"api_url": payload["url"], "token": payload["token"]}))
os.chmod(agent_cfg, 0o600)
cfg_path = os.path.join(tmp, "connector.json")
open(cfg_path, "w").write(json.dumps({"agent_config": agent_cfg, "herdr_agent": "inbox-agent",
                                      "state_dir": os.path.join(tmp, "queue"), "trusted_senders": []}))
cfg = load_connector_config(cfg_path)
queue = Queue(cfg.state_dir)
if payload.get("start_cursor") is not None:
    with queue.lock():
        queue.set_cursor(payload["start_cursor"])
sessions = os.path.join(tmp, "sessions"); os.makedirs(sessions, exist_ok=True)
conn = Connector(cfg, ApiClient(payload["url"], payload["token"]), FakeHerdr(), queue, identity={},
                 log=lambda line: None, sleep=lambda s: None, sessions_state=sessions)
conn.poll()
print(json.dumps({"records": sorted(r["id"] for r in queue.all()), "cursor": queue.cursor()}))
"""


def _poll(tag, tmp_path, up, token, start_cursor=None):
    """One poll by the tagged connector, its queue kept in ``tmp_path`` between calls."""
    root = tagged_client(tag, tmp_path / "client")
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (tmp_path / "payload.json").write_text(json.dumps({"url": up["url"], "token": token, "tmp": str(state),
                                                       "start_cursor": start_cursor}))
    code = f"""
import json, sys
sys.path.insert(0, {str(root)!r})
import raincli_agent
assert raincli_agent.__version__ == {tag[1:]!r}, raincli_agent.__version__
payload = json.load(open({str(tmp_path / 'payload.json')!r}))
{POLL}
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                            env={**os.environ, "PYTHONPATH": "", "HOME": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _acked(up, ids):
    with up["db"].connect() as conn:
        return conn.execute(text("SELECT count(*) FROM messages WHERE id = ANY(:i) AND acked_at IS NOT NULL"),
                            {"i": [uuid.UUID(i) for i in ids]}).scalar()


@pytest.mark.parametrize("tag", ("v0.5.1", "v0.6.0"))
def test_a_cursor_stored_before_the_upgrade_keeps_working(tag, upgraded, tmp_path):
    """The connector's cursor is the highest number issued before the upgrade (the worst case): every message after
    it, in its team, is above it, whatever other teams send."""
    up, top = upgraded, upgraded["top"]
    for _ in range(3):  # contoso moves on first; northwind's numbers don't
        _send(up, up["tokens"]["maya_ct"], "lee@example.test", "contoso traffic")
    first = _send(up, up["tokens"]["laptop"], "jon@example.test", "after the upgrade")
    out = _poll(tag, tmp_path, up, up["tokens"]["jon"], start_cursor=top)
    assert out["records"] == [first] and out["cursor"] == top + 1
    second = _send(up, up["tokens"]["laptop"], "jon@example.test", "and again")
    out = _poll(tag, tmp_path, up, up["tokens"]["jon"])
    assert out["records"] == sorted([first, second]) and out["cursor"] == top + 2
    assert _acked(up, [first, second]) == 2


@pytest.mark.parametrize("tag", ("v0.5.1", "v0.6.0"))
def test_a_cursor_kept_from_another_team_misses_nothing(tag, upgraded, tmp_path):
    """A queue kept when its machine is signed in to another team carries that team's cursor, above anything this
    team has issued: the poll is answered from the start, and nothing is missed when the team's numbers pass it."""
    up, top = upgraded, upgraded["top"]
    with up["db"].connect() as conn:  # from before the upgrade, never picked up: answered from the start too
        waiting = [str(i) for i in conn.execute(text(
            "SELECT m.id FROM messages m JOIN agents a ON a.id = m.recipient_agent_id "
            "WHERE a.handle = 'priya-pc' AND m.acked_at IS NULL")).scalars()]
    assert waiting  # the seed's "to priya"
    for _ in range(4):
        _send(up, up["tokens"]["maya_ct"], "lee@example.test", "contoso traffic")
    foreign = top + 4  # contoso's newest number
    sent = [_send(up, up["tokens"]["laptop"], "priya@example.test", "one")]  # northwind's top + 1
    out = _poll(tag, tmp_path, up, up["tokens"]["priya"], start_cursor=foreign)
    assert out["records"] == sorted(waiting + sent) and out["cursor"] == foreign  # taken; the cursor stays
    # Northwind passes the foreign cursor before the next poll: the ones below it and above it all arrive.
    sent += [_send(up, up["tokens"]["laptop"], "priya@example.test", f"more {i}") for i in range(6)]
    out = _poll(tag, tmp_path, up, up["tokens"]["priya"])
    assert out["records"] == sorted(waiting + sent) and out["cursor"] == top + 7
    assert _acked(up, waiting + sent) == len(waiting) + 7
    out = _poll(tag, tmp_path, up, up["tokens"]["priya"])  # an ordinary poll from the new cursor
    assert out["cursor"] == top + 7
    with up["db"].connect() as conn:  # and the stale cursor is forgotten once the client moved past it
        assert conn.execute(text("SELECT stale_cursor FROM agents WHERE handle = 'priya-pc'")).scalar() is None
