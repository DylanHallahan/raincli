"""The person-session lock order (protocol §23.6/§23.7, server review S1): every transaction locks the family ROOT
before any delegated session, then the delegated sessions by id. A window switch (the handoff, FOR SHARE) and a
revocation of the same family (FOR UPDATE: a sign-out, a grant-remove) then wait for each other and never deadlock,
whichever of parent and child has the lower id. Each interleaving is made deterministic by pausing one side right
after it has locked the family's roots. Real PostgreSQL."""

from __future__ import annotations

import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from conftest_accounts import accounts_seed  # noqa: F401  (fixture)
from conftest_v06 import v06_seed  # noqa: F401  (fixture)
from raincli_server import grants, identity
from raincli_server.models import PersonSession, WebSession
from test_app_mode import NAV, WEBVIEW, handoff_code

LOW, HIGH = uuid.UUID("00000000-0000-4000-8000-000000000001"), uuid.UUID("ffffffff-ffff-4fff-bfff-fffffffffff1")


def _child_with_id(session, s, new_id):
    """Help Desk's delegated session, renumbered so it sorts below or above its parent."""
    old = s["sessions"]["helpdesk"].id
    session.execute(text("UPDATE person_sessions SET id = :n WHERE id = :o"), {"n": new_id, "o": old})
    session.commit()
    session.expire_all()
    return new_id


class Pause:
    """Pause the first ``identity._lock_rows`` call of the chosen kind (``share``: the handoff; not: a revocation)
    right after it has locked the family roots, until ``go`` is set."""

    def __init__(self, monkeypatch, share):
        self.share, self.locked, self.go, self.used = share, threading.Event(), threading.Event(), False
        original = identity._lock_rows

        def lock_rows(session, ids, *, share):
            original(session, ids, share=share)
            if share == self.share and not self.used:
                self.used = True
                self.locked.set()
                assert self.go.wait(20)
        monkeypatch.setattr(identity, "_lock_rows", lock_rows)


def _run(target):
    box = {}

    def body():
        try:
            box["result"] = target()
        except Exception as exc:  # noqa: BLE001 - reported to the test
            box["error"] = exc
    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread, box


def _revoke(engine, **filters):
    from raincli_server.db import make_sessionmaker

    def work():
        s = make_sessionmaker(engine)()
        try:
            identity.revoke_person_sessions(s, **filters)
            s.commit()
        finally:
            s.close()
    return work


@pytest.mark.parametrize("child_id", [LOW, HIGH], ids=["child_below_parent", "child_above_parent"])
def test_a_revocation_holding_the_root_makes_a_switch_wait_then_refuse(app, client, session, engine, monkeypatch,
                                                                       accounts_seed, child_id):  # noqa: F811
    s = accounts_seed
    _child_with_id(session, s, child_id)
    parent_id = s["sessions"]["maya_own"].id
    with TestClient(app, headers=WEBVIEW) as webview:
        assert webview.get(f"/app/handoff?code={handoff_code(client, s['person_tokens']['Maya'])}", headers=NAV,
                           follow_redirects=False).status_code == 303
        code = handoff_code(client, s["delegated_token"])
        pause = Pause(monkeypatch, share=False)
        revoker, rbox = _run(_revoke(engine, session_id=parent_id))  # the grantee's sign-out
        assert pause.locked.wait(10)
        switch, sbox = _run(lambda: webview.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False))
        switch.join(1.5)
        assert switch.is_alive()  # waiting for the root, holding nothing of the family
        pause.go.set()
        revoker.join(20)
        switch.join(20)
    assert "error" not in rbox and "error" not in sbox, (rbox, sbox)  # no DeadlockDetected on either side
    assert sbox["result"].status_code == 400
    session.expire_all()
    assert session.get(PersonSession, child_id).revoked_at is not None


