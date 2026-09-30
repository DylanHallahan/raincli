"""Auth, scopes, cross-team isolation, spoofing and the error envelope."""

import uuid

from api_helpers import auth, err, send
from raincli_server import identity


def test_missing_and_bad_credentials(client, world):
    for headers in ({}, {"Authorization": "Bearer rca_nope"}, {"Authorization": "Basic abc"},
                    {"Authorization": world["tokens"]["alice"]}):
        r = client.get("/api/v1/me", headers=headers)
        assert r.status_code == 401 and err(r) == "unauthorized"
        assert world["tokens"]["alice"] not in r.text


def test_rotated_and_revoked_credentials(client, world, session):
    old = world["tokens"]["bob"]
    assert client.get("/api/v1/me", headers=auth(old)).status_code == 200
    new = identity.rotate_agent_credential(session, world["agents"]["bob"])
    session.commit()
    assert client.get("/api/v1/me", headers=auth(old)).status_code == 401
    assert client.get("/api/v1/me", headers=auth(new)).status_code == 200
    identity.revoke_agent(session, world["agents"]["bob"])
    session.commit()
    r = client.get("/api/v1/inbox", headers=auth(new))
    assert r.status_code == 401 and err(r) == "unauthorized"
    # a revoked agent is no longer a valid recipient, and looks the same as an unknown handle
    unknown = send(client, world["tokens"]["alice"], "no-such-agent")
    revoked = send(client, world["tokens"]["alice"], "bob-agent")
    assert revoked.status_code == unknown.status_code == 400 and revoked.json() == unknown.json()
    agents = client.get("/api/v1/agents", headers=auth(world["tokens"]["alice"])).json()["agents"]
    assert {"handle": "bob-agent", "display_name": "bob-agent", "active": False,
            "presence": {"status": "offline", "seen_at": None, "expires_at": None},
            "machine": None, "agents": []} in agents


def test_missing_scope(client, world, session):
    acme, alice = world["teams"]["acme"], world["users"]["alice"]
    _, read_only = identity.register_agent(session, acme, alice, "reader", scopes=("messages:read",))
    _, send_only = identity.register_agent(session, acme, alice, "sender", scopes=("messages:send",))
    session.commit()
    r = send(client, read_only, "bob-agent")
    assert r.status_code == 403 and err(r) == "forbidden"
    assert client.get("/api/v1/me", headers=auth(read_only)).status_code == 200  # any scope
    assert client.get("/api/v1/inbox", headers=auth(read_only)).status_code == 200
    mid = send(client, send_only, "reader").json()["message"]["id"]
    assert client.get("/api/v1/inbox", headers=auth(send_only)).status_code == 403
    assert client.get("/api/v1/agents", headers=auth(send_only)).status_code == 403
    assert client.get("/api/v1/conversations", headers=auth(send_only)).status_code == 403
    assert client.post(f"/api/v1/messages/{mid}/ack", headers=auth(read_only)).status_code == 403


def test_spoofed_from(client, world):
    t_alice = world["tokens"]["alice"]
    r = send(client, t_alice, "bob-agent", **{"from": "bob-agent"})
    assert r.status_code == 403 and err(r) == "forbidden"
    assert send(client, t_alice, "bob-agent", sender="eve-agent").status_code == 403
    ok = send(client, t_alice, "bob-agent", **{"from": "alice-agent"})
    assert ok.status_code == 201 and ok.json()["message"]["from"] == "alice-agent"


def test_ack_by_recipient_only(client, world, session):
    t_alice, t_bob, t_eve = world["tokens"]["alice"], world["tokens"]["bob"], world["tokens"]["eve"]
    _, t_carol = identity.register_agent(session, world["teams"]["acme"], world["users"]["bob"], "carol-agent")
    session.commit()
    mid = send(client, t_alice, "bob-agent").json()["message"]["id"]
    r = client.post(f"/api/v1/messages/{mid}/ack", headers=auth(t_alice))
    assert r.status_code == 403 and err(r) == "forbidden"
    r = client.post(f"/api/v1/messages/{mid}/ack", headers=auth(t_eve))
    assert r.status_code == 404 and err(r) == "not_found"
    assert client.post(f"/api/v1/messages/{mid}/ack", headers=auth(t_carol)).status_code == 404  # non-participant
    assert client.get(f"/api/v1/messages/{mid}", headers=auth(t_alice)).json()["message"]["acked_at"] is None
    assert client.post(f"/api/v1/messages/{mid}/ack", headers=auth(t_bob)).json()["acked"] is True


