"""Auth, scopes, cross-team isolation, spoofing and the error envelope."""

import uuid

from api_helpers import auth, err, send
from raincli_server import identity

BOB = "bob@example.test"  # teammates are people (§21 C2)
GENERIC = "Address people: send to a teammate's name or email"


def third_person(session, world):
    """carol: a same-team member who is in none of alice's and bob's messages, with her own machine."""
    carol = identity.create_user(session, "carol@example.test", "Carol", "correct horse battery")
    identity.add_member(session, world["teams"]["acme"], carol)
    _, token = identity.register_agent(session, world["teams"]["acme"], carol, "carol-agent")
    session.commit()
    return token


def test_missing_and_bad_credentials(client, world):
    for headers in ({}, {"Authorization": "Bearer rca_nope"}, {"Authorization": "Basic abc"},
                    {"Authorization": world["tokens"]["alice"]}):
        r = client.get("/api/v1/me", headers=headers)
        assert r.status_code == 401 and err(r) == "unauthorized"
        assert world["tokens"]["alice"] not in r.text


def test_rotated_and_revoked_credentials(client, world, session):
    old = world["tokens"]["bob"]
    _, t_bob_two = identity.register_agent(session, world["teams"]["acme"], world["users"]["bob"], "bob-two")
    session.commit()
    assert client.get("/api/v1/me", headers=auth(old)).status_code == 200
    new = identity.rotate_agent_credential(session, world["agents"]["bob"])
    session.commit()
    assert client.get("/api/v1/me", headers=auth(old)).status_code == 401
    assert client.get("/api/v1/me", headers=auth(new)).status_code == 200
    identity.revoke_agent(session, world["agents"]["bob"])
    session.commit()
    r = client.get("/api/v1/inbox", headers=auth(new))
    assert r.status_code == 401 and err(r) == "unauthorized"
    # a revoked machine's handle looks the same as an unknown handle (not mapped to its person, C13 L3)
    unknown = send(client, world["tokens"]["alice"], "no-such-agent")
    revoked = send(client, world["tokens"]["alice"], "bob-agent")
    assert revoked.status_code == unknown.status_code == 400 and revoked.json() == unknown.json()
    assert err(revoked) == "address_people" and revoked.json()["error"]["message"] == GENERIC
    # it is no longer Bob's inbox: a message to him waits in his app (C6.4)
    m = send(client, world["tokens"]["alice"], BOB).json()["message"]
    assert (m["delivery_state"], m["hold_reason"]) == ("held", "no_inbox")
    # teammates never see his machines; his other machine sees it, inactive, in the old shape (C13 M2)
    assert "bob-agent" not in client.get("/api/v1/agents", headers=auth(world["tokens"]["alice"])).text
    agents = client.get("/api/v1/agents", headers=auth(t_bob_two)).json()["agents"]
    assert {"handle": "bob-agent", "display_name": "bob-agent", "owner": "Bob", "active": False,
            "presence": {"status": "offline", "seen_at": None, "expires_at": None},
            "machine": None, "routing": "all", "agents": []} in agents


def test_missing_scope(client, world, session):
    acme, alice = world["teams"]["acme"], world["users"]["alice"]
    _, read_only = identity.register_agent(session, acme, alice, "reader", scopes=("messages:read",))
    _, send_only = identity.register_agent(session, acme, alice, "sender", scopes=("messages:send",))
    session.commit()
    r = send(client, read_only, BOB)
    assert r.status_code == 403 and err(r) == "forbidden"
    assert client.get("/api/v1/me", headers=auth(read_only)).status_code == 200  # any scope
    assert client.get("/api/v1/inbox", headers=auth(read_only)).status_code == 200
    mid = send(client, send_only, BOB).json()["message"]["id"]
    assert client.get("/api/v1/inbox", headers=auth(send_only)).status_code == 403
    assert client.get("/api/v1/agents", headers=auth(send_only)).status_code == 403
    assert client.get("/api/v1/conversations", headers=auth(send_only)).status_code == 403
    assert client.post(f"/api/v1/messages/{mid}/ack", headers=auth(read_only)).status_code == 403