@pytest.mark.parametrize("child_id", [LOW, HIGH], ids=["child_below_parent", "child_above_parent"])
def test_a_switch_holding_the_root_makes_a_revocation_wait_then_end_it(app, client, session, engine, monkeypatch,
                                                                       accounts_seed, child_id):  # noqa: F811
    s = accounts_seed
    _child_with_id(session, s, child_id)
    parent_id = s["sessions"]["maya_own"].id
    with TestClient(app, headers=WEBVIEW) as webview:
        assert webview.get(f"/app/handoff?code={handoff_code(client, s['person_tokens']['Maya'])}", headers=NAV,
                           follow_redirects=False).status_code == 303
        code = handoff_code(client, s["delegated_token"])
        pause = Pause(monkeypatch, share=True)
        switch, sbox = _run(lambda: webview.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False))
        assert pause.locked.wait(10)
        revoker, rbox = _run(_revoke(engine, session_id=parent_id))
        revoker.join(1.5)
        assert revoker.is_alive()  # waiting for the root the switch holds
        pause.go.set()
        switch.join(20)
        revoker.join(20)
    assert "error" not in rbox and "error" not in sbox, (rbox, sbox)
    assert sbox["result"].status_code == 303  # the switch decided first
    session.expire_all()
    assert session.get(PersonSession, child_id).revoked_at is not None  # then the sign-out ended the family
    assert not session.scalars(select(WebSession).where(WebSession.app_mode.is_(True),
                                                        WebSession.revoked_at.is_(None))).all()


@pytest.mark.parametrize("first", ["grant_remove", "switch"])
def test_a_sibling_switch_against_grant_remove(app, client, session, engine, monkeypatch, accounts_seed,  # noqa: F811
                                               first):
    """Help Desk's window switches to Billing (its sibling) while the operator removes Billing's grant."""
    from raincli_server.db import make_sessionmaker

    s = accounts_seed
    billing_token, billing, _ = identity.issue_delegated_session(session, s["sessions"]["maya_own"],
                                                                 "billing@example.test")
    session.commit()

    def remove():
        other = make_sessionmaker(engine)()
        try:
            team = other.get(type(s["teams"]["northwind"]), s["teams"]["northwind"].id)
            grants.remove_grant(other, team, "maya@example.test", "billing@example.test")
            other.commit()
        finally:
            other.close()
    with TestClient(app, headers=WEBVIEW) as webview:
        assert webview.get(f"/app/handoff?code={handoff_code(client, s['delegated_token'])}", headers=NAV,
                           follow_redirects=False).status_code == 303
        code = handoff_code(client, billing_token)
        pause = Pause(monkeypatch, share=(first == "switch"))
        switch_call = lambda: webview.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False)  # noqa
        a, abox = _run(remove if first == "grant_remove" else switch_call)
        assert pause.locked.wait(10)
        b, bbox = _run(switch_call if first == "grant_remove" else remove)
        b.join(1.5)
        assert b.is_alive()  # waiting for the root, not cycling
        pause.go.set()
        a.join(20)
        b.join(20)
    assert "error" not in abox and "error" not in bbox, (abox, bbox)
    switched = (bbox if first == "grant_remove" else abox)["result"]
    assert switched.status_code == (400 if first == "grant_remove" else 303)
    session.expire_all()
    assert session.get(PersonSession, billing.id).revoked_at is not None  # the grant's session ended either way


def test_every_path_locks_roots_before_children(session, monkeypatch, accounts_seed):  # noqa: F811
    s = accounts_seed
    calls = []
    original = identity._lock_rows

    def spy(db, ids, *, share):
        calls.append((list(ids), share))
        original(db, ids, share=share)
    monkeypatch.setattr(identity, "_lock_rows", spy)
    parent, child = s["sessions"]["maya_own"].id, s["sessions"]["helpdesk"].id
    identity.lock_families(session, {child, parent}, share=True)
    assert calls == [([parent], True), ([child], True)]
    calls.clear()
    identity.lock_families(session, {child})  # a child alone: its root is locked first all the same
    assert calls == [([parent], False), ([child], False)]
    calls.clear()
    identity.revoke_person_sessions(session, session_id=parent)
    assert calls[:2] == [([parent], False), ([child], False)]
    session.rollback()


