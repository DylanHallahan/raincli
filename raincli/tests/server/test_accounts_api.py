"""Account switching over the person API and the app window (protocol §23 A3–A7, A14–A16), on the ``accounts_seed``:
issuing delegated sessions (no oracle, the cap, re-add, re-sign-in), what a delegated session may not do (AS3),
``/person/me``, attribution ("sent by", recipients never), escalations under a grant (A15), the hidden "Your agents"
history (A16), and the cascade with the account's password exception (A7, M4) and Signed-in apps (M3)."""

from __future__ import annotations

import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api_helpers import auth
from conftest_accounts import HELPDESK_STRINGS, accounts_seed, make_grant  # noqa: F401  (fixture)
from conftest_v06 import PASSWORD, v06_seed  # noqa: F401  (fixture)
from raincli_server import identity, messaging
from raincli_server.models import Grant, Message, PersonSession, WebSession
from test_app_mode import NAV, WEBVIEW, handoff_code
from test_web_app import csrf_of, login


def window(app, client, token):
    """An app window (app mode) signed in through a real handoff of ``token``."""
    webview = TestClient(app, headers=WEBVIEW)
    r = webview.get(f"/app/handoff?code={handoff_code(client, token)}", headers=NAV, follow_redirects=False)
    assert r.status_code == 303, r.text
    return webview


def app_csrf(webview):
    return re.search(r'data-csrf="([^"]+)"', webview.get("/app/inbox").text).group(1)


def no_helpdesk_machine(text: str):
    assert not [s for s in HELPDESK_STRINGS if s in text], [s for s in HELPDESK_STRINGS if s in text]


@pytest.fixture
def hd(app, client, accounts_seed):  # noqa: F811
    """Maya's window acting as Help Desk (the delegated session)."""
    webview = window(app, client, accounts_seed["delegated_token"])
    yield webview
    webview.close()


# Issuing (A3, A14 M5) -------------------------------------------------------------------------

