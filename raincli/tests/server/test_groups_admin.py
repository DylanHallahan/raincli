"""The group operator commands (protocol §22 G8, C1, C2): raincli-admin group-create, group-add, group-remove,
group-list and group-members. People only; a refusal is a clear message and exit code 2. Real PostgreSQL."""

from __future__ import annotations

import pytest

from conftest_groups import groups_seed  # noqa: F401  (fixture)
from conftest_v06 import TEAMMATE_STRINGS, v06_seed  # noqa: F401  (fixture)
from raincli_server.models import Conversation, GroupMember, Message
from test_api_admin import run


def _members(session, group):
    session.expire_all()
    return {m.user_id for m in session.query(GroupMember).filter_by(conversation_id=group.id)}


def test_create_list_add_remove_and_members(database_url, session, v06_seed):  # noqa: F811
    p = v06_seed["people"]
    code, out, err = run(database_url, "group-create", "northwind", "checkout", "maya@example.test",
                         "jon@example.test", "priya@example.test")
    assert code == 0, err
    assert out.startswith('created group "checkout" in northwind with 3 members\n')
    assert "Jon\tjon@example.test\tnot taking part" in out  # no groups client yet: people, never machines
    group = session.query(Conversation).filter_by(kind="group", name="checkout").one()
    assert _members(session, group) == {p["Maya"].id, p["Jon"].id, p["Priya"].id}
    assert group.a_key == f"group:{group.id}" and group.is_default is False
    code, out, _ = run(database_url, "group-add", "northwind", "CHECKOUT", "sam@example.test")  # any case
    assert code == 0 and 'added 1 to "checkout"' in out and "Sam\tsam@example.test\tnot taking part" in out
    code, out, _ = run(database_url, "group-remove", "northwind", "checkout", "jon@example.test")
    assert code == 0 and 'removed 1 from "checkout"; 0 undelivered copies withdrawn' in out
    assert _members(session, group) == {p["Maya"].id, p["Priya"].id, p["Sam"].id}
    code, out, _ = run(database_url, "group-list", "northwind")
    assert code == 0 and out == "checkout\t3 members\n"
    code, out, _ = run(database_url, "group-members", "northwind", "checkout")
    assert code == 0 and out.splitlines()[0] == '"checkout" in northwind: 3 members'
    assert [line.split("\t")[0] for line in out.splitlines()[1:]] == ["Maya", "Priya", "Sam"]
    for text in (out, err):
        assert not [s for s in TEAMMATE_STRINGS + ("zq-handle-maya", "zq-agent") if s in text]
    assert run(database_url, "group-list", "contoso")[1] == "contoso has no groups\n"


@pytest.mark.parametrize("argv, message", [
    (("group-create", "northwind", "checkout", "lee@example.test"), "lee@example.test isn't an active member of northwind"),
    (("group-create", "northwind", "checkout", "nobody@example.test"), "nobody@example.test isn't an active member"),
    (("group-create", "northwind", "checkout", "jon@example.test", "JON@example.test"), "is named twice"),
    (("group-create", "northwind", "", "jon@example.test"), "a group name has 1 to 40 characters"),
    (("group-create", "northwind", "x" * 41, "jon@example.test"), "a group name has 1 to 40 characters"),
    (("group-create", "northwind", "a\nb", "jon@example.test"), "can't contain control characters"),
    (("group-create", "northwind", "evil‮gnp", "jon@example.test"), "direction overrides"),
    (("group-add", "northwind", "nope", "jon@example.test"), 'there is no group "nope" in northwind'),
    (("group-members", "contoso", "checkout"), 'there is no group "checkout" in contoso'),
])
def test_refusals_are_clear_and_exit_2(database_url, session, v06_seed, argv, message):  # noqa: F811
    code, out, err = run(database_url, *argv)
    assert code == 2 and out == "" and message in err
    assert session.query(Conversation).filter_by(kind="group").count() == 0


def test_names_are_unique_per_team_without_case_or_form(database_url, v06_seed):  # noqa: F811
    assert run(database_url, "group-create", "northwind", "Café", "jon@example.test")[0] == 0
    for name in ("café", "CAFÉ", "Café", "  café  "):  # case, a decomposed é and padding: the same name
        code, _, err = run(database_url, "group-create", "northwind", name, "jon@example.test")
        assert code == 2 and "already has a group" in err, name
    assert run(database_url, "group-create", "contoso", "café", "lee@example.test")[0] == 0  # another team


def test_at_most_twenty_members(database_url, session, v06_seed):  # noqa: F811
    from raincli_server import identity

    nw = v06_seed["teams"]["northwind"]
    extra = []
    for i in range(16):
        user = identity.create_user(session, f"m{i}@example.test", f"Member {i}", "correct horse battery")
        identity.add_member(session, nw, user)
        extra.append(user.email)
    session.commit()
    base = ["maya@example.test", "jon@example.test", "priya@example.test", "sam@example.test"]
    assert run(database_url, "group-create", "northwind", "big", *base, *extra)[0] == 0  # 20
    code, _, err = run(database_url, "group-add", "northwind", "big", "ryan@example.test")
    assert code == 2 and '"big" would have more than 20 members (it has 20)' in err
    code, _, err = run(database_url, "group-create", "northwind", "bigger", *base, *extra, "ryan@example.test")
    assert code == 2 and "a group has at most 20 members" in err


def test_already_in_or_not_in(database_url, groups_seed):  # noqa: F811
    code, _, err = run(database_url, "group-add", "northwind", "checkout", "jon@example.test")
    assert code == 2 and 'jon@example.test is already in "checkout"' in err
    code, _, err = run(database_url, "group-remove", "northwind", "checkout", "sam@example.test")
    assert code == 2 and 'sam@example.test isn\'t in "checkout"' in err


def test_group_remove_withdraws_that_groups_undelivered_copies(database_url, session, groups_seed):  # noqa: F811
    s = groups_seed
    jon_copies = session.query(Message).filter_by(route="group", recipient_agent_id=s["machines"]["jon"].id).all()
    assert len(jon_copies) == 2  # the ask (replied: picked up) and Priya's reply (stored, not yet picked up)
    code, out, _ = run(database_url, "group-remove", "northwind", "checkout", "jon@example.test")
    assert code == 0 and "1 undelivered copies withdrawn" in out
    session.expire_all()
    withdrawn = {c.post_id: c.withdrawn_at is not None for c in session.query(Message).filter_by(
        route="group", recipient_agent_id=s["machines"]["jon"].id)}
    assert withdrawn == {s["posts"]["ask"].id: False, s["posts"]["priya_reply"].id: True}  # delivered stays delivered
    assert session.query(Message).filter(Message.route == "group", Message.withdrawn_at.is_(None)).count() == 5


def test_no_other_management_path(app):
    """C2: no API or app route creates groups or changes members; the API's only group writes are posts."""
    from test_v06_privacy_api import _routes

    group_routes = sorted((m, p) for m, p in _routes(app) if "group" in p)
    mutating = [(m, p) for m, p in group_routes if m != "GET"]
    assert mutating == [("POST", "/api/v1/groups/{group_id}/posts")], group_routes
    assert not [p for _m, p in _routes(app) if any(w in p for w in ("member", "join", "leave")) and "group" in p
                and not p.endswith("/members")]
