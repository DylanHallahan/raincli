"""Team scope (protocol §21.13) on the shared v0.6 seed: Maya is in northwind (her machine zq-handle-maya, which
issued her person session) and in contoso (zq-handle-maya-ct), where Lee writes to her.

- An app sign-in covers one team: the person API and app mode answer contoso's resources exactly like unknown ones.
- A website sign-in covers both teams, labelled; a typed recipient needs a team when there are several.
- Message numbers are per team: nothing a northwind member can fetch changes when contoso sends.
- An archive authorizes before it locks; an id used in another team is answered like a malformed one.
Real PostgreSQL."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql

from api_helpers import auth
from conftest_v06 import PASSWORD, v06_seed  # noqa: F401  (fixture)
from raincli_server import identity, messaging
from raincli_server.models import Message
from test_app_mode import NAV, WEBVIEW, handoff_code
from test_web_app import csrf_of, login
from test_v06_web import text

UNKNOWN = str(uuid.uuid4())


def machine_send(client, token, to, body="hello", **extra):
    return client.post("/api/v1/messages", headers=auth(token),
                       json={"id": str(uuid.uuid4()), **({"to": to} if to is not None else {}), "body": body, **extra})


@pytest.fixture
def contoso(client, v06_seed):  # noqa: F811
    """Lee's machine writes to Maya in contoso, with a file: a message, its conversation and its attachment."""
    data = b"# contoso only\n"
    r = machine_send(client, v06_seed["machine_tokens"]["lee"], "maya@example.test", "Contoso business.",
                     attachments=[{"filename": "ct.md", "content_b64": base64.b64encode(data).decode(),
                                   "sha256": hashlib.sha256(data).hexdigest()}])
    assert r.status_code == 201, r.text
    msg = r.json()["message"]
    return {"message": msg["id"], "conversation": msg["conversation_id"], "attachment": msg["attachments"][0]["id"]}


def same_answer(a, b):
    """The same status and body: another team's resource reads exactly like an unknown one."""
    return (a.status_code, a.content) == (b.status_code, b.content)


# T1: an app sign-in covers the team of the machine it's signed in on ----------------------------------------------

def test_the_person_api_answers_another_teams_resources_like_unknown_ones(client, v06_seed, contoso):  # noqa: F811
    h = auth(v06_seed["person_tokens"]["Maya"])  # issued by her northwind machine
    get = lambda path: client.get(f"/api/v1/person/{path}", headers=h)  # noqa: E731
    post = lambda path, **kw: client.post(f"/api/v1/person/{path}", headers=h, **kw)  # noqa: E731
    mid, cid, aid = contoso["message"], contoso["conversation"], contoso["attachment"]
    assert get(f"messages/{mid}").status_code == 404 and same_answer(get(f"messages/{mid}"), get(f"messages/{UNKNOWN}"))
    assert same_answer(get(f"conversations/{cid}"), get(f"conversations/{UNKNOWN}"))
    assert same_answer(get(f"messages/{mid}/attachments/{aid}"), get(f"messages/{UNKNOWN}/attachments/{aid}"))
    assert same_answer(get(f"messages/{mid}/attachments/1"), get(f"messages/{UNKNOWN}/attachments/1"))
    assert same_answer(post(f"messages/{mid}/ack"), post(f"messages/{UNKNOWN}/ack"))
    for verb in ("archive", "unarchive"):
        assert same_answer(post(f"conversations/{cid}/{verb}"), post(f"conversations/{UNKNOWN}/{verb}"))
    reply = lambda parent: post("send", json={"id": str(uuid.uuid4()), "body": "re", "in_reply_to": parent})  # noqa
    assert reply(mid).status_code == 404 and reply(mid).json() == reply(UNKNOWN).json()
    named = lambda slug: post("send", json={"id": str(uuid.uuid4()), "to": "lee@example.test", "body": "hi",  # noqa
                                            "team": slug})
    assert named("contoso").status_code == 400 and named("contoso").json() == named("no-such-team").json()
    assert same_answer(get("me/inbox?team=contoso"), get("me/inbox?team=no-such-team"))
    # Lists show northwind only, the app's toast feed included; /person/me names that one team.
    assert [t["slug"] for t in get("me").json()["teams"]] == ["northwind"]
    for path in ("inbox?include_acked=true", "inbox?routed=1&include_acked=true", "conversations?archived=include"):
        body = get(path).text
        assert mid not in body and cid not in body and "Contoso business." not in body, path
    # Without a team, the session's own team is used: a send goes to northwind, never contoso.
    sent = post("send", json={"id": str(uuid.uuid4()), "to": "jon@example.test", "body": "nw"})
    assert sent.status_code == 201, sent.text


