"""Account switching foundation (protocol §23): delegated sessions, the belt (``delegated_ok``), the one definition of
linked sessions, ``owner_view``, issuance with re-add and the cap, the cascade, and the app-mode viewer, on the
``accounts_seed`` (real PostgreSQL)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from conftest_accounts import accounts_seed, make_grant  # noqa: F401  (fixture)
from conftest_v06 import v06_seed  # noqa: F401  (fixture)
from raincli_server import identity
from raincli_server.models import Membership, PersonSession, WebSession
from test_app_mode import NAV, WEBVIEW, handoff_code


def _auth(session, token):
    session.expire_all()
    return identity.authenticate_person(session, token, touch=False)


def test_a_delegated_session_acts_as_the_account_for_the_grantee(session, accounts_seed):  # noqa: F811
    s = accounts_seed
    auth = _auth(session, s["delegated_token"])
    assert auth.user.email == "helpdesk@example.test" and auth.acting_user.email == "maya@example.test"
    assert auth.delegated and auth.team_id == s["teams"]["northwind"].id
    ps = auth.person_session
    assert (ps.machine_agent_id, ps.parent_session_id, ps.grant_id) == (
        s["machines"]["maya"].id, s["sessions"]["maya_own"].id, s["grants"]["helpdesk"].id)
    assert ps.scopes == s["sessions"]["maya_own"].scopes
    own = _auth(session, s["person_tokens"]["Maya"])
    assert own.acting_user is None and not own.delegated


@pytest.mark.parametrize("breaks", ["grant_revoked", "parent_revoked", "parent_idle", "account_left",
                                    "grantee_left", "grantee_disabled", "account_disabled", "machine_revoked"])
def test_the_belt_refuses_a_child_whose_family_or_grant_ended(session, accounts_seed, breaks):  # noqa: F811
    s = accounts_seed
    nw = s["teams"]["northwind"]
    parent, maya, helpdesk = s["sessions"]["maya_own"], s["people"]["Maya"], s["people"]["Help Desk"]
    # Break each condition directly (no cascade), so only the belt stands between the child and a request.
    if breaks == "grant_revoked":
        s["grants"]["helpdesk"].revoked_at = identity.now()
    elif breaks == "parent_revoked":
        parent.revoked_at = identity.now()
    elif breaks == "parent_idle":
        parent.last_used_at = identity.now() - identity.PERSON_IDLE - timedelta(minutes=1)
    elif breaks in ("account_left", "grantee_left"):
        who = helpdesk if breaks == "account_left" else maya
        session.execute(delete(Membership).where(Membership.team_id == nw.id, Membership.user_id == who.id))
    elif breaks == "grantee_disabled":
        maya.is_active = False
    elif breaks == "account_disabled":
        helpdesk.is_active = False
    else:
        s["machines"]["maya"].revoked_at = identity.now()
    session.commit()
    child = session.get(PersonSession, s["sessions"]["helpdesk"].id)
    assert not identity.delegated_ok(session, child)
    assert _auth(session, s["delegated_token"]) is None
    assert identity.linked_sessions(session, child) in ([], [session.get(PersonSession, parent.id)])


def test_using_a_child_keeps_its_parent_alive(session, accounts_seed):  # noqa: F811
    s = accounts_seed
    parent, child = s["sessions"]["maya_own"], s["sessions"]["helpdesk"]
    old = identity.now() - timedelta(days=20)
    parent.last_used_at = child.last_used_at = old
    session.commit()
    assert identity.authenticate_person(session, s["delegated_token"], touch=True) is not None
    session.commit()
    session.expire_all()
    assert session.get(PersonSession, parent.id).last_used_at > old + timedelta(days=19)
    # A child never outlives its parent: its expiry is the earlier of the two.
    parent = session.get(PersonSession, parent.id)
    parent.created_at = identity.now() - identity.PERSON_ABSOLUTE + timedelta(hours=1)
    session.commit()
    child = session.get(PersonSession, child.id)
    assert identity.session_expires_at(session, child) == identity.person_session_expires_at(parent)


def test_linked_sessions_is_the_family_on_one_machine(session, accounts_seed):  # noqa: F811
    s = accounts_seed
    parent, child = s["sessions"]["maya_own"], s["sessions"]["helpdesk"]
    assert [p.id for p in identity.linked_sessions(session, parent)] == [parent.id, child.id]
    assert [p.id for p in identity.linked_sessions(session, child)] == [parent.id, child.id]
    billing_token, billing, _ = identity.issue_delegated_session(session, parent, "billing@example.test")
    session.commit()
    assert [p.id for p in identity.linked_sessions(session, billing)] == [parent.id, child.id, billing.id]
    assert identity.linked(session, child, billing) and identity.linked(session, billing, parent)
    # Another person's own session, even of a teammate in the same team, is never linked.
    jon = _auth(session, s["person_tokens"]["Jon"]).person_session
    assert not identity.linked(session, parent, jon) and not identity.linked(session, jon, child)
    identity.revoke_person_sessions(session, session_id=child.id)
    session.commit()
    assert [p.id for p in identity.linked_sessions(session, parent)] == [parent.id, billing.id]
    assert not identity.linked(session, parent, child)


def test_owner_view_is_owner_and_not_delegated():
    import uuid

    me, other = uuid.uuid4(), uuid.uuid4()
    assert identity.owner_view(me, me, False)
    assert not identity.owner_view(me, me, True)  # the account's own machine, seen through a grant
    assert not identity.owner_view(me, other, False) and not identity.owner_view(None, None, False)


def test_issuing_needs_a_live_grant_and_answers_one_way_otherwise(session, accounts_seed):  # noqa: F811
    s = accounts_seed
    parent = s["sessions"]["maya_own"]
    for email in ("jon@example.test", "nobody@example.test", "lee@example.test", "maya@example.test", None, 7):
        with pytest.raises(identity.DelegationNotFound, match="no such account"):
            identity.issue_delegated_session(session, parent, email)
        session.rollback()
    s["grants"]["billing"].revoked_at = identity.now()
    session.commit()
    with pytest.raises(identity.DelegationNotFound):
        identity.issue_delegated_session(session, parent, "billing@example.test")
    session.rollback()
    # Only from a live own session: never from a delegated one (depth 1).
    with pytest.raises(identity.PermissionDenied):
        identity.issue_delegated_session(session, session.get(PersonSession, s["sessions"]["helpdesk"].id),
                                         "billing@example.test")


def test_re_adding_replaces_the_child_and_the_cap_counts_accounts(session, accounts_seed):  # noqa: F811
    s = accounts_seed
    parent, nw, maya = s["sessions"]["maya_own"], s["teams"]["northwind"], s["people"]["Maya"]
    token, child, _ = identity.issue_delegated_session(session, parent, "HELPDESK@example.test")
    session.commit()
    assert _auth(session, s["delegated_token"]) is None and _auth(session, token) is not None  # M5: one per grant
    assert session.scalar(select(PersonSession).where(PersonSession.id == s["sessions"]["helpdesk"].id)).revoked_at
    for n in range(3):
        extra = identity.create_user(session, f"acct{n}@example.test", f"Account {n}", "correct horse battery")
        identity.add_member(session, nw, extra)
        make_grant(session, nw, maya, extra)
    fifth = identity.create_user(session, "acct9@example.test", "Account 9", "correct horse battery")
    identity.add_member(session, nw, fifth)
    make_grant(session, nw, maya, fifth)
    session.commit()
    for email in ("billing@example.test", "acct0@example.test", "acct1@example.test"):
        identity.issue_delegated_session(session, parent, email)
    session.commit()
    with pytest.raises(identity.TooManyAccounts):  # own + 4 accounts is the limit
        identity.issue_delegated_session(session, parent, "acct9@example.test")
    session.rollback()
    identity.issue_delegated_session(session, parent, "acct0@example.test")  # re-adding one still works
    session.commit()


def test_revoking_a_parent_ends_its_children_and_their_app_sessions(app, client, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    with TestClient(app, headers=WEBVIEW) as webview:
        code = handoff_code(client, s["delegated_token"])
        r = webview.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False)
        assert r.status_code == 303, r.text
        assert webview.get("/app/inbox").status_code == 200
        identity.revoke_person_sessions(session, session_id=s["sessions"]["maya_own"].id)
        session.commit()
        session.expire_all()
        assert session.get(PersonSession, s["sessions"]["helpdesk"].id).revoked_at is not None
        ws = session.scalar(select(WebSession).where(WebSession.person_session_id == s["sessions"]["helpdesk"].id))
        assert ws.revoked_at is not None
        assert webview.get("/app/inbox", follow_redirects=False).status_code in (302, 303, 401)


def test_the_app_mode_viewer_carries_the_session_and_the_grantee(app, client, session, accounts_seed):  # noqa: F811
    from starlette.requests import Request

    from raincli_server.web import auth as web_auth

    s = accounts_seed
    with TestClient(app, headers=WEBVIEW) as webview:
        code = handoff_code(client, s["delegated_token"])
        r = webview.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False)
        assert r.status_code == 303
        cookie = webview.cookies.get(web_auth.APP_COOKIE)
    scope = {"type": "http", "headers": [(b"cookie", f"{web_auth.APP_COOKIE}={cookie}".encode()),
                                         (b"user-agent", WEBVIEW["User-Agent"].encode())]}
    viewer = web_auth.load_viewer(session, Request(scope))
    assert viewer.user.email == "helpdesk@example.test" and viewer.acting_user.email == "maya@example.test"
    assert viewer.delegated and viewer.person_session.id == s["sessions"]["helpdesk"].id
    assert [t.slug for t, _ in viewer.teams] == ["northwind"]
    # The belt holds for app-mode cookies too: a revoked grant ends the window at once.
    s["grants"]["helpdesk"].revoked_at = identity.now()
    session.commit()
    session.expire_all()
    assert web_auth.load_viewer(session, Request(scope)) is None
