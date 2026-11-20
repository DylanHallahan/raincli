"""Team-wide grants (protocol §23, contract A17): one grant lets every current and future active member of a team but
the account act as it. Issuance takes a per-user grant first, then the team-wide one; a member leaving ends their
sessions under it and leaves the grant; the account leaving revokes it. Real PostgreSQL."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from api_helpers import auth
from conftest_accounts import accounts_seed, make_grant  # noqa: F401  (fixture)
from conftest_v06 import PASSWORD, _machine, v06_seed  # noqa: F401  (fixture)
from raincli_server import grants, identity
from raincli_server.models import Grant, PersonSession
from test_api_admin import run

RELEASE = "release@example.test"


@pytest.fixture
def team_grant(session, accounts_seed):  # noqa: F811
    """The Release coordinator, a northwind member with its own machine, granted to all members of northwind."""
    s = accounts_seed
    nw = s["teams"]["northwind"]
    release = identity.create_user(session, RELEASE, "Release", PASSWORD)
    identity.add_member(session, nw, release)
    machine, _ = _machine(session, nw, release, "zq-handle-release")
    grant = grants.add_team_grant(session, nw, RELEASE)
    session.commit()
    return {**s, "release": release, "release_machine": machine, "team_grant": grant}


def _add(client, token, email=RELEASE):
    return client.post("/api/v1/person/delegations", headers=auth(token), json={"account": email})


def _listed(client, token):
    return [a["email"] for a in client.get("/api/v1/person/delegations", headers=auth(token)).json()["accounts"]]


def test_every_member_is_covered_and_a_new_member_needs_no_new_grant(client, session, team_grant):
    s = team_grant
    for who in ("Maya", "Jon", "Olive", "Sam"):
        assert RELEASE in _listed(client, s["person_tokens"][who])
        r = _add(client, s["person_tokens"][who])
        assert r.status_code == 200, (who, r.text)
        child = identity.authenticate_person(session, r.json()["person_session"], touch=False).person_session
        assert child.grant_id == s["team_grant"].id and child.user_id == s["release"].id
    nw = s["teams"]["northwind"]
    newcomer = identity.create_user(session, "nina@example.test", "Nina", PASSWORD)
    identity.add_member(session, nw, newcomer)
    machine, _ = _machine(session, nw, newcomer, "zq-handle-nina")
    token = identity.issue_person_session(session, newcomer, machine)
    session.commit()
    assert RELEASE in _listed(client, token) and _add(client, token).status_code == 200
    assert session.scalars(select(Grant).where(Grant.account_user_id == s["release"].id)).all() == [s["team_grant"]]


def test_the_account_itself_and_delegated_sessions_are_refused(client, session, team_grant):
    s = team_grant
    own = identity.issue_person_session(session, s["release"], s["release_machine"])
    session.commit()
    assert RELEASE not in _listed(client, own)
    assert _add(client, own).status_code == 404
    r = _add(client, s["delegated_token"])  # Help Desk, through Maya's grant, can never mint
    assert r.status_code == 403 and r.json()["error"]["code"] == "delegated_scope"


def test_another_teams_member_is_refused(client, session, team_grant):
    """C19: the grant is northwind's; Lee (contoso) and Maya's contoso session get the unknown answer."""
    s = team_grant
    lee_token = identity.issue_person_session(session, s["people"]["Lee"], s["machines"]["lee"])
    maya_ct = identity.issue_person_session(session, s["people"]["Maya"], s["machines"]["maya_ct"])
    session.commit()
    for token in (lee_token, maya_ct):
        assert RELEASE not in _listed(client, token)
        r = _add(client, token)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_a_member_leaving_ends_their_sessions_and_the_grant_stays(client, session, team_grant):
    s = team_grant
    nw = s["teams"]["northwind"]
    jon = _add(client, s["person_tokens"]["Jon"]).json()["person_session"]
    sam = _add(client, s["person_tokens"]["Sam"]).json()["person_session"]
    maya = _add(client, s["person_tokens"]["Maya"]).json()["person_session"]
    identity.remove_member(session, nw, s["people"]["Jon"])
    identity.set_user_active(session, s["people"]["Sam"], False)
    session.commit()
    session.expire_all()
    for token in (jon, sam):
        assert client.get("/api/v1/person/me", headers=auth(token)).status_code == 401
    assert client.get("/api/v1/person/me", headers=auth(maya)).status_code == 200  # everyone else keeps theirs
    assert session.get(Grant, s["team_grant"].id).revoked_at is None


def test_leaving_ends_sessions_by_the_acting_person_even_on_another_teams_machine_rules(session, team_grant):
    """revoke_grants ends the member's delegated sessions in that team by acting person, whatever their machine."""
    s = team_grant
    token, child, _ = identity.issue_delegated_session(session, s["sessions"]["maya_own"], RELEASE)
    session.commit()
    identity.revoke_grants(session, s["people"]["Maya"].id, s["teams"]["northwind"].id)
    session.commit()
    session.expire_all()
    assert session.get(PersonSession, child.id).revoked_at is not None
    assert session.get(Grant, s["team_grant"].id).revoked_at is None
    assert session.get(Grant, s["grants"]["helpdesk"].id).revoked_at is not None  # her per-user grants go


