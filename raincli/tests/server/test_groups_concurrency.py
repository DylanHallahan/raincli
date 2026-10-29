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