def test_spoofed_from(client, world):
    t_alice = world["tokens"]["alice"]
    for spoof in ("bob-agent", BOB, "@" + BOB):
        r = send(client, t_alice, BOB, **{"from": spoof})
        assert r.status_code == 403 and err(r) == "forbidden", spoof
    assert send(client, t_alice, BOB, sender="eve-agent").status_code == 403
    ok = send(client, t_alice, BOB, **{"from": "alice-agent"})
    assert ok.status_code == 201 and ok.json()["message"]["from"] == "alice-agent"  # the reader's own handle


def test_ack_by_recipient_only(client, world, session):
    t_alice, t_bob, t_eve = world["tokens"]["alice"], world["tokens"]["bob"], world["tokens"]["eve"]
    t_carol = third_person(session, world)
    _, t_bob_two = identity.register_agent(session, world["teams"]["acme"], world["users"]["bob"], "bob-two")
    session.commit()
    mid = send(client, t_alice, BOB).json()["message"]["id"]
    r = client.post(f"/api/v1/messages/{mid}/ack", headers=auth(t_alice))
    assert r.status_code == 403 and err(r) == "forbidden"
    r = client.post(f"/api/v1/messages/{mid}/ack", headers=auth(t_eve))
    assert r.status_code == 404 and err(r) == "not_found"
    assert client.post(f"/api/v1/messages/{mid}/ack", headers=auth(t_carol)).status_code == 404  # non-participant
    # Bob's other machine is not the recipient: its ack is a no-op (C13 L1) and acks nothing.
    r = client.post(f"/api/v1/messages/{mid}/ack", headers=auth(t_bob_two))
    assert r.status_code == 200 and r.json()["acked"] is False
    assert client.get(f"/api/v1/messages/{mid}", headers=auth(t_bob)).json()["message"]["acked_at"] is None
    assert client.post(f"/api/v1/messages/{mid}/ack", headers=auth(t_bob)).json()["acked"] is True


def test_cross_team_isolation(client, world, session):
    t_alice, t_bob, t_eve = world["tokens"]["alice"], world["tokens"]["bob"], world["tokens"]["eve"]
    _, t_mallory = identity.register_agent(session, world["teams"]["globex"], world["users"]["eve"], "mallory")
    session.commit()
    # cross-team send: same answer as for a nonexistent handle or person, so other teams are not revealed
    missing = send(client, t_alice, "zz-missing")
    assert missing.status_code == 400 and err(missing) == "address_people"
    for cross in ("eve-agent", {"machine": "eve-agent"}, "eve@example.test", {"person": "eve@example.test"}, "Eve",
                  "nobody@example.test"):
        r = send(client, t_alice, cross)
        assert r.status_code == 400 and r.json() == missing.json(), cross
        assert "eve" not in r.json()["error"]["message"].lower()
    for cross in ("alice-agent", "alice@example.test", "Alice"):
        assert send(client, t_eve, cross).json() == send(client, t_eve, "zz-missing").json(), cross

    m = send(client, t_alice, BOB, "acme only").json()["message"]
    unknown = str(uuid.uuid4())
    for path in (f"/api/v1/messages/{m['id']}", f"/api/v1/conversations/{m['conversation_id']}/messages"):
        r = client.get(path, headers=auth(t_eve))
        assert r.status_code == 404 and err(r) == "not_found" and "acme only" not in r.text
    assert client.get(f"/api/v1/messages/{unknown}", headers=auth(t_eve)).json() == \
        client.get(f"/api/v1/messages/{m['id']}", headers=auth(t_eve)).json()
    assert client.post(f"/api/v1/messages/{m['id']}/ack", headers=auth(t_eve)).status_code == 404
    assert client.post(f"/api/v1/messages/{m['id']}/events", json={"state": "held"},
                       headers=auth(t_eve)).status_code == 404
    # replying to another team's message, or reusing its conversation, fails without leaking it
    assert send(client, t_eve, "eve@example.test", in_reply_to=m["id"]).status_code == 404
    r = send(client, t_eve, "eve@example.test", conversation_id=m["conversation_id"])
    assert r.status_code == 400 and err(r) == "invalid" and "acme only" not in r.text
    assert client.get("/api/v1/inbox?include_acked=true", headers=auth(t_eve)).json()["messages"] == []
    assert client.get("/api/v1/conversations", headers=auth(t_eve)).json()["conversations"] == []
    handles = [a["handle"] for a in client.get("/api/v1/agents", headers=auth(t_eve)).json()["agents"]]
    assert handles == ["eve-agent", "mallory"]
    # same-team non-participants cannot read either
    t_carol = third_person(session, world)
    assert client.get(f"/api/v1/messages/{m['id']}", headers=auth(t_carol)).status_code == 404
    assert client.get(f"/api/v1/messages/{m['id']}", headers=auth(t_bob)).status_code == 200