def test_the_accounts_removal_revokes_the_grant(client, session, team_grant):
    s = team_grant
    jon = _add(client, s["person_tokens"]["Jon"]).json()["person_session"]
    identity.remove_member(session, s["teams"]["northwind"], s["release"])
    session.commit()
    session.expire_all()
    assert session.get(Grant, s["team_grant"].id).revoked_at is not None
    assert client.get("/api/v1/person/me", headers=auth(jon)).status_code == 401


def test_a_per_user_grant_beside_the_team_wide_one(client, session, team_grant):
    s = team_grant
    nw, maya = s["teams"]["northwind"], s["people"]["Maya"]
    personal = make_grant(session, nw, maya, s["release"])
    session.commit()
    assert _listed(client, s["person_tokens"]["Maya"]).count(RELEASE) == 1  # listed once
    first = _add(client, s["person_tokens"]["Maya"]).json()["person_session"]
    child = identity.authenticate_person(session, first, touch=False).person_session
    assert child.grant_id == personal.id  # the per-user grant first
    second = _add(client, s["person_tokens"]["Maya"]).json()["person_session"]  # re-adding replaces it
    assert client.get("/api/v1/person/me", headers=auth(first)).status_code == 401
    live = session.scalars(select(PersonSession).where(PersonSession.parent_session_id == s["sessions"]["maya_own"].id,
                                                       PersonSession.user_id == s["release"].id,
                                                       PersonSession.revoked_at.is_(None))).all()
    assert len(live) == 1  # one child per account, whichever grant


def test_grant_remove_all_members_ends_every_session_under_it(database_url, client, session, team_grant):
    s = team_grant
    tokens = [_add(client, s["person_tokens"][who]).json()["person_session"] for who in ("Maya", "Jon", "Olive")]
    code, out, err = run(database_url, "grant-remove", "northwind", "--account", RELEASE, "--all-members")
    assert code == 0 and out == f"members of northwind may no longer act as {RELEASE}; 3 sessions ended\n", err
    for token in tokens:
        assert client.get("/api/v1/person/me", headers=auth(token)).status_code == 401
    assert client.get("/api/v1/person/me", headers=auth(s["delegated_token"])).status_code == 200  # Help Desk's stays
    assert _add(client, s["person_tokens"]["Jon"]).status_code == 404


