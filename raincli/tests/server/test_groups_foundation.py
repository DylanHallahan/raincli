"""The shared group helpers (``raincli_server.groups``, protocol §22) on the group seed: visibility, taking part,
"Delivered to N of M", the latest post and the member list. Real PostgreSQL."""

from __future__ import annotations

import uuid

from conftest_groups import groups_seed, make_capable  # noqa: F401  (fixture)
from conftest_v06 import v06_seed  # noqa: F401  (fixture)
from raincli_server import groups, identity
from raincli_server.models import AgentPresence, Message


def test_a_group_is_visible_to_its_current_members_only(session, groups_seed):  # noqa: F811
    s = groups_seed
    p, m, checkout = s["people"], s["machines"], s["groups"]["checkout"]
    nw, ct = s["teams"]["northwind"].id, s["teams"]["contoso"].id
    for who in ("Maya", "Jon", "Priya", "Ryan"):
        assert groups.visible_group(session, checkout.id, user=p[who]) == checkout, who
        assert groups.visible_group(session, str(checkout.id), user=p[who], scope=nw) == checkout, who
    for who in ("Sam", "Olive", "Lee"):  # Olive owns the team: still not a member
        assert groups.visible_group(session, checkout.id, user=p[who]) is None, who
    assert groups.visible_group(session, checkout.id, user=p["Maya"], scope=ct) is None  # an app session in contoso
    assert groups.visible_group(session, checkout.id, machine=m["jon"]) == checkout
    assert groups.visible_group(session, checkout.id, machine=m["maya_ct"]) is None  # her machine in another team
    assert groups.visible_group(session, checkout.id, machine=m["olive"]) is None
    for unknown in (uuid.uuid4(), "not-a-uuid", s["conversations"]["jon"].id):  # a 1:1 conversation isn't a group
        assert groups.visible_group(session, unknown, user=p["Maya"]) is None


def test_taking_part_needs_an_inbox_on_a_current_groups_client(session, groups_seed):  # noqa: F811
    s = groups_seed
    p, m, checkout = s["people"], s["machines"], s["groups"]["checkout"]
    rows = {r.name: r.taking_part for r in groups.members_with_taking_part(session, checkout)}
    assert rows == {"Jon": True, "Maya": True, "Priya": True, "Ryan": False}  # Ryan: an old client
    infra = {r.name: r.taking_part for r in groups.members_with_taking_part(session, s["groups"]["infra"])}
    assert infra == {"Maya": True, "Sam": False}  # Sam: no inbox
    nw = s["teams"]["northwind"].id
    session.get(AgentPresence, m["jon"].id).client_version = "0.6.0"  # rolled back: out, whatever it polled
    assert not groups.taking_part(session, nw, p["Jon"].id)
    make_capable(session, m["ryan"])
    assert groups.taking_part(session, nw, p["Ryan"].id)
    m["ryan"].revoked_at = identity.now()
    assert not groups.taking_part(session, nw, p["Ryan"].id)
    assert [r.json() for r in groups.members_with_taking_part(session, checkout)][0] == {
        "display_name": "Jon", "email": "jon@example.test", "taking_part": False}


def test_delivered_counts_copy_states_only(session, groups_seed):  # noqa: F811
    s = groups_seed
    ask = s["posts"]["ask"]
    assert groups.delivered_summary(session, ask) == (2, 2)  # Jon replied, Priya received
    priya_copy = session.query(Message).filter_by(post_id=ask.id, recipient_agent_id=s["machines"]["priya"].id).one()
    priya_copy.withdrawn_at = identity.now()
    assert groups.delivered_summary(session, ask) == (1, 2)
    assert groups.delivered_summary(session, s["posts"]["jon_reply"]) == (0, 2)  # stored copies aren't delivered


def test_the_latest_post_and_the_list_of_groups(session, groups_seed):  # noqa: F811
    s = groups_seed
    checkout, infra = s["groups"]["checkout"], s["groups"]["infra"]
    assert groups.latest_post_seq(session, checkout.id) == s["posts"]["priya_reply"].seq
    assert groups.latest_post_seq(session, infra.id) is None
    nw = [s["teams"]["northwind"].id]
    rows = groups.groups_of(session, s["people"]["Maya"], nw)
    assert [(r.group.name, r.members_n, r.latest_seq) for r in rows] == [
        ("checkout", 4, s["posts"]["priya_reply"].seq), ("infra", 2, None)]
    assert [r.group.name for r in groups.groups_of(session, s["people"]["Jon"], nw)] == ["checkout"]
    assert groups.groups_of(session, s["people"]["Olive"], nw) == []
    assert groups.groups_of(session, s["people"]["Maya"], [s["teams"]["contoso"].id]) == []
    # Posts and copies share the team counter with messages, and the thread is in post order.
    ask, jon_reply, priya_reply = (s["posts"][k] for k in ("ask", "jon_reply", "priya_reply"))
    assert [x.id for x in groups.posts_of(session, checkout)] == [ask.id, jon_reply.id, priya_reply.id]
    assert [x.id for x in groups.posts_of(session, checkout, after=ask.seq)] == [jon_reply.id, priya_reply.id]
    copies = session.query(Message).filter_by(post_id=ask.id).all()
    assert all(c.seq > ask.seq for c in copies) and all(c.seq < jon_reply.seq for c in copies)
    assert [name for _, name in groups.mention_names(session, ask)] == ["Jon", "Priya"]


def test_the_groups_client_version():
    assert groups.client_takes_groups("0.6.1") and groups.client_takes_groups("0.7.0")
    assert groups.client_takes_groups("0.6.1.post1")  # the first three parts decide
    for old in ("0.6.0", "0.5.6", None, "", "dev"):
        assert not groups.client_takes_groups(old), old
