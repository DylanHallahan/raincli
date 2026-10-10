"""Archive and unarchive on the website and in app mode (protocol §17.2, §17.3 A1, A3, A5, A8), real PostgreSQL."""

from __future__ import annotations

import html as htmllib
import re
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from raincli_server.models import Conversation
from test_app_mode import alice_app  # noqa: F401  (fixture)
from test_web_app import add_message, csrf_of, login

THROUGH_RE = re.compile(r'name="archived_through_seq" value="(\d+)"')


def thread(session, world, body="hello"):
    msg = add_message(session, world["agents"]["bob"], world["agents"]["alice"], body=body)
    return str(msg.conversation_id)


def signed_in(app, email="alice@example.test"):
    browser = TestClient(app)
    assert login(browser, email).status_code == 303
    return browser


def archive(browser, cid, through=None):
    page = browser.get(f"/app/conversations/{cid}").text
    if through is None:
        through = THROUGH_RE.search(page).group(1)
    return browser.post(f"/app/conversations/{cid}/archive",
                        data={"csrf_token": csrf_of(page), "archived_through_seq": through}, follow_redirects=False)


def test_archive_unarchive_on_the_website(app, session, world):
    cid = thread(session, world, "please archive me")
    browser = signed_in(app)
    page = browser.get(f"/app/conversations/{cid}").text
    assert ">Archive</button>" in page and "onclick" not in page and "confirm(" not in page  # no confirmation
    r = archive(browser, cid)
    assert r.status_code == 303 and r.headers["location"].endswith("/app/inbox?notice=archived")
    inbox = browser.get("/app/inbox?notice=archived").text
    assert "Conversation archived" in inbox and "please archive me" not in inbox and "/app/archived" in inbox
    archived = htmllib.unescape(browser.get("/app/archived").text)
    assert f"/app/conversations/{cid}" in archived and "Archived by you" in archived
    assert "1 new" in archived  # §17.3 A3: the unread count stays
    # Opened directly, the thread says so and offers Unarchive (A8: who archived it).
    page = htmllib.unescape(browser.get(f"/app/conversations/{cid}").text)
    assert 'class="archived-marker">Archived by you<' in page and ">Unarchive</button>" in page
    r = browser.post(f"/app/conversations/{cid}/unarchive",
                     data={"csrf_token": csrf_of(page), "back": "archived"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/app/archived?notice=unarchived")
    assert f"/app/conversations/{cid}" not in browser.get("/app/archived").text
    assert "please archive me" in browser.get("/app/inbox").text
    session.expire_all()
    conv = session.get(Conversation, uuid.UUID(cid))
    assert conv.archived_at is None and conv.archived_by_key is None and conv.archived_through_seq is None


def test_unarchive_from_the_thread_returns_to_it(app, session, world):
    cid = thread(session, world)
    browser = signed_in(app)
    archive(browser, cid)
    page = browser.get(f"/app/conversations/{cid}").text
    r = browser.post(f"/app/conversations/{cid}/unarchive", data={"csrf_token": csrf_of(page)},
                     follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith(f"/app/conversations/{cid}?notice=unarchived#latest")


def test_the_other_side_sees_who_archived_and_a_new_message_brings_it_back(app, session, world):
    cid = thread(session, world, "first")
    bob = signed_in(app, "bob@example.test")
    assert archive(bob, cid).status_code == 303
    alice = signed_in(app)
    assert "first" not in alice.get("/app/inbox").text  # archived for both sides
    assert "Archived by Bob" in htmllib.unescape(alice.get("/app/archived").text)
    add_message(session, world["agents"]["alice"], world["agents"]["bob"], body="second")
    assert "second" in alice.get("/app/inbox").text and f"/app/conversations/{cid}" not in alice.get("/app/archived").text


def test_a_stale_archive_does_not_hide_a_newer_message(app, session, world):
    """§17.3 A1: the form carries the newest seq the person saw; a message that arrived since keeps it active."""
    cid = thread(session, world, "seen")
    browser = signed_in(app)
    page = browser.get(f"/app/conversations/{cid}").text
    through = THROUGH_RE.search(page).group(1)
    add_message(session, world["agents"]["bob"], world["agents"]["alice"], body="arrived meanwhile")
    r = browser.post(f"/app/conversations/{cid}/archive",
                     data={"csrf_token": csrf_of(page), "archived_through_seq": through}, follow_redirects=False)
    assert r.status_code == 303
    assert "arrived meanwhile" in browser.get("/app/inbox").text
    assert f"/app/conversations/{cid}" not in browser.get("/app/archived").text


def test_archive_needs_csrf_and_visibility(app, session, world):
    cid = thread(session, world)
    browser = signed_in(app)
    r = browser.post(f"/app/conversations/{cid}/archive", data={"csrf_token": "nope"}, follow_redirects=False)
    assert r.status_code == 403
    eve = signed_in(app, "eve@example.test")
    csrf = csrf_of(eve.get("/app/compose").text)
    for target in (cid, str(uuid.uuid4()), "not-a-uuid"):
        for action in ("archive", "unarchive"):
            r = eve.post(f"/app/conversations/{target}/{action}", data={"csrf_token": csrf}, follow_redirects=False)
            assert r.status_code == 404, (target, action)
    anonymous = TestClient(app)
    r = anonymous.post(f"/app/conversations/{cid}/archive", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code in (303, 401, 403)
    session.expire_all()
    assert session.scalar(select(Conversation.archived_at).where(Conversation.id == uuid.UUID(cid))) is None


def test_archive_in_app_mode(alice_app, session):  # noqa: F811
    webview, world = alice_app["webview"], alice_app["world"]
    cid = thread(session, world, "app mode thread")
    inbox = webview.get("/app/inbox").text
    assert 'href="/app/archived"' in inbox and "app mode thread" in inbox  # the rail link
    assert archive(webview, cid).status_code == 303
    assert "app mode thread" not in webview.get("/app/inbox").text
    archived = webview.get("/app/archived")
    assert archived.status_code == 200 and "rc-nav" in archived.text and 'aria-current="page"' in archived.text
    text = htmllib.unescape(archived.text)
    assert "Archived by you" in text and f"/app/conversations/{cid}" in text
    r = webview.post(f"/app/conversations/{cid}/unarchive",
                     data={"csrf_token": csrf_of(archived.text), "back": "archived"}, follow_redirects=False)
    assert r.status_code == 303
    assert "app mode thread" in webview.get("/app/inbox").text


def test_machines_page_says_named_agents_can_be_messaged(app, world):
    """v0.5.2: the Machines page no longer says only the inbox agent can be reached."""
    page = htmllib.unescape(signed_in(app).get("/app/agents").text)
    for stale in ("You can't message the other agents directly", "hands messages to one",
                  "The other agents on a machine are listed for visibility only"):
        assert stale not in page
    assert "any named agent marked Instant" in re.sub(r"<[^>]+>", "", page) and "Next turn" in page
    assert "shown for visibility only and can't be messaged" in page


def test_every_layout_loads_localtime_without_inline_script(app, session, world, alice_app):  # noqa: F811
    """§17.1: local time comes from static/localtime.js on the website and in app mode; the CSP is unchanged."""
    cid = thread(session, world)
    browser = signed_in(app)
    for client, path in ((browser, "/app/inbox"), (browser, f"/app/conversations/{cid}"),
                         (alice_app["webview"], "/app/inbox"), (alice_app["webview"], f"/app/conversations/{cid}")):
        r = client.get(path)
        src = re.search(r'<script src="([^"]*localtime\.js[^"]*)" defer></script>', r.text)
        assert src and "<script>" not in r.text and "onclick" not in r.text, path
        assert r.headers["content-security-policy"] == ("default-src 'self'; base-uri 'none'; form-action 'self'; "
                                                         "frame-ancestors 'none'; object-src 'none'")
        assert '<time datetime="' in r.text and " UTC</time>" in r.text  # the no-JS fallback
    js = browser.get(src.group(1))
    assert js.status_code == 200 and "javascript" in js.headers["content-type"] and "Intl.DateTimeFormat" in js.text
