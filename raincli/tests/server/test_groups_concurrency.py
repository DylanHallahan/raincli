"""Lock order (protocol §22, G12 M2): a post's fan-out against group-remove, a team removal and an inbox change, on
two connections, in both orders. Group rows first (by id), then membership and inbox rows (by user id), then the
inserts with the team counter last: nothing deadlocks, and the result is what the order of commits says.
Every session has a lock timeout, so an unexpected wait fails instead of hanging. Real PostgreSQL."""

from __future__ import annotations

import uuid

from conftest_groups import groups_seed  # noqa: F401  (fixture)
from conftest_v06 import _machine, v06_seed  # noqa: F401  (fixture)
from raincli_server import groups, identity, messaging
from raincli_server.models import Agent, Message, Team, User
from test_archive import Background, sessions


def _post(s, seed, body="raced"):
    maya = s.get(Agent, seed["machines"]["maya"].id)
    p, _ = groups.post(s, maya, seed["groups"]["checkout"].id, id=str(uuid.uuid4()), body=body)
    return p.id


def _jon_copy(session, post_id, seed):
    session.expire_all()
    return session.query(Message).filter_by(post_id=post_id, recipient_agent_id=seed["machines"]["jon"].id).first()


def _remove(s, seed):
    team = s.get(Team, seed["teams"]["northwind"].id)
    return groups.remove_members(s, team, "checkout", ["jon@example.test"])


def _team_removal(s, seed):
    identity.remove_member(s, s.get(Team, seed["teams"]["northwind"].id), s.get(User, seed["people"]["Jon"].id))


def test_a_post_then_group_remove(app, session, groups_seed):  # noqa: F811
    a, b = sessions(app, 2)
    try:
        pid = _post(a, groups_seed)
        removal = Background(lambda: (_remove(b, groups_seed), b.commit()))
        assert removal.blocked()  # FOR UPDATE on the group waits for the post's KEY SHARE
        a.commit()
        removal.join()
    finally:
        a.close(), b.close()
    assert _jon_copy(session, pid, groups_seed).withdrawn_at is not None  # made, then withdrawn


def test_group_remove_then_a_post(app, session, groups_seed):  # noqa: F811
    a, b = sessions(app, 2)
    try:
        _remove(b, groups_seed)
        posting = Background(lambda: (_post(a, groups_seed), a.commit())[0])
        assert posting.blocked()
        b.commit()
        pid = posting.join()
    finally:
        a.close(), b.close()
    assert _jon_copy(session, pid, groups_seed) is None  # Jon had left: no copy


def test_a_post_then_a_team_removal(app, session, groups_seed):  # noqa: F811
    a, b = sessions(app, 2)
    try:
        pid = _post(a, groups_seed)
        removal = Background(lambda: (_team_removal(b, groups_seed), b.commit()))
        assert removal.blocked()
        a.commit()
        removal.join()
    finally:
        a.close(), b.close()
    assert _jon_copy(session, pid, groups_seed).withdrawn_at is not None


def test_a_team_removal_then_a_post(app, session, groups_seed):  # noqa: F811
    a, b = sessions(app, 2)
    try:
        _team_removal(b, groups_seed)
        posting = Background(lambda: (_post(a, groups_seed), a.commit())[0])
        assert posting.blocked()
        b.commit()
        pid = posting.join()
    finally:
        a.close(), b.close()
    assert _jon_copy(session, pid, groups_seed) is None


def _new_inbox(s, seed, machine_id):
    nw = seed["teams"]["northwind"].id
    return messaging.set_person_inbox(s, s.get(User, seed["people"]["Jon"].id), nw, machine_id, "zq-agent-jon-2",
                                      via="api")


def _second_machine(session, seed):
    other, _ = _machine(session, seed["teams"]["northwind"], seed["people"]["Jon"], "zq-handle-jon-2",
                        [("zq-agent-jon-2", "instant", "idle")])
    from conftest_groups import make_capable
    make_capable(session, other)
    session.commit()
    return other.id


def test_a_post_then_an_inbox_change(app, session, groups_seed):  # noqa: F811
    other = _second_machine(session, groups_seed)
    a, b = sessions(app, 2)
    try:
        pid = _post(a, groups_seed)
        change = Background(lambda: (_new_inbox(b, groups_seed, other), b.commit()))
        assert change.blocked()  # the inbox write's FOR UPDATE waits for the fan-out's KEY SHARE
        a.commit()
        change.join()
    finally:
        a.close(), b.close()
    session.expire_all()
    moved = session.query(Message).filter_by(post_id=pid, recipient_agent_id=other).one()
    assert moved.redeliver and moved.withdrawn_at is None  # moved to the new groups inbox


