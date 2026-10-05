"""Website tests against real PostgreSQL (protocol §6, §8 web)."""

from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from raincli_server import identity, security
from raincli_server.models import Attachment, Invitation, Membership, Message, User, WebSession

PASSWORD = "correct horse battery"
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_of(html: str) -> str:
    m = CSRF_RE.search(html)
    assert m, "form has no CSRF token"
    return m.group(1)


def login(client, email="alice@example.test", password=PASSWORD, root=""):
    page = client.get(f"{root}/login")
    assert page.status_code == 200
    return client.post(
        f"{root}/login", data={"email": email, "password": password, "csrf_token": csrf_of(page.text)},
        follow_redirects=False,
    )


def app_csrf(client, root=""):
    return csrf_of(client.get(f"{root}/app/agents").text)


def add_message(session, sender, recipient, body="hello", **kw):
    """Store a message through the messaging service, as an agent would."""
    from raincli_server import messaging

    msg, _ = messaging.send_message(
        session, sender, id=uuid.uuid4(), to_handle=recipient.handle, body=body, max_pending=1000, **kw,
    )
    session.commit()
    return msg


# Public pages -----------------------------------------------------------------

def test_public_page_needs_no_auth_and_shows_no_private_data(client, world, session):
    add_message(session, world["agents"]["alice"], world["agents"]["bob"], body="secret launch plan")
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "RainCLI" in html and "Example" in html and "end-to-end" in html and "Invite-only" in html
    for private in ("secret launch plan", "alice-agent", "alice@example.test", "Acme", "rca_", "rci_"):
        assert private not in html
    assert "<script>" not in html and "onclick" not in html  # no inline script (CSP)


def test_security_headers_on_web_pages(client, world):
    for path in ("/", "/login", "/app"):
        r = client.get(path, follow_redirects=False)
        h = r.headers
        assert h["content-security-policy"].startswith("default-src 'self'")
        assert h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "same-origin"
        assert h["x-content-type-options"] == "nosniff"
    assert client.get("/app", follow_redirects=False).headers["cache-control"] == "no-store"
    static = client.get("/static/app.css")
    assert static.status_code == 200 and static.headers["x-frame-options"] == "DENY"


