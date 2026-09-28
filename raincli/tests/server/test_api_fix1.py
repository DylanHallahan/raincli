"""Regression tests for review rounds 1-2 (protocol §11, §12.5): MED-2, LOW-4, LOW-1, MED-4, LOW-2, R2-L12."""

import io
import re
import uuid

import pytest
from sqlalchemy import func, select

from api_helpers import auth, err, send
from raincli_server import admin, identity, security
from raincli_server.models import DeliveryEvent, Invitation, Membership, WebSession

PASSWORD = "correct horse battery"


def events_of(engine, mid):
    with engine.connect() as c:
        return c.execute(select(DeliveryEvent.state, DeliveryEvent.detail)
                         .where(DeliveryEvent.message_id == uuid.UUID(mid))
                         .order_by(DeliveryEvent.id)).all()


def web_login(client, email):
    page = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    r = client.post("/login", data={"email": email, "password": PASSWORD, "csrf_token": csrf},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    assert client.get("/app", follow_redirects=False).status_code == 200


# MED-2 --------------------------------------------------------------------------

def test_replied_is_sticky_against_later_events(client, world, engine):
    t_alice, t_bob = world["tokens"]["alice"], world["tokens"]["bob"]
    m = send(client, t_alice, "bob-agent", "question").json()["message"]
    client.post(f"/api/v1/messages/{m['id']}/ack", headers=auth(t_bob))
    assert send(client, t_bob, "alice-agent", "answer", in_reply_to=m["id"]).status_code == 201
    before = client.get(f"/api/v1/messages/{m['id']}", headers=auth(t_alice)).json()["message"]
    assert before["delivery_state"] == "replied"
    for state in ("held", "submitted", "submission_uncertain", "rejected"):
        r = client.post(f"/api/v1/messages/{m['id']}/events", json={"state": state}, headers=auth(t_bob))
        assert r.status_code == 200 and r.json()["message"]["delivery_state"] == "replied"
    after = client.get(f"/api/v1/messages/{m['id']}", headers=auth(t_alice)).json()["message"]
    assert after["delivery_state"] == "replied"
    assert after["delivery_updated_at"] > before["delivery_updated_at"]
    assert [s for s, _ in events_of(engine, m["id"])] == [
        "received", "replied", "held", "submitted", "submission_uncertain", "rejected"]


def test_reply_before_ack_stays_replied_after_ack(client, world):
    t_alice, t_bob = world["tokens"]["alice"], world["tokens"]["bob"]
    m = send(client, t_alice, "bob-agent").json()["message"]
    send(client, t_bob, "alice-agent", "quick answer", in_reply_to=m["id"])
    r = client.post(f"/api/v1/messages/{m['id']}/ack", headers=auth(t_bob)).json()
    assert r["acked"] is True and r["message"]["delivery_state"] == "replied"


# LOW-4 --------------------------------------------------------------------------

def test_repeated_identical_event_is_idempotent(client, world, engine):
    t_alice, t_bob = world["tokens"]["alice"], world["tokens"]["bob"]
    mid = send(client, t_alice, "bob-agent").json()["message"]["id"]
    client.post(f"/api/v1/messages/{mid}/ack", headers=auth(t_bob))
    url = f"/api/v1/messages/{mid}/events"
    first = client.post(url, json={"state": "submitted", "detail": "ok"}, headers=auth(t_bob))
    retry = client.post(url, json={"state": "submitted", "detail": "ok"}, headers=auth(t_bob))
    assert first.status_code == retry.status_code == 200
    assert retry.json()["message"] == first.json()["message"]
    assert events_of(engine, mid) == [("received", None), ("submitted", "ok")]
    # a different detail, or a later change and back, is a new event
    client.post(url, json={"state": "submitted", "detail": "again"}, headers=auth(t_bob))
    client.post(url, json={"state": "held"}, headers=auth(t_bob))
    client.post(url, json={"state": "held", "detail": ""}, headers=auth(t_bob))  # "" == no detail
    client.post(url, json={"state": "submitted", "detail": "ok"}, headers=auth(t_bob))
    assert events_of(engine, mid) == [("received", None), ("submitted", "ok"), ("submitted", "again"),
                                      ("held", None), ("submitted", "ok")]


# LOW-1 --------------------------------------------------------------------------

def test_auth_before_body_and_deep_json(client, world, app):
    t = world["tokens"]["alice"]
    big = b'{"pad": "' + b"x" * (1500 * 1024) + b'"}'
    r = client.post("/api/v1/messages", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 401 and err(r) == "unauthorized"
    deep = b"[" * 60000
    for path in ("/api/v1/messages", f"/api/v1/messages/{uuid.uuid4()}/events"):
        r = client.post(path, content=deep, headers={"Content-Type": "application/json"})
        assert r.status_code == 401
        r = client.post(path, content=deep, headers={**auth(t), "Content-Type": "application/json"})
        assert r.status_code == 400 and err(r) == "invalid"


def test_scope_and_rate_limit_before_body(settings, world, session):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from raincli_server.app import create_app

    _, read_only = identity.register_agent(session, world["teams"]["acme"], world["users"]["alice"], "reader",
                                           scopes=("messages:read",))
    session.commit()
    app = create_app(replace(settings, rate_limit_per_min=2))
    try:
        with TestClient(app) as c:
            garbage = {"Content-Type": "application/json"}
            r = c.post("/api/v1/messages", content=b"[" * 60000, headers={**auth(read_only), **garbage})
            assert r.status_code == 403 and err(r) == "forbidden"
            t = world["tokens"]["alice"]
            ok = send(c, t, "bob-agent")  # one send = one rate-limit hit, not two
            assert ok.status_code == 201
            assert c.get("/api/v1/me", headers=auth(t)).status_code == 200
            r = c.post("/api/v1/messages", content=b"{bad", headers={**auth(t), **garbage})
            assert r.status_code == 429 and err(r) == "rate_limited" and "Retry-After" in r.headers
    finally:
        app.state.engine.dispose()


# MED-4 --------------------------------------------------------------------------

def test_remove_member_revokes_agents_and_web_sessions(client, world, session):
    acme, alice, bob = world["teams"]["acme"], world["users"]["alice"], world["users"]["bob"]
    web_login(client, "bob@example.test")
    t_bob = world["tokens"]["bob"]
    with pytest.raises(identity.PermissionDenied):
        identity.remove_member(session, acme, alice, actor=bob)  # members cannot remove
    session.rollback()
    identity.remove_member(session, acme, bob, actor=alice)
    session.commit()
    assert identity.membership(session, acme.id, bob.id) is None
    assert client.get("/api/v1/me", headers=auth(t_bob)).status_code == 401
    assert client.get("/app", follow_redirects=False).status_code == 303  # web session no longer works
    assert session.scalar(select(func.count()).select_from(WebSession).where(
        WebSession.user_id == bob.id, WebSession.revoked_at.is_(None))) == 0
    assert client.get("/api/v1/me", headers=auth(world["tokens"]["alice"])).status_code == 200
    with pytest.raises(identity.IdentityError):
        identity.remove_member(session, acme, bob, actor=alice)  # no longer a member


def test_remove_member_only_touches_that_team(session, world):
    globex, eve, bob = world["teams"]["globex"], world["users"]["eve"], world["users"]["bob"]
    identity.add_member(session, globex, bob)
    agent, _ = identity.register_agent(session, globex, bob, "bob-globex")
    session.commit()
    identity.remove_member(session, globex, bob, actor=eve)
    session.commit()
    assert agent.revoked_at is not None
    assert world["agents"]["bob"].revoked_at is None  # bob's acme agent is untouched


def test_last_owner_cannot_be_removed(session, world):
    acme, alice, bob = world["teams"]["acme"], world["users"]["alice"], world["users"]["bob"]
    with pytest.raises(identity.IdentityError, match="last owner"):
        identity.remove_member(session, acme, alice)  # operator, but alice is the only owner
    session.rollback()
    identity.add_member(session, acme, bob)
    session.get(Membership, (acme.id, bob.id)).role = "owner"
    session.commit()
    identity.remove_member(session, acme, alice, actor=bob)
    session.commit()
    with pytest.raises(identity.IdentityError, match="last owner"):
        identity.remove_member(session, acme, bob, actor=bob)


def test_set_user_active(client, world, session):
    bob = world["users"]["bob"]
    web_login(client, "bob@example.test")
    with pytest.raises(identity.PermissionDenied):
        identity.set_user_active(session, bob, False, actor=world["users"]["alice"])
    identity.set_user_active(session, bob, False)
    session.commit()
    assert client.get("/app", follow_redirects=False).status_code == 303
    assert client.get("/api/v1/me", headers=auth(world["tokens"]["bob"])).status_code == 401
    assert identity.authenticate_user(session, "bob@example.test", PASSWORD) is None
    identity.set_user_active(session, bob, True)
    session.commit()
    assert identity.authenticate_user(session, "bob@example.test", PASSWORD) is not None
    assert world["agents"]["bob"].revoked_at is not None  # agents stay revoked


def _admin(database_url, *argv):
    out, errs = io.StringIO(), io.StringIO()
    code = admin.main(list(argv), env={"RAINCLI_DATABASE_URL": database_url}, stdin=io.StringIO(),
                      stdout=out, stderr=errs)
    return code, out.getvalue(), errs.getvalue()


def test_admin_remove_member_disable_enable(database_url, client, world, session):
    assert _admin(database_url, "remove-member", "--team", "acme", "--email", "alice@example.test")[0] == 1
    code, out, _ = _admin(database_url, "remove-member", "--team", "acme", "--email", "bob@example.test")
    assert code == 0 and "removed" in out
    assert client.get("/api/v1/me", headers=auth(world["tokens"]["bob"])).status_code == 401
    code, _, errs = _admin(database_url, "remove-member", "--team", "acme", "--email", "bob@example.test")
    assert code == 1 and "not a member" in errs
    assert _admin(database_url, "disable-user", "--email", "eve@example.test")[0] == 0
    assert client.get("/api/v1/me", headers=auth(world["tokens"]["eve"])).status_code == 401
    session.expire_all()  # the CLI committed in its own session
    assert identity.authenticate_user(session, "eve@example.test", PASSWORD) is None
    session.rollback()
    assert _admin(database_url, "enable-user", "--email", "eve@example.test")[0] == 0
    session.expire_all()
    assert identity.authenticate_user(session, "eve@example.test", PASSWORD) is not None


# LOW-2 (accepted, §11.8) ----------------------------------------------------------

def test_cross_team_id_collision_is_generic_conflict(client, world, session):
    identity.register_agent(session, world["teams"]["globex"], world["users"]["eve"], "eve-two")
    session.commit()
    m = send(client, world["tokens"]["alice"], "bob-agent", "acme secret").json()["message"]
    r = send(client, world["tokens"]["eve"], "eve-two", "probe", id=m["id"])
    assert r.status_code == 409 and err(r) == "id_conflict"
    assert "acme" not in r.text and "secret" not in r.text and "bob" not in r.text
    same_team = send(client, world["tokens"]["bob"], "alice-agent", "other", id=m["id"])
    assert same_team.json() == r.json()  # indistinguishable from any other conflicting id


# R2-L12 (protocol §12.5) ------------------------------------------------------------

def _make_owner(session, team, user):
    identity.add_member(session, team, user)
    session.get(Membership, (team.id, user.id)).role = "owner"
    session.flush()


def test_remove_member_revokes_their_open_invitations(client, world, session):
    acme, globex = world["teams"]["acme"], world["teams"]["globex"]
    alice, bob, eve = world["users"]["alice"], world["users"]["bob"], world["users"]["eve"]
    _make_owner(session, acme, bob)
    _make_owner(session, globex, bob)
    _, bob_acme = identity.create_invitation(session, acme, bob, "new1@example.test")
    _, bob_globex = identity.create_invitation(session, globex, bob)
    _, alice_acme = identity.create_invitation(session, acme, alice)
    session.commit()
    assert client.get(f"/invite/{bob_acme}").status_code == 200
    identity.remove_member(session, acme, bob, actor=alice)
    session.commit()
    assert identity.find_open_invitation(session, bob_acme) is None
    assert client.get(f"/invite/{bob_acme}").status_code != 200
    with pytest.raises(identity.IdentityError):
        identity.accept_invitation(session, bob_acme, email="new1@example.test", display_name="New",
                                   password="another long password")
    session.rollback()
    # only that team's invitations by that user are revoked
    assert identity.find_open_invitation(session, bob_globex) is not None
    assert identity.find_open_invitation(session, alice_acme) is not None
    session.rollback()


def test_disable_user_revokes_all_their_open_invitations(world, session):
    acme, globex = world["teams"]["acme"], world["teams"]["globex"]
    alice, bob = world["users"]["alice"], world["users"]["bob"]
    _make_owner(session, acme, bob)
    _make_owner(session, globex, bob)
    _, t1 = identity.create_invitation(session, acme, bob)
    _, t2 = identity.create_invitation(session, globex, bob)
    _, used = identity.create_invitation(session, acme, bob)
    identity.accept_invitation(session, used, email="used@example.test", display_name="Used",
                               password="another long password")
    _, other = identity.create_invitation(session, acme, alice)
    session.commit()
    identity.set_user_active(session, bob, False)
    session.commit()
    assert identity.find_open_invitation(session, t1) is None
    assert identity.find_open_invitation(session, t2) is None
    assert identity.find_open_invitation(session, other) is not None
    accepted = session.scalar(select(Invitation).where(Invitation.token_hash == security.hash_token(used)))
    assert accepted.accepted_at is not None and accepted.revoked_at is None  # history untouched
    session.rollback()
