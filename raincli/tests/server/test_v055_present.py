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
    ("/app/conversations/0B9F6E0E-6A51-4C8E-9B0E-0E7F3C2A1D4F", False), ("/app/inbox", False), ("/app/agents", True),
    ("/app/archived", True), ("/app/compose", True), ("/app/compose?to=x", False), ("/app/account", False),
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


@pytest.mark.skip(reason="frozen for v0.5.5 (R7/P1/Team/M3)")
def _first_from_bob(session, ask):
    m = session.query(Message).filter(Message.conversation_id == uuid.UUID(ask["conversation_id"]),
                                      Message.sender_agent_id.isnot(None)).order_by(Message.seq).first()
    return {"id": str(m.id)}


@pytest.mark.skip(reason="frozen for v0.5.5 (R7/P1/Team/M3)")
def test_the_website_shows_no_via_line(client, world, alice_app, session):  # noqa: F811
    """§21 C7.5: P1 and the "via" line are gone everywhere; a stated agent name never reaches a teammate."""
    bob_reports(client, world)
    ask = person_send(client, alice_app["person"], BOB_AGENT, "please review")
    agent_send(client, world["tokens"]["bob"], {"person": "alice@example.test"}, "same name", from_agent="reviewer",
               in_reply_to=ask["id"])
    with TestClient(client.app) as site:
        login(site)
        page = htmllib.unescape(site.get(f"/app/conversations/{ask['conversation_id']}").text)
    assert "stated by the machine" not in page and "via reviewer" not in page


def test_avatars_use_the_persons_initials(client, world, alice_app, session):  # noqa: F811
    bob_reports(client, world)
    ask = person_send(client, alice_app["person"], BOB_AGENT, "please review")
    agent_send(client, world["tokens"]["bob"], {"person": "alice@example.test"}, "done", from_agent="reviewer",
               in_reply_to=ask["id"])
    page = _thread(alice_app, ask["conversation_id"])
    assert re.findall(r'<span class="rc-avatar" aria-hidden="true">([^<]*)</span>', page) == ["AL", "BO"]


# First-run step 1 and the owner notice (§20.4 R8, §20.6 M1) ---------------------------------------------------


def test_step_one_is_done_once_an_inbox_is_picked(client, world, alice_app, session):  # noqa: F811
    """§21 C7.6: step 1 is "Pick your inbox agent", done (with the agent's name, the viewer's own) once picked."""
    from raincli_server.models import PersonInbox

    session.query(PersonInbox).filter(PersonInbox.user_id == world["users"]["alice"].id).delete()
    session.commit()
    page = htmllib.unescape(alice_app["webview"].get("/app/inbox").text)
    assert "rc-step-done" not in page and "Pick your inbox agent" in page
    session.add(PersonInbox(user_id=world["users"]["alice"].id, team_id=world["teams"]["acme"].id,
                            agent_id=world["agents"]["alice"].id, agent_name="team-inbox", set_via="app"))
    session.commit()
    page = htmllib.unescape(alice_app["webview"].get("/app/inbox").text)
    assert "rc-step-done" in page and "Your inbox: team-inbox" in page


def test_the_owner_notice_sits_at_the_top_of_the_list_on_every_page(client, world, alice_app, session):  # noqa: F811
    session.execute(update(AgentCredential).values(last_used_at=None))  # alice-agent: never connected
    session.commit()
    bob_reports(client, world)
    ask = person_send(client, alice_app["person"], BOB_AGENT, "hello")
    for path in ("/app/inbox", f"/app/conversations/{ask['conversation_id']}", "/app/compose"):
        page = htmllib.unescape(alice_app["webview"].get(path).text)
        listing = page[page.index('<section class="rc-list"'):page.index("</section>", page.index('<section class="rc-list"'))]
        assert "has never connected" in listing or "of your machines aren't connecting" in listing, path
        assert listing.index("owner-warnings") < listing.index("rc-convs"), path


# §20.7: P2 only for an app window whose own User-Agent says 0.5.5 or later ---------------------------------------

def _inbox(alice_app, version=None, ua=None):
    from test_app_mode import WEBVIEW_UA

    agent = ua or (WEBVIEW_UA + (f" RainCLIAppVersion/{version}" if version else ""))
    return alice_app["webview"].get("/app/inbox", headers={"User-Agent": agent}).text


@pytest.mark.parametrize("version, opens", [("0.5.3", False), ("0.5.4", False), ("0.5.5", True), ("0.5.6", True),
                                            ("0.6.0", True), ("1.0.0", True), (None, False), ("garbage", False),
                                            ("0.5", False), ("0.5.5-beta", False)])
def test_inbox_opens_the_latest_only_for_a_0_5_5_window(client, world, alice_app, version, opens):  # noqa: F811
    bob_reports(client, world)
    person_send(client, alice_app["person"], BOB_AGENT, "hello")
    assert ("data-open-latest" in _inbox(alice_app, version)) is opens


@pytest.mark.parametrize("text", ["RainCLIAppVersion/0.\u0665.5", "RainCLIAppVersion/\uff10.5.5",
                                  "RainCLIAppVersion/0.5.\u0e55"])
def test_non_ascii_digits_are_no_version(text):
    """ASCII digits only (review recheck 2): other Unicode digits in a header don't parse as a version."""
    from raincli_server.security import app_version

    assert app_version("Mozilla/5.0 " + text) is None and app_version("x RainCLIAppVersion/0.5.5") == (0, 5, 5)


def test_the_version_needs_no_presence_report(client, world, alice_app, session):  # noqa: F811
    """A cold or logon launch: the window's first page decides from its own User-Agent, before any presence report
    (review recheck R1)."""
    from raincli_server.models import AgentPresence

    bob_reports(client, world)
    person_send(client, alice_app["person"], BOB_AGENT, "hello")
    session.query(AgentPresence).delete()
    session.commit()
    assert "data-open-latest" in _inbox(alice_app, "0.5.5")


def test_two_version_tokens_or_a_website_session_open_nothing(client, world, alice_app):  # noqa: F811
    from test_app_mode import WEBVIEW_UA

    bob_reports(client, world)
    person_send(client, alice_app["person"], BOB_AGENT, "hello")
    assert "data-open-latest" not in _inbox(alice_app, ua=WEBVIEW_UA + " RainCLIAppVersion/0.5.5 RainCLIAppVersion/0.5.6")
    with TestClient(client.app) as site:  # the website, even with a 0.5.5 version in its User-Agent
        login(site)
        page = site.get("/app/inbox", headers={"User-Agent": "Mozilla/5.0 RainCLIAppVersion/0.5.5"}).text
    assert "data-open-latest" not in page


def test_no_marker_without_a_conversation(client, world, alice_app):  # noqa: F811
    assert "data-open-latest" not in _inbox(alice_app, "0.5.5")
