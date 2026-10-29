"""The shared group seed (§22), on top of the v0.6 seed (tests/conftest_v06.py). Use the ``groups_seed`` fixture.

Taking part: Maya (her inbox: zq-agent-maya-laptop), Jon and Priya, on a groups client (0.6.1, ``groups=1`` polled).
Not taking part: Sam (no inbox) and Ryan (an old client, 0.5.6).

Groups in northwind:
- "checkout": Maya, Jon, Priya and Ryan. Maya's agent posts, mentioning Jon and Priya; Jon's agent replies, then
  Priya's agent. Copies go to the members taking part other than the poster.
- "infra": Sam and Maya, no posts.

Olive (the team owner) is in neither group; Lee (contoso) neither.
"""

from __future__ import annotations

import uuid

import pytest

from conftest_v06 import _inbox, v06_seed  # noqa: F401  (fixture)


def make_group(session, team, name, members):
    """A group row and its members, as the operator command makes them (no checks here)."""
    from raincli_server.models import Conversation, GroupMember

    gid = uuid.uuid4()
    group = Conversation(id=gid, team_id=team.id, kind="group", name=name, a_key=f"group:{gid}",
                         b_key=f"group:{gid}:", is_default=False)
    session.add(group)
    session.flush()
    for person in members:
        session.add(GroupMember(conversation_id=gid, user_id=person.id, team_id=team.id))
    session.flush()
    return group


def make_post(session, group, author, author_agent, body, *, mentions=(), in_reply_to=None, states=None):
    """A post by ``author`` (a machine) and one copy per member taking part other than the author's owner, as
    fan-out makes them. ``states``: a copy's delivery state by recipient person id (default "stored")."""
    from raincli_server import groups, identity
    from raincli_server.models import GroupMember, GroupPost, Message, PersonInbox

    post = GroupPost(id=uuid.uuid4(), team_id=group.team_id, conversation_id=group.id, author_agent_id=author.id,
                     author_agent_name=author_agent, author_user_id=author.owner_user_id, body=body,
                     mentions=[p.id for p in mentions], in_reply_to_post=in_reply_to.id if in_reply_to else None)
    session.add(post)
    session.flush()
    copies = []
    members = session.query(GroupMember).filter_by(conversation_id=group.id).order_by(GroupMember.user_id).all()
    for member in members:
        if member.user_id == author.owner_user_id or not groups.taking_part(session, group.team_id, member.user_id):
            continue
        inbox = session.get(PersonInbox, (member.user_id, group.team_id))
        copy = Message(id=uuid.uuid4(), team_id=group.team_id, conversation_id=group.id, body=body, kind="message",
                       route="group", post_id=post.id, sender_agent_id=author.id, sender_agent_name=author_agent,
                       recipient_agent_id=inbox.agent_id, recipient_agent_name=inbox.agent_name,
                       delivery_state=(states or {}).get(member.user_id, "stored"))
        if copy.delivery_state != "stored":  # picked up by the member's machine
            copy.acked_at = copy.created_at = identity.now()
        session.add(copy)
        copies.append(copy)
    post.recipients_m = len(copies)
    session.flush()
    return post, copies


def make_capable(session, machine, version="0.6.1"):
    """A machine on a groups client that has polled with ``groups=1``."""
    from raincli_server import identity
    from raincli_server.models import AgentPresence

    session.get(AgentPresence, machine.id).client_version = version
    machine.groups_capable_at = identity.now()
    session.flush()


@pytest.fixture
def groups_seed(session, v06_seed):  # noqa: F811
    s = v06_seed
    people, machines, nw = s["people"], s["machines"], s["teams"]["northwind"]
    _inbox(session, people["Maya"], nw, machines["maya"], "zq-agent-maya-laptop")
    for key in ("maya", "jon", "priya"):
        make_capable(session, machines[key])
    maya, jon, priya, sam, ryan = (people[n] for n in ("Maya", "Jon", "Priya", "Sam", "Ryan"))
    checkout = make_group(session, nw, "checkout", [maya, jon, priya, ryan])
    infra = make_group(session, nw, "infra", [sam, maya])
    ask, _ = make_post(session, checkout, machines["maya"], "zq-agent-maya-laptop",
                       "@Jon @Priya the orders API renamed `total` to `total_cents`. Check your side.",
                       mentions=[jon, priya], states={jon.id: "replied", priya.id: "received"})
    jon_reply, _ = make_post(session, checkout, machines["jon"], "zq-agent-jon-api",
                             "Done on my side: the client reads `total_cents`.", in_reply_to=ask)
    priya_reply, _ = make_post(session, checkout, machines["priya"], "zq-agent-priya-web",
                               "Updated the web totals too.", in_reply_to=ask)
    session.commit()
    return {**s, "groups": {"checkout": checkout, "infra": infra},
            "posts": {"ask": ask, "jon_reply": jon_reply, "priya_reply": priya_reply}}