def test_send_validation(client, world):
    t = world["tokens"]["alice"]
    cases = [
        {"id": str(uuid.uuid4()), "to": BOB, "body": "x", "extra": 1},
        {"id": "not-a-uuid", "to": BOB, "body": "x"},
        {"id": str(uuid.uuid1()), "to": BOB, "body": "x"},
        {"id": str(uuid.uuid4()), "to": BOB, "body": ""},
        {"id": str(uuid.uuid4()), "to": BOB, "body": "   "},
        {"id": str(uuid.uuid4()), "to": BOB, "body": "bad \x1b[31m escape"},
        {"id": str(uuid.uuid4()), "to": BOB, "body": "x" * 16001},
        {"id": str(uuid.uuid4()), "to": BOB, "body": 5},
        {"id": str(uuid.uuid4()), "to": BOB, "body": "x", "in_reply_to": "nope"},
        {"id": str(uuid.uuid4()), "body": "x"},
        ["not", "an", "object"],
    ]
    for payload in cases:
        r = client.post("/api/v1/messages", json=payload, headers=auth(t))
        assert r.status_code == 400 and err(r) == "invalid", (payload, r.text)
    r = client.post("/api/v1/messages", content=b"{not json", headers=auth(t))
    assert r.status_code == 400 and err(r) == "invalid"
    # A machine form, including its own handle, is refused (§21 C2.2, C13 H2 iii).
    for to in ("alice-agent", "bob-agent"):
        r = client.post("/api/v1/messages", json={"id": str(uuid.uuid4()), "to": to, "body": "x"}, headers=auth(t))
        assert r.status_code == 400 and err(r) == "address_people", to
    assert send(client, t, BOB, "multi\nline\ttext").status_code == 201


def test_error_envelope_for_framework_errors(client, world):
    t = world["tokens"]["alice"]
    r = client.get("/api/v1/nope", headers=auth(t))
    assert r.status_code == 404 and err(r) == "not_found"
    r = client.delete("/api/v1/inbox", headers=auth(t))
    assert r.status_code == 405 and err(r) == "method_not_allowed"
    for q in ("after=-1", "limit=0", "after=abc", "wait=-2", "include_acked=maybe", "after=99999999999999999999"):
        r = client.get(f"/api/v1/inbox?{q}", headers=auth(t))
        assert r.status_code == 400 and err(r) == "invalid", (q, r.text)
    assert client.get(f"/api/v1/messages/not-a-uuid", headers=auth(t)).status_code == 404


def test_web_routes_are_not_wrapped(client):
    r = client.get("/definitely-not-an-api-route")
    assert r.status_code == 404 and "error" not in r.json()


def test_health_exposes_no_private_data(client, world):
    send(client, world["tokens"]["alice"], BOB, "secret body")
    r = client.get("/api/v1/health")
    assert r.status_code == 200 and r.json() == {"ok": True, "db": "ok"}
    for needle in ("acme", "alice", "secret", "rca_"):
        assert needle not in r.text
