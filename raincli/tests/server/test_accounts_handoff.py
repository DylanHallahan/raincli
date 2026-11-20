"""The narrow handoff relaxation (protocol §23 A8, C20b, A14 L1, Q6): one app window switches between LINKED person
sessions on the same machine (the own session and its delegated children) and nothing else. Every other replacement
of an app-mode session by another user's is refused exactly as before. Real PostgreSQL."""

from __future__ import annotations

import hashlib
import threading
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from api_helpers import auth
from conftest_accounts import HELPDESK_STRINGS, accounts_seed  # noqa: F401  (fixture)
from conftest_v06 import v06_seed  # noqa: F401  (fixture)
from raincli_server import identity, security
from raincli_server.models import PersonSession, WebSession
from raincli_server.web import auth as web_auth
from test_app_mode import APP_TOKEN, INSTALL_HASH, NAV, WEBVIEW, handoff_code


def _maya_only(s):  # Maya's own conversations (v0.6 seed)
    return [str(s["conversations"][k].id) for k in ("jon", "sam", "priya", "self")]


def _helpdesk_only(s):
    return [str(s["conversations"][k].id) for k in ("helpdesk_jon", "helpdesk_priya", "helpdesk_self")]


def _open(webview, client, token, install_hash=INSTALL_HASH):
    return webview.get(f"/app/handoff?code={handoff_code(client, token, install_hash)}", headers=NAV,
                       follow_redirects=False)


def _ws_of(session, ps_id):
    session.expire_all()
    return session.scalars(select(WebSession).where(WebSession.person_session_id == ps_id)).all()


def _child_of_another_family(session, s):
    """A second own session of Maya on her machine made WITHOUT the re-sign-in rule (a row, as an older server left
    it), and a Billing child under it: on the same machine as the Help Desk child, but not linked to it."""
    token = security.new_token(security.PERSON_TOKEN_PREFIX)
    other = PersonSession(user_id=s["people"]["Maya"].id, machine_agent_id=s["machines"]["maya"].id,
                          token_hash=security.hash_token(token), prefix=security.token_prefix(token),
                          scopes=list(s["sessions"]["maya_own"].scopes))
    session.add(other)
    session.flush()
    billing_token, _, _ = identity.issue_delegated_session(session, other, "billing@example.test")
    session.commit()
    return billing_token