def test_the_operator_commands(database_url, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    release = identity.create_user(session, RELEASE, "Release", PASSWORD)
    identity.add_member(session, s["teams"]["northwind"], release)
    session.commit()
    code, out, err = run(database_url, "grant-add", "northwind", "--account", RELEASE, "--all-members")
    assert code == 0 and out == f"every member of northwind may now act as {RELEASE} (toasts off)\n", err
    code, _, err = run(database_url, "grant-add", "northwind", "--account", RELEASE, "--all-members")
    assert code == 2 and "all members may already act as" in err
    code, out, _ = run(database_url, "grant-list", "northwind")
    assert f"Release <{RELEASE}>: all members\ttoasts off\tgranted " in out
    for argv, message in (
            (("grant-add", "northwind", "--account", "lee@example.test", "--all-members"), "not an active member"),
            (("grant-add", "northwind", "maya@example.test", "--account", RELEASE, "--all-members"), "no other email"),
            (("grant-add", "northwind", "--account", RELEASE), "or --account EMAIL --all-members"),
            (("grant-add", "northwind", "maya@example.test"), "or --account EMAIL --all-members"),
            (("grant-remove", "northwind", "--account", "jon@example.test", "--all-members"), "no team-wide grant")):
        code, out, err = run(database_url, *argv)
        assert (code, out) == (2, "") and message in err, (argv, err)


# A18/A19 ----------------------------------------------------------------------------------------------------

def _me(client, token):
    return client.get("/api/v1/person/me", headers=auth(token))


def _child(session, token):
    session.expire_all()
    return identity.authenticate_person(session, token, touch=False).person_session


def test_overlapping_grants_re_point_and_only_the_last_one_ends_the_session(client, session, team_grant):
    """A18.2: per-user + team-wide; revoking either keeps the session (re-pointed); revoking the last ends it."""
    s = team_grant
    nw = s["teams"]["northwind"]
    make_grant(session, nw, s["people"]["Maya"], s["release"])
    session.commit()
    token = _add(client, s["person_tokens"]["Maya"]).json()["person_session"]
    child_id = _child(session, token).id
    assert grants.remove_grant(session, nw, "maya@example.test", RELEASE) == 0  # re-pointed, not ended
    session.commit()
    assert _me(client, token).status_code == 200
    assert session.get(PersonSession, child_id).grant_id == s["team_grant"].id
    personal = make_grant(session, nw, s["people"]["Maya"], s["release"])
    session.commit()
    assert grants.remove_team_grant(session, nw, RELEASE) == 0  # the per-user grant still covers her
    session.commit()
    assert _me(client, token).status_code == 200 and _child(session, token).grant_id == personal.id
    assert grants.remove_grant(session, nw, "maya@example.test", RELEASE) == 1  # the last one
    session.commit()
    assert _me(client, token).status_code == 401


def test_the_resolver_never_picks_someone_elses_grant(client, session, team_grant):
    """A19 E1 negatives: another grantee's, another account's and another team's grants are never chosen; a revoked
    session is never revived by a grant-add; an excluded shared account isn't covered."""
    s = team_grant
    nw, ct = s["teams"]["northwind"], s["teams"]["contoso"]
    grants.remove_team_grant(session, nw, RELEASE)
    make_grant(session, nw, s["people"]["Jon"], s["release"])  # Jon's, not Maya's
    session.commit()
    assert _add(client, s["person_tokens"]["Maya"]).status_code == 404
    lee_ct = identity.issue_person_session(session, s["people"]["Lee"], s["machines"]["lee"])
    identity.add_member(session, ct, s["release"])
    grants.add_team_grant(session, ct, RELEASE)  # contoso's team-wide grant covers no northwind session
    session.commit()
    assert _add(client, s["person_tokens"]["Maya"]).status_code == 404
    assert _add(client, lee_ct).status_code == 200
    # A revoked session stays revoked when a covering grant comes back.
    token = _add(client, s["person_tokens"]["Jon"]).json()["person_session"]
    grants.remove_grant(session, nw, "jon@example.test", RELEASE)
    session.commit()
    assert _me(client, token).status_code == 401
    grants.add_team_grant(session, nw, RELEASE)
    session.commit()
    assert _me(client, token).status_code == 401


def test_a_shared_account_is_never_covered_by_an_all_members_grant(client, session, team_grant):
    """A18.1: Help Desk (the account of Maya's grant) can't switch to the Release coordinator, at issuance and at use."""
    s = team_grant
    hd_own = identity.issue_person_session(session, s["people"]["Help Desk"], s["machines"]["helpdesk"])
    session.commit()
    assert RELEASE not in _listed(client, hd_own) and _add(client, hd_own).status_code == 404
    jon = _add(client, s["person_tokens"]["Jon"]).json()["person_session"]
    assert _me(client, jon).status_code == 200
    make_grant(session, s["teams"]["northwind"], s["people"]["Priya"], s["people"]["Jon"])  # Jon becomes shared
    session.commit()
    assert _me(client, jon).status_code == 401  # checked at use


def test_toasts_follow_the_effective_grant(database_url, client, session, team_grant):
    """A18.4: off for an all-members grant by default, on for an individual one; on /person/delegations and /me."""
    s = team_grant
    entry = next(a for a in client.get("/api/v1/person/delegations", headers=auth(s["person_tokens"]["Jon"]))
                 .json()["accounts"] if a["email"] == RELEASE)
    assert (entry["toasts"], entry["kind"]) == (False, "all_members")
    token = _add(client, s["person_tokens"]["Jon"]).json()["person_session"]
    me = _me(client, token).json()
    assert (me["toasts"], me["grant_kind"]) == (False, "all_members")
    assert run(database_url, "grant-add", "northwind", "jon@example.test", RELEASE, "--toasts", "off")[0] == 0
    me = _me(client, token).json()  # the individual grant now governs, at once
    assert (me["toasts"], me["grant_kind"]) == (False, "individual")
    code, out, _ = run(database_url, "grant-list", "northwind")
    assert "as Release <release@example.test>\ttoasts off" in out and ": all members\ttoasts off" in out
    code, out, _ = run(database_url, "grant-add", "northwind", "--account", "billing@example.test", "--all-members",
                       "--toasts", "on")
    assert code == 0 and out.endswith("(toasts on)\n")


def test_escalation_replies_need_an_individual_grant(client, session, accounts_seed):  # noqa: F811
    """A18.5: under an all-members grant the delegate reads the account's escalations but can't answer them;
    an individual grant can (A15); a session re-pointed from individual to all-members loses it at once; archive
    stays allowed (A19 E4)."""
    s = accounts_seed
    nw = s["teams"]["northwind"]
    esc, conv = s["messages"]["helpdesk_escalation"], s["conversations"]["helpdesk_self"]
    grants.add_team_grant(session, nw, "helpdesk@example.test")
    session.commit()
    jon = _add(client, s["person_tokens"]["Jon"], "helpdesk@example.test").json()["person_session"]

    def reply(token):
        return client.post("/api/v1/person/send", headers=auth(token), json={
            "id": str(__import__("uuid").uuid4()), "in_reply_to": str(esc.id), "body": "ok"})
    assert client.get(f"/api/v1/person/messages/{esc.id}", headers=auth(jon)).status_code == 200  # may read
    r = reply(jon)
    assert r.status_code == 403 and r.json()["error"] == {
        "code": "delegated_scope", "message": "Replying to Help Desk's agents needs an individual grant."}
    assert client.post(f"/api/v1/person/conversations/{conv.id}/archive", headers=auth(jon)).status_code == 200
    assert client.post(f"/api/v1/person/conversations/{conv.id}/unarchive", headers=auth(jon)).status_code == 200
    teammate = client.post("/api/v1/person/send", headers=auth(jon), json={
        "id": str(__import__("uuid").uuid4()), "in_reply_to": str(s["messages"]["to_helpdesk"].id), "body": "on it"})
    assert teammate.status_code == 201  # replies to teammates are fine
    assert reply(s["delegated_token"]).status_code == 201  # Maya: individual grant (A15)
    grants.remove_grant(session, nw, "maya@example.test", "helpdesk@example.test")  # Maya is re-pointed...
    session.commit()
    assert _me(client, s["delegated_token"]).json()["grant_kind"] == "all_members"
    assert reply(s["delegated_token"]).status_code == 403  # ...and loses the reply at once


def test_a_team_wide_delegate_passes_every_gate_and_all_refuse_after_removal(app, client, session,  # noqa: F811
                                                                             database_url, team_grant):
    """A19 D1: authenticate_person, the app cookie, the handoff and the A8 switch (and back), then all four refuse."""
    from fastapi.testclient import TestClient

    from test_app_mode import NAV, WEBVIEW, handoff_code

    s = team_grant
    token = _add(client, s["person_tokens"]["Jon"]).json()["person_session"]
    with TestClient(app, headers=WEBVIEW) as webview:
        assert webview.get(f"/app/handoff?code={handoff_code(client, s['person_tokens']['Jon'])}", headers=NAV,
                           follow_redirects=False).status_code == 303
        assert webview.get(f"/app/handoff?code={handoff_code(client, token)}", headers=NAV,
                           follow_redirects=False).status_code == 303  # switch to Release
        assert webview.get("/app/inbox").status_code == 200
        assert webview.get(f"/app/handoff?code={handoff_code(client, s['person_tokens']['Jon'])}", headers=NAV,
                           follow_redirects=False).status_code == 303  # and back
        code = handoff_code(client, token)
        assert webview.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False).status_code == 303
        late = handoff_code(client, token)
        assert run(database_url, "grant-remove", "northwind", "--account", RELEASE, "--all-members")[0] == 0
        assert _me(client, token).status_code == 401  # authenticate_person
        assert webview.get("/app/inbox", follow_redirects=False).status_code in (302, 303, 401)  # the cookie
        assert webview.get(f"/app/handoff?code={late}", headers=NAV, follow_redirects=False).status_code == 400
    with TestClient(app, headers=WEBVIEW) as fresh:  # the switch from Jon's own window
        assert fresh.get(f"/app/handoff?code={handoff_code(client, s['person_tokens']['Jon'])}", headers=NAV,
                         follow_redirects=False).status_code == 303
        assert client.post("/api/v1/app/handoff", headers=auth(token), json={"app_install_hash": "a" * 64}
                           ).status_code == 401


