"""Core send/receive/ack/event flows through the HTTP API on real PostgreSQL."""

import threading
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from api_helpers import auth, err, send
from raincli_server import messaging
from raincli_server.app import create_app
from raincli_server.db import make_sessionmaker
from raincli_server.models import Agent, Conversation, DeliveryEvent, Message


def test_send_and_reply_between_two_agents(client, world):
    t_alice, t_bob = world["tokens"]["alice"], world["tokens"]["bob"]
    r = send(client, t_alice, "bob-agent", "can you review PR 12?")
    assert r.status_code == 201, r.text
    m = r.json()["message"]
    assert r.json()["created"] is True
    assert m["from"] == "alice-agent" and m["to"] == "bob-agent" and m["delivery_state"] == "stored"
    assert m["created_at"].endswith("Z") and m["acked_at"] is None and m["in_reply_to"] is None

    inbox = client.get("/api/v1/inbox", headers=auth(t_bob)).json()
    assert [x["id"] for x in inbox["messages"]] == [m["id"]] and inbox["cursor"] == m["seq"]

    a = client.post(f"/api/v1/messages/{m['id']}/ack", headers=auth(t_bob))
    assert a.status_code == 200 and a.json()["acked"] is True
    assert a.json()["message"]["delivery_state"] == "received" and a.json()["message"]["acked_at"]
    again = client.post(f"/api/v1/messages/{m['id']}/ack", headers=auth(t_bob)).json()
    assert again["acked"] is False and again["message"]["acked_at"] == a.json()["message"]["acked_at"]

    reply = send(client, t_bob, "alice-agent", "done, LGTM", in_reply_to=m["id"])
    assert reply.status_code == 201, reply.text
    rm = reply.json()["message"]
    assert rm["conversation_id"] == m["conversation_id"] and rm["in_reply_to"] == m["id"]

    parent = client.get(f"/api/v1/messages/{m['id']}", headers=auth(t_alice)).json()["message"]
    assert parent["delivery_state"] == "replied"

    convs = client.get("/api/v1/conversations", headers=auth(t_alice)).json()["conversations"]
    assert len(convs) == 1 and convs[0]["peer"] == "bob-agent" and convs[0]["last_seq"] == rm["seq"]
    assert convs[0]["unacked"] == 1 and convs[0]["id"] == m["conversation_id"]
    thread = client.get(f"/api/v1/conversations/{m['conversation_id']}/messages", headers=auth(t_alice)).json()
    assert [x["id"] for x in thread["messages"]] == [m["id"], rm["id"]] and thread["cursor"] == rm["seq"]
    later = client.get(f"/api/v1/conversations/{m['conversation_id']}/messages?after={m['seq']}",
                       headers=auth(t_bob)).json()
    assert [x["id"] for x in later["messages"]] == [rm["id"]]


def test_reply_by_original_sender_does_not_mark_replied(client, world):
    t_alice = world["tokens"]["alice"]
    m = send(client, t_alice, "bob-agent", "first").json()["message"]
    r = send(client, t_alice, "bob-agent", "follow-up", in_reply_to=m["id"])
    assert r.status_code == 201
    got = client.get(f"/api/v1/messages/{m['id']}", headers=auth(t_alice)).json()["message"]
    assert got["delivery_state"] == "stored"


def test_reply_rules(client, world):
    t_alice, t_bob = world["tokens"]["alice"], world["tokens"]["bob"]
    m = send(client, t_alice, "bob-agent").json()["message"]
    assert send(client, t_bob, "bob-agent", in_reply_to=m["id"]).status_code == 400  # self
    assert err(send(client, t_bob, "alice-agent", in_reply_to=str(uuid.uuid4()))) == "not_found"
    assert err(send(client, t_bob, "alice-agent", in_reply_to=m["id"],
                    conversation_id=str(uuid.uuid4()))) == "invalid"
    # explicit conversation_id without a reply must be this pair's conversation
    assert send(client, t_bob, "alice-agent", conversation_id=m["conversation_id"]).status_code == 201
    assert err(send(client, t_bob, "alice-agent", conversation_id=str(uuid.uuid4()))) == "invalid"


