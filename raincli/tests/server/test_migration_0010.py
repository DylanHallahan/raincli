"""Migration 0010 on a throwaway PostgreSQL database: person-pair, self and own_agents conversations, the inbox
table and an exact downgrade (with data sent after the upgrade); then old conversation ids at the app, the website and the API, and a retried send naming one."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from api_helpers import auth
from test_app_mode import alice_app  # noqa: F401  (fixture)
from test_migration_0006 import scratch_db  # noqa: F401 - fixture
from test_web_app import login


def _user(conn, name):
    return conn.execute(text("INSERT INTO users (id, email, display_name, password_hash, is_active) "
                             "VALUES (gen_random_uuid(), :e, :n, 'x', true) RETURNING id"),
                        {"e": f"{name}@example.test", "n": name}).scalar()


def _team(conn, slug):
    return conn.execute(text("INSERT INTO teams (id, slug, name) VALUES (gen_random_uuid(), :s, :s) RETURNING id"),
                        {"s": slug}).scalar()


def _machine(conn, team, owner, handle):
    return conn.execute(text("INSERT INTO agents (id, team_id, owner_user_id, handle, display_name) "
                             "VALUES (gen_random_uuid(), :t, :o, :h, :h) RETURNING id"),
                        {"t": team, "o": owner, "h": handle}).scalar()


def P(user):
    return {"key": f"p:{user}", "agent": None, "name": None, "user": user}


def M(agent):
    return {"key": f"m:{agent}", "agent": agent, "name": None, "user": None}


def A(agent, name):
    return {"key": f"a:{agent}:{name}", "agent": agent, "name": name, "user": None}


def _conv(conn, team, x, y, *, created, default=True):
    a, b = sorted((x, y), key=lambda e: e["key"])
    return conn.execute(text("""
        INSERT INTO conversations (id, team_id, agent_a_id, agent_b_id, a_agent_name, b_agent_name, a_user_id,
            b_user_id, a_key, b_key, is_default, created_at)
        VALUES (gen_random_uuid(), :t, :aa, :ab, :an, :bn, :au, :bu, :ak, :bk, :d, :c) RETURNING id"""),
        {"t": team, "aa": a["agent"], "ab": b["agent"], "an": a["name"], "bn": b["name"], "au": a["user"],
         "bu": b["user"], "ak": a["key"], "bk": b["key"], "d": default, "c": created}).scalar()


def _msg(conn, team, conv, s, r, body, kind="message"):
    return conn.execute(text("""
        INSERT INTO messages (id, team_id, conversation_id, sender_agent_id, sender_agent_name, sender_user_id,
            recipient_agent_id, recipient_agent_name, recipient_user_id, body, delivery_state, kind)
        VALUES (gen_random_uuid(), :t, :c, :sa, :sn, :su, :ra, :rn, :ru, :b, 'stored', :k) RETURNING id, seq"""),
        {"t": team, "c": conv, "sa": s["agent"], "sn": s["name"], "su": s["user"], "ra": r["agent"],
         "rn": r["name"], "ru": r["user"], "b": body, "k": kind}).one()


def _archive(conn, conv, through, by="p:x", days_ago=1):
    conn.execute(text("UPDATE conversations SET archived_at = now() - make_interval(days => :d), "
                      "archived_by_key = :by, archived_through_seq = :s WHERE id = :c"),
                 {"s": through, "by": by, "c": conv, "d": days_ago})


def _seed(conn):
    """The brief's pre-migration data in two teams. Northwind: Maya's "Jon · api-agent", "Jon · inbox" and
    agent-to-machine threads with Jon, plus an archived person-to-person thread (reused, counts as a source); her
    escalations and an archived agent-to-her thread (to "self"); a thread between her own two machines (to
    "own_agents"); an all-archived pair with Priya (a new pair row). Contoso (Maya's second team): a pair with Lee,
    an own-machines thread and an archived self thread."""
    maya, jon, priya, lee = (_user(conn, n) for n in ("maya", "jon", "priya", "lee"))
    nw, ct = _team(conn, "northwind"), _team(conn, "contoso")
    laptop, desktop = _machine(conn, nw, maya, "maya-laptop"), _machine(conn, nw, maya, "maya-desktop")
    jon_laptop, priya_pc = _machine(conn, nw, jon, "jon-laptop"), _machine(conn, nw, priya, "priya-pc")
    maya_ct, maya_ct2 = _machine(conn, ct, maya, "maya-ct"), _machine(conn, ct, maya, "maya-ct2")

    c_people = _conv(conn, nw, P(maya), P(jon), created="2026-09-01")
    m0 = _msg(conn, nw, c_people, P(jon), P(maya), "hi from the app")
    _archive(conn, c_people, m0.seq, by=f"p:{maya}", days_ago=3)
    c_api = _conv(conn, nw, P(maya), A(jon_laptop, "api-agent"), created="2026-09-02")
    _msg(conn, nw, c_api, P(maya), A(jon_laptop, "api-agent"), "to the api agent")
    _msg(conn, nw, c_api, A(jon_laptop, "api-agent"), P(maya), "the report")
    c_inbox = _conv(conn, nw, P(maya), M(jon_laptop), created="2026-09-03")
    i1 = _msg(conn, nw, c_inbox, P(maya), M(jon_laptop), "to the inbox")
    _archive(conn, c_inbox, i1.seq)
    c_a2m = _conv(conn, nw, A(laptop, "laptop-agent"), M(jon_laptop), created="2026-09-04")
    _msg(conn, nw, c_a2m, A(laptop, "laptop-agent"), M(jon_laptop), "agent to inbox")
    c_esc = _conv(conn, nw, M(laptop), P(maya), created="2026-09-05")
    _msg(conn, nw, c_esc, M(laptop), P(maya), "escalation: need your OK", kind="escalation")
    _msg(conn, nw, c_esc, P(maya), M(laptop), "OK")
    c_agent_me = _conv(conn, nw, A(laptop, "laptop-agent"), P(maya), created="2026-09-06")
    a1 = _msg(conn, nw, c_agent_me, A(laptop, "laptop-agent"), P(maya), "done")
    _archive(conn, c_agent_me, a1.seq)
    c_own = _conv(conn, nw, M(laptop), M(desktop), created="2026-09-07")
    _msg(conn, nw, c_own, M(laptop), M(desktop), "own 1")
    _msg(conn, nw, c_own, M(desktop), M(laptop), "own 2")
    c_pr1 = _conv(conn, nw, P(maya), M(priya_pc), created="2026-09-08")
    p1 = _msg(conn, nw, c_pr1, P(maya), M(priya_pc), "to priya")
    _archive(conn, c_pr1, p1.seq, days_ago=2)
    c_pr2 = _conv(conn, nw, A(laptop, "laptop-agent"), A(priya_pc, "web-agent"), created="2026-09-09")
    p2 = _msg(conn, nw, c_pr2, A(priya_pc, "web-agent"), A(laptop, "laptop-agent"), "from priya's agent")
    _archive(conn, c_pr2, p2.seq, by=f"a:{laptop}:laptop-agent")
    c_empty = _conv(conn, nw, P(jon), M(priya_pc), created="2026-09-10")  # no messages: left alone
    c_lee = _conv(conn, ct, M(maya_ct), P(lee), created="2026-09-11")
    _msg(conn, ct, c_lee, P(lee), M(maya_ct), "contoso hello")
    c_own_ct = _conv(conn, ct, M(maya_ct), M(maya_ct2), created="2026-09-12")
    _msg(conn, ct, c_own_ct, M(maya_ct), M(maya_ct2), "own contoso")
    c_self_ct = _conv(conn, ct, M(maya_ct2), P(maya), created="2026-09-13")
    s1 = _msg(conn, ct, c_self_ct, M(maya_ct2), P(maya), "contoso escalation", kind="escalation")
    _archive(conn, c_self_ct, s1.seq)
    return {"users": {"maya": maya, "jon": jon, "priya": priya, "lee": lee}, "teams": (nw, ct),
            "machines": {"laptop": laptop, "desktop": desktop, "jon_laptop": jon_laptop},
            "jon": [c_people, c_api, c_inbox, c_a2m], "self": [c_esc, c_agent_me], "own": c_own,
            "priya": [c_pr1, c_pr2], "empty": c_empty, "lee": c_lee, "own_ct": c_own_ct, "self_ct": c_self_ct}


def _snapshot(conn):
    convs = [dict(r) for r in conn.execute(text("SELECT * FROM conversations ORDER BY id")).mappings()]
    msgs = [tuple(r) for r in conn.execute(text(
        "SELECT id, conversation_id, moved_from_conversation_id FROM messages ORDER BY seq"))]
    return convs, msgs


def _count(conn, conv):
    return conn.execute(text("SELECT count(*) FROM messages WHERE conversation_id = :c"), {"c": conv}).scalar()


def _row(conn, conv):
    return conn.execute(text("SELECT * FROM conversations WHERE id = :c"), {"c": conv}).mappings().one()


def _max_seq(conn, conv):
    return conn.execute(text("SELECT max(seq) FROM messages WHERE conversation_id = :c"), {"c": conv}).scalar()


def test_0010_merges_by_person_pair_and_downgrades_exactly(scratch_db):
    from alembic import command

    from raincli_server.migrate import alembic_config

    url, db = scratch_db
    cfg = alembic_config(url)
    command.upgrade(cfg, "0009")
    with db.begin() as conn:
        seed = _seed(conn)
        before = _snapshot(conn)
        sources = [*seed["jon"], *seed["self"], seed["own"], *seed["priya"], seed["lee"], seed["own_ct"],
                   seed["self_ct"]]
        counts = {c: _count(conn, c) for c in sources}
    users, (nw, ct) = seed["users"], seed["teams"]
    maya = str(users["maya"])

    command.upgrade(cfg, "0010")
    with db.begin() as conn:
        # Maya and Jon: the existing person-to-person row is reused (and counted as a source); one source is
        # unarchived, so the merged thread is unarchived.
        target = seed["jon"][0]
        assert _count(conn, target) == sum(counts[c] for c in seed["jon"])
        row = _row(conn, target)
        assert (row["kind"], row["is_default"], row["merged_into"]) == ("pair", True, None)
        assert row["archived_at"] is None and row["archived_through_seq"] is None
        assert row["premerge_archive"]["archived_by_key"] == f"p:{maya}"  # its own state, kept for the downgrade
        for source in seed["jon"][1:]:
            src = _row(conn, source)
            assert (src["merged_into"], src["is_default"], src["premerge_is_default"]) == (target, False, True)
            assert _count(conn, source) == 0
        moved = conn.execute(text("SELECT premerge_conversation_id FROM messages WHERE conversation_id = :c "
                                  "ORDER BY seq"), {"c": target}).scalars().all()
        assert moved == [None, seed["jon"][1], seed["jon"][1], seed["jon"][2], seed["jon"][3]]

        # Maya and Priya: a new pair row; every source archived through its newest message, so it is archived
        # through the newest of them, by the most recent archiver mapped to their person.
        priya_target = _row(conn, seed["priya"][0])["merged_into"]
        assert _row(conn, seed["priya"][1])["merged_into"] == priya_target
        new = _row(conn, priya_target)
        lo, hi = sorted((maya, str(users["priya"])))
        assert (new["kind"], new["a_key"], new["b_key"], new["is_default"]) == ("pair", f"p:{lo}", f"p:{hi}", True)
        assert new["premerge_archive"] == {"created": True}
        assert new["archived_at"] is not None and new["archived_through_seq"] == _max_seq(conn, priya_target)
        assert new["archived_by_key"] == f"p:{maya}"
        assert _count(conn, priya_target) == counts[seed["priya"][0]] + counts[seed["priya"][1]]

        # Maya and her own machines: "self" (visible; unarchived because one source is) and "own_agents"
        # (machine-to-machine only, archived), per team.
        self_nw = _row(conn, _row(conn, seed["self"][0])["merged_into"])
        assert _row(conn, seed["self"][1])["merged_into"] == self_nw["id"]
        assert (self_nw["kind"], self_nw["a_key"], self_nw["b_key"], self_nw["is_default"]) == (
            "self", f"p:{maya}", f"self:{maya}", True)
        assert self_nw["archived_at"] is None and _count(conn, self_nw["id"]) == 3
        own_nw = _row(conn, _row(conn, seed["own"])["merged_into"])
        assert (own_nw["kind"], own_nw["is_default"], own_nw["team_id"]) == ("own_agents", False, nw)
        assert own_nw["archived_at"] is not None and own_nw["archived_through_seq"] == _max_seq(conn, own_nw["id"])
        self_ct = _row(conn, _row(conn, seed["self_ct"])["merged_into"])
        own_ct = _row(conn, _row(conn, seed["own_ct"])["merged_into"])
        assert (self_ct["kind"], self_ct["team_id"], own_ct["kind"], own_ct["team_id"]) == ("self", ct, "own_agents", ct)
        assert self_ct["archived_at"] is not None  # its one source was archived
        lee_target = _row(conn, seed["lee"])["merged_into"]
        assert _row(conn, lee_target)["team_id"] == ct and _count(conn, lee_target) == 1

        empty = _row(conn, seed["empty"])
        assert (empty["merged_into"], empty["is_default"], empty["premerge_is_default"]) == (None, True, None)
        assert conn.execute(text("SELECT count(*) FROM messages")).scalar() == len(before[1])
        assert conn.execute(text("SELECT count(*) FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                                 "WHERE c.team_id <> m.team_id")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM messages WHERE route IS NOT NULL OR redeliver")).scalar() == 0

    # One self and one own_agents conversation per person and team, of the private key shape.
    for kind in ("self", "own_agents"):
        with pytest.raises(IntegrityError), db.begin() as conn:
            conn.execute(text("INSERT INTO conversations (id, team_id, a_user_id, b_user_id, a_key, b_key, kind, "
                              "is_default) VALUES (gen_random_uuid(), :t, :u, :u, :a, :b, :k, false)"),
                         {"t": nw, "u": maya, "a": f"p:{maya}", "b": f"self:{maya}", "k": kind})
    with pytest.raises(IntegrityError), db.begin() as conn:
        conn.execute(text("INSERT INTO conversations (id, team_id, a_user_id, b_user_id, a_key, b_key, kind) "
                          "VALUES (gen_random_uuid(), :t, :u, :v, :a, :b, 'self')"),
                     {"t": ct, "u": users["jon"], "v": users["lee"], "a": f"p:{users['jon']}", "b": f"self:{users['lee']}"})
    # The inbox table: one row per person and team; a name may not start with "-".
    with db.begin() as conn:
        conn.execute(text("INSERT INTO person_inboxes (user_id, team_id, agent_id, agent_name, set_via) "
                          "VALUES (:u, :t, :a, 'laptop-agent', 'app')"),
                     {"u": maya, "t": nw, "a": seed["machines"]["laptop"]})
    with pytest.raises(IntegrityError), db.begin() as conn:
        conn.execute(text("INSERT INTO person_inboxes (user_id, team_id, agent_id, agent_name, set_via) "
                          "VALUES (:u, :t, :a, 'laptop-agent', 'web')"),
                     {"u": maya, "t": nw, "a": seed["machines"]["laptop"]})
    with pytest.raises(IntegrityError), db.begin() as conn:
        conn.execute(text("INSERT INTO person_inboxes (user_id, team_id, agent_id, agent_name, set_via) "
                          "VALUES (:u, :t, :a, '-x', 'web')"), {"u": users["jon"], "t": nw, "a": seed["machines"]["laptop"]})

    # Sent after the upgrade (as v0.6 stores them): a person message in the Maya-Jon pair (stays), Maya's agent to
    # Jon's inbox in that pair (back to its endpoints' thread), an escalation in "self" (back to its machine's
    # thread with Maya) and a message into the created Maya-Priya row (which then stays).
    laptop, jon_laptop = seed["machines"]["laptop"], seed["machines"]["jon_laptop"]
    with db.begin() as conn:
        later_pair = _msg(conn, nw, target, P(users["maya"]), P(users["jon"]), "after the upgrade")
        later_inbox = _msg(conn, nw, target, A(laptop, "laptop-agent"), M(jon_laptop), "to jon's inbox, after")
        later_esc = _msg(conn, nw, self_nw["id"], M(laptop), P(users["maya"]), "escalation, after", kind="escalation")
        later_priya = _msg(conn, nw, priya_target, P(users["priya"]), P(users["maya"]), "priya, after")
        conn.execute(text("UPDATE messages SET route = 'inbox', redeliver = true WHERE id = :m"), {"m": later_inbox.id})

    command.downgrade(cfg, "0009")
    with db.begin() as conn:
        convs, msgs = _snapshot(conn)
        by_id = {c["id"]: c for c in convs}
        for row in before[0]:  # every pre-existing conversation row is back byte for byte
            assert by_id[row["id"]] == row, row["id"]
        later = {later_pair.id, later_inbox.id, later_esc.id, later_priya.id}
        assert [m for m in msgs if m[0] not in later] == before[1]
        where = {m[0]: m[1] for m in msgs}
        assert where[later_pair.id] == seed["jon"][0] and where[later_inbox.id] == seed["jon"][3]
        assert where[later_esc.id] == seed["self"][0] and where[later_priya.id] == priya_target
        assert set(by_id) == {r["id"] for r in before[0]} | {priya_target}
        assert conn.execute(text("SELECT to_regclass('person_inboxes')")).scalar() is None

    command.upgrade(cfg, "0010")  # and up again, with nothing in the way
    with db.begin() as conn:
        assert _row(conn, seed["jon"][1])["merged_into"] == seed["jon"][0]


# Old ids after a merge (contract C1.6) -------------------------------------------------------------------------

def _merge_by_hand(session, conv_id):
    """What 0010 does to one conversation: a new person-to-person row takes its messages and its default mark."""
    from raincli_server.models import Conversation, Message

    old = session.get(Conversation, uuid.UUID(conv_id))
    old.is_default = False
    session.flush()
    new = Conversation(id=uuid.uuid4(), team_id=old.team_id, a_user_id=old.a_user_id, b_user_id=old.b_user_id,
                       agent_a_id=old.agent_a_id, agent_b_id=old.agent_b_id, a_agent_name=old.a_agent_name,
                       b_agent_name=old.b_agent_name, a_key=old.a_key, b_key=old.b_key, is_default=True)
    session.add(new)
    session.flush()
    for m in session.query(Message).filter(Message.conversation_id == old.id):
        m.premerge_conversation_id, m.conversation_id = old.id, new.id
    old.merged_into = new.id
    session.commit()
    return str(new.id)


def _person_send(client, token, to, body, **extra):
    return client.post("/api/v1/person/send", headers=auth(token),
                       json={"id": extra.pop("id", str(uuid.uuid4())), "to": to, "body": body, **extra})


def test_old_ids_redirect_resolve_and_keep_a_retried_send_idempotent(app, client, world, alice_app, session):  # noqa: F811
    token = alice_app["person"]
    first = _person_send(client, token, {"person": "bob@example.test"}, "hello")
    assert first.status_code == 201, first.text
    message = first.json()["message"]
    old = message["conversation_id"]
    new = _merge_by_hand(session, old)

    # The website and app mode answer an old id with a 302 to where it went, query string kept.
    from fastapi.testclient import TestClient

    with TestClient(app) as browser:
        assert login(browser).status_code == 303
        r = browser.get(f"/app/conversations/{old}?reply_to={message['id']}", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"].endswith(f"/app/conversations/{new}?reply_to={message['id']}")
        assert browser.get(f"/app/conversations/{new}", follow_redirects=False).status_code == 200
    webview = alice_app["webview"]
    assert webview.get(f"/app/conversations/{old}", follow_redirects=False).status_code == 302
    live = webview.get("/app/live", params={"conversation": old, "after": 0})
    assert live.status_code == 200 and message["id"] in live.text

    # The API answers the final conversation's data.
    r = client.get(f"/api/v1/person/conversations/{old}", headers=auth(token))
    assert r.status_code == 200 and r.json()["conversation"]["id"] == new, r.text
    r = client.post(f"/api/v1/person/conversations/{old}/archive", headers=auth(token), json={})
    assert r.status_code == 200 and r.json()["conversation"]["id"] == new, r.text

    # A retried send naming the old conversation is the same message, not a new one.
    again = _person_send(client, token, {"person": "bob@example.test"}, "hello", id=message["id"],
                         conversation_id=old)
    assert again.status_code in (200, 201), again.text
    assert again.json()["message"]["id"] == message["id"] and again.json()["message"]["conversation_id"] == new
    # And a new send naming the old id lands in the conversation it went into.
    later = _person_send(client, token, {"person": "bob@example.test"}, "later", conversation_id=old)
    assert later.status_code == 201 and later.json()["message"]["conversation_id"] == new, later.text