def test_a_session_from_the_contoso_machine_covers_contoso_only(client, session, v06_seed, contoso):  # noqa: F811
    s = v06_seed
    token = identity.issue_person_session(session, s["people"]["Maya"], s["machines"]["maya_ct"])
    session.commit()
    h = auth(token)
    assert client.get(f"/api/v1/person/messages/{contoso['message']}", headers=h).status_code == 200
    nw_msg = str(s["messages"]["by_hand"].id)
    assert same_answer(client.get(f"/api/v1/person/messages/{nw_msg}", headers=h),
                       client.get(f"/api/v1/person/messages/{UNKNOWN}", headers=h))
    assert [t["slug"] for t in client.get("/api/v1/person/me", headers=h).json()["teams"]] == ["contoso"]
    sent = client.post("/api/v1/person/send", headers=h, json={"id": str(uuid.uuid4()), "to": "lee@example.test",
                                                               "body": "ct"})
    assert sent.status_code == 201 and sent.json()["message"]["conversation_id"] == contoso["conversation"]


@pytest.fixture
def maya_app(app, client, v06_seed):  # noqa: F811
    """Maya's app window, signed in from her northwind machine."""
    webview = TestClient(app, headers=WEBVIEW)
    r = webview.get(f"/app/handoff?code={handoff_code(client, v06_seed['person_tokens']['Maya'])}", headers=NAV,
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    yield webview
    webview.close()


def test_app_mode_answers_another_teams_resources_like_unknown_ones(maya_app, v06_seed, contoso):  # noqa: F811
    mid, cid, aid = contoso["message"], contoso["conversation"], contoso["attachment"]
    get = maya_app.get
    assert get(f"/app/conversations/{cid}").status_code == 404
    assert same_answer(get(f"/app/conversations/{cid}"), get(f"/app/conversations/{UNKNOWN}"))
    assert same_answer(get(f"/app/messages/{mid}/attachments/{aid}"), get(f"/app/messages/{UNKNOWN}/attachments/{aid}"))
    live = lambda conv: get(f"/app/live?conversation={conv}&after=0")  # noqa: E731
    assert live(cid).status_code == 404 and same_answer(live(cid), live(UNKNOWN))
    for page in ("/app/inbox", "/app/people", "/app/compose", "/app/archived", "/app/recipients"):
        body = get(page).text
        assert cid not in body and "Contoso" not in body and "Lee" not in body, page
    # The window's whoami and team context: northwind only.
    assert "Northwind" in text(get("/app/inbox").text) or "northwind" in get("/app/inbox").text


def test_a_website_sign_in_covers_both_teams_labelled(client, v06_seed, contoso):  # noqa: F811
    assert login(client, "maya@example.test", PASSWORD).status_code == 303
    inbox = client.get("/app/inbox").text
    assert contoso["conversation"] in inbox and str(v06_seed["conversations"]["jon"].id) in inbox
    teams = {t for t in __import__("re").findall(r'<span class="conv-team">([^<]+)</span>', inbox)}
    assert teams == {"Northwind", "Contoso"}  # each row says its team
    assert client.get(f"/app/conversations/{contoso['conversation']}").status_code == 200


# T2: numbers are per team ------------------------------------------------------------------------------------------

def _masked(value):
    """Presence times masked (refreshed before each snapshot, so the presence words stay the same)."""
    if isinstance(value, dict):
        return {k: ("…" if k in ("seen_at", "last_seen_at", "expires_at") else _masked(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(v) for v in value]
    return value


def _everything(client, session, v06_seed):  # noqa: F811
    """Everything Jon (northwind only) can fetch through his machine and his person session."""
    for table in ("agent_presence", "machine_agents"):  # every report fresh, whatever the run's pace
        session.execute(sql(f"UPDATE {table} SET seen_at = now()"))
    session.commit()
    m, p = auth(v06_seed["machine_tokens"]["jon"]), auth(v06_seed["person_tokens"]["Jon"])
    out = {}
    for path in ("/api/v1/me", "/api/v1/people", "/api/v1/agents", "/api/v1/me/inbox",
                 "/api/v1/inbox?routing=1&include_acked=true", "/api/v1/conversations?archived=include"):
        out[path] = client.get(path, headers=m).json()
    for conv in out["/api/v1/conversations?archived=include"]["conversations"]:
        out[conv["id"]] = client.get(f"/api/v1/conversations/{conv['id']}/messages", headers=m).json()
    for path in ("/api/v1/person/inbox?include_acked=true", "/api/v1/person/inbox?routed=1&include_acked=true",
                 "/api/v1/person/conversations?archived=include", "/api/v1/person/me/inbox"):
        out[path] = client.get(path, headers=p).json()
    for conv in out["/api/v1/person/conversations?archived=include"]["conversations"]:
        out["p" + conv["id"]] = client.get(f"/api/v1/person/conversations/{conv['id']}", headers=p).json()
    return _masked(out)


def test_another_teams_messages_change_nothing_a_member_can_fetch(client, session, v06_seed):  # noqa: F811
    """The diff probe: snapshot everything a northwind member can fetch, have contoso send, snapshot again."""
    before = _everything(client, session, v06_seed)
    ct = v06_seed["machine_tokens"]
    for i in range(5):
        assert machine_send(client, ct["lee"], "maya@example.test", f"contoso {i}").status_code == 201
        assert machine_send(client, ct["maya_ct"], "lee@example.test", f"answer {i}").status_code == 201
    after = _everything(client, session, v06_seed)
    assert after.keys() == before.keys()
    for key in before:
        assert after[key] == before[key], json.dumps([key, before[key], after[key]])


def test_numbers_are_consecutive_in_a_team_whatever_other_teams_send(client, v06_seed):  # noqa: F811
    t = v06_seed["machine_tokens"]
    first = machine_send(client, t["maya"], "jon@example.test", "one").json()["message"]["seq"]
    for i in range(5):
        machine_send(client, t["lee"], "maya@example.test", f"contoso {i}")
    second = machine_send(client, t["maya"], "jon@example.test", "two").json()["message"]["seq"]
    assert second == first + 1


# T3: a typed recipient needs a team when there are several ---------------------------------------------------------

@pytest.fixture
def two_teams(client, session, v06_seed):  # noqa: F811
    """Lee joins northwind too (one person in both teams), and contoso gets its own "Jon" (a different person)."""
    s = v06_seed
    identity.add_member(session, s["teams"]["northwind"], s["people"]["Lee"])
    jon_ct = identity.create_user(session, "jon.ct@example.test", "Jon", PASSWORD)
    identity.add_member(session, s["teams"]["contoso"], jon_ct)
    session.commit()
    assert login(client, "maya@example.test", PASSWORD).status_code == 303
    return {**s, "jon_ct": jon_ct}


def _web_send(client, to, team):
    page = client.get("/app/compose")
    return client.post("/app/conversations/new/send", data={
        "csrf_token": csrf_of(page.text), "message_id": str(uuid.uuid4()), "to": to, "team": team, "body": "hi"},
        follow_redirects=False)


def _count(session):
    session.expire_all()
    return session.scalar(sql("SELECT count(*) FROM messages"))


def test_a_typed_recipient_without_a_valid_team_is_refused(client, session, two_teams):
    before = _count(session)
    both = _web_send(client, "lee@example.test", "")
    assert both.status_code == 400 and "lee@example.test is in more than one of your teams. Choose a team." in text(both.text)
    bad = _web_send(client, "lee@example.test", "no-such-team")  # never widened to all teams
    assert bad.status_code == 400 and "Choose one of your teams." in text(bad.text)
    one = _web_send(client, "priya@example.test", "")  # in one of them only: still a team to choose
    assert one.status_code == 400 and "Choose one of your teams." in text(one.text)
    alike = _web_send(client, "Jon", "")  # a "Jon" in each team
    assert alike.status_code == 400 and "Jon is in more than one of your teams" in text(alike.text)
    assert _count(session) == before
    sent = _web_send(client, "Jon", "contoso")
    assert sent.status_code == 303
    session.expire_all()
    newest = session.query(Message).order_by(Message.global_seq.desc()).first()
    assert (newest.team_id, newest.recipient_user_id) == (two_teams["teams"]["contoso"].id, two_teams["jon_ct"].id)


def test_the_compose_warning_resolves_only_in_the_chosen_team(client, two_teams):
    warn = lambda team: text(client.get(f"/app/person-warning?to=Jon&team={team}").text)  # noqa: E731
    assert warn("") == ""  # several teams, none chosen: no guess
    assert "hasn't picked an inbox yet" in warn("contoso")  # contoso's Jon has no inbox
    assert "hasn't picked an inbox" not in warn("northwind")  # northwind's Jon has one
    compose = text(client.get("/app/compose?to=Jon").text)
    assert "hasn't picked an inbox yet" not in compose


# T4: an archive authorizes before it locks -------------------------------------------------------------------------

def _row_locks(session):
    return set(session.scalars(sql("SELECT mode FROM pg_locks WHERE pid = pg_backend_pid() "
                                   "AND relation = 'conversations'::regclass")))


def test_archiving_another_teams_conversation_takes_no_lock(session, v06_seed, contoso):  # noqa: F811
    jon, jon_machine = v06_seed["people"]["Jon"], v06_seed["machines"]["jon"]
    session.rollback()
    with pytest.raises(messaging.MessagingError) as person:
        messaging.archive_as_person(session, jon, contoso["conversation"], True)
    assert person.value.status == 404 and "RowShareLock" not in _row_locks(session)
    session.rollback()
    with pytest.raises(messaging.MessagingError) as machine:
        messaging.archive_as_machine(session, jon_machine, contoso["conversation"], True)
    assert machine.value.status == 404 and "RowShareLock" not in _row_locks(session)
    session.rollback()
    messaging.archive_as_person(session, jon, v06_seed["conversations"]["jon"].id, True)  # his own: locked
    assert "RowShareLock" in _row_locks(session)
    session.rollback()


# T6: an id used in another team ------------------------------------------------------------------------------------

def test_an_id_used_in_another_team_is_answered_like_a_malformed_one(client, v06_seed, contoso):  # noqa: F811
    t = v06_seed["machine_tokens"]
    send = lambda token, mid: client.post("/api/v1/messages", headers=auth(token),  # noqa: E731
                                          json={"id": mid, "to": "jon@example.test", "body": "hi"})
    taken, malformed = send(t["maya"], contoso["message"]), send(t["maya"], "not-a-uuid")
    assert taken.status_code == 400 and taken.json() == malformed.json()
    assert taken.json()["error"]["code"] == "invalid"
    person = client.post("/api/v1/person/send", headers=auth(v06_seed["person_tokens"]["Jon"]),
                         json={"id": contoso["message"], "to": "maya@example.test", "body": "hi"})
    assert person.status_code == 400 and person.json() == malformed.json()
    # In its own team an id still answers as before: a retry is the same message, a different one id_conflict.
    mine = send(t["maya"], str(uuid.uuid4()))
    again = client.post("/api/v1/messages", headers=auth(t["maya"]),
                        json={"id": mine.json()["message"]["id"], "to": "jon@example.test", "body": "different"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "id_conflict"
    assert json.loads(taken.content)["error"]["message"] == "id must be a uuid4 string"