def test_offline_catch_up_with_cursor(client, world):
    t_alice, t_bob = world["tokens"]["alice"], world["tokens"]["bob"]
    ids = [send(client, t_alice, "bob-agent", f"msg {i}").json()["message"]["id"] for i in range(4)]
    page = client.get("/api/v1/inbox?after=0&limit=2", headers=auth(t_bob)).json()
    assert [x["id"] for x in page["messages"]] == ids[:2]
    rest = client.get(f"/api/v1/inbox?after={page['cursor']}&limit=100", headers=auth(t_bob)).json()
    assert [x["id"] for x in rest["messages"]] == ids[2:]
    empty = client.get(f"/api/v1/inbox?after={rest['cursor']}", headers=auth(t_bob)).json()
    assert empty == {"messages": [], "cursor": rest["cursor"]}
    # acked messages drop out of the default inbox but come back with include_acked
    client.post(f"/api/v1/messages/{ids[0]}/ack", headers=auth(t_bob))
    assert [x["id"] for x in client.get("/api/v1/inbox", headers=auth(t_bob)).json()["messages"]] == ids[1:]
    full = client.get("/api/v1/inbox?include_acked=true", headers=auth(t_bob)).json()["messages"]
    assert [x["id"] for x in full] == ids
    # the sender's own inbox is empty
    assert client.get("/api/v1/inbox", headers=auth(t_alice)).json()["messages"] == []


def test_idempotent_retry_and_id_conflict(client, world, engine):
    t_alice = world["tokens"]["alice"]
    mid = str(uuid.uuid4())
    first = send(client, t_alice, "bob-agent", "same", id=mid)
    retry = send(client, t_alice, "bob-agent", "same", id=mid)
    assert first.status_code == 201 and retry.status_code == 200
    assert retry.json()["created"] is False and retry.json()["message"] == first.json()["message"]
    conv = first.json()["message"]["conversation_id"]
    assert send(client, t_alice, "bob-agent", "same", id=mid, conversation_id=conv).status_code == 200
    diff = send(client, t_alice, "bob-agent", "different body", id=mid)
    assert diff.status_code == 409 and err(diff) == "id_conflict"
    # the same id from another agent is a conflict, never a read of someone else's message
    other = send(client, world["tokens"]["bob"], "alice-agent", "same", id=mid)
    assert other.status_code == 409 and "message" not in other.json()
    with engine.connect() as c:
        assert c.execute(select(func.count()).select_from(Message)).scalar() == 1