def test_unauthenticated_app_redirects_to_login(client, world):
    r = client.get("/app/agents", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/app/agents"


def test_oversized_web_post_rejected(client, world):
    r = client.post("/login", content=b"x" * (70 * 1024), headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


# Login and logout -----------------------------------------------------------------

def test_login_sets_secure_session_cookie_and_logout_revokes(client, world, session, settings):
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("raincli_session="))
    lower = cookie.lower()
    assert "httponly" in lower and "samesite=lax" in lower and "path=/" in lower
    assert "secure" not in lower  # RAINCLI_COOKIE_SECURE=0 in tests
    raw = cookie.split(";", 1)[0].split("=", 1)[1]
    stored = session.scalar(select(WebSession))
    assert stored.token_hash == security.hash_token(raw) and raw not in stored.token_hash

    inbox = client.get("/app")
    assert inbox.status_code == 200 and "Inbox" in inbox.text
    out = client.post("/logout", data={"csrf_token": csrf_of(inbox.text)}, follow_redirects=False)
    assert out.status_code == 303 and out.headers["location"] == "/"
    session.expire_all()
    assert session.scalar(select(WebSession)).revoked_at is not None
    # The old cookie value no longer works even if replayed.
    client.cookies.set("raincli_session", raw)
    assert client.get("/app", follow_redirects=False).status_code == 303


def test_secure_cookie_flag_by_default(settings, engine):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from raincli_server.app import create_app

    app = create_app(replace(settings, cookie_secure=True))
    with TestClient(app, base_url="https://testserver") as c:
        page = c.get("/login")
        assert "secure" in page.headers["set-cookie"].lower()


def test_wrong_password_is_generic(client, world):
    r = login(client, password="wrong password here")
    assert r.status_code == 400
    unknown = login(client, email="nobody@example.test", password="wrong password here")
    assert unknown.status_code == 400
    msg = "That email and password combination is not correct."
    assert msg in r.text and msg in unknown.text
    assert "raincli_session" not in r.headers.get("set-cookie", "")


def test_login_rate_limited_per_email_and_ip_pair(client, world, app):
    for _ in range(6):
        assert login(client, password="wrong password here").status_code == 400
    r = login(client)  # even the right password is refused from the attacking IP
    assert r.status_code == 429 and "Too many sign-in attempts" in r.text
    assert login(client, email="bob@example.test").status_code == 303  # other emails unaffected


def test_single_attacker_ip_cannot_lock_out_user(client, world, app):
    """Review LOW-3: failures from one IP must not lock the user out from their own IP."""
    from fastapi.testclient import TestClient

    for _ in range(10):
        login(client, password="wrong password here")
    assert login(client).status_code == 429
    with TestClient(app, client=("203.0.113.7", 40000)) as alice_elsewhere:
        assert login(alice_elsewhere).status_code == 303


def test_global_per_email_ceiling(client, world, app):
    from fastapi.testclient import TestClient

    limiter = app.state.web_login_limiter
    for i in range(limiter.per_email):
        limiter.failure(f"198.51.100.{i % 250}", "alice@example.test")
    with TestClient(app, client=("203.0.113.8", 40000)) as fresh:
        r = login(fresh)
        assert r.status_code == 429 and "Too many sign-in attempts" in r.text
        assert "alice" not in r.text.lower().replace("alice@example.test", "")  # generic text


def test_login_rate_limited_per_ip(client, world, app):
    app.state.web_login_limiter.per_ip = 3
    for i in range(3):
        login(client, email=f"user{i}@example.test", password="wrong password here")
    assert login(client, email="bob@example.test").status_code == 429


# CSRF ---------------------------------------------------------------------------

def test_csrf_missing_or_wrong_is_403(client, world, session):
    page = client.get("/login")
    r = client.post("/login", data={"email": "alice@example.test", "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 403
    r = client.post("/login", data={"email": "alice@example.test", "password": PASSWORD, "csrf_token": "x" * 64},
                    follow_redirects=False)
    assert r.status_code == 403
    assert login(client).status_code == 303
    good = app_csrf(client)
    assert good != csrf_of(page.text)
    before = session.scalar(select(func.count()).select_from(User))
    for data in ({"team": "acme", "handle": "sneaky"}, {"team": "acme", "handle": "sneaky", "csrf_token": "nope"}):
        assert client.post("/app/agents", data=data).status_code == 403
    assert client.post("/logout", data={}).status_code == 403
    assert identity.find_agent(session, world["teams"]["acme"], "sneaky") is None
    assert session.scalar(select(func.count()).select_from(User)) == before


def test_csrf_token_is_per_session(client, world, app):
    from fastapi.testclient import TestClient

    login(client)
    alice_csrf = app_csrf(client)
    with TestClient(app) as other:
        login(other, email="bob@example.test")
        r = other.post("/app/agents", data={"team": "acme", "handle": "bob-two", "csrf_token": alice_csrf})
        assert r.status_code == 403


# Invitations ----------------------------------------------------------------------

def test_invite_accept_creates_account_and_is_single_use(client, world, session):
    login(client)
    team_page = client.get("/app/teams/acme")
    r = client.post("/app/teams/acme/invitations", data={"csrf_token": csrf_of(team_page.text), "email": "",
                                                         "role": "member"})
    assert r.status_code == 200
    link = re.search(r'value="(http://testserver/invite/(rci_[^"]+))"', r.text)
    assert link, "invite link shown once"
    token = link.group(2)
    assert session.scalar(select(Invitation.token_hash)) == security.hash_token(token)
    assert token not in client.get("/app/teams/acme").text  # not shown again

    from fastapi.testclient import TestClient

    with TestClient(client.app) as carol:
        page = carol.get(f"/invite/{token}")
        assert page.status_code == 200 and "Join Acme" in page.text
        mismatch = carol.post(f"/invite/{token}", data={
            "csrf_token": csrf_of(page.text), "display_name": "Carol", "email": "carol@example.test",
            "password": "a long enough pw", "password_confirm": "different pw!!",
        })
        assert mismatch.status_code == 400
        ok = carol.post(f"/invite/{token}", data={
            "csrf_token": csrf_of(page.text), "display_name": "Carol", "email": "carol@example.test",
            "password": "a long enough pw", "password_confirm": "a long enough pw",
        }, follow_redirects=False)
        assert ok.status_code == 303 and ok.headers["location"] == "/app?notice=joined"
        assert carol.get("/app/teams/acme").status_code == 200
        assert "Carol" in carol.get("/app").text

    carol_user = identity.find_user_by_email(session, "carol@example.test")
    assert identity.membership(session, world["teams"]["acme"].id, carol_user.id).role == "member"

    with TestClient(client.app) as mallory:
        again = mallory.get(f"/invite/{token}")
        assert again.status_code == 404 and "can't be used" in again.text
        reuse = mallory.post(f"/invite/{token}", data={
            "csrf_token": "x", "display_name": "M", "email": "m@example.test",
            "password": "a long enough pw", "password_confirm": "a long enough pw",
        })
        assert reuse.status_code == 403  # no valid pre-session CSRF
        page = mallory.get("/login")
        reuse = mallory.post(f"/invite/{token}", data={
            "csrf_token": csrf_of(page.text), "display_name": "M", "email": "m@example.test",
            "password": "a long enough pw", "password_confirm": "a long enough pw",
        })
        assert reuse.status_code == 404
    assert identity.find_user_by_email(session, "m@example.test") is None


def test_invite_accept_as_logged_in_user_and_revoke(client, world, session):
    inv, token = identity.create_invitation(session, world["teams"]["acme"], world["users"]["alice"])
    session.commit()
    login(client, email="eve@example.test")
    page = client.get(f"/invite/{token}")
    assert "signed in as" in page.text
    r = client.post(f"/invite/{token}", data={"csrf_token": csrf_of(page.text)}, follow_redirects=False)
    assert r.status_code == 303
    assert identity.membership(session, world["teams"]["acme"].id, world["users"]["eve"].id) is not None

    _, token2 = identity.create_invitation(session, world["teams"]["acme"], world["users"]["alice"])
    session.commit()
    inv2 = session.scalar(select(Invitation).where(Invitation.token_hash == security.hash_token(token2)))
    # Eve is now a member, not an owner: she can neither revoke nor create invitations.
    r = client.post(f"/app/teams/acme/invitations/{inv2.id}/revoke", data={"csrf_token": app_csrf(client)})
    assert r.status_code == 404
    assert client.post("/app/teams/acme/invitations", data={"csrf_token": app_csrf(client)}).status_code == 403


def test_owner_revokes_invitation(client, world, session):
    _, token = identity.create_invitation(session, world["teams"]["acme"], world["users"]["alice"])
    session.commit()
    inv = session.scalar(select(Invitation))
    login(client)
    r = client.post(f"/app/teams/acme/invitations/{inv.id}/revoke", data={"csrf_token": app_csrf(client)},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.get(f"/invite/{token}").status_code == 404


# Agents ------------------------------------------------------------------------

def test_register_agent_shows_token_once_with_config_download(client, world, session, settings):
    login(client)
    r = client.post("/app/agents", data={"csrf_token": app_csrf(client), "team": "acme", "handle": "alice-two"})
    assert r.status_code == 200
    token = re.search(r'id="token"[^>]*value="(rca_[A-Za-z0-9_-]{43})"', r.text).group(1)
    assert "raincli config init --api-url http://testserver --token-file -" in r.text
    # Matches SETUP.md and the skill; the long first line uses a shell continuation so it never
    # overflows its code block, and pasting it still runs exactly one command.
    setup = ("raincli config init --api-url http://testserver \\\n"
             "  --token-file ~/Downloads/raincli-alice-two.json\n"
             "raincli whoami\n# After verifying the expected handle and team:\n"
             "rm ~/Downloads/raincli-alice-two.json")
    assert setup in r.text
    prompt = re.search(r'<textarea id="setup-prompt"[^>]*>(.*?)</textarea>', r.text, re.S).group(1)
    assert token not in prompt and "rca_" not in prompt
    assert "~/Downloads/raincli-alice-two.json" in prompt and "in team acme" in prompt
    assert "preserve them" in prompt and "CLI-only messaging" in prompt
    assert "data-download" in r.text  # download submissions must remain retryable
    assert "install -m 600" not in r.text and "mkdir -p" not in r.text
    assert f"--token {token}" not in r.text and "--token rca_" not in r.text  # never in a command line
    assert r.headers["cache-control"] == "no-store"
    agent = identity.find_agent(session, world["teams"]["acme"], "alice-two")
    assert identity.authenticate_agent(session, token, touch=False).agent.id == agent.id

    later = client.get("/app/agents")
    assert "alice-two" in later.text and token not in later.text and "Never connected" in later.text

    dl = client.post(f"/app/agents/{agent.id}/config", data={"csrf_token": csrf_of(r.text), "token": token})
    assert dl.status_code == 200
    assert "attachment" in dl.headers["content-disposition"] and "raincli-alice-two.json" in dl.headers["content-disposition"]
    assert json.loads(dl.content) == {"api_url": "http://testserver", "token": token}
    # The download only re-serves a token that belongs to that agent.
    bad = client.post(f"/app/agents/{agent.id}/config", data={"csrf_token": csrf_of(r.text),
                                                               "token": world["tokens"]["alice"]})
    assert bad.status_code == 404

    dup = client.post("/app/agents", data={"csrf_token": app_csrf(client), "team": "acme", "handle": "alice-two"})
    assert dup.status_code == 400 and "already registered" in dup.text
    assert client.post("/app/agents", data={"csrf_token": app_csrf(client), "team": "globex",
                                            "handle": "sneak"}).status_code == 404


def test_rotate_and_revoke(client, world, session):
    login(client)
    agent = world["agents"]["alice"]
    old = world["tokens"]["alice"]
    r = client.post(f"/app/agents/{agent.id}/rotate", data={"csrf_token": app_csrf(client)})
    assert r.status_code == 200 and "New credential" in r.text
    new = re.search(r'id="token"[^>]*value="(rca_[^"]+)"', r.text).group(1)
    session.expire_all()
    assert identity.authenticate_agent(session, old) is None
    assert identity.authenticate_agent(session, new).agent.id == agent.id
    assert new not in client.get("/app/agents").text

    r = client.post(f"/app/agents/{agent.id}/revoke", data={"csrf_token": app_csrf(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/agents?notice=revoked"
    session.expire_all()
    assert identity.authenticate_agent(session, new) is None
    assert "Revoked" in client.get("/app/agents").text


def test_connection_state_from_last_used(client, world, session):
    identity.authenticate_agent(session, world["tokens"]["alice"])  # touches last_used_at
    session.commit()
    login(client)
    assert "Connected recently" in client.get("/app/agents").text
    from raincli_server.models import AgentCredential

    for cred in session.scalars(select(AgentCredential)):
        cred.last_used_at = identity.now() - timedelta(hours=2)
    session.commit()
    assert "Idle" in client.get("/app/agents").text


def _availability_cell(html: str, handle: str) -> str:
    row = re.search(rf'<span class="handle">{handle}</span>.*?</tr>', html, re.S)
    assert row, f"no agents row for {handle}"
    cell = re.search(r'<td data-label="Session availability">(.*?)</td>', row.group(0), re.S)
    assert cell, f"no availability cell for {handle}"
    return cell.group(1)


def test_session_availability_column_expires_and_stays_separate_from_delivery(client, world, session, app):
    from fastapi.testclient import TestClient

    from raincli_server import presence
    from raincli_server.models import AgentPresence

    login(client)
    page = client.get("/app/agents").text
    assert "Session availability" in page and "does not show that a message was delivered or read" in page
    assert ">Unknown<" in _availability_cell(page, "alice-agent")  # never reported

    presence.publish(session, world["agents"]["alice"], {"status": "busy"})
    presence.publish(session, world["agents"]["bob"], {"status": "ready"})
    session.commit()
    page = client.get("/app/agents").text
    cell = _availability_cell(page, "alice-agent")
    assert ">Busy<" in cell and "presence-busy" in cell
    # Every machine in the team is listed, the viewer's own first (protocol §14.9); other teams never.
    assert ">Ready<" in _availability_cell(page, "bob-agent")
    assert page.index("alice-agent") < page.index("bob-agent") and "eve-agent" not in page
    row = session.get(AgentPresence, world["agents"]["alice"].id)
    assert row.seen_at.isoformat() not in page  # no report timestamps or runtime details

    row.seen_at -= timedelta(seconds=presence.TTL_SECONDS)
    session.commit()
    assert ">Offline<" in _availability_cell(client.get("/app/agents").text, "alice-agent")

    presence.publish(session, world["agents"]["alice"], {"status": "ready"})
    identity.revoke_agent(session, world["agents"]["alice"])
    session.commit()
    assert ">Offline<" in _availability_cell(client.get("/app/agents").text, "alice-agent")

    with TestClient(app) as bob:
        login(bob, email="bob@example.test")
        page = bob.get("/app/agents").text
        assert ">Ready<" in _availability_cell(page, "bob-agent")
        assert page.index('<span class="handle">bob-agent') < page.index('<span class="handle">alice-agent')
        assert "eve-agent" not in page


def test_member_cannot_manage_others_agents_but_owner_can_revoke(client, world, session, app):
    from fastapi.testclient import TestClient

    alice_agent, bob_agent = world["agents"]["alice"], world["agents"]["bob"]
    login(client, email="bob@example.test")
    csrf = app_csrf(client)
    assert client.post(f"/app/agents/{alice_agent.id}/rotate", data={"csrf_token": csrf}).status_code == 404
    assert client.post(f"/app/agents/{alice_agent.id}/revoke", data={"csrf_token": csrf}).status_code == 404
    assert client.post(f"/app/teams/acme/agents/{alice_agent.id}/revoke", data={"csrf_token": csrf}).status_code == 403
    with TestClient(app) as owner:
        login(owner)
        r = owner.post(f"/app/teams/acme/agents/{bob_agent.id}/revoke", data={"csrf_token": app_csrf(owner)},
                       follow_redirects=False)
        assert r.status_code == 303
    session.expire_all()
    assert identity.authenticate_agent(session, world["tokens"]["bob"]) is None


# Cross-team isolation ------------------------------------------------------------

def test_cross_team_ids_are_404(client, world, session):
    msg = add_message(session, world["agents"]["alice"], world["agents"]["bob"], body="acme only")
    login(client, email="eve@example.test")
    csrf = app_csrf(client)
    assert client.get(f"/app/conversations/{msg.conversation_id}").status_code == 404
    assert client.get("/app/teams/acme").status_code == 404
    for aid in (world["agents"]["alice"].id, world["agents"]["bob"].id):
        assert client.post(f"/app/agents/{aid}/rotate", data={"csrf_token": csrf}).status_code == 404
        assert client.post(f"/app/agents/{aid}/revoke", data={"csrf_token": csrf}).status_code == 404
        assert client.post(f"/app/teams/globex/agents/{aid}/revoke", data={"csrf_token": csrf}).status_code == 404
    assert client.get(f"/app/conversations/{uuid.uuid4()}").status_code == 404
    assert client.get("/app/conversations/not-a-uuid").status_code == 404
    inbox = client.get("/app").text
    assert "acme only" not in inbox and "alice-agent" not in inbox
    team = client.get("/app/teams/globex").text
    assert "alice" not in team.lower()
    session.expire_all()
    assert identity.authenticate_agent(session, world["tokens"]["alice"]) is not None


def test_same_team_member_cannot_read_others_conversation(client, world, session):
    identity.add_member(session, world["teams"]["acme"],
                        identity.create_user(session, "carol@example.test", "Carol", PASSWORD))
    session.commit()
    msg = add_message(session, world["agents"]["alice"], world["agents"]["bob"])
    login(client, email="carol@example.test")
    assert client.get(f"/app/conversations/{msg.conversation_id}").status_code == 404


# Inbox, conversation and compose -------------------------------------------------------

def test_inbox_and_conversation_show_delivery_state(client, world, session):
    from raincli_server.models import DeliveryEvent

    msg = add_message(session, world["agents"]["bob"], world["agents"]["alice"], body="Can you <b>review</b>?")
    m = session.get(Message, msg.id)
    m.acked_at = identity.now()
    m.delivery_state = "held"
    session.add(DeliveryEvent(message_id=m.id, state="held", detail="approval_required",
                              reported_by=world["agents"]["alice"].id))
    session.commit()
    login(client)
    inbox = client.get("/app")
    assert "bob-agent" in inbox.text and "Held" in inbox.text
    conv = client.get(f"/app/conversations/{msg.conversation_id}")
    assert conv.status_code == 200
    assert "Can you &lt;b&gt;review&lt;/b&gt;?" in conv.text and "<b>review</b>" not in conv.text
    assert "approval_required" in conv.text and "state-held" in conv.text and "UTC" in conv.text
    assert "Acme · 1 message" in conv.text and "Team Acme" not in conv.text


def test_compose_as_the_person_and_reply(client, world, session):
    """§16.9: the website sends as the person; there is no "send as one of your agents" choice."""
    login(client)
    page = client.get("/app/compose")
    assert 'name="from_agent"' not in page.text and "Sent as <strong>you (Alice)</strong>" in page.text
    mid = str(uuid.uuid4())
    data = {"csrf_token": csrf_of(page.text), "message_id": mid, "to": "bob-agent", "body": "Hi Bob\r\nline two"}
    r = client.post("/app/conversations/new/send", data=data, follow_redirects=False)
    assert r.status_code == 303 and "notice=sent" in r.headers["location"]
    stored = session.get(Message, uuid.UUID(mid))
    assert stored.sender_user_id == world["users"]["alice"].id and stored.sender_agent_id is None
    assert stored.recipient_agent_id == world["agents"]["bob"].id and stored.body == "Hi Bob\nline two"
    assert stored.delivery_state == "stored"
    # Resubmitting the same form is idempotent; a stray from_agent field is ignored.
    again = client.post("/app/conversations/new/send", data={**data, "from_agent": str(world["agents"]["bob"].id)},
                        follow_redirects=False)
    assert "notice=duplicate" in again.headers["location"]
    assert session.scalar(select(func.count()).select_from(Message)) == 1

    # Bob sees it as a message to his machine and replies as himself; the reply goes to Alice.
    from fastapi.testclient import TestClient

    with TestClient(client.app) as bob:
        login(bob, email="bob@example.test")
        conv = bob.get(f"/app/conversations/{stored.conversation_id}?reply_to={mid}")
        assert "Reply to @alice@example.test" in conv.text
        r = bob.post(f"/app/conversations/{stored.conversation_id}/send", data={
            "csrf_token": csrf_of(conv.text), "message_id": str(uuid.uuid4()), "body": "On it",
            "in_reply_to": mid}, follow_redirects=False)
        assert r.status_code == 303
    session.expire_all()
    assert session.get(Message, uuid.UUID(mid)).delivery_state == "replied"
    reply = session.scalar(select(Message).where(Message.in_reply_to == uuid.UUID(mid)))
    assert reply.sender_user_id == world["users"]["bob"].id and reply.recipient_user_id == world["users"]["alice"].id
    # Alice reads the reply; viewing it acks it.
    page = client.get(f"/app/conversations/{reply.conversation_id}").text
    assert "On it" in page
    session.expire_all()
    assert session.get(Message, reply.id).acked_at is not None


def test_compose_endpoints_and_cross_team_recipients(client, world, session):
    login(client)
    csrf = csrf_of(client.get("/app/compose").text)
    sent = {}
    for to in ("bob-agent", "@bob@example.test"):
        r = client.post("/app/conversations/new/send", data={
            "csrf_token": csrf, "message_id": str(uuid.uuid4()), "to": to, "body": "hi"}, follow_redirects=False)
        assert r.status_code == 303, to
        sent[to] = r
    # Cross-team and unknown recipients are rejected without revealing whether they exist.
    for to in ("eve-agent", "@eve@example.test", "nobody", "@nobody@example.test", "bob-agent/ghost"):
        r = client.post("/app/conversations/new/send", data={
            "csrf_token": csrf, "message_id": str(uuid.uuid4()), "to": to, "body": "hi"})
        assert r.status_code == 400, to
    assert session.scalar(select(func.count()).select_from(Message)) == 2


def test_compose_capacity_and_invalid_body(client, world, session, settings):
    for i in range(settings.max_pending):
        add_message(session, world["agents"]["bob"], world["agents"]["alice"], body=f"m{i}")
    login(client, email="bob@example.test")
    csrf = csrf_of(client.get("/app/compose").text)
    base = {"csrf_token": csrf, "from_agent": str(world["agents"]["bob"].id), "to": "alice-agent"}
    r = client.post("/app/conversations/new/send", data={**base, "message_id": str(uuid.uuid4()), "body": "more"})
    assert r.status_code == 429 and "inbox is full" in r.text
    r = client.post("/app/conversations/new/send", data={**base, "message_id": str(uuid.uuid4()), "body": "  "})
    assert r.status_code == 400


# Attachments (§8) ---------------------------------------------------------------------

def _send_with_files(client, conversation, sender, to, files, body="see attached"):
    page = client.get("/app/compose" if conversation == "new" else f"/app/conversations/{conversation}")
    mid = str(uuid.uuid4())
    r = client.post(f"/app/conversations/{conversation}/send", data={
        "csrf_token": csrf_of(page.text), "message_id": mid, "from_agent": str(sender.id), "to": to, "body": body,
    }, files=files, follow_redirects=False)
    return r, mid


def test_compose_upload_and_download(client, world, session):
    login(client)
    content = "# Plan\r\n\nünïcode  \n".encode()
    r, mid = _send_with_files(client, "new", world["agents"]["alice"], "bob-agent",
                              [("files", ("plan.md", content, "text/markdown")),
                               ("files", ("notes.md", b"two", "text/markdown"))])
    assert r.status_code == 303
    atts = session.scalars(select(Attachment).where(Attachment.message_id == uuid.UUID(mid))
                           .order_by(Attachment.position)).all()
    assert [a.filename for a in atts] == ["plan.md", "notes.md"]
    assert atts[0].content == content and atts[0].sha256 == security.sha256_hex(content)

    conv_id = session.get(Message, uuid.UUID(mid)).conversation_id
    conv = client.get(f"/app/conversations/{conv_id}").text
    href = f"/app/messages/{mid}/attachments/{atts[0].id}"
    assert href in conv and atts[0].sha256[:12] in conv and "# Plan" not in conv  # never inline

    dl = client.get(href)
    assert dl.status_code == 200 and dl.content == content
    h = dl.headers
    assert h["content-type"] == "text/markdown; charset=utf-8"
    assert h["content-disposition"] == 'attachment; filename="plan.md"'
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-raincli-sha256"] == atts[0].sha256
    assert h["cache-control"] == "no-store" and h["content-length"] == str(len(content))
    assert "sandbox" in h["content-security-policy"]

    # Reply in the existing conversation with an attachment.
    r, _ = _send_with_files(client, conv_id, world["agents"]["alice"], "bob-agent",
                            [("files", ("more.md", b"# more", "text/markdown"))])
    assert r.status_code == 303


def test_attachment_download_authorization(client, world, session, app):
    from fastapi.testclient import TestClient

    login(client)
    r, mid = _send_with_files(client, "new", world["agents"]["alice"], "bob-agent",
                              [("files", ("a.md", b"# a", "text/markdown"))])
    other_r, other_mid = _send_with_files(client, "new", world["agents"]["alice"], "bob-agent",
                                          [("files", ("b.md", b"# b", "text/markdown"))])
    att = session.scalar(select(Attachment).where(Attachment.message_id == uuid.UUID(mid)))
    href = f"/app/messages/{mid}/attachments/{att.id}"
    assert client.get(f"/app/messages/{other_mid}/attachments/{att.id}").status_code == 404  # wrong message
    with TestClient(app) as bob:  # recipient's owner may download
        login(bob, email="bob@example.test")
        assert bob.get(href).status_code == 200
    identity.add_member(session, world["teams"]["acme"],
                        identity.create_user(session, "carol@example.test", "Carol", PASSWORD))
    session.commit()
    for email in ("carol@example.test", "eve@example.test"):  # same-team non-participant, other team
        with TestClient(app) as outsider:
            login(outsider, email=email)
            assert outsider.get(href).status_code == 404
    with TestClient(app) as anon:
        assert anon.get(href, follow_redirects=False).status_code == 303


@pytest.mark.parametrize("name", ["../x.md", "a/b.md", ".x.md", "notes.txt", "con.md"])
def test_compose_rejects_bad_attachment_name(client, world, session, name):
    login(client)
    r, _ = _send_with_files(client, "new", world["agents"]["alice"], "bob-agent",
                            [("files", (name, b"# x", "text/markdown"))])
    assert r.status_code == 400 and "filename must be a plain .md name" in r.text
    assert session.scalar(select(func.count()).select_from(Message)) == 0
    assert session.scalar(select(func.count()).select_from(Attachment)) == 0


def test_compose_rejects_bad_attachment_sets(client, world, session):
    login(client)
    alice = world["agents"]["alice"]
    cases = [
        [("files", (f"f{i}.md", b"# x", "text/markdown")) for i in range(6)],
        [("files", ("A.md", b"1", "text/markdown")), ("files", ("a.md", b"2", "text/markdown"))],
        [("files", ("ok.md", b"# ok", "text/markdown")), ("files", ("bin.md", b"\x00\x01", "text/markdown"))],
        [("files", ("big.md", b"x" * (256 * 1024 + 1), "text/markdown"))],
    ]
    for files in cases:
        r, _ = _send_with_files(client, "new", alice, "bob-agent", files)
        assert r.status_code == 400, files[0][1][0]
    assert session.scalar(select(func.count()).select_from(Message)) == 0
    assert session.scalar(select(func.count()).select_from(Attachment)) == 0


def test_upload_size_limit_is_path_specific(client, world):
    login(client)
    big = b"x" * (3 * 1024 * 1024)
    r = client.post("/app/conversations/new/send", content=big,
                    headers={"content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413
    r = client.post("/app/agents", content=b"x" * (100 * 1024),
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


# Root path ---------------------------------------------------------------------------

@pytest.fixture
def rooted(settings, engine):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from raincli_server.app import create_app

    app = create_app(replace(settings, root_path="/raincli"))
    with TestClient(app, root_path="/raincli") as c:
        yield c


def test_root_path_prefixes_links_redirects_and_cookie(rooted, world, session):
    home = rooted.get("/raincli/")
    assert home.status_code == 200
    assert 'href="/raincli/login"' in home.text and 'href="/raincli/static/app.css?v=' in home.text
    assert 'src="/raincli/static/app.js?v=' in home.text
    assert rooted.get("/raincli/static/app.css").status_code == 200
    r = rooted.get("/raincli/app/agents", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/raincli/login?next=/app/agents"

    r = login(rooted, root="/raincli")
    assert r.status_code == 303 and r.headers["location"] == "/raincli/app"
    cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("raincli_session="))
    assert "Path=/raincli" in cookie
    inbox = rooted.get("/raincli/app")
    assert 'action="/raincli/logout"' in inbox.text and 'href="/raincli/app/agents"' in inbox.text
    reg = rooted.post("/raincli/app/agents", data={"csrf_token": app_csrf(rooted, "/raincli"), "team": "acme",
                                                   "handle": "rooted-agent"})
    assert 'action="/raincli/app/agents/' in reg.text
    assert "--api-url http://testserver/raincli " in reg.text
    team = rooted.post("/raincli/app/teams/acme/invitations", data={"csrf_token": app_csrf(rooted, "/raincli")})
    assert "http://testserver/raincli/invite/rci_" in team.text


# Inbox-agent recommendation (change notice 2, protocol §10) ------------------------------

def _inbox_block(html: str) -> str:
    import html as html_mod

    start = html.index('id="inbox-agent"')
    return html_mod.unescape(html[start:html.index("</section>", start)])


def test_inbox_agent_block_on_agents_and_token_pages_without_tokens(client, world):
    login(client)
    agents = client.get("/app/agents").text
    block = _inbox_block(agents)
    for needle in ('A dedicated inbox agent', '"mode": "inbox"', '"trust_mode": "team"', '"shareable_context"',
                   '"escalation"', "docs/raincli-inbox-agent.md", "Alternative: direct delivery", '"mode": "direct"'):
        assert needle in block, needle
    assert "rca_" not in block and "token" not in block.lower()
    assert '<a href="docs/' not in block  # the docs are not served; referenced as plain text

    r = client.post("/app/agents", data={"csrf_token": app_csrf(client), "team": "acme", "handle": "alice-two"})
    token = re.search(r'id="token"[^>]*value="(rca_[^"]+)"', r.text).group(1)
    block = _inbox_block(r.text)
    assert '"herdr_agent": "alice-two-inbox"' in block and "connector-alice-two.json" in block
    assert token not in block and "rca_" not in block


def test_public_page_mentions_inbox_agent_and_direct_alternative(client):
    html = client.get("/").text
    assert "Recommended: a dedicated inbox agent" in html and "direct mode" in html


# Fix round 1: member removal (MED-4) and invitation secrecy (MED-3) ------------------------

@pytest.fixture
def remove_member_impl(monkeypatch):
    """Use identity.remove_member once it exists; until then a stub with the §11.5 signature."""
    if hasattr(identity, "remove_member"):
        yield identity.remove_member
        return
    from sqlalchemy import update

    from raincli_server.models import Agent

    def remove_member(session, team, user, actor=None):
        if actor is not None:
            m = identity.membership(session, team.id, actor.id)
            if m is None or m.role != "owner":
                raise identity.PermissionDenied("only team owners can remove members")
        target = identity.membership(session, team.id, user.id)
        if target is None:
            raise identity.IdentityError("that user is not a member of this team")
        if target.role == "owner":
            owners = session.scalar(select(func.count()).select_from(Membership).where(
                Membership.team_id == team.id, Membership.role == "owner"))
            if owners <= 1:
                raise identity.IdentityError("cannot remove the last owner of a team")
        session.delete(target)
        for agent in session.scalars(select(Agent).where(Agent.team_id == team.id, Agent.owner_user_id == user.id)):
            identity.revoke_agent(session, agent)
        session.execute(update(WebSession).where(WebSession.user_id == user.id, WebSession.revoked_at.is_(None))
                        .values(revoked_at=identity.now()))
        session.flush()

    monkeypatch.setattr(identity, "remove_member", remove_member, raising=False)
    yield remove_member


def test_owner_removes_member_with_confirm_step(client, world, session, app, remove_member_impl):
    from fastapi.testclient import TestClient

    bob, acme = world["users"]["bob"], world["teams"]["acme"]
    with TestClient(app) as bob_client:
        login(bob_client, email="bob@example.test")
        assert bob_client.get("/app").status_code == 200

        login(client)
        team = client.get("/app/teams/acme").text
        confirm_href = f"/app/teams/acme/members/{bob.id}/remove"
        assert confirm_href in team
        confirm = client.get(confirm_href)
        assert confirm.status_code == 200 and "bob-agent" in confirm.text and "Remove member" in confirm.text
        assert identity.membership(session, acme.id, bob.id) is not None  # GET changes nothing

        assert client.post(confirm_href, data={}).status_code == 403  # CSRF required
        assert client.post(confirm_href, data={"csrf_token": "forged"}).status_code == 403
        r = client.post(confirm_href, data={"csrf_token": csrf_of(confirm.text)}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/app/teams/acme?notice=member-removed"

        session.expire_all()
        assert identity.membership(session, acme.id, bob.id) is None
        assert identity.authenticate_agent(session, world["tokens"]["bob"]) is None
        # The removed member's existing browser session no longer works.
        assert bob_client.get("/app", follow_redirects=False).status_code == 303
        assert bob_client.get("/app/teams/acme", follow_redirects=False).status_code == 303


def test_member_cannot_remove_and_last_owner_refused(client, world, session, remove_member_impl):
    alice, bob = world["users"]["alice"], world["users"]["bob"]
    login(client, email="bob@example.test")
    href = f"/app/teams/acme/members/{alice.id}/remove"
    assert "/members/" not in client.get("/app/teams/acme").text
    assert client.get(href).status_code == 403
    assert client.post(href, data={"csrf_token": app_csrf(client)}).status_code == 403
    assert client.get(f"/app/teams/globex/members/{world['users']['eve'].id}/remove").status_code == 404

    client.post("/logout", data={"csrf_token": app_csrf(client)})
    login(client)
    r = client.post(f"/app/teams/acme/members/{alice.id}/remove", data={"csrf_token": app_csrf(client)})
    assert r.status_code == 400 and "last owner" in r.text.lower()
    assert identity.membership(session, world["teams"]["acme"].id, alice.id) is not None
    # Unknown or cross-team user ids are 404.
    assert client.get(f"/app/teams/acme/members/{world['users']['eve'].id}/remove").status_code == 404
    assert client.get(f"/app/teams/acme/members/{uuid.uuid4()}/remove").status_code == 404
    assert bob  # bob is still a member
    assert identity.membership(session, world["teams"]["acme"].id, bob.id) is not None


def test_invite_page_headers_and_no_token_in_logs(client, world, session, caplog):
    import logging

    _, token = identity.create_invitation(session, world["teams"]["acme"], world["users"]["alice"])
    session.commit()
    caplog.set_level(logging.DEBUG)
    page = client.get(f"/invite/{token}")
    assert page.status_code == 200
    for r in (page, client.get("/invite/rci_doesnotexist"),
              client.post(f"/invite/{token}", data={"csrf_token": csrf_of(page.text), "display_name": "C",
                                                    "email": "c@example.test", "password": "short",
                                                    "password_confirm": "short"})):
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["cache-control"] == "no-store"
    # The test client's own request log (httpx) is not the application; everything else must be clean.
    app_logs = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore")))
    assert token not in app_logs and "rci_" not in app_logs
    assert client.get("/login").headers["referrer-policy"] == "same-origin"


# Cache busting (visual polish brief) -----------------------------------------------------------

def test_static_urls_carry_current_content_hash(client, world):
    import hashlib
    from pathlib import Path

    import raincli_server.web as web

    static_dir = Path(web.__file__).parent / "static"
    pages = [client.get("/").text, client.get("/login").text]
    login(client)
    pages.append(client.get("/app").text)
    for name in ("app.css", "app.js", "favicon.svg"):
        digest = hashlib.sha256((static_dir / name).read_bytes()).hexdigest()[:12]
        for html in pages:
            assert f"/static/{name}?v={digest}" in html, name
            assert f'/static/{name}"' not in html  # never an unversioned URL


def test_static_responses_are_immutable_and_html_is_not_cached(client):
    css = client.get("/static/app.css?v=anything")
    assert css.status_code == 200
    assert css.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert css.headers["x-content-type-options"] == "nosniff"
    assert client.get("/static/app.js").headers["cache-control"] == "public, max-age=31536000, immutable"
    missing = client.get("/static/does-not-exist.css")
    assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"
    assert client.get("/").headers["cache-control"] == "no-store"
    assert client.get("/login").headers["cache-control"] == "no-store"


def test_templates_have_no_inline_script_or_style(client, world):
    login(client)
    for path in ("/", "/app", "/app/agents", "/app/teams/acme", "/app/compose"):
        html = client.get(path).text
        assert "<style" not in html and " style=" not in html
        assert re.search(r"<script(?![^>]*\bsrc=)", html) is None
        assert re.search(r"\son[a-z]+=", html) is None


# Stale-cache safeguards (post-cutover) ----------------------------------------------------------

@pytest.mark.parametrize("path", ["/sw.js", "/service-worker.js", "/serviceworker.js", "/sw.min.js"])
def test_legacy_service_worker_kill_switch(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["clear-site-data"] == '"cache", "storage"'
    body = r.text
    for needle in ("skipWaiting()", "caches.keys()", "caches.delete", "self.registration.unregister()",
                   'matchAll({ type: "window" })', "client.navigate(client.url)"):
        assert needle in body, needle
    assert "http" not in body and "importScripts" not in body  # same-origin only, loads nothing
    assert "set-cookie" not in r.headers


def test_one_time_cache_purge_cookie(client, world):
    first = client.get("/login")
    assert first.headers["clear-site-data"] == '"cache"'
    cookies = first.headers.get_list("set-cookie")
    cv = next(c for c in cookies if c.startswith("raincli_cv=1"))
    lower = cv.lower()
    assert "httponly" in lower and "samesite=lax" in lower and "path=/" in lower and "max-age=31536000" in lower
    assert "secure" not in lower  # RAINCLI_COOKIE_SECURE=0 in tests
    assert any(c.startswith("raincli_csrf=") for c in cookies)  # pre-login CSRF cookie untouched
    # Never repeated once the marker cookie is present, on public or app pages.
    again = client.get("/login")
    assert "clear-site-data" not in again.headers
    assert not any(c.startswith("raincli_cv=") for c in again.headers.get_list("set-cookie"))
    assert login(client).status_code == 303
    app_page = client.get("/app")
    assert app_page.status_code == 200 and "clear-site-data" not in app_page.headers
    # Only HTML GETs purge: static files and the service worker script keep their own headers.
    with TestClient_fresh(client) as fresh:
        assert "clear-site-data" not in fresh.get("/static/app.css").headers
        assert "raincli_cv" not in fresh.get("/static/app.css").headers.get("set-cookie", "")


def TestClient_fresh(client):
    from fastapi.testclient import TestClient

    return TestClient(client.app)


def test_cache_purge_does_not_disturb_session(client, world, session):
    assert login(client).status_code == 303
    client.cookies.delete("raincli_cv")  # e.g. a browser that predates the marker
    r = client.get("/app")
    assert r.status_code == 200 and r.headers["clear-site-data"] == '"cache"'
    assert not any(c.startswith(("raincli_session=", "raincli_csrf=")) for c in r.headers.get_list("set-cookie"))
    assert client.get("/app/agents").status_code == 200  # still signed in


def test_cache_purge_cookie_secure_and_root_path(settings, engine):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from raincli_server.app import create_app

    app = create_app(replace(settings, cookie_secure=True, root_path="/raincli"))
    with TestClient(app, base_url="https://testserver", root_path="/raincli") as c:
        cv = next(x for x in c.get("/raincli/login").headers.get_list("set-cookie") if x.startswith("raincli_cv="))
        assert "Secure" in cv and "Path=/raincli" in cv


def test_health_is_not_cached(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"


def test_every_web_html_redirect_and_error_response_is_no_store(client, world, session, app):
    from fastapi.testclient import TestClient

    _, token = identity.create_invitation(session, world["teams"]["acme"], world["users"]["alice"])
    session.commit()
    msg = add_message(session, world["agents"]["bob"], world["agents"]["alice"])
    seen = []

    def check(r):
        seen.append(r.status_code)
        assert r.headers.get("cache-control") == "no-store", (r.request.method, r.request.url, r.status_code)
        return r

    anon = [
        "/", "/login", "/login?next=/app/agents", f"/invite/{token}", "/invite/rci_nope", "/app", "/app/agents",
        "/app/teams/acme", "/no-such-page", "/sw.js",
    ]
    for path in anon:
        check(client.get(path, follow_redirects=False))
    page = client.get("/login")
    check(client.post("/login", data={"email": "alice@example.test", "password": "bad"}, follow_redirects=False))
    check(client.post("/login", data={"email": "alice@example.test", "password": "wrong password here",
                                      "csrf_token": csrf_of(page.text)}, follow_redirects=False))
    check(client.post("/login", content=b"x" * (70 * 1024),
                      headers={"content-type": "application/x-www-form-urlencoded"}))
    check(login(client))
    csrf = app_csrf(client)
    agent_id = world["agents"]["alice"].id
    for path in ("/", "/login", "/app", "/app/compose", "/app/agents", "/app/team", "/app/teams/acme",
                 f"/app/conversations/{msg.conversation_id}", f"/app/conversations/{uuid.uuid4()}",
                 f"/app/teams/acme/members/{world['users']['bob'].id}/remove", "/app/teams/globex",
                 f"/invite/{token}"):
        check(client.get(path, follow_redirects=False))
    check(client.post("/app/agents", data={"csrf_token": "forged", "team": "acme", "handle": "x-agent"}))
    check(client.post("/app/agents", data={"csrf_token": csrf, "team": "acme", "handle": "Bad Handle"}))
    token_page = check(client.post("/app/agents", data={"csrf_token": csrf, "team": "acme", "handle": "nostore"}))
    check(client.post(f"/app/agents/{identity.find_agent(session, world['teams']['acme'], 'nostore').id}/config",
                      data={"csrf_token": csrf, "token": re.search(r'value="(rca_[^"]+)"', token_page.text).group(1)}))
    check(client.post(f"/app/agents/{agent_id}/rotate", data={"csrf_token": csrf}))
    check(client.post("/app/conversations/new/send", data={
        "csrf_token": csrf, "message_id": str(uuid.uuid4()), "from_agent": str(agent_id), "to": "bob-agent",
        "body": "hi"}, follow_redirects=False))
    check(client.post("/app/teams/acme/invitations", data={"csrf_token": csrf}))
    check(client.post(f"/app/agents/{agent_id}/revoke", data={"csrf_token": csrf}, follow_redirects=False))
    check(client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False))
    assert {200, 303, 400, 403, 404, 413} <= set(seen)


def test_hashed_static_urls_return_immutable(client):
    html = client.get("/").text
    urls = re.findall(r'(?:href|src)="(/static/[^"?]+\?v=[0-9a-f]{12})"', html)
    assert {u.split("?")[0] for u in urls} == {"/static/app.css", "/static/app.js", "/static/favicon.svg",
                                               "/static/tokens.css"}
    for url in urls:
        r = client.get(url)
        assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_self_hosted_fonts_are_small_licensed_and_immutable(client):
    from pathlib import Path

    import raincli_server.web as web

    fonts = Path(web.__file__).parent / "static" / "fonts"
    files = sorted(fonts.glob("*.woff2"))
    assert files and sum(f.stat().st_size for f in files) < 150 * 1024
    assert "SIL Open Font License" in (fonts / "OFL.txt").read_text()
    css = client.get("/static/app.css").text
    for f in files:
        assert f'url("fonts/{f.name}")' in css
        r = client.get(f"/static/fonts/{f.name}")
        assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert r.content[:4] == b"wOF2"
    assert "fonts.googleapis" not in css and "http" not in css  # no external requests


def test_layout_guards_for_code_blocks_and_landing_footer(client):
    """Long commands wrap inside code blocks (never clipped), and on the landing page the footer
    follows the content instead of being pushed down by a tall window."""
    css = client.get("/static/app.css").text
    pre_rule = css[css.index("pre.code {"):css.index("}", css.index("pre.code {"))]
    assert "white-space: pre-wrap" in pre_rule and "overflow-wrap: anywhere" in pre_rule
    assert ".public main { flex: none; }" in css


def _machine_sessions(html: str, handle: str) -> list[str]:
    table = re.search(rf'<table class="table machine-sessions" data-machine="{handle}">(.*?)</table>', html, re.S)
    assert table, f"no agents table for {handle}"
    return re.findall(r"<tr>(.*?)</tr>", table.group(1).split("</thead>", 1)[1], re.S)


def test_machines_page_lists_agents_with_inbox_first_and_no_keys(client, world, session):
    from raincli_server import presence
    from raincli_server.models import MachineAgent

    login(client)
    page = client.get("/app/agents").text
    assert "Your machines" in page and "Teammates' machines" in page
    assert "Add a machine" in page and "Register an agent" not in page
    assert "No agents reported in the last two minutes" in page and "Not reported" in page

    presence.publish(session, world["agents"]["alice"], {
        "status": "ready",
        "client": {"version": "0.3.1", "update_mode": "automatic", "update_state": "rolled_back",
                   "error": "verify_failed"},
        "agents": [
            {"key": "b" * 32, "name": "notes", "type": "codex", "status": "working", "source": "hook"},
            {"key": "a" * 32, "name": "team-inbox", "type": "claude", "status": "idle", "role": "inbox",
             "reachability": "next-turn", "source": "hook"},
            {"key": "c" * 32, "name": "aider", "type": "other", "status": "unknown", "source": "scan"},
        ]})
    presence.publish(session, world["agents"]["bob"], {"status": "ready", "agents": [
        {"key": "d" * 32, "name": "bobs-secret-session", "type": "claude", "status": "idle", "source": "herdr"}]})
    session.commit()
    page = client.get("/app/agents").text
    rows = _machine_sessions(page, "alice-agent")
    assert len(rows) == 3
    # The inbox badge comes first, on the inbox row only, with its reachability.
    assert 'class="badge role-inbox"' in rows[0] and "team-inbox" in rows[0] and ">Next turn<" in rows[0]
    assert "role-inbox" not in rows[1] + rows[2]
    # §16.2: every agent shows its reachability; v0.4 reports leave non-inbox agents listed.
    assert ">Listed<" in rows[1] and ">Listed<" in rows[2] and "reach-instant" not in rows[1] + rows[2]
    assert ">aider<" in rows[1] and ">Unknown<" in rows[1] and ">notes<" in rows[2] and ">Working<" in rows[2]
    # The machine's version and update state appear on each agent.
    assert all("v0.3.1" in r and ">Rolled back<" in r for r in rows)
    assert "a" * 32 not in page and "b" * 32 not in page  # keys are never rendered
    # Bob's machine and its agents are visible to his teammate, after Alice's own.
    assert [r for r in _machine_sessions(page, "bob-agent") if "bobs-secret-session" in r]
    assert page.index('data-machine="alice-agent"') < page.index('data-machine="bob-agent"')

    for row in session.scalars(select(MachineAgent)):
        row.seen_at -= timedelta(seconds=presence.TTL_SECONDS)
    session.commit()
    page = client.get("/app/agents").text
    assert "machine-sessions" not in page and "team-inbox" not in page

    presence.publish(session, world["agents"]["alice"], {"status": "ready", "agents": [
        {"key": "a" * 32, "name": "inbox <b>x", "type": "claude", "status": "idle", "role": "inbox",
         "reachability": "instant", "source": "herdr"}]})
    identity.revoke_agent(session, world["agents"]["alice"])
    session.commit()
    page = client.get("/app/agents").text
    assert "machine-sessions" not in page and "<b>x" not in page and "v0.3.1" not in page


def test_machine_names_are_escaped(client, world, session):
    from raincli_server import presence

    login(client)
    presence.publish(session, world["agents"]["alice"], {"status": "ready", "agents": [
        {"key": "a" * 32, "name": "<img src=x onerror=alert(1)>", "type": "claude", "status": "idle", "source": "herdr"}]})
    session.commit()
    page = client.get("/app/agents").text
    assert "<img src=x" not in page and "&lt;img src=x onerror=alert(1)&gt;" in page


def test_add_machine_requires_csrf_and_shows_one_time_machine_credential(client, world, session):
    login(client)
    data = {"team": "acme", "handle": "alice-laptop"}
    assert client.post("/app/agents", data=data).status_code == 403
    assert client.post("/app/agents", data={**data, "csrf_token": "wrong"}).status_code == 403
    assert identity.find_agent(session, world["teams"]["acme"], "alice-laptop") is None
    r = client.post("/app/agents", data={**data, "csrf_token": app_csrf(client)})
    assert r.status_code == 200 and "alice-laptop is added" in r.text and "machine credential" in r.text
    prompt = re.search(r'<textarea id="setup-prompt"[^>]*>(.*?)</textarea>', r.text, re.S).group(1)
    assert "rca_" not in prompt and "managed install" in prompt and "inbox" in prompt


def test_teammates_machines_are_visible_but_not_manageable(client, world, session, app):
    from fastapi.testclient import TestClient

    login(client)  # Alice owns team acme
    page = client.get("/app/agents").text
    bob_id, alice_id = world["agents"]["bob"].id, world["agents"]["alice"].id
    bob_row = re.search(r'<span class="handle">bob-agent</span>.*?</tr>', page, re.S).group(0)
    assert "Bob" in bob_row and "?to=bob-agent" in bob_row
    assert f"/app/agents/{bob_id}/rotate" not in page and f"/app/agents/{bob_id}/revoke" not in page
    assert f"/app/teams/acme/agents/{bob_id}/revoke" in bob_row  # a team owner may revoke it
    assert world["tokens"]["bob"][:12] not in page  # never a teammate's credential prefix
    assert world["tokens"]["alice"][:12] in page

    with TestClient(app) as bob:  # a member: sees Alice's machine, cannot manage it
        login(bob, email="bob@example.test")
        page = bob.get("/app/agents").text
        alice_row = re.search(r'<span class="handle">alice-agent</span>.*?</tr>', page, re.S).group(0)
        assert "revoke" not in alice_row and "rotate" not in alice_row and "?to=alice-agent" in alice_row
        assert world["tokens"]["alice"][:12] not in page
        csrf = app_csrf(bob)
        assert bob.post(f"/app/agents/{alice_id}/rotate", data={"csrf_token": csrf}).status_code == 404
        assert bob.post(f"/app/agents/{alice_id}/revoke", data={"csrf_token": csrf}).status_code == 404