def test_an_inbox_change_then_a_post(app, session, groups_seed):  # noqa: F811
    other = _second_machine(session, groups_seed)
    a, b = sessions(app, 2)
    try:
        _new_inbox(b, groups_seed, other)
        posting = Background(lambda: (_post(a, groups_seed), a.commit())[0])
        assert posting.blocked()  # fan-out waits for Jon's inbox row
        b.commit()
        pid = posting.join()
    finally:
        a.close(), b.close()
    session.expire_all()
    assert session.query(Message).filter_by(post_id=pid, recipient_agent_id=other).one().redeliver is False
    assert _jon_copy(session, pid, groups_seed) is None  # straight to the new inbox


# The review's cases (S1, S2): interleaved, with the plan the server normally gets for a small analysed table.

def test_a_team_removal_in_the_middle_of_a_fan_out_never_deadlocks(app, session, groups_seed, monkeypatch):  # noqa: F811
    """S1: the post holds its first recipient's rows when a team removal (on a sequential scan, so in heap order
    without the ORDER BY) starts; the post then carries on. Whoever is removed, nothing deadlocks."""
    import threading

    from sqlalchemy import text

    from raincli_server.models import GroupMember

    real_lock = messaging._lock_inbox_row
    nw, checkout = groups_seed["teams"]["northwind"], groups_seed["groups"]["checkout"]
    first = min(m.user_id for m in session.query(GroupMember).filter_by(conversation_id=checkout.id))
    # The post locks this row first. Rewriting it moves it to the end of the table, so a sequential scan in heap
    # order would lock the post's later recipients before it: the deadlock the review found, without the ORDER BY.
    session.execute(text("UPDATE memberships SET role = role WHERE team_id = :t AND user_id = :u"),
                    {"t": nw.id, "u": first})
    session.commit()
    for who in ("Sam", "Ryan", "Jon"):  # not in the group (no group lock first); not taking part; a recipient
        first_locked, go_on = threading.Event(), threading.Event()
        calls = []

        def paused(s, user_id, team_id, *, write):
            row = real_lock(s, user_id, team_id, write=write)
            if write:  # the removal clearing an inbox, not the post's fan-out
                return row
            calls.append(user_id)
            if len(calls) == 1:
                first_locked.set()
                go_on.wait(15)
            return row
        monkeypatch.setattr(messaging, "_lock_inbox_row", paused)
        a, b = sessions(app, 2)
        b.execute(text("SET enable_indexscan = off"))
        b.execute(text("SET enable_bitmapscan = off"))
        try:
            posting = Background(lambda: (_post(a, groups_seed), a.commit())[0])
            assert first_locked.wait(15)
            removal = Background(lambda: identity.remove_member(b, b.get(Team, groups_seed["teams"]["northwind"].id),
                                                                b.get(User, groups_seed["people"][who].id)))
            removal.blocked(pause=0.5)
            go_on.set()
            posting.join()  # raises DeadlockDetected if the two waited on each other
            removal.join()
            b.rollback()  # keep the team as it was for the next case
        finally:
            go_on.set()
            monkeypatch.setattr(messaging, "_lock_inbox_row", real_lock)
            a.close(), b.close()
        assert calls == sorted(calls), who  # the post locks its recipients in user-id order


def test_a_concurrent_retry_of_a_post_is_a_duplicate(app, session, groups_seed):  # noqa: F811
    """S2: two sessions post the same id at once: one post, one fan-out; the other answers as a retry, or 409."""
    pid = str(uuid.uuid4())
    maya = groups_seed["machines"]["maya"]
    gid = groups_seed["groups"]["checkout"].id
    a, b = sessions(app, 2)
    try:
        first, created = groups.post(a, a.get(Agent, maya.id), gid, id=pid, body="same")
        assert created
        retry = Background(lambda: groups.post(b, b.get(Agent, maya.id), gid, id=pid, body="same"))
        assert retry.blocked()  # waits on the uncommitted post's key
        a.commit()
        again, created_again = retry.join()
        b.commit()
        assert created_again is False and str(again.id) == pid
        other = Background(lambda: groups.post(b, b.get(Agent, maya.id), gid, id=pid, body="different"))
        try:
            other.join()
            raise AssertionError("a different post under a used id must be refused")
        except messaging.MessagingError as exc:
            assert (exc.status, exc.code) == (409, "id_conflict")
    finally:
        a.close(), b.close()
    session.expire_all()
    assert session.query(Message).filter_by(post_id=uuid.UUID(pid)).count() == 2  # one fan-out


def test_a_concurrent_group_create_of_the_same_name_is_refused(app, session, groups_seed):  # noqa: F811
    import pytest

    a, b = sessions(app, 2)
    try:
        team_a = a.get(Team, groups_seed["teams"]["northwind"].id)
        groups.create_group(a, team_a, "release", ["jon@example.test"])
        racing = Background(lambda: groups.create_group(b, b.get(Team, team_a.id), "Release", ["jon@example.test"]))
        assert racing.blocked()
        a.commit()
        with pytest.raises(groups.GroupRuleError, match='already has a group "Release"'):
            racing.join()
    finally:
        a.close(), b.close()
