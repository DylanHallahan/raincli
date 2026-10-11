"""v0.5.5 presentation on the server (protocol §20.3, §20.4, §20.6 L7, L8), real PostgreSQL: initials, the P1
"via" rule, first-run step 1, the toast handoff target and the owner notice in the list."""

from __future__ import annotations

import html as htmllib
import re
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from api_helpers import auth
from raincli_server import identity
from raincli_server.models import AgentCredential, Message
from raincli_server.web import present
from test_app_mode import NAV, WEBVIEW, alice_app, handoff_code, person_session  # noqa: F401  (fixture)
from test_live import BOB_AGENT, agent_send, bob_reports, person_send
from test_web_app import login


@pytest.mark.parametrize("name, letters", [
    ("Jon", "JO"), ("Maya", "MA"), ("Priya Shah", "PS"), ("Ana María López", "AL"), ("J", "JJ"), ("O'Brien", "OB"),
    ("Bob (bob)", "BO"), ("42", "?"), ("", "?"), ("   ", "?"), (None, "?"), ("élan vital", "ÉV")])
def test_initials_from_the_display_name(name, letters):
    assert present.initials(name) == letters


@pytest.mark.parametrize("value, ok", [
    ("/app/conversations/0b9f6e0e-6a51-4c8e-9b0e-0e7f3c2a1d4f", True),
    ("/app/conversations/0B9F6E0E-6A51-4C8E-9B0E-0E7F3C2A1D4F", False), ("/app/inbox", False), ("/app/agents", False),
    ("https://evil.example/app/conversations/0b9f6e0e-6a51-4c8e-9b0e-0e7f3c2a1d4f", False),
    ("//evil.example/x", False), ("/app/conversations/x", False),
    ("/app/conversations/0b9f6e0e-6a51-4c8e-9b0e-0e7f3c2a1d4f/../../login", False),
    ("/app/conversations/0b9f6e0e-6a51-4c8e-9b0e-0e7f3c2a1d4f?x=1", False), ("", False)])
def test_the_handoff_continues_only_to_a_conversation(value, ok):
    assert (present.handoff_next(value) == value) is ok


