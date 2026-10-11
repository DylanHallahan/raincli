"""The app's live thread (protocol §19.1-§19.4, §19.6, §19.7), real PostgreSQL."""

from __future__ import annotations

import html as htmllib
import re
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, update

from api_helpers import auth
from raincli_server import identity, messaging
from raincli_server.models import Agent, AgentCredential, AgentPresence, Message, MachineAgent
from raincli_server.web import thread
from test_app_mode import alice_app  # noqa: F401  (fixture)
from test_routing import entry, report
from test_web_app import login

BOB_AGENT = {"machine": "bob-agent", "agent": "reviewer"}


def person_send(client, person, to, body="hello"):
    r = client.post("/api/v1/person/send", headers=auth(person), json={"id": str(uuid.uuid4()), "to": to, "body": body})
    assert r.status_code == 201, r.text
    return r.json()["message"]


def agent_send(client, token, to, body="hello", **extra):
    r = client.post("/api/v1/messages", headers=auth(token),
                    json={"id": str(uuid.uuid4()), "to": to, "body": body, **extra})
    assert r.status_code == 201, r.text
    return r.json()["message"]


def age(session, *messages, seconds=60):
    """Make messages older than the 30 s window that /app/live repeats (§19.8 M1)."""
    ids = [uuid.UUID(m["id"]) for m in messages]
    session.execute(update(Message).where(Message.id.in_(ids))
                    .values(created_at=identity.now() - timedelta(seconds=seconds)))
    session.commit()


