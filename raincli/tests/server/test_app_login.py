"""Machine sign-in, POST /api/v1/app/login and /app/sign-out (protocol §15.1, §15.7), on real PostgreSQL."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from raincli_server import identity
from raincli_server.models import Agent, AgentCredential, Base

from api_helpers import auth, err
from test_web_app import PASSWORD, app_csrf, login as web_login

WRONG = "wrong password here"


def app_login(client, email="alice@example.test", password=PASSWORD, machine_name="alice-laptop", **extra):
    return client.post("/api/v1/app/login", json={"email": email, "password": password,
                                                  "machine_name": machine_name, **extra})


def credentials(session, agent_id):
    session.expire_all()
    return session.scalars(select(AgentCredential).where(AgentCredential.agent_id == agent_id)
                           .order_by(AgentCredential.created_at)).all()


# Sign-in ---------------------------------------------------------------------------

def test_new_machine_created_with_one_credential(client, world, session):
    r = app_login(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"api_url", "token", "handle", "team", "rotated"}
    assert body["api_url"] == "http://testserver" and body["handle"] == "alice-laptop"
    assert body["team"] == {"slug": "acme", "name": "Acme"} and body["rotated"] is False
    assert body["token"].startswith("rca_")
    agent = session.scalar(select(Agent).where(Agent.handle == "alice-laptop"))
    assert agent.owner_user_id == world["users"]["alice"].id and agent.signed_in_from == "alice-laptop"
    assert len(credentials(session, agent.id)) == 1
    me = client.get("/api/v1/me", headers=auth(body["token"])).json()
    assert me["agent"]["handle"] == "alice-laptop" and me["agent"]["team"]["slug"] == "acme"
    assert set(me["credential"]["scopes"]) == {"messages:read", "messages:send", "messages:ack"}


def test_re_sign_in_with_the_previous_token_rotates(client, world, session):
    first = app_login(client).json()["token"]
    r = app_login(client, previous_token=first)
    assert r.status_code == 200 and r.json()["rotated"] is True and r.json()["handle"] == "alice-laptop"
    second = r.json()["token"]
    assert second != first
    assert client.get("/api/v1/me", headers=auth(first)).status_code == 401
    assert client.get("/api/v1/me", headers=auth(second)).status_code == 200
    assert session.scalar(select(text("count(*)")).select_from(Agent).where(Agent.handle == "alice-laptop")) == 1
    agent = session.scalar(select(Agent).where(Agent.handle == "alice-laptop"))
    session.refresh(agent)
    assert agent.rotated_by == "app-login" and agent.rotated_at is not None
    creds = credentials(session, agent.id)
    assert len(creds) == 2 and creds[0].revoked_at is not None and creds[1].revoked_at is None


def test_re_sign_in_without_proof_is_name_in_use(client, world, session):
    token = app_login(client).json()["token"]
    for extra in ({}, {"previous_token": "rca_not-a-real-token"}, {"previous_token": world["tokens"]["bob"]},
                  {"replace": False}):
        r = app_login(client, **extra)
        assert r.status_code == 409 and err(r) == "name_in_use", extra
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 200  # untouched
    agent = session.scalar(select(Agent).where(Agent.handle == "alice-laptop"))
    assert agent.rotated_at is None and len(credentials(session, agent.id)) == 1


def test_replace_without_delivery_history_rotates(client, world):
    old = app_login(client).json()["token"]
    r = app_login(client, replace=True)
    assert r.status_code == 200 and r.json()["rotated"] is True
    assert client.get("/api/v1/me", headers=auth(old)).status_code == 401


def test_replace_refused_after_a_message_was_received(client, world, session):
    from api_helpers import send

    token = app_login(client).json()["token"]
    assert send(client, world["tokens"]["bob"], "alice-laptop").status_code == 201
    r = app_login(client, replace=True)
    assert r.status_code == 409 and err(r) == "name_in_use"
    r = app_login(client, replace=True, previous_token=token)  # proof of the credential still works
    assert r.status_code == 200 and r.json()["rotated"] is True


def test_replace_refused_after_an_inbox_role_was_published(client, world, session):
    token = app_login(client).json()["token"]
    report = {"status": "ready", "agents": [{"key": "1" * 32, "name": "inbox", "type": "claude", "status": "idle",
                                             "role": "inbox", "reachability": "next-turn", "source": "hook"}]}
    assert client.put("/api/v1/presence", json=report, headers=auth(token)).status_code == 200
    report["agents"] = []  # the role is gone from the snapshot, but the history stays
    assert client.put("/api/v1/presence", json=report, headers=auth(token)).status_code == 200
    r = app_login(client, replace=True)
    assert r.status_code == 409 and err(r) == "name_in_use"
    assert app_login(client, previous_token=token).status_code == 200


def test_website_machine_needs_proof(client, world):
    old = world["tokens"]["alice"]
    assert app_login(client, machine_name="alice-agent").status_code == 409
    r = app_login(client, machine_name="alice-agent", previous_token=old)
    assert r.status_code == 200 and r.json()["rotated"] is True
    assert client.get("/api/v1/me", headers=auth(old)).status_code == 401


def test_rotation_records_its_source(client, world, session):
    agent = world["agents"]["bob"]
    identity.rotate_agent_credential(session, agent)
    assert agent.rotated_by == "operator"
    identity.rotate_agent_credential(session, agent, world["users"]["bob"])
    assert agent.rotated_by == "website" and agent.rotated_at is not None


def test_name_taken_by_another_member_or_revoked(client, world, session):
    r = app_login(client, machine_name="bob-agent", previous_token=world["tokens"]["bob"], replace=True)
    assert r.status_code == 409 and err(r) == "name_taken"
    assert client.get("/api/v1/me", headers=auth(world["tokens"]["bob"])).status_code == 200  # untouched
    old = world["tokens"]["alice"]
    identity.revoke_agent(session, world["agents"]["alice"])
    session.commit()
    r = app_login(client, machine_name="alice-agent", previous_token=old, replace=True)
    assert r.status_code == 409 and err(r) == "name_taken"


@pytest.mark.parametrize("name", ["Alice-Laptop", "1laptop", "a", "x" * 33, "alice laptop", "alice_laptop", ""])
def test_machine_name_must_match_the_handle_grammar(client, world, name):
    r = app_login(client, machine_name=name)
    assert r.status_code == 400 and err(r) == "invalid"


def test_team_choice(client, world, session):
    identity.add_member(session, world["teams"]["globex"], world["users"]["alice"])
    session.commit()
    r = app_login(client)
    assert r.status_code == 409 and set(r.json()) == {"error", "teams"}
    assert r.json()["error"]["code"] == "team_choice_required"
    assert r.json()["teams"] == [{"slug": "acme", "name": "Acme"}, {"slug": "globex", "name": "Globex"}]
    assert session.scalar(select(Agent).where(Agent.handle == "alice-laptop")) is None
    r = app_login(client, team="globex")
    assert r.status_code == 201 and r.json()["team"] == {"slug": "globex", "name": "Globex"}
    # The same name is a different machine in another team.
    assert app_login(client, team="acme").status_code == 201


def test_unknown_or_foreign_team_is_invalid(client, world):
    for team in ("nope", "globex", "Not A Slug"):
        r = app_login(client, team=team)
        assert r.status_code == 400 and err(r) == "invalid", team
        assert r.json()["error"]["message"] == "unknown team"  # never confirms another team exists


def test_account_without_team(client, world, session):
    identity.create_user(session, "loner@example.test", "Loner", PASSWORD)
    session.commit()
    r = app_login(client, email="loner@example.test", machine_name="loner-pc")
    assert r.status_code == 400 and err(r) == "invalid"


@pytest.mark.parametrize("body", [
    [], "x", {"email": "alice@example.test", "password": PASSWORD},
    {"email": "alice@example.test", "password": PASSWORD, "machine_name": "m-1", "extra": 1},
    {"email": 1, "password": PASSWORD, "machine_name": "m-pc"},
    {"email": "alice@example.test", "password": PASSWORD, "machine_name": "m-pc", "team": 5},
    {"email": "alice@example.test", "password": PASSWORD, "machine_name": "m-pc", "replace": "yes"},
    {"email": "alice@example.test", "password": PASSWORD, "machine_name": "m-pc", "previous_token": 1},
])
def test_malformed_bodies(client, world, body):
    r = client.post("/api/v1/app/login", json=body)
    assert r.status_code == 400 and err(r) == "invalid"
    assert PASSWORD not in r.text


def test_wrong_password_and_unknown_user_are_generic(client, world):
    bad = app_login(client, password=WRONG)
    unknown = app_login(client, email="nobody@example.test", password=WRONG)
    disabled_or_long = app_login(client, password="x" * 257)
    for r in (bad, unknown, disabled_or_long):
        assert r.status_code == 401 and err(r) == "invalid_credentials"
        assert r.json() == bad.json()


def test_disabled_user_cannot_sign_in(client, world, session):
    identity.set_user_active(session, world["users"]["alice"], False)
    session.commit()
    r = app_login(client)
    assert r.status_code == 401 and err(r) == "invalid_credentials"


# The shared limiter ----------------------------------------------------------------------

def test_one_limiter_object_is_shared(client, world, app):
    limiter = app.state.web_login_limiter
    assert app.state.api.state.login_limiter is limiter
    app_login(client, password=WRONG)
    assert limiter._hits["pair:testclient|alice@example.test"]


def test_only_a_wrong_password_counts(client, world, session):
    identity.add_member(session, world["teams"]["globex"], world["users"]["alice"])
    session.commit()
    app_login(client, machine_name="alice-laptop", team="acme")
    for _ in range(8):  # right password, refused for other reasons: never a failure
        assert app_login(client).status_code == 409  # team_choice_required
        assert app_login(client, team="acme").status_code == 409  # name_in_use
        assert app_login(client, team="nope").status_code == 400
        assert app_login(client, team="acme", machine_name="bob-agent").status_code == 409  # name_taken
        assert app_login(client, machine_name="Bad Name").status_code == 400
    assert web_login(client).status_code == 303


def test_blocked_is_checked_before_scrypt(client, world, monkeypatch):
    from raincli_server import security

    for _ in range(6):
        app_login(client, password=WRONG)
    calls = []
    monkeypatch.setattr(security, "verify_password", lambda *a: calls.append(1) or True)
    monkeypatch.setattr(security, "hash_password", lambda *a: calls.append(1) or "x")
    assert app_login(client).status_code == 429 and calls == []


def test_app_failures_lock_the_web_login(client, world):
    for _ in range(6):
        assert app_login(client, password=WRONG).status_code == 401
    r = app_login(client)  # even the right password is refused now
    assert r.status_code == 429 and err(r) == "rate_limited"
    assert web_login(client).status_code == 429
    assert app_login(client, email="bob@example.test", machine_name="bob-pc").status_code == 201


def test_web_failures_lock_the_app_login(client, world):
    for _ in range(6):
        assert web_login(client, password=WRONG).status_code == 400
    r = app_login(client)
    assert r.status_code == 429 and err(r) == "rate_limited"


def test_mixed_failures_count_together(client, world):
    for i in range(6):
        if i % 2:
            web_login(client, password=WRONG)
        else:
            app_login(client, password=WRONG)
    assert web_login(client).status_code == 429 and app_login(client).status_code == 429


def test_app_lockout_is_per_ip_pair(client, world, app):
    for _ in range(6):
        app_login(client, password=WRONG)
    with TestClient(app, client=("203.0.113.9", 40000)) as elsewhere:
        assert app_login(elsewhere).status_code == 201


def test_success_resets_the_pair(client, world):
    for _ in range(5):
        app_login(client, password=WRONG)
    assert app_login(client).status_code == 201
    for _ in range(5):
        web_login(client, password=WRONG)
    assert web_login(client).status_code == 303


# Sign-out -----------------------------------------------------------------------------

def test_sign_out_revokes_the_machine(client, world, session):
    token = app_login(client).json()["token"]
    r = client.post("/api/v1/app/sign-out", headers=auth(token))
    assert r.status_code == 200 and r.json() == {"signed_out": True}
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 401
    assert client.post("/api/v1/app/sign-out", headers=auth(token)).status_code == 401
    agent = session.scalar(select(Agent).where(Agent.handle == "alice-laptop"))
    session.refresh(agent)
    assert agent.revoked_at is not None
    assert all(c.revoked_at is not None for c in credentials(session, agent.id))
    # The name is now taken by a revoked machine.
    r = app_login(client)
    assert r.status_code == 409 and err(r) == "name_taken"


def test_sign_out_needs_a_credential(client, world):
    assert client.post("/api/v1/app/sign-out").status_code == 401
    assert client.post("/api/v1/app/sign-out", headers=auth("rca_bogus")).status_code == 401


# The Machines page (§15.7) -------------------------------------------------------------

def test_machines_page_labels_signed_in_machines(client, world, session):
    token = app_login(client).json()["token"]
    assert web_login(client).status_code == 303
    page = client.get("/app/agents").text
    assert "Signed in from alice-laptop" in page
    assert page.count("Signed in from") == 1  # website-added machines carry no label
    assert "Credential replaced" not in page
    app_login(client, previous_token=token)
    assert "Credential replaced by app sign-in" in client.get("/app/agents").text
    agent = session.scalar(select(Agent).where(Agent.handle == "alice-laptop"))
    r = client.post(f"/app/agents/{agent.id}/revoke", data={"csrf_token": app_csrf(client)},
                    follow_redirects=False)
    assert r.status_code == 303
    session.refresh(agent)
    assert agent.revoked_at is not None


# The password is never stored or logged -------------------------------------------------

def test_password_never_stored_logged_or_echoed(client, world, session, engine, caplog):
    secret = "Unique-Secret-Passphrase-9431"
    identity.create_user(session, "carol@example.test", "Carol", secret)
    identity.add_member(session, world["teams"]["acme"], session.scalar(
        select(identity.User).where(identity.User.email == "carol@example.test")))
    identity.add_member(session, world["teams"]["globex"], session.scalar(
        select(identity.User).where(identity.User.email == "carol@example.test")))
    session.commit()
    caplog.set_level(logging.DEBUG)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)  # statements and their parameters
    try:
        responses = [
            app_login(client, email="carol@example.test", password=secret, machine_name="carol-pc"),
            app_login(client, email="carol@example.test", password=secret, machine_name="carol-pc", team="acme"),
            app_login(client, email="carol@example.test", password=secret, machine_name="carol-pc", team="acme",
                      replace=True),
            app_login(client, email="carol@example.test", password=secret, machine_name="bob-agent", team="acme"),
            app_login(client, email="carol@example.test", password=secret, machine_name="carol-pc", team="nope"),
            app_login(client, email="carol@example.test", password=secret, machine_name="Bad Name"),
            app_login(client, email="carol@example.test", password=secret + "x", machine_name="carol-pc"),
            client.post("/api/v1/app/login", json={"email": "carol@example.test", "password": secret,
                                                   "machine_name": "carol-pc", "bogus": secret}),
        ]
    finally:
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    assert [r.status_code for r in responses] == [409, 201, 200, 409, 400, 400, 401, 400]
    assert any("app sign-in: machine carol-pc" in m for m in caplog.messages)
    assert any("SELECT" in m for m in caplog.messages)  # the capture really saw the SQL log
    assert secret not in caplog.text
    for r in responses:
        assert secret not in r.text
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            dump = conn.execute(text(f'SELECT coalesce(json_agg(t)::text, \'\') FROM "{table.name}" t')).scalar()
            assert secret not in dump, table.name


# Machine-name slugs (§15.8 L1) -----------------------------------------------------------

def test_slug_vectors_shared_with_the_client():
    import json
    from pathlib import Path

    from raincli_server import security

    vectors = json.loads((Path(__file__).parents[1] / "machine_slug_vectors.json").read_text("utf-8"))["vectors"]
    assert len(vectors) >= 20
    for v in vectors:
        assert security.slugify_machine_name(v["name"]) == v["slug"], v
        assert security.valid_handle(v["slug"]), v