def test_a_handoff_with_next_lands_on_that_conversation(app, client, world):
    signed = person_session(client)
    with TestClient(app, headers=WEBVIEW) as webview:
        target = "/app/conversations/" + str(uuid.uuid4())
        r = webview.get(f"/app/handoff?code={handoff_code(client, signed['person_session'])}&next={target}",
                        headers=NAV, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith(target)
        r = webview.get(f"/app/handoff?code={handoff_code(client, signed['person_session'])}&next=https://evil.example/",
                        headers=NAV, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("/app/inbox")


# P1 (§20.4, §20.6 L7) ---------------------------------------------------------------------------------------

def _thread(alice_app, conversation_id):
    return htmllib.unescape(alice_app["webview"].get(f"/app/conversations/{conversation_id}").text)


def _legacy(session, like, stated, body):
    """A row the API can no longer create (a stated name that isn't the sender side's own), shaped like ``like``:
    older data, or a conversation chosen before §16.6."""
    src = session.get(Message, uuid.UUID(like["id"]))
    session.add(Message(id=uuid.uuid4(), team_id=src.team_id, conversation_id=src.conversation_id,
                        sender_agent_id=src.sender_agent_id, sender_agent_name=stated,
                        recipient_user_id=src.recipient_user_id, body=body, kind="message"))
    session.commit()


def test_via_is_hidden_only_when_it_equals_the_sender_sides_agent(client, world, alice_app, session):  # noqa: F811
    bob_reports(client, world)
    ask = person_send(client, alice_app["person"], BOB_AGENT, "please review")
    agent_send(client, world["tokens"]["bob"], {"person": "alice@example.test"}, "same name", from_agent="reviewer",
               in_reply_to=ask["id"])
    _legacy(session, _first_from_bob(session, ask), "deploy-bot", "other name")
    _legacy(session, _first_from_bob(session, ask), "Reviewer", "case differs")
    page = _thread(alice_app, ask["conversation_id"])
    assert "via reviewer (stated by the machine)" not in page  # equal: hidden
    assert "via deploy-bot (stated by the machine)" in page
    assert "via Reviewer (stated by the machine)" in page  # byte for byte: case counts


def _first_from_bob(session, ask):
    m = session.query(Message).filter(Message.conversation_id == uuid.UUID(ask["conversation_id"]),
                                      Message.sender_agent_id.isnot(None)).order_by(Message.seq).first()
    return {"id": str(m.id)}


def test_via_shows_when_the_sender_side_is_a_machine_inbox(client, world, alice_app, session):  # noqa: F811
    plain = agent_send(client, world["tokens"]["bob"], {"person": "alice@example.test"}, "from the machine")
    _legacy(session, plain, "reviewer", "stated on an inbox conversation")
    page = _thread(alice_app, plain["conversation_id"])
    assert page.count("via reviewer (stated by the machine)") == 1


def test_the_website_keeps_its_via_line(client, world, alice_app, session):  # noqa: F811
    bob_reports(client, world)
    ask = person_send(client, alice_app["person"], BOB_AGENT, "please review")
    agent_send(client, world["tokens"]["bob"], {"person": "alice@example.test"}, "same name", from_agent="reviewer",
               in_reply_to=ask["id"])
    with TestClient(client.app) as site:
        login(site)
        page = htmllib.unescape(site.get(f"/app/conversations/{ask['conversation_id']}").text)
    assert "via reviewer (stated by the machine)" in page


def test_avatars_use_the_persons_initials(client, world, alice_app, session):  # noqa: F811
    bob_reports(client, world)
    ask = person_send(client, alice_app["person"], BOB_AGENT, "please review")
    agent_send(client, world["tokens"]["bob"], {"person": "alice@example.test"}, "done", from_agent="reviewer",
               in_reply_to=ask["id"])
    page = _thread(alice_app, ask["conversation_id"])
    assert re.findall(r'<span class="rc-avatar" aria-hidden="true">([^<]*)</span>', page) == ["AL", "BO"]


# First-run step 1 and the owner notice (§20.4 R8, §20.6 M1) ---------------------------------------------------

def test_step_one_is_done_after_a_connection_in_the_last_day(client, world, alice_app, session):  # noqa: F811
    page = htmllib.unescape(alice_app["webview"].get("/app/inbox").text)
    assert "rc-step-done" not in page  # signed in, but the machine has not connected yet
    r = client.put("/api/v1/presence", headers=auth(alice_app["token"]), json={"status": "ready"})
    assert r.status_code == 200, r.text
    page = htmllib.unescape(alice_app["webview"].get("/app/inbox").text)
    assert "rc-step-done" in page and "Connected: alice-laptop" in page
    session.execute(update(AgentCredential).values(last_used_at=identity.now() - timedelta(hours=25)))
    session.commit()
    page = htmllib.unescape(alice_app["webview"].get("/app/inbox").text)
    assert "rc-step-done" not in page and "Connected:" not in page


def test_the_owner_notice_sits_at_the_top_of_the_list_on_every_page(client, world, alice_app, session):  # noqa: F811
    session.execute(update(AgentCredential).values(last_used_at=None))  # alice-agent: never connected
    session.commit()
    bob_reports(client, world)
    ask = person_send(client, alice_app["person"], BOB_AGENT, "hello")
    for path in ("/app/inbox", f"/app/conversations/{ask['conversation_id']}", "/app/compose"):
        page = htmllib.unescape(alice_app["webview"].get(path).text)
        listing = page[page.index('<section class="rc-list"'):page.index("</section>", page.index('<section class="rc-list"'))]
        assert "has never connected" in listing, path
        assert listing.index("owner-warnings") < listing.index("rc-convs"), path