def live(webview, **params):
    r = webview.get("/app/live", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def bob_reports(client, world, status="idle", reachability="instant"):
    report(client, world["tokens"]["bob"], [entry("reviewer", "a" * 32, reachability=reachability, status=status)])


def to_bob_agent(client, world, alice_app, body="hello"):  # noqa: F811
    bob_reports(client, world)
    return person_send(client, alice_app["person"], BOB_AGENT, body)


# GET /app/live ---------------------------------------------------------------------------------------

def test_live_returns_new_messages_rendered_and_never_acks(client, world, alice_app, session):  # noqa: F811
    webview = alice_app["webview"]
    first = person_send(client, alice_app["person"], {"machine": "bob-agent"}, "first")
    cid = first["conversation_id"]
    reply = agent_send(client, world["tokens"]["bob"], {"person": "alice@example.test"}, "**bold** reply",
                       conversation_id=cid)
    age(session, first)
    out = live(webview, conversation=cid, after=first["seq"])
    assert [m["id"] for m in out["messages"]] == [reply["id"]]
    fragment = out["messages"][0]["html"]
    assert fragment.lstrip().startswith("<li") and "<strong>bold</strong>" in fragment and 'id="m-' in fragment
    assert "data-unread" in fragment and "rc-trail" not in fragment  # incoming: no state
    assert "<script" not in fragment and " style=" not in fragment
    age(session, reply)
    assert live(webview, conversation=cid, after=reply["seq"])["messages"] == []
    session.expire_all()
    assert session.get(Message, uuid.UUID(reply["id"])).acked_at is None  # polling never acks (§19.2)


def test_live_reports_state_changes_of_your_own_messages(client, world, alice_app):  # noqa: F811
    webview = alice_app["webview"]
    mine = person_send(client, alice_app["person"], {"machine": "bob-agent"}, "to bob")
    cid = mine["conversation_id"]
    since = live(webview, conversation=cid, after=mine["seq"])["now"]
    r = client.post(f"/api/v1/messages/{mine['id']}/ack", headers=auth(world["tokens"]["bob"]))
    assert r.status_code == 200, r.text
    out = live(webview, conversation=cid, after=mine["seq"], states_since=since)
    assert [s["id"] for s in out["states"]] == [mine["id"]] and out["states"][0]["state"] == "received"
    trail = out["states"][0]["html"]
    assert 'data-trail="' in trail and "Delivery: received, not yet replied" in trail
    again = live(webview, conversation=cid, after=mine["seq"], states_since=out["now"])["states"]
    assert [s["state"] for s in again] == ["received"]  # not final: sent again, applied idempotently (§19.8 M1)


def test_live_needs_an_app_mode_session_and_a_visible_conversation(app, client, world, alice_app):  # noqa: F811
    with TestClient(app) as browser:
        assert login(browser).status_code == 303
        assert browser.get("/app/live").status_code == 404  # the website doesn't poll
    webview = alice_app["webview"]
    for bad in (str(uuid.uuid4()), "not-a-uuid"):
        assert webview.get("/app/live", params={"conversation": bad}).status_code == 404
    with TestClient(app) as stranger:
        assert stranger.get("/app/live", follow_redirects=False).status_code in (303, 401)


def test_the_list_comes_only_when_it_changed(client, world, alice_app):  # noqa: F811
    webview = alice_app["webview"]
    person_send(client, alice_app["person"], {"machine": "bob-agent"}, "hello")
    first = live(webview)
    assert first["list"] and 'class="rc-list"' in first["list"]["html"]
    again = live(webview, list_rev=first["list_rev"])
    assert again["list"] is None and again["list_rev"] == first["list_rev"]
    agent_send(client, world["tokens"]["bob"], {"person": "alice@example.test"}, "news")
    changed = live(webview, list_rev=first["list_rev"])
    assert changed["list"] and changed["list_rev"] != first["list_rev"]


def test_a_poll_costs_the_same_however_long_the_thread(client, world, alice_app, app, session):  # noqa: F811
    """Indexed queries only, nothing per message: a poll of a 60-message thread costs what a 3-message one does."""
    webview = alice_app["webview"]
    counts = []
    for n in (3, 60):
        world_msg = person_send(client, alice_app["person"], {"machine": "bob-agent"}, "start")
        cid = world_msg["conversation_id"]
        with app.state.sessionmaker() as s:
            bob = s.get(Agent, world["agents"]["bob"].id)
            for i in range(n):
                last_msg, _ = messaging.send_message(s, bob, id=str(uuid.uuid4()), to={"person": "alice@example.test"},
                                                     body=f"m{i}", conversation_id=cid, max_pending=10_000)
            s.commit()
            last = {"seq": last_msg.seq}
        from sqlalchemy import text
        session.execute(text("UPDATE messages SET created_at = now() - interval '1 minute' WHERE conversation_id = :c"),
                        {"c": cid})
        session.commit()
        seen = []
        engine = app.state.engine

        def count(conn, cursor, statement, *args):
            seen.append(statement)
        event.listen(engine, "before_cursor_execute", count)
        try:
            out = live(webview, conversation=cid, after=last["seq"], states_since=world_msg["created_at"])
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert out["messages"] == []
        counts.append(len(seen))
    assert counts[1] <= counts[0] + 2, counts  # list rows may differ by a query; never per message


# POST /app/ack -----------------------------------------------------------------------------------------

def test_ack_needs_csrf_and_acks_only_messages_to_you(client, world, alice_app, session):  # noqa: F811
    webview = alice_app["webview"]
    mine = person_send(client, alice_app["person"], {"machine": "bob-agent"}, "from alice")
    to_me = agent_send(client, world["tokens"]["bob"], {"person": "alice@example.test"}, "for alice",
                       conversation_id=mine["conversation_id"])
    page = webview.get(f"/app/conversations/{mine['conversation_id']}").text
    csrf = re.search(r'data-csrf="([^"]+)"', page).group(1)
    assert webview.post("/app/ack", json={"ids": [to_me["id"]]}).status_code == 403
    assert webview.post("/app/ack", json={"csrf_token": csrf, "ids": ["x"] * 101}).status_code == 400
    r = webview.post("/app/ack", json={"csrf_token": csrf, "ids": [to_me["id"], mine["id"], "junk", str(uuid.uuid4())]})
    assert r.status_code == 200 and r.json()["acked"] == [to_me["id"]]
    session.expire_all()
    assert session.get(Message, uuid.UUID(to_me["id"])).acked_at is not None
    assert session.get(Message, uuid.UUID(mine["id"])).acked_at is None
    assert webview.post("/app/ack", json={"csrf_token": csrf, "ids": [to_me["id"]]}).json()["acked"] == []


def test_opening_a_thread_acks_on_the_website_but_never_in_app_mode(app, client, world, alice_app, session):  # noqa: F811
    first = person_send(client, alice_app["person"], {"machine": "bob-agent"}, "hi")
    to_me = agent_send(client, world["tokens"]["bob"], {"person": "alice@example.test"}, "read me",
                       conversation_id=first["conversation_id"])
    alice_app["webview"].get(f"/app/conversations/{first['conversation_id']}")
    session.expire_all()
    assert session.get(Message, uuid.UUID(to_me["id"])).acked_at is None
    with TestClient(app) as browser:
        assert login(browser).status_code == 303
        browser.get(f"/app/conversations/{first['conversation_id']}")
    session.expire_all()
    assert session.get(Message, uuid.UUID(to_me["id"])).acked_at is not None  # §16.4, until D4


# Names, header and trail ---------------------------------------------------------------------------------

def test_conversations_are_named_after_owners_and_agents(client, world, alice_app, session):  # noqa: F811
    webview = alice_app["webview"]
    agent_conv = to_bob_agent(client, world, alice_app)["conversation_id"]
    inbox_conv = person_send(client, alice_app["person"], {"machine": "bob-agent"}, "to the inbox")["conversation_id"]
    page = htmllib.unescape(webview.get(f"/app/conversations/{agent_conv}").text)
    assert "<h2>Bob · reviewer</h2>" in page and "bob-agent/reviewer · Acme" in page
    page = htmllib.unescape(webview.get(f"/app/conversations/{inbox_conv}").text)
    assert "<h2>Bob · inbox</h2>" in page and "rc-pill" not in page
    from raincli_server.web import queries

    alice = alice_app["world"]["users"]["alice"]
    names = {str(r.conversation.id): r.name for r in queries.list_conversations(
        session, alice, [world["teams"]["acme"].id])}
    assert names == {agent_conv: "Bob · reviewer", inbox_conv: "Bob · inbox"}  # row.name, for the list


def test_the_header_shows_chip_and_presence_for_agents_only_with_the_offline_rule(client, world, alice_app, session):  # noqa: F811
    webview = alice_app["webview"]
    cid = to_bob_agent(client, world, alice_app)["conversation_id"]
    header = live(webview, conversation=cid)["header"]
    assert header["chip"] == "Instant" and header["word"] == "Ready" and "Instant" in header["html"]
    bob_reports(client, world, status="working")
    header = live(webview, conversation=cid)["header"]
    assert header["word"] == "Busy" and "working at its last report" in header["html"]
    assert "message" not in header["html"].lower().replace("messages", "")  # never about any message
    old = identity.now() - timedelta(seconds=121)
    session.execute(update(AgentPresence).where(AgentPresence.agent_id == world["agents"]["bob"].id).values(seen_at=old))
    session.execute(update(MachineAgent).where(MachineAgent.agent_id == world["agents"]["bob"].id).values(seen_at=old))
    session.commit()
    header = live(webview, conversation=cid)["header"]
    assert header["word"] == "Offline" and header["chip"] is None and "Instant" not in header["html"]
    person_conv = person_send(client, alice_app["person"], {"person": "bob@example.test"}, "hi bob")["conversation_id"]
    assert live(webview, conversation=person_conv)["header"]["word"] is None


@pytest.mark.parametrize("kind,steps", [("agent", ["stored", "received", "submitted", "replied"]),
                                        ("machine", ["stored", "received", "replied"]),
                                        ("person", ["stored", "received", "replied"])])
def test_the_trail_has_the_steps_of_the_recipient_kind(kind, steps):
    from types import SimpleNamespace

    recipient = SimpleNamespace(kind=kind)
    msg = SimpleNamespace(delivery_state="stored", acked_at=None, created_at=identity.now())
    trail = thread.trail_for(msg, recipient, [], None)
    assert [s.name for s in trail.steps] == steps and [s.reached for s in trail.steps] == [True] + [False] * (len(steps) - 1)
    assert trail.phrase == "Delivery: stored, not yet received"


def test_held_uncertain_and_rejected_replace_the_remaining_steps_with_a_reason():
    from types import SimpleNamespace

    agent = SimpleNamespace(kind="agent")
    acked = SimpleNamespace(delivery_state="held", acked_at=identity.now(), created_at=identity.now())
    held = thread.trail_for(acked, agent, [], "approval_required")
    assert [s.name for s in held.steps] == ["stored", "received"] and held.outcome == "held"
    assert held.reason == "waiting for approval" and held.tone == "warn"
    assert held.phrase == "Delivery: held, waiting for approval"
    stored = SimpleNamespace(delivery_state="stored", acked_at=None, created_at=identity.now())
    derived = thread.trail_for(stored, agent, [], "busy")  # derived before pick-up
    assert [s.name for s in derived.steps] == ["stored"] and derived.reason == "the session is busy"
    for state, outcome, tone in (("submission_uncertain", "uncertain", "warn"), ("rejected", "rejected", "danger")):
        msg = SimpleNamespace(delivery_state=state, acked_at=identity.now(), created_at=identity.now())
        trail = thread.trail_for(msg, agent, [], None)
        assert trail.outcome == outcome and trail.tone == tone and trail.reason
    replied = SimpleNamespace(delivery_state="replied", acked_at=identity.now(), created_at=identity.now())
    assert all(s.reached for s in thread.trail_for(replied, agent, [], None).steps)


def test_the_thread_shows_trails_only_under_your_messages(client, world, alice_app):  # noqa: F811
    mine = person_send(client, alice_app["person"], {"machine": "bob-agent"}, "mine")
    agent_send(client, world["tokens"]["bob"], {"person": "alice@example.test"}, "theirs",
               conversation_id=mine["conversation_id"])
    page = alice_app["webview"].get(f"/app/conversations/{mine['conversation_id']}").text
    assert page.count('data-trail="') == 1 and f'data-trail="{mine["id"]}"' in page
    # Bob's machine has never reported as routing-capable, so the server shows the message held for that reason.
    assert "Delivery: held, the recipient's RainCLI needs an update" in htmllib.unescape(page)
    # §20.4 R5: the trail itself is the <details> summary; there is no separate Details line.
    assert '<summary class="rc-trail-steps">' in page and "<summary>Details</summary>" not in page


# The sender warning (§19.6) ------------------------------------------------------------------------------

def credential_used(session, agent_id, when):
    session.execute(update(AgentCredential).where(AgentCredential.agent_id == agent_id).values(last_used_at=when))
    session.commit()


def test_warning_cases(client, world, session, alice_app):  # noqa: F811
    bob = world["agents"]["bob"].id
    credential_used(session, bob, None)
    assert thread.machine_warning(session, session.get(Agent, bob)).kind == "never"
    credential_used(session, bob, identity.now() - timedelta(hours=25))
    w = thread.machine_warning(session, session.get(Agent, bob))
    assert w.kind == "not_connected" and w.since is not None
    credential_used(session, bob, identity.now())
    assert thread.machine_warning(session, session.get(Agent, bob)) is None
    msg = person_send(client, alice_app["person"], {"machine": "bob-agent"}, "waiting")
    credential_used(session, bob, identity.now())
    session.execute(update(Message).where(Message.id == uuid.UUID(msg["id"]))
                    .values(created_at=identity.now() - timedelta(hours=25)))
    session.commit()
    w = thread.machine_warning(session, session.get(Agent, bob))
    assert w.kind == "not_picking_up"
    assert thread.recipient_warning(session, [world["teams"]["acme"].id], "@bob@example.test") is None  # never a person


def test_the_warning_shows_to_senders_and_to_the_owner(app, client, world, session, alice_app):  # noqa: F811
    bob = world["agents"]["bob"].id
    credential_used(session, bob, identity.now() - timedelta(days=3))
    webview = alice_app["webview"]
    for client_ in (webview,):
        page = htmllib.unescape(client_.get("/app/compose?to=bob-agent").text)
        assert "hasn't connected since" in page and "Messages will wait until it does." in page
    fragment = webview.get("/app/recipient-warning", params={"to": "bob-agent/reviewer"}).text
    assert "<strong>bob-agent</strong> hasn't connected since <time datetime=" in fragment
    assert webview.get("/app/recipient-warning", params={"to": "@bob@example.test"}).text.strip() == ""
    cid = person_send(client, alice_app["person"], {"machine": "bob-agent"}, "hello")["conversation_id"]
    credential_used(session, bob, identity.now() - timedelta(days=3))  # the send didn't use bob's credential
    assert "hasn't connected since" in webview.get(f"/app/conversations/{cid}").text
    assert live(webview, conversation=cid)["warning"]["kind"] == "not_connected"
    with TestClient(app) as browser:  # the website: compose, conversation, and the owner's Inbox
        assert login(browser).status_code == 303
        assert "hasn't connected since" in browser.get("/app/compose?to=bob-agent").text
        assert "hasn't connected since" in browser.get(f"/app/conversations/{cid}").text
    with TestClient(app) as owner:
        assert login(owner, "bob@example.test").status_code == 303
        assert "<strong>bob-agent</strong> hasn't connected since" in owner.get("/app/inbox").text


def test_migration_0009_orders_pending_messages_by_seq(engine):
    from sqlalchemy import inspect

    index = {i["name"]: i for i in inspect(engine).get_indexes("messages")}["ix_messages_recipient_pending"]
    assert index["column_names"] == ["recipient_agent_id", "seq"]
    assert "acked_at IS NULL" in index["dialect_options"]["postgresql_where"]


# §19.8 amendments ----------------------------------------------------------------------------------------

def test_out_of_order_commits_both_appear(client, world, alice_app, app, session):  # noqa: F811
    """M1: a message that commits after a higher seq still reaches the window on the next poll."""
    webview = alice_app["webview"]
    first = person_send(client, alice_app["person"], {"machine": "bob-agent"}, "start")
    cid = first["conversation_id"]
    age(session, first)
    # Sends to one recipient are serialized, so the two run in opposite directions, as two senders would.
    late_session, early_session = app.state.sessionmaker(), app.state.sessionmaker()
    try:
        alice = late_session.get(type(world["users"]["alice"]), world["users"]["alice"].id)
        late, _ = messaging.send_as_person(late_session, alice, world["teams"]["acme"].id, id=str(uuid.uuid4()),
                                           to={"machine": "bob-agent"}, body="lower seq, committed last",
                                           conversation_id=cid, max_pending=1000)
        bob_early = early_session.get(Agent, world["agents"]["bob"].id)
        early, _ = messaging.send_message(early_session, bob_early, id=str(uuid.uuid4()),
                                          to={"person": "alice@example.test"}, body="higher seq",
                                          conversation_id=cid, max_pending=1000)
        early_session.commit()
        assert late.seq < early.seq
        out = live(webview, conversation=cid, after=first["seq"])
        assert [m["id"] for m in out["messages"]] == [str(early.id)]
        late_session.commit()
    finally:
        late_session.close()
        early_session.close()
    again = live(webview, conversation=cid, after=early.seq)  # the cursor is past it, the 30 s window isn't
    assert str(late.id) in [m["id"] for m in again["messages"]]


def test_a_state_committed_before_the_polls_now_still_shows(client, world, alice_app, session):  # noqa: F811
    """M1: states are sent as they are for the viewer's own messages that aren't final, whatever the cursor."""
    webview = alice_app["webview"]
    mine = person_send(client, alice_app["person"], {"machine": "bob-agent"}, "to bob")
    cid = mine["conversation_id"]
    now = live(webview, conversation=cid, after=mine["seq"])["now"]
    assert client.post(f"/api/v1/messages/{mine['id']}/ack", headers=auth(world["tokens"]["bob"])).status_code == 200
    session.execute(update(Message).where(Message.id == uuid.UUID(mine["id"]))  # as if it committed late
                    .values(delivery_updated_at=identity.now() - timedelta(seconds=10)))
    session.commit()
    out = live(webview, conversation=cid, after=mine["seq"], states_since=now)
    assert {s["id"]: s["state"] for s in out["states"]} == {mine["id"]: "received"}
    reply = agent_send(client, world["tokens"]["bob"], {"person": "alice@example.test"}, "answer",
                       conversation_id=cid, in_reply_to=mine["id"])
    out = live(webview, conversation=cid, after=reply["seq"], states_since=out["now"])
    assert {s["id"]: s["state"] for s in out["states"]} == {mine["id"]: "replied"}
    session.execute(update(Message).where(Message.id == uuid.UUID(mine["id"]))
                    .values(delivery_updated_at=identity.now() - timedelta(seconds=60)))
    session.commit()
    later = live(webview, conversation=cid, after=reply["seq"], states_since=out["now"])
    assert later["states"] == []  # final, and changed more than 30 s ago: no longer sent (§19.9 F1)


def test_ack_never_acks_a_message_to_the_viewers_own_machine(client, world, alice_app, session):  # noqa: F811
    """M2: only messages to the viewer as a person; their machines' connectors ack their own."""
    webview = alice_app["webview"]
    to_machine = agent_send(client, world["tokens"]["bob"], "alice-agent", "for alice's machine")
    page = webview.get("/app/inbox").text
    csrf = re.search(r'data-csrf="([^"]+)"', page).group(1)
    r = webview.post("/app/ack", json={"csrf_token": csrf, "ids": [to_machine["id"]]})
    assert r.status_code == 200 and r.json()["acked"] == []
    session.expire_all()
    assert session.get(Message, uuid.UUID(to_machine["id"])).acked_at is None


def test_presence_mapping_and_the_injectable_clock(client, world, alice_app, session):  # noqa: F811
    """L3 and L6."""
    to_bob_agent(client, world, alice_app)
    peer = messaging.Endpoint("agent", agent=session.get(Agent, world["agents"]["bob"].id), name="reviewer")
    for status, word, tip in (("idle", "Ready", "idle"), ("working", "Busy", "working at its last report"),
                              ("blocked", "Busy", "waiting for its user at its last report"), ("unknown", None, "")):
        bob_reports(client, world, status=status)
        session.expire_all()
        presence = thread.header_presence(session, peer)
        assert presence.word == word and tip in presence.tooltip and presence.chip == "Instant", status
    bob_reports(client, world, status="idle")
    session.expire_all()
    seen = session.get(AgentPresence, world["agents"]["bob"].id).seen_at
    assert thread.header_presence(session, peer, now=seen + timedelta(seconds=119)).word == "Ready"
    late = thread.header_presence(session, peer, now=seen + timedelta(seconds=121))
    assert late.word == "Offline" and late.chip is None


def test_warning_precedence_scope_and_clock(client, world, session):
    """L4 and L6: one warning at a time, in order; only in the viewer's current team; a 24 h threshold."""
    bob = session.get(Agent, world["agents"]["bob"].id)
    used = identity.now()
    credential_used(session, bob.id, used)
    assert thread.machine_warning(session, bob, now=used + timedelta(hours=23)) is None
    assert thread.machine_warning(session, bob, now=used + timedelta(hours=25)).kind == "not_connected"
    acme, globex = world["teams"]["acme"].id, world["teams"]["globex"].id
    credential_used(session, bob.id, None)
    assert thread.recipient_warning(session, [acme], "bob-agent").kind == "never"
    assert thread.recipient_warning(session, [globex], "bob-agent") is None  # not in that team


def test_live_headers_and_the_list_digest_follow_team_presence(client, world, alice_app):  # noqa: F811
    """L5 headers; L2: the Team section's presence is part of the list revision."""
    webview = alice_app["webview"]
    to_bob_agent(client, world, alice_app)
    r = webview.get("/app/live")
    assert r.headers["content-type"].startswith("application/json") and r.headers["cache-control"] == "no-store"
    rev = r.json()["list_rev"]
    assert live(webview, list_rev=rev)["list"] is None
    bob_reports(client, world, status="working")
    changed = live(webview, list_rev=rev)
    assert changed["list"] is not None and changed["list_rev"] != rev


def test_a_poll_costs_the_same_however_many_of_your_messages_are_in_flight(client, world, alice_app, app, session):  # noqa: F811
    """§19.1 with §19.8 M1: the states of the viewer's non-final messages come without a query per message."""
    from raincli_server.models import User

    webview = alice_app["webview"]
    counts = []
    for n in (3, 30):
        alice = session.get(User, world["users"]["alice"].id)
        sent = [messaging.send_as_person(session, alice, world["teams"]["acme"].id, id=str(uuid.uuid4()),
                                         to={"machine": "bob-agent"}, body=f"in flight {i}", max_pending=10_000)[0]
                for i in range(n)]
        session.commit()
        cid = str(sent[0].conversation_id)
        seen = []

        def count(conn, cursor, statement, *args):
            seen.append(statement)
        event.listen(app.state.engine, "before_cursor_execute", count)
        try:
            out = live(webview, conversation=cid, after=sent[-1].seq, states_since=identity.now().isoformat())
        finally:
            event.remove(app.state.engine, "before_cursor_execute", count)
        assert len(out["states"]) >= n
        counts.append(len(seen))
    assert counts[1] <= counts[0] + 2, counts


def test_a_message_you_send_to_your_own_machine_is_yours(client, world, alice_app):  # noqa: F811
    """Your own message, whoever receives it: a trail and the accent avatar; your machine writing to you is not."""
    mine = person_send(client, alice_app["person"], {"machine": "alice-agent"}, "to my own machine")
    back = agent_send(client, world["tokens"]["alice"], {"person": "alice@example.test"}, "from my machine",
                      conversation_id=mine["conversation_id"])
    page = alice_app["webview"].get(f"/app/conversations/{mine['conversation_id']}").text
    assert f'data-trail="{mine["id"]}"' in page and f'data-trail="{back["id"]}"' not in page
    assert re.search(rf'class="rc-msg rc-out" id="m-{mine["id"]}"', page)
    assert re.search(rf'class="rc-msg" id="m-{back["id"]}"', page)


# §19.9 ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("final", ["replied", "rejected"])
def test_a_final_state_committed_after_a_poll_still_reaches_the_trail(client, world, alice_app, session, final):  # noqa: F811
    """F1: delivery_updated_at is a transaction's start, so a final state can commit 'before' the poll's now."""
    webview = alice_app["webview"]
    mine = person_send(client, alice_app["person"], {"machine": "bob-agent"}, "to bob")
    cid = mine["conversation_id"]
    assert client.post(f"/api/v1/messages/{mine['id']}/ack", headers=auth(world["tokens"]["bob"])).status_code == 200
    now = live(webview, conversation=cid, after=mine["seq"])["now"]
    if final == "replied":
        last = agent_send(client, world["tokens"]["bob"], {"person": "alice@example.test"}, "answer",
                          conversation_id=cid, in_reply_to=mine["id"])
        after = last["seq"]
    else:
        r = client.post(f"/api/v1/messages/{mine['id']}/events", headers=auth(world["tokens"]["bob"]),
                        json={"state": "rejected"})
        assert r.status_code == 200, r.text
        after = mine["seq"]
    session.execute(update(Message).where(Message.id == uuid.UUID(mine["id"]))  # as if committed late
                    .values(delivery_updated_at=identity.now() - timedelta(seconds=10)))
    session.commit()
    out = live(webview, conversation=cid, after=after, states_since=now)
    assert {s["id"]: s["state"] for s in out["states"]} == {mine["id"]: final}


def test_ack_is_app_mode_only(app, client, world):
    with TestClient(app) as browser:
        assert login(browser).status_code == 303
        page = browser.get("/app/compose").text
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        assert browser.post("/app/ack", json={"csrf_token": csrf, "ids": []}).status_code == 404


def test_qualifiers_reach_assistive_technology_and_warning_dates_are_dates(client, world, alice_app, session):  # noqa: F811
    """F7 and F9."""
    webview = alice_app["webview"]
    cid = to_bob_agent(client, world, alice_app)["conversation_id"]
    bob_reports(client, world, status="working")
    header = live(webview, conversation=cid)["header"]["html"]
    assert '<span class="sr-only">: the session was working at its last report.</span>' in header
    credential_used(session, world["agents"]["bob"].id, identity.now() - timedelta(days=3))
    warning = live(webview, conversation=cid)["warning"]["html"]
    assert re.search(r'since <time datetime="[^"]+" data-format="date">\d{4}-\d\d-\d\d</time>\. Messages', warning)


def test_live_queries_stay_on_the_conversation_index(client, world, alice_app, session, engine):  # noqa: F811
    """F6: on a 20 000-message thread both /app/live queries use (conversation_id, seq), not a scan."""
    from sqlalchemy import text

    first = person_send(client, alice_app["person"], {"machine": "bob-agent"}, "start")
    cid = first["conversation_id"]
    bob = world["agents"]["bob"].id
    session.execute(text("""
        INSERT INTO messages (id, team_id, conversation_id, sender_agent_id, recipient_user_id, body, delivery_state,
                              created_at)
        SELECT gen_random_uuid(), :team, :cid, :bob, :alice, 'bulk ' || g, 'stored', now() - interval '1 hour'
        FROM generate_series(1, 20000) g"""), {"team": world["teams"]["acme"].id, "cid": cid, "bob": bob,
                                               "alice": world["users"]["alice"].id})
    session.commit()
    session.execute(text("ANALYZE messages"))
    seen = []
    app_engine = alice_app["webview"].app.state.engine

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("SELECT") and "messages" in statement and "seq" in statement:
            seen.append((statement, parameters))
    event.listen(app_engine, "before_cursor_execute", capture)
    try:
        newest = session.scalar(text("SELECT max(seq) FROM messages WHERE conversation_id = :c"), {"c": cid})
        out = live(alice_app["webview"], conversation=cid, after=newest, states_since=identity.now().isoformat())
    finally:
        event.remove(app_engine, "before_cursor_execute", capture)
    assert out["messages"] == []
    checked = 0
    with engine.connect() as conn:
        for statement, parameters in seen:
            if "ORDER BY messages.seq" not in statement:
                continue
            plan = "\n".join(conn.exec_driver_sql("EXPLAIN " + statement, parameters).scalars())
            assert "Seq Scan on messages" not in plan, plan
            checked += 1
    assert checked >= 2  # the message range and the states
