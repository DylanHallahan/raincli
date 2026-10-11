"""Send in place (protocol §20.2, §20.6 M2, L4, L5): POST /app/send into an existing conversation, real PostgreSQL."""

from __future__ import annotations

import re
import uuid

from raincli_server.api import RateLimiter
from raincli_server.models import Message
from test_app_mode import alice_app  # noqa: F401  (fixture)
from test_live import live, person_send
from test_web_app import login

BOB = {"machine": "bob-agent"}


def page_of(webview, cid):
    r = webview.get(f"/app/conversations/{cid}")
    assert r.status_code == 200, r.text
    return r.text


def csrf(page):
    return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)


def conversation(client, alice_app):  # noqa: F811
    first = person_send(client, alice_app["person"], BOB, "first")
    return first["conversation_id"]


def send(webview, cid, token, body="in place", mid=None, files=None, **extra):
    data = {"csrf_token": token, "conversation_id": cid, "message_id": mid or str(uuid.uuid4()), "body": body, **extra}
    return webview.post("/app/send", data=data, files=files or [])


def test_the_composer_sends_in_place_and_the_page_loads_the_script(client, alice_app):  # noqa: F811
    cid = conversation(client, alice_app)
    page = page_of(alice_app["webview"], cid)
    assert 'data-send-in-place="/app/send"' in page and f'name="conversation_id" value="{cid}"' in page
    assert re.search(r'<script src="/static/app-send\.js\?v=[0-9a-f]{12}" defer>', page)
    compose = alice_app["webview"].get("/app/compose").text
    assert "data-send-in-place" not in compose  # a new conversation (N1) is the picker's, not this


def test_a_send_returns_the_rendered_message_and_stores_it(client, alice_app, session):  # noqa: F811
    webview = alice_app["webview"]
    cid = conversation(client, alice_app)
    mid = str(uuid.uuid4())
    r = send(webview, cid, csrf(page_of(webview, cid)), "**hello** in place", mid=mid)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.headers["cache-control"] == "no-store"
    out = r.json()
    assert out["conversation_id"] == cid and out["id"] == mid and out["duplicate"] is False
    html = out["message_html"]
    assert html.lstrip().startswith("<li") and f'id="m-{mid}"' in html and "<strong>hello</strong>" in html
    assert "<script" not in html and " style=" not in html
    msg = session.get(Message, uuid.UUID(mid))
    assert f'data-trail="{mid}"' in html  # its delivery trail, in whatever state the server has it
    assert str(msg.conversation_id) == cid and msg.seq == out["seq"] and msg.body == "**hello** in place"
    # The same fragment the poll would send (§20.6 L4: one merge path).
    polled = {m["id"]: m["html"] for m in live(webview, conversation=cid, after=0)["messages"]}
    assert polled[mid] == html


def test_two_sends_in_a_row_each_with_its_own_id(client, alice_app, session):  # noqa: F811
    webview = alice_app["webview"]
    cid = conversation(client, alice_app)
    token = csrf(page_of(webview, cid))
    a, b = send(webview, cid, token, "one").json(), send(webview, cid, token, "two").json()
    assert a["id"] != b["id"] and b["seq"] > a["seq"] and a["conversation_id"] == b["conversation_id"] == cid


def test_a_retry_after_a_lost_response_is_a_replay(client, alice_app, session):  # noqa: F811
    webview = alice_app["webview"]
    cid = conversation(client, alice_app)
    token, mid = csrf(page_of(webview, cid)), str(uuid.uuid4())
    first = send(webview, cid, token, "once", mid=mid).json()
    again = send(webview, cid, token, "once", mid=mid)
    assert again.status_code == 200
    assert again.json() == {**first, "duplicate": True}
    assert session.query(Message).filter(Message.body == "once").count() == 1


def test_an_edited_retry_returns_the_stored_version(client, alice_app, session):  # noqa: F811
    webview = alice_app["webview"]
    cid = conversation(client, alice_app)
    token, mid = csrf(page_of(webview, cid)), str(uuid.uuid4())
    send(webview, cid, token, "first draft", mid=mid)
    r = send(webview, cid, token, "edited draft", mid=mid)
    assert r.status_code == 409
    out = r.json()
    assert out["error"] == {"code": "id_conflict", "message": "Your earlier version was already sent."}
    assert out["id"] == mid and "first draft" in out["message_html"] and "edited" not in out["message_html"]
    assert session.query(Message).filter(Message.body == "edited draft").count() == 0