# Review L1: a disable or a member's removal locks everything it may touch ONCE ---------------------------------

def _shared_grantees(session, s):
    """Maya and Jon may each act as Help Desk AND Billing, and hold a session for both: two accounts, two grantees."""
    from conftest_accounts import make_grant, own_session

    nw, jon = s["teams"]["northwind"], s["people"]["Jon"]
    make_grant(session, nw, jon, s["people"]["Help Desk"])
    make_grant(session, nw, jon, s["people"]["Billing"])
    session.commit()
    maya_root, jon_root = s["sessions"]["maya_own"], own_session(session, s["person_tokens"]["Jon"])
    children = [s["sessions"]["helpdesk"].id]
    for parent, email in ((maya_root, "billing@example.test"), (jon_root, "helpdesk@example.test"),
                          (jon_root, "billing@example.test")):
        children.append(identity.issue_delegated_session(session, parent, email)[1].id)
    session.commit()
    return children


@pytest.mark.parametrize("who", ["Help Desk", "Maya"])
def test_a_disable_and_a_removal_lock_everything_in_one_call(session, monkeypatch, accounts_seed, who):  # noqa: F811
    s = accounts_seed
    _shared_grantees(session, s)
    calls = []
    original = identity.lock_families

    def spy(db, ids, *, share=False):
        calls.append(set(ids))
        original(db, ids, share=share)
    monkeypatch.setattr(identity, "lock_families", spy)
    for run in (lambda: identity.set_user_active(session, s["people"][who], False),
                lambda: identity.remove_member(session, s["teams"]["northwind"], s["people"][who])):
        calls.clear()
        run()
        assert calls and calls[0], "nothing was locked up front"
        held = set(calls[0])
        held |= {r for (r,) in session.execute(select(PersonSession.parent_session_id).where(
            PersonSession.id.in_(held), PersonSession.parent_session_id.is_not(None)))}
        assert all(c <= held for c in calls[1:]), "a later lock reached rows the first one didn't hold"
        session.rollback()


def test_two_disables_over_shared_grantees_both_complete(session, engine, monkeypatch, accounts_seed):  # noqa: F811
    """A smoke test, not a reproduction of review L1 (it passes on the per-grant locking of 10106d8 too, since that
    cycle needs each disable paused between its per-grant locks, and the old per-grant order isn't controllable from
    here). Disabling Help Desk and Billing at once, the first paused after its first lock: the second waits, both
    complete without error, and every shared grantee's session for either account ends. The deterministic guard for
    L1 is ``test_a_disable_and_a_removal_lock_everything_in_one_call``, which fails on 10106d8."""
    from raincli_server.db import make_sessionmaker

    s = accounts_seed
    children = _shared_grantees(session, s)

    def disable(email):
        def work():
            other = make_sessionmaker(engine)()
            try:
                person = identity.find_user_by_email(other, email)
                identity.set_user_active(other, person, False)
                other.commit()
            finally:
                other.close()
        return work
    pause = Pause(monkeypatch, share=False)
    first, fbox = _run(disable("helpdesk@example.test"))
    assert pause.locked.wait(10)
    second, sbox = _run(disable("billing@example.test"))
    second.join(1.5)
    assert second.is_alive()  # waiting on the families the first holds
    pause.go.set()
    first.join(20)
    second.join(20)
    assert "error" not in fbox and "error" not in sbox, (fbox, sbox)  # no DeadlockDetected
    session.expire_all()
    assert all(session.get(PersonSession, c).revoked_at is not None for c in children)
    assert identity.authenticate_person(session, s["person_tokens"]["Maya"], touch=False) is not None  # hers stays