def test_an_own_session_lists_its_grants_and_adds_an_account(client, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    own = auth(s["person_tokens"]["Maya"])
    r = client.get("/api/v1/person/delegations", headers=own)
    assert r.status_code == 200 and r.json() == {"accounts": [
        {"email": "billing@example.test", "display_name": "Billing", "team": "northwind", "toasts": True,
         "kind": "individual"},
        {"email": "helpdesk@example.test", "display_name": "Help Desk", "team": "northwind", "toasts": True,
         "kind": "individual"}]}
    r = client.post("/api/v1/person/delegations", headers=own, json={"account": "Billing@Example.test"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account"] == {"email": "billing@example.test", "display_name": "Billing", "team": "northwind",
                               "toasts": True, "kind": "individual"}
    assert body["person_session"].startswith("rps_")
    me = client.get("/api/v1/person/me", headers=auth(body["person_session"])).json()
    assert me["user"]["email"] == "billing@example.test" and me["delegated"] is True
    assert me["acting_user"] == {"display_name": "Maya", "email": "maya@example.test"}
    assert (me["toasts"], me["grant_kind"]) == (True, "individual")


def test_no_grant_unknown_and_other_team_get_one_answer(client, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    own = auth(s["person_tokens"]["Maya"])
    answers = {client.post("/api/v1/person/delegations", headers=own, json={"account": who}).text
               for who in ("jon@example.test", "nobody@example.test", "lee@example.test", "maya@example.test")}
    assert len(answers) == 1 and json.loads(answers.pop())["error"]["code"] == "not_found"  # no oracle
    for body in ({}, {"account": 7}, {"account": "x", "more": 1}, []):
        assert client.post("/api/v1/person/delegations", headers=own, json=body).status_code == 400
    # Maya's contoso session (her machine there) sees no northwind grant (§21.13, C19).
    ct_token = identity.issue_person_session(session, s["people"]["Maya"], s["machines"]["maya_ct"])
    session.commit()
    assert client.get("/api/v1/person/delegations", headers=auth(ct_token)).json() == {"accounts": []}
    assert client.post("/api/v1/person/delegations", headers=auth(ct_token),
                       json={"account": "helpdesk@example.test"}).status_code == 404


def test_a_delegated_session_cannot_delegate(client, accounts_seed):  # noqa: F811
    d = auth(accounts_seed["delegated_token"])
    for r in (client.get("/api/v1/person/delegations", headers=d),
              client.post("/api/v1/person/delegations", headers=d, json={"account": "billing@example.test"})):
        assert r.status_code == 403
        assert r.json()["error"] == {"code": "delegated_scope", "message": "Not allowed while acting for Help Desk."}


def test_the_cap_and_re_adding(client, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    own = auth(s["person_tokens"]["Maya"])
    nw, maya = s["teams"]["northwind"], s["people"]["Maya"]
    # Re-adding replaces: the old token stops working (a lost token is fixed by adding again).
    first = client.post("/api/v1/person/delegations", headers=own, json={"account": "helpdesk@example.test"}).json()
    second = client.post("/api/v1/person/delegations", headers=own, json={"account": "helpdesk@example.test"}).json()
    assert client.get("/api/v1/person/me", headers=auth(s["delegated_token"])).status_code == 401
    assert client.get("/api/v1/person/me", headers=auth(first["person_session"])).status_code == 401
    assert client.get("/api/v1/person/me", headers=auth(second["person_session"])).status_code == 200
    live = session.scalars(select(PersonSession).where(PersonSession.grant_id == s["grants"]["helpdesk"].id,
                                                       PersonSession.revoked_at.is_(None))).all()
    assert len(live) == 1
    for n in range(4):
        extra = identity.create_user(session, f"acct{n}@example.test", f"Account {n}", PASSWORD)
        identity.add_member(session, nw, extra)
        make_grant(session, nw, maya, extra)
    session.commit()
    for email in ("billing@example.test", "acct0@example.test", "acct1@example.test"):
        assert client.post("/api/v1/person/delegations", headers=own, json={"account": email}).status_code == 200
    r = client.post("/api/v1/person/delegations", headers=own, json={"account": "acct2@example.test"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "too_many_accounts"
    # Removing one (its own sign-out) makes room; the grant stays (C13).
    billing = session.scalar(select(PersonSession).where(PersonSession.grant_id == s["grants"]["billing"].id,
                                                         PersonSession.revoked_at.is_(None)))
    identity.revoke_person_sessions(session, session_id=billing.id)
    session.commit()
    assert client.post("/api/v1/person/delegations", headers=own, json={"account": "acct2@example.test"}
                       ).status_code == 200


def test_a_new_own_sign_in_on_the_machine_ends_the_previous_delegations(client, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    fresh = identity.issue_person_session(session, s["people"]["Maya"], s["machines"]["maya"])
    session.commit()
    assert client.get("/api/v1/person/me", headers=auth(s["delegated_token"])).status_code == 401
    r = client.post("/api/v1/person/delegations", headers=auth(fresh), json={"account": "helpdesk@example.test"})
    assert r.status_code == 200  # the app adds its accounts again under the new session
    child = identity.authenticate_person(session, r.json()["person_session"], touch=False).person_session
    assert child.parent_session_id == identity.authenticate_person(session, fresh, touch=False).person_session.id


# What a delegated session may do (A4, AS3) -------------------------------------------------------

def test_the_inbox_setting_is_refused_through_a_grant(client, hd, accounts_seed):  # noqa: F811
    d = auth(accounts_seed["delegated_token"])
    for r in (client.get("/api/v1/person/me/inbox", headers=d),
              client.put("/api/v1/person/me/inbox", headers=d, json={"none": True}),
              client.put("/api/v1/person/me/inbox", headers=d,
                         json={"machine": "zq-handle-helpdesk", "agent": "zq-agent-helpdesk-ops"})):
        assert r.status_code == 403 and r.json()["error"]["code"] == "delegated_scope"
        no_helpdesk_machine(r.text)
    r = hd.post("/app/inbox", data={"csrf_token": app_csrf(hd), "team": "northwind", "none": "1"},
                follow_redirects=False)
    assert r.status_code == 403 and "Not allowed while acting for Help Desk." in r.text


def test_account_pages_stay_unreachable_and_sign_out_ends_only_this_session(client, hd, accounts_seed):  # noqa: F811
    s = accounts_seed
    assert hd.get("/app/account").status_code == 403  # app mode never reaches account settings (A4 pin)
    machines = hd.get("/app/agents", follow_redirects=False)  # nor the Machines page: app mode goes to People
    assert machines.status_code in (302, 303) and machines.headers["location"].endswith("/app/people")
    no_helpdesk_machine(hd.get("/app/people").text)
    r = hd.post("/app/account/password", data={"csrf_token": app_csrf(hd), "current_password": PASSWORD,
                                                "password": "a new password!", "password_confirm": "a new password!"})
    assert r.status_code == 403
    assert client.post("/api/v1/person/sign-out", headers=auth(s["delegated_token"])).json() == {"signed_out": True}
    assert client.get("/api/v1/person/me", headers=auth(s["delegated_token"])).status_code == 401
    assert client.get("/api/v1/person/me", headers=auth(s["person_tokens"]["Maya"])).status_code == 200


def test_person_me_has_the_accounts_own_unread_count(client, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    d = client.get("/api/v1/person/me", headers=auth(s["delegated_token"])).json()
    expected = messaging.unread_count(session, s["people"]["Help Desk"].id, s["teams"]["northwind"].id)
    assert d["unread"] == expected > 0 and d["delegated"] is True
    own = client.get("/api/v1/person/me", headers=auth(s["person_tokens"]["Maya"])).json()
    assert own["delegated"] is False and own["acting_user"] is None
    assert own["unread"] == messaging.unread_count(session, s["people"]["Maya"].id, s["teams"]["northwind"].id)
    no_helpdesk_machine(json.dumps(d))


def test_reading_through_a_grant_is_the_accounts_read_state(client, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    d = auth(s["delegated_token"])
    msg = s["messages"]["to_helpdesk"]
    before = client.get("/api/v1/person/me", headers=d).json()["unread"]
    r = client.post(f"/api/v1/person/messages/{msg.id}/ack", headers=d)
    assert r.status_code == 200 and r.json()["acked"] is True
    assert client.get("/api/v1/person/me", headers=d).json()["unread"] == before - 1  # AS12


# Attribution (A6) ------------------------------------------------------------------------------------------

def test_a_delegated_send_records_the_grantee_and_only_the_account_sees_it(client, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    mid = str(uuid.uuid4())
    r = client.post("/api/v1/person/send", headers=auth(s["delegated_token"]),
                    json={"id": mid, "to": "jon@example.test", "body": "Fixed it."})
    assert r.status_code == 201, r.text
    assert r.json()["message"]["sent_by"] == {"display_name": "Maya", "email": "maya@example.test"}
    session.expire_all()
    stored = session.get(Message, uuid.UUID(mid))
    assert (stored.sender_user_id, stored.acting_user_id) == (s["people"]["Help Desk"].id, s["people"]["Maya"].id)
    # The account's own session (on its own machine) sees "sent by" too.
    hd_own = identity.issue_person_session(session, s["people"]["Help Desk"], s["machines"]["helpdesk"])
    session.commit()
    assert client.get(f"/api/v1/person/messages/{mid}", headers=auth(hd_own)).json()["message"]["sent_by"][
        "email"] == "maya@example.test"
    # Recipients never: Jon as a person, and his machine (the frame is built from this JSON).
    for headers in (auth(s["person_tokens"]["Jon"]), auth(s["machine_tokens"]["jon"])):
        path = f"/api/v1/person/messages/{mid}" if "rps_" in headers["Authorization"] else f"/api/v1/messages/{mid}"
        r = client.get(path, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["message"]["sent_by"] is None
        assert "maya@example.test" not in r.text and '"Maya"' not in r.text
    # An own send carries no acting person.
    r = client.post("/api/v1/person/send", headers=auth(s["person_tokens"]["Maya"]),
                    json={"id": str(uuid.uuid4()), "to": "jon@example.test", "body": "As myself."})
    assert r.json()["message"]["sent_by"] is None
    assert session.get(Message, uuid.UUID(r.json()["message"]["id"])).acting_user_id is None


def test_a_send_from_the_delegated_window_is_attributed(hd, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    mid = str(uuid.uuid4())
    r = hd.post("/app/send", data={"csrf_token": app_csrf(hd), "conversation_id": str(
        s["conversations"]["helpdesk_jon"].id), "message_id": mid, "body": "From the window."})
    assert r.status_code in (200, 201), r.text
    session.expire_all()
    assert session.get(Message, uuid.UUID(mid)).acting_user_id == s["people"]["Maya"].id


# A15: escalations under a grant --------------------------------------------------------------------------

def test_a_delegate_sees_the_accounts_escalation_without_agent_names(client, hd, accounts_seed):  # noqa: F811
    s = accounts_seed
    esc = s["messages"]["helpdesk_escalation"]
    d = auth(s["delegated_token"])
    for path in (f"/api/v1/person/messages/{esc.id}", "/api/v1/person/inbox", "/api/v1/person/inbox?routed=1",
                 f"/api/v1/person/conversations/{s['conversations']['helpdesk_self'].id}"):
        r = client.get(path, headers=d)
        assert r.status_code == 200 and str(esc.id) in r.text, (path, r.text)
        no_helpdesk_machine(r.text)
    shown = client.get(f"/api/v1/person/messages/{esc.id}", headers=d).json()["message"]
    assert shown["kind"] == "escalation" and shown["from_agent"] is None
    assert shown["from_endpoint"] == {"person": "helpdesk@example.test", "display_name": "Help Desk"}
    page = hd.get(f"/app/conversations/{s['conversations']['helpdesk_self'].id}")
    assert page.status_code == 200 and "Should I reset Ryan" in page.text and "Help Desk · agent" in page.text
    no_helpdesk_machine(page.text)
    live = hd.get("/app/live", params={"conversation": str(s["conversations"]["helpdesk_self"].id), "after": 0})
    assert live.status_code == 200
    no_helpdesk_machine(live.text)


def test_the_account_itself_still_sees_its_agent(client, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    hd_own = identity.issue_person_session(session, s["people"]["Help Desk"], s["machines"]["helpdesk"])
    session.commit()
    shown = client.get(f"/api/v1/person/messages/{s['messages']['helpdesk_escalation'].id}",
                       headers=auth(hd_own)).json()["message"]
    assert shown["from_agent"] == "zq-agent-helpdesk-ops"  # owner_view: the owner, not through a grant
    held = client.get(f"/api/v1/person/messages/{s['messages']['held_to_helpdesk'].id}",
                      headers=auth(hd_own)).json()["message"]
    via_grant = client.get(f"/api/v1/person/messages/{s['messages']['held_to_helpdesk'].id}",
                           headers=auth(s["delegated_token"])).json()["message"]
    assert held["hold_reason"] == "busy" and via_grant["hold_reason"] != "busy"  # raw for the owner, coarse here


def test_a_delegate_replies_to_an_escalation_and_it_reaches_the_agent(client, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    esc = s["messages"]["helpdesk_escalation"]
    mid = str(uuid.uuid4())
    r = client.post("/api/v1/person/send", headers=auth(s["delegated_token"]),
                    json={"id": mid, "in_reply_to": str(esc.id), "body": "Yes, reset it."})
    assert r.status_code == 201, r.text
    no_helpdesk_machine(r.text)
    assert r.json()["message"]["to_endpoint"] == {"person": "helpdesk@example.test", "display_name": "Help Desk"}
    session.expire_all()
    stored = session.get(Message, uuid.UUID(mid))
    assert (stored.recipient_agent_id, stored.recipient_agent_name, stored.route) == (
        s["machines"]["helpdesk"].id, "zq-agent-helpdesk-ops", "self")  # H2 (ii): to the escalating agent
    assert stored.acting_user_id == s["people"]["Maya"].id and stored.in_reply_to == esc.id
    # The escalating machine picks it up for its agent (its own endpoint, as a connector needs).
    page = client.get("/api/v1/inbox", headers=auth(s["machine_tokens"]["helpdesk"]), params={"routing": 1}).json()
    got = next(m for m in page["messages"] if m["id"] == mid)
    assert got["to_endpoint"].get("agent") == "zq-agent-helpdesk-ops" and got["sent_by"] is None


def test_a_reply_from_the_delegated_window_shows_no_agent_name(hd, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    conv = s["conversations"]["helpdesk_self"]
    mid = str(uuid.uuid4())
    r = hd.post("/app/send", data={"csrf_token": app_csrf(hd), "conversation_id": str(conv.id), "message_id": mid,
                                   "body": "Go ahead.", "in_reply_to": str(s["messages"]["helpdesk_escalation"].id)})
    assert r.status_code in (200, 201), r.text
    no_helpdesk_machine(r.text)
    live = hd.get("/app/live", params={"conversation": str(conv.id), "after": 0})
    assert "Go ahead." in live.text
    no_helpdesk_machine(live.text)
    session.expire_all()
    stored = session.get(Message, uuid.UUID(mid))
    assert stored.recipient_agent_name == "zq-agent-helpdesk-ops" and stored.acting_user_id == s["people"]["Maya"].id


# A16 (2): "Your agents" stays hidden under a grant -----------------------------------------------------------

def test_the_accounts_own_agents_history_is_hidden_from_the_delegate(client, session, hd, accounts_seed):  # noqa: F811
    s = accounts_seed
    conv, msg = s["conversations"]["helpdesk_own_agents"], s["messages"]["helpdesk_own_agents"]
    d = auth(s["delegated_token"])
    listed = client.get("/api/v1/person/conversations", params={"archived": "include"}, headers=d).json()
    assert str(conv.id) not in {c["id"] for c in listed["conversations"]}
    assert str(s["conversations"]["helpdesk_self"].id) in {c["id"] for c in listed["conversations"]}
    unknown = client.get(f"/api/v1/person/conversations/{uuid.uuid4()}", headers=d)
    for path in (f"/api/v1/person/conversations/{conv.id}", f"/api/v1/person/messages/{msg.id}"):
        r = client.get(path, headers=d)
        assert r.status_code == 404 and r.json()["error"]["code"] == unknown.json()["error"]["code"]
    assert client.post(f"/api/v1/person/conversations/{conv.id}/unarchive", headers=d).status_code == 404
    # The account's own session still has it.
    hd_own = identity.issue_person_session(session, s["people"]["Help Desk"], s["machines"]["helpdesk"])
    session.commit()
    own_list = client.get("/api/v1/person/conversations", params={"archived": "include"}, headers=auth(hd_own)).json()
    assert str(conv.id) in {c["id"] for c in own_list["conversations"]}
    # The window: not in the list, the Archived view or /app/live; by id the unknown-id answer.
    for page in (hd.get("/app/inbox"), hd.get("/app/archived")):
        assert page.status_code == 200 and str(conv.id) not in page.text
    assert hd.get(f"/app/conversations/{conv.id}").status_code == 404
    assert hd.get("/app/live", params={"conversation": str(conv.id)}).status_code == 404
    assert str(conv.id) not in hd.get("/app/live", params={"list_rev": "x"}).text


# The cascade (A7, A14 M4) and Signed-in apps (M3) -----------------------------------------------------------

def _web_session(session, user):
    from raincli_server import security

    ws = WebSession(user_id=user.id, token_hash=security.hash_token(uuid.uuid4().hex), csrf_token="c",
                    expires_at=identity.now() + identity.PERSON_IDLE)
    session.add(ws)
    session.flush()
    return ws


@pytest.mark.parametrize("ends", ["grantee_sign_out", "grantee_password", "machine_revoked", "machine_rotated",
                                  "grant_removed", "account_removed", "grantee_removed", "account_disabled",
                                  "grantee_disabled"])
def test_the_delegated_session_ends_with_its_family(client, session, accounts_seed, ends):  # noqa: F811
    s = accounts_seed
    nw, maya, helpdesk = s["teams"]["northwind"], s["people"]["Maya"], s["people"]["Help Desk"]
    if ends == "grantee_sign_out":
        assert client.post("/api/v1/person/sign-out", headers=auth(s["person_tokens"]["Maya"])).status_code == 200
    elif ends == "grantee_password":
        identity.change_password(session, maya.id, _web_session(session, maya).id, PASSWORD, "another long password")
    elif ends == "machine_revoked":
        identity.revoke_agent(session, s["machines"]["maya"])
    elif ends == "machine_rotated":
        identity.rotate_agent_credential(session, s["machines"]["maya"])
    elif ends == "grant_removed":
        from raincli_server import grants

        grants.remove_grant(session, nw, "maya@example.test", "helpdesk@example.test")
    elif ends in ("account_removed", "grantee_removed"):
        identity.remove_member(session, nw, helpdesk if ends == "account_removed" else maya)
    else:
        identity.set_user_active(session, helpdesk if ends == "account_disabled" else maya, False)
    session.commit()
    session.expire_all()
    assert session.get(PersonSession, s["sessions"]["helpdesk"].id).revoked_at is not None  # in the same transaction
    assert client.get("/api/v1/person/me", headers=auth(s["delegated_token"])).status_code == 401
    if ends in ("grant_removed", "account_removed", "grantee_removed", "account_disabled", "grantee_disabled"):
        assert session.get(Grant, s["grants"]["helpdesk"].id).revoked_at is not None


def test_the_accounts_own_password_change_leaves_the_delegation_working(app, client, session, accounts_seed):  # noqa: F811
    """A14 M4 (C14 exception): the delegated window (no new handoff) and the CLI's --as keep working."""
    s = accounts_seed
    helpdesk = s["people"]["Help Desk"]
    webview = window(app, client, s["delegated_token"])
    try:
        hd_own = identity.issue_person_session(session, helpdesk, s["machines"]["helpdesk"])
        session.commit()
        site = TestClient(app)
        assert login(site, "helpdesk@example.test").status_code == 303
        page = site.get("/app/account")
        r = site.post("/app/account/password", data={"csrf_token": csrf_of(page.text), "current_password": PASSWORD,
                                                      "password": "a brand new password",
                                                      "password_confirm": "a brand new password"},
                      follow_redirects=False)
        assert r.status_code == 303, r.text
        assert webview.get("/app/inbox").status_code == 200  # the window, with its existing cookie
        assert client.get("/api/v1/person/me", headers=auth(s["delegated_token"])).status_code == 200
        assert client.get("/api/v1/person/me", headers=auth(hd_own)).status_code == 401  # its own sessions end
    finally:
        webview.close()


def test_signed_in_apps_never_shows_a_delegation(app, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    site = TestClient(app)
    assert login(site, "helpdesk@example.test").status_code == 303
    page = site.get("/app/account")
    assert page.status_code == 200
    assert "zq-handle-maya" not in page.text and str(s["sessions"]["helpdesk"].id) not in page.text
    r = site.post(f"/app/account/apps/{s['sessions']['helpdesk'].id}/revoke",
                  data={"csrf_token": csrf_of(page.text)}, follow_redirects=False)
    assert r.status_code == 404
    session.expire_all()
    assert session.get(PersonSession, s["sessions"]["helpdesk"].id).revoked_at is None