@pytest.mark.parametrize("order", ["team_wide_first", "individual_first"])
def test_re_adding_replaces_by_account_whatever_the_grant(client, session, team_grant, order):
    """A19 D2: one live child per account under a parent, whichever kind of grant each came through."""
    s = team_grant
    nw = s["teams"]["northwind"]
    if order == "individual_first":
        make_grant(session, nw, s["people"]["Maya"], s["release"])
        session.commit()
    first = _add(client, s["person_tokens"]["Maya"]).json()["person_session"]
    if order == "team_wide_first":
        make_grant(session, nw, s["people"]["Maya"], s["release"])
    else:
        grants.remove_grant(session, nw, "maya@example.test", RELEASE)
    session.commit()
    second = _add(client, s["person_tokens"]["Maya"]).json()["person_session"]
    assert _me(client, first).status_code == 401 and _me(client, second).status_code == 200
    live = session.scalars(select(PersonSession).where(PersonSession.parent_session_id == s["sessions"]["maya_own"].id,
                                                       PersonSession.user_id == s["release"].id,
                                                       PersonSession.revoked_at.is_(None))).all()
    assert len(live) == 1


def test_concurrent_revocations_of_both_covering_grants_end_the_session(client, session, engine, team_grant):
    """A19 E2: grant-remove locks every live grant of (team, account) first, so two revocations of the two grants
    covering a session can't each re-point it to the other: the second waits, then ends it."""
    import threading

    from raincli_server.db import make_sessionmaker

    s = team_grant
    nw = s["teams"]["northwind"]
    make_grant(session, nw, s["people"]["Maya"], s["release"])
    session.commit()
    token = _add(client, s["person_tokens"]["Maya"]).json()["person_session"]
    first, second = make_sessionmaker(engine)(), make_sessionmaker(engine)()
    try:
        grants.remove_grant(first, first.get(type(nw), nw.id), "maya@example.test", RELEASE)  # uncommitted
        done = {}

        def other():
            done["ended"] = grants.remove_team_grant(second, second.get(type(nw), nw.id), RELEASE)
            second.commit()
        worker = threading.Thread(target=other, daemon=True)  # never keeps a stuck run alive
        worker.start()
        worker.join(1.5)
        assert worker.is_alive()  # waiting on the grant locks
        first.commit()
        worker.join(20)
        assert done["ended"] == 1
    finally:
        first.close()
        second.close()
    assert _me(client, token).status_code == 401


def test_the_window_refuses_an_escalation_reply_under_an_all_members_grant(app, client, session, accounts_seed):  # noqa: F811
    from test_accounts_api import app_csrf, window

    s = accounts_seed
    grants.add_team_grant(session, s["teams"]["northwind"], "helpdesk@example.test")
    session.commit()
    jon = _add(client, s["person_tokens"]["Jon"], "helpdesk@example.test").json()["person_session"]
    webview = window(app, client, jon)
    try:
        r = webview.post("/app/send", data={
            "csrf_token": app_csrf(webview), "message_id": str(__import__("uuid").uuid4()), "body": "x",
            "conversation_id": str(s["conversations"]["helpdesk_self"].id),
            "in_reply_to": str(s["messages"]["helpdesk_escalation"].id)})
        assert r.status_code == 403 and "needs an individual grant" in r.text
    finally:
        webview.close()