def test_a_conflict_on_someone_elses_id_reveals_nothing(client, world, alice_app, session):  # noqa: F811
    webview = alice_app["webview"]
    cid = conversation(client, alice_app)
    theirs = person_send(client, alice_app["person"], BOB, "x")  # stored, then made someone else's below
    session.query(Message).filter(Message.id == uuid.UUID(theirs["id"])).update(
        {"sender_user_id": world["users"]["bob"].id})
    session.commit()
    r = send(webview, cid, csrf(page_of(webview, cid)), "mine", mid=theirs["id"])
    assert r.status_code == 409 and set(r.json()) == {"error"} and r.json()["error"]["code"] == "id_conflict"


def test_a_replay_is_answered_inside_the_rate_limit(app, client, alice_app):  # noqa: F811
    webview = alice_app["webview"]
    cid = conversation(client, alice_app)
    token = csrf(page_of(webview, cid))
    app.state.web_send_limiter = RateLimiter(2)
    mid = str(uuid.uuid4())
    assert send(webview, cid, token, "a", mid=mid).status_code == 200
    assert send(webview, cid, token, "b").status_code == 200
    limited = send(webview, cid, token, "c")
    assert limited.status_code == 429
    err = limited.json()["error"]
    assert err["code"] == "rate_limited" and err["retry_after"] >= 1 and "too fast" in err["message"]
    replay = send(webview, cid, token, "a", mid=mid)  # the limit is full, but this one was stored
    assert replay.status_code == 200 and replay.json()["duplicate"] is True


def test_every_failure_is_json_with_its_status(client, alice_app):  # noqa: F811
    webview = alice_app["webview"]
    cid = conversation(client, alice_app)
    token = csrf(page_of(webview, cid))

    def error(r, status, code):
        assert r.status_code == status and r.headers["content-type"].startswith("application/json"), r.text
        assert r.json()["error"]["code"] == code and r.json()["error"]["message"]

    error(send(webview, cid, "wrong"), 403, "forbidden")
    error(send(webview, str(uuid.uuid4()), token), 404, "not_found")
    error(send(webview, "junk", token), 404, "not_found")
    error(send(webview, cid, token, mid="junk"), 400, "invalid")
    error(send(webview, cid, token, body=""), 400, "invalid")
    error(send(webview, cid, token, files=[("files", ("a.txt", b"x", "text/plain"))]), 400, "invalid")
    error(send(webview, cid, token, files=[("files", ("big.md", b"x" * (256 * 1024 + 1), "text/markdown"))]), 400,
          "invalid")  # over the per-file limit: past the 64 KiB web limit, so /app/send takes uploads (2 MiB)
    error(send(webview, cid, token, files=[("files", ("huge.md", b"x" * (2 * 1024 * 1024 + 1), "text/markdown"))]),
          413, "too_large")  # the middleware's answer, JSON here too


def test_attachments_are_sent_the_same_way(client, alice_app, session):  # noqa: F811
    webview = alice_app["webview"]
    cid = conversation(client, alice_app)
    r = send(webview, cid, csrf(page_of(webview, cid)), "with a report",
             files=[("files", ("report.md", b"# Report\n", "text/markdown"))])
    assert r.status_code == 200 and "report.md" in r.json()["message_html"]


def test_the_website_session_gets_a_404(client, world, alice_app):  # noqa: F811
    cid = conversation(client, alice_app)
    login(client)
    page = client.get(f"/app/conversations/{cid}").text
    assert "data-send-in-place" not in page
    r = send(client, cid, csrf(page))
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_the_form_send_in_app_mode_redirects_with_no_banner(client, alice_app):  # noqa: F811
    webview = alice_app["webview"]
    cid = conversation(client, alice_app)
    r = webview.post(f"/app/conversations/{cid}/send", data={"csrf_token": csrf(page_of(webview, cid)),
                                                              "message_id": str(uuid.uuid4()), "body": "no js"},
                     follow_redirects=False)
    assert r.status_code == 303 and "notice=" not in r.headers["location"]
    assert "Message stored on the server." not in webview.get(r.headers["location"]).text