def test_cross_team_isolation(client, world, session):
    t_alice, t_bob, t_eve = world["tokens"]["alice"], world["tokens"]["bob"], world["tokens"]["eve"]
    _, t_mallory = identity.register_agent(session, world["teams"]["globex"], world["users"]["eve"], "mallory")
    session.commit()
    # cross-team send: same answer as for a nonexistent handle, so handles in other teams are not revealed
    cross = send(client, t_alice, "eve-agent")
    missing = send(client, t_alice, "zz-missing")
    assert cross.status_code == missing.status_code == 400 and cross.json() == missing.json()
    assert "eve" not in cross.json()["error"]["message"]
    assert send(client, t_eve, "alice-agent").json() == send(client, t_eve, "zz-missing").json()

    m = send(client, t_alice, "bob-agent", "acme only").json()["message"]
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
    assert send(client, t_eve, "mallory", in_reply_to=m["id"]).status_code == 404
    assert send(client, t_eve, "mallory", conversation_id=m["conversation_id"]).status_code == 400
    assert client.get("/api/v1/inbox?include_acked=true", headers=auth(t_eve)).json()["messages"] == []
    assert client.get("/api/v1/conversations", headers=auth(t_eve)).json()["conversations"] == []
    handles = [a["handle"] for a in client.get("/api/v1/agents", headers=auth(t_eve)).json()["agents"]]
    assert handles == ["eve-agent", "mallory"]
    # same-team non-participants cannot read either
    _, t_carol = identity.register_agent(session, world["teams"]["acme"], world["users"]["bob"], "carol-agent")
    session.commit()
    assert client.get(f"/api/v1/messages/{m['id']}", headers=auth(t_carol)).status_code == 404
    assert client.get(f"/api/v1/messages/{m['id']}", headers=auth(t_bob)).status_code == 200


def test_send_validation(client, world):
    t = world["tokens"]["alice"]
    cases = [
        {"id": str(uuid.uuid4()), "to": "bob-agent", "body": "x", "extra": 1},
        {"id": "not-a-uuid", "to": "bob-agent", "body": "x"},
        {"id": str(uuid.uuid1()), "to": "bob-agent", "body": "x"},
        {"id": str(uuid.uuid4()), "to": "bob-agent", "body": ""},
        {"id": str(uuid.uuid4()), "to": "bob-agent", "body": "   "},
        {"id": str(uuid.uuid4()), "to": "bob-agent", "body": "bad \x1b[31m escape"},
        {"id": str(uuid.uuid4()), "to": "bob-agent", "body": "x" * 16001},
        {"id": str(uuid.uuid4()), "to": "bob-agent", "body": 5},
        {"id": str(uuid.uuid4()), "to": "bob-agent", "body": "x", "in_reply_to": "nope"},
        {"id": str(uuid.uuid4()), "to": "alice-agent", "body": "self"},
        {"id": str(uuid.uuid4()), "body": "x"},
        ["not", "an", "object"],
    ]
    for payload in cases:
        r = client.post("/api/v1/messages", json=payload, headers=auth(t))
        assert r.status_code == 400 and err(r) == "invalid", (payload, r.text)
    r = client.post("/api/v1/messages", content=b"{not json", headers=auth(t))
    assert r.status_code == 400 and err(r) == "invalid"
    assert send(client, t, "bob-agent", "multi\nline\ttext").status_code == 201


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
    send(client, world["tokens"]["alice"], "bob-agent", "secret body")
    r = client.get("/api/v1/health")
    assert r.status_code == 200 and r.json() == {"ok": True, "db": "ok"}
    for needle in ("acme", "alice", "secret", "rca_"):
        assert needle not in r.text