def test_concurrent_duplicate_sends_store_one_row(world, engine, settings):
    """Real DB concurrency: threads with their own sessions race the same id."""
    factory = make_sessionmaker(engine)
    alice_id = world["agents"]["alice"].id
    mid = uuid.uuid4()
    n = 8
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker(body):
        s = factory()
        try:
            alice = s.get(Agent, alice_id)
            barrier.wait()
            _, created = messaging.send_message(
                s, alice, id=mid, to_handle="bob-agent", body=body, max_pending=settings.max_pending)
            s.commit()
            results.append(created)
        except messaging.MessagingError as exc:
            s.rollback()
            errors.append(exc.code)
        except Exception as exc:  # anything else is a bug (would be a 500)
            s.rollback()
            errors.append(repr(exc))
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=("same body",)) for _ in range(n - 2)]
    threads += [threading.Thread(target=worker, args=("other body",)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with engine.connect() as c:
        rows = c.execute(select(Message.body).where(Message.id == mid)).scalars().all()
        convs = c.execute(select(func.count()).select_from(Conversation)).scalar()
    assert len(rows) == 1 and convs == 1
    assert results.count(True) == 1
    winner_body = rows[0]
    # every loser got either created:false (same payload) or id_conflict (different payload)
    assert set(errors) <= {"id_conflict"}
    assert len(results) + len(errors) == n
    same_count = n - 2 if winner_body == "same body" else 2
    assert results.count(False) == same_count - 1


def test_concurrent_http_duplicates(client, world, engine):
    mid = str(uuid.uuid4())
    statuses = []

    def go():
        statuses.append(send(client, world["tokens"]["alice"], "bob-agent", "x", id=mid).status_code)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses) == [200] * 5 + [201]
    with engine.connect() as c:
        assert c.execute(select(func.count()).select_from(Message)).scalar() == 1


def test_concurrent_capacity_not_bypassed(world, engine, settings):
    factory = make_sessionmaker(engine)
    alice_id = world["agents"]["alice"].id
    n = settings.max_pending + 5
    barrier = threading.Barrier(n)
    codes = []

    def worker():
        s = factory()
        try:
            alice = s.get(Agent, alice_id)
            barrier.wait()
            messaging.send_message(s, alice, id=uuid.uuid4(), to_handle="bob-agent", body="x",
                                   max_pending=settings.max_pending)
            s.commit()
            codes.append("ok")
        except messaging.MessagingError as exc:
            s.rollback()
            codes.append(exc.code)
        finally:
            s.close()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes.count("ok") == settings.max_pending and codes.count("inbox_full") == 5


def test_restart_keeps_data(settings, world):
    t_alice, t_bob = world["tokens"]["alice"], world["tokens"]["bob"]
    app1 = create_app(settings)
    with TestClient(app1) as c1:
        mid = send(c1, t_alice, "bob-agent", "survives a restart").json()["message"]["id"]
    app1.state.engine.dispose()
    app2 = create_app(settings)  # new app, new engine and connection pool
    try:
        with TestClient(app2) as c2:
            inbox = c2.get("/api/v1/inbox", headers=auth(t_bob)).json()
            assert [m["id"] for m in inbox["messages"]] == [mid]
            assert inbox["messages"][0]["body"] == "survives a restart"
            assert send(c2, t_alice, "bob-agent", "survives a restart", id=mid).status_code == 200
    finally:
        app2.state.engine.dispose()


def test_inbox_full_and_retry_of_existing_id(client, world, settings):
    t_alice = world["tokens"]["alice"]
    first_id = str(uuid.uuid4())
    assert send(client, t_alice, "bob-agent", "m0", id=first_id).status_code == 201
    for i in range(1, settings.max_pending):
        assert send(client, t_alice, "bob-agent", f"m{i}").status_code == 201
    full = send(client, t_alice, "bob-agent", "one too many")
    assert full.status_code == 429 and err(full) == "inbox_full"
    assert send(client, t_alice, "bob-agent", "m0", id=first_id).status_code == 200
    # acking frees capacity
    client.post(f"/api/v1/messages/{first_id}/ack", headers=auth(world["tokens"]["bob"]))
    assert send(client, t_alice, "bob-agent", "fits now").status_code == 201


def test_events_only_after_ack_and_only_by_recipient(client, world, engine):
    t_alice, t_bob, t_eve = world["tokens"]["alice"], world["tokens"]["bob"], world["tokens"]["eve"]
    mid = send(client, t_alice, "bob-agent").json()["message"]["id"]
    url = f"/api/v1/messages/{mid}/events"
    r = client.post(url, json={"state": "held", "detail": "approval_required"}, headers=auth(t_bob))
    assert r.status_code == 409 and err(r) == "not_acked"
    client.post(f"/api/v1/messages/{mid}/ack", headers=auth(t_bob))
    assert err(client.post(url, json={"state": "held"}, headers=auth(t_alice))) == "forbidden"
    assert client.post(url, json={"state": "held"}, headers=auth(t_eve)).status_code == 404
    assert err(client.post(url, json={"state": "replied"}, headers=auth(t_bob))) == "invalid"
    assert err(client.post(url, json={"state": "held", "x": 1}, headers=auth(t_bob))) == "invalid"
    assert err(client.post(url, json={"state": "held", "detail": "x" * 501}, headers=auth(t_bob))) == "invalid"
    for state in ("held", "submitted"):
        r = client.post(url, json={"state": state, "detail": "busy"}, headers=auth(t_bob))
        assert r.status_code == 200 and r.json()["message"]["delivery_state"] == state
    seen = client.get(f"/api/v1/messages/{mid}", headers=auth(t_alice)).json()["message"]
    assert seen["delivery_state"] == "submitted" and seen["delivery_updated_at"]
    with engine.connect() as c:
        history = c.execute(select(DeliveryEvent.state).where(DeliveryEvent.message_id == uuid.UUID(mid))
                            .order_by(DeliveryEvent.id)).scalars().all()
    assert history == ["received", "held", "submitted"]


def test_me_and_agents(client, world):
    me = client.get("/api/v1/me", headers=auth(world["tokens"]["alice"])).json()
    assert me["agent"] == {"handle": "alice-agent", "display_name": "alice-agent",
                           "team": {"slug": "acme", "name": "Acme"}}
    assert me["credential"]["prefix"] == world["tokens"]["alice"][:12]
    assert sorted(me["credential"]["scopes"]) == ["messages:ack", "messages:read", "messages:send"]
    agents = client.get("/api/v1/agents", headers=auth(world["tokens"]["alice"])).json()["agents"]
    assert agents == [{"handle": "alice-agent", "display_name": "alice-agent", "active": True,
                       "presence": {"status": "unknown", "seen_at": None, "expires_at": None}},
                      {"handle": "bob-agent", "display_name": "bob-agent", "active": True,
                       "presence": {"status": "unknown", "seen_at": None, "expires_at": None}}]