def test_switching_between_the_own_session_and_its_children(app, client, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    billing = client.post("/api/v1/person/delegations", headers=auth(s["person_tokens"]["Maya"]),
                          json={"account": "billing@example.test"}).json()["person_session"]
    with TestClient(app, headers=WEBVIEW) as webview:
        assert _open(webview, client, s["person_tokens"]["Maya"]).status_code == 303  # Maya
        maya_ws = _ws_of(session, s["sessions"]["maya_own"].id)
        assert _open(webview, client, s["delegated_token"]).status_code == 303  # own -> child: Help Desk
        assert [w.revoked_at is not None for w in _ws_of(session, s["sessions"]["maya_own"].id)] == [True]
        page = webview.get("/app/inbox").text
        assert all(c in page for c in _helpdesk_only(s))
        assert not [c for c in _maya_only(s) if c in page]  # nothing of the previous account (C20b)
        assert _open(webview, client, billing).status_code == 303  # child -> sibling: Billing
        assert _open(webview, client, s["person_tokens"]["Maya"]).status_code == 303  # child -> own
        page = webview.get("/app/inbox").text
        assert all(c in page for c in _maya_only(s))
        assert not [c for c in _helpdesk_only(s) if c in page] and not [t for t in HELPDESK_STRINGS if t in page]
        live = [w for w in session.scalars(select(WebSession).where(WebSession.app_mode.is_(True),
                                                                    WebSession.revoked_at.is_(None)))]
        assert len(live) == 1 and live[0].user_id == s["people"]["Maya"].id  # one window, one session
        assert maya_ws[0].id != live[0].id


def test_an_unlinked_session_on_the_same_machine_is_refused(app, client, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    stranger = _child_of_another_family(session, s)
    with TestClient(app, headers=WEBVIEW) as webview:
        assert _open(webview, client, s["delegated_token"]).status_code == 303
        r = _open(webview, client, stranger)
        assert r.status_code == 400 and "This sign-in link can" in r.text
        assert str(s["conversations"]["helpdesk_jon"].id) in webview.get("/app/inbox").text  # unchanged
        assert all(w.revoked_at is None for w in _ws_of(session, s["sessions"]["helpdesk"].id))


def test_a_third_users_window_is_never_replaced(app, client, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    with TestClient(app, headers=WEBVIEW) as webview:
        assert _open(webview, client, s["person_tokens"]["Jon"]).status_code == 303
        assert _open(webview, client, s["delegated_token"]).status_code == 400
        assert _open(webview, client, s["person_tokens"]["Maya"]).status_code == 400  # as before (§16.12 C3)


def test_a_child_on_another_machine_or_with_a_dead_parent_is_refused(app, client, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    with TestClient(app, headers=WEBVIEW) as webview:
        assert _open(webview, client, s["person_tokens"]["Maya"]).status_code == 303
        code = handoff_code(client, s["delegated_token"])
        # A child moved to another machine (never issued so; the belt and the machine check both refuse it).
        child = session.get(PersonSession, s["sessions"]["helpdesk"].id)
        child.machine_agent_id = s["machines"]["jon"].id
        session.commit()
        assert webview.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False).status_code == 400
        child.machine_agent_id = s["machines"]["maya"].id
        session.commit()
        code = handoff_code(client, s["delegated_token"])
        # The parent revoked by a bare row change (no cascade): the belt refuses the child.
        parent = session.get(PersonSession, s["sessions"]["maya_own"].id)
        parent.revoked_at = identity.now()
        session.commit()
        assert webview.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False).status_code == 400


def test_another_installs_window_is_left_alone(app, client, session, accounts_seed):  # noqa: F811
    """Q6: a cookie of ANOTHER install reads as no window here; the handoff succeeds as today and never revokes it."""
    s = accounts_seed
    other_token = "o" * 43
    other_hash = hashlib.sha256(other_token.encode()).hexdigest()
    other_ua = WEBVIEW["User-Agent"].replace(APP_TOKEN, other_token)
    with TestClient(app, headers={"User-Agent": other_ua}) as other:
        assert _open(other, client, s["person_tokens"]["Maya"], other_hash).status_code == 303
        theirs = _ws_of(session, s["sessions"]["maya_own"].id)
        cookie = other.cookies.get(web_auth.APP_COOKIE)
        with TestClient(app, headers=WEBVIEW) as here:
            here.cookies.set(web_auth.APP_COOKIE, cookie)  # the other install's cookie, in this install's window
            assert _open(here, client, s["delegated_token"]).status_code == 303
        assert all(w.revoked_at is None for w in _ws_of(session, theirs[0].person_session_id))
        assert other.get("/app/inbox").status_code == 200


def test_the_same_user_re_handoff_is_unchanged(app, client, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    with TestClient(app, headers=WEBVIEW) as webview:
        assert _open(webview, client, s["delegated_token"]).status_code == 303
        assert _open(webview, client, s["delegated_token"]).status_code == 303
        assert [w.revoked_at for w in _ws_of(session, s["sessions"]["helpdesk"].id)] == [None, None]  # as today


def test_a_handoff_racing_a_revocation_waits_and_refuses(app, client, session, engine, accounts_seed):  # noqa: F811
    """L1: the handoff locks both person sessions FOR SHARE before deciding; a cascade in flight wins outright."""
    from raincli_server.db import make_sessionmaker

    s = accounts_seed
    with TestClient(app, headers=WEBVIEW) as webview:
        assert _open(webview, client, s["person_tokens"]["Maya"]).status_code == 303
        code = handoff_code(client, s["delegated_token"])
        revoker = make_sessionmaker(engine)()
        try:
            identity.revoke_person_sessions(revoker, session_id=s["sessions"]["maya_own"].id)  # uncommitted
            result = {}
            worker = threading.Thread(target=lambda: result.update(
                r=webview.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False)), daemon=True)
            worker.start()
            worker.join(1.5)
            assert worker.is_alive()  # waiting on the lock, not deciding on stale rows
            revoker.commit()
        finally:
            revoker.close()
        worker.join(20)
        assert result["r"].status_code == 400
        session.expire_all()
        assert not session.scalars(select(WebSession).where(WebSession.app_mode.is_(True),
                                                            WebSession.revoked_at.is_(None))).all()


def test_the_team_iso_probe_across_a_switch(app, client, session, accounts_seed):  # noqa: F811
    """C19/C20b: switched to Help Desk, the window answers every contoso id as unknown and shows no contoso data."""
    s = accounts_seed
    lee_conv = uuid.uuid4()
    with TestClient(app, headers=WEBVIEW) as webview:
        assert _open(webview, client, s["person_tokens"]["Maya"]).status_code == 303
        assert _open(webview, client, s["delegated_token"]).status_code == 303
        for path in (f"/app/conversations/{lee_conv}", f"/app/live?conversation={lee_conv}"):
            assert webview.get(path).status_code == 404
        page = webview.get("/app/inbox").text + webview.get("/app/people").text
        assert "Lee" not in page and "contoso" not in page.lower()
