"""The shared v0.6 seed (§21): people with inboxes, and distinctive machine handles and agent names, so the privacy
scans (tests/server/test_v06_privacy_api.py, tests/gui/test_v06_privacy_pages.py) can assert that none of them
reach a teammate. Use the ``v06_seed`` fixture; it needs the ``session`` fixture (real PostgreSQL).

People in team "northwind" (Olive is the team owner; Maya is the viewer and also in "contoso"):

| Person | Machines (handle: agents) | Inbox in northwind |
|---|---|---|
| Maya | zq-handle-maya: zq-agent-maya-laptop, zq-agent-maya-notes | none yet (first run) |
| Jon | zq-handle-jon: zq-agent-jon-api (instant, idle) | zq-agent-jon-api |
| Priya | zq-handle-priya: zq-agent-priya-web (next-turn, working) | zq-agent-priya-web |
| Sam | zq-handle-sam-app (no agents) | none |
| Ryan | zq-handle-ryan: zq-agent-ryan-old (seen 2 days ago) | zq-agent-ryan-old |
| Olive (owner) | zq-handle-olive: zq-agent-olive-ops | zq-agent-olive-ops |

In "contoso": Maya (zq-handle-maya-ct) and Lee (zq-handle-lee: zq-agent-lee-desk, his inbox there).

Messages (northwind): Maya's agent to Jon (to his inbox), Jon's agent's reply, a message Jon wrote in the app, one
from Maya in the app to Sam (no inbox), one from Maya's agent to Priya held "busy" with a raw event detail, and an
escalation from Maya's machine to Maya (her "From your agents" conversation).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

# Every teammate's handle and agent name, as Maya (a member) must never see them.
TEAMMATE_STRINGS = (
    "zq-handle-jon", "zq-agent-jon-api", "zq-handle-priya", "zq-agent-priya-web", "zq-handle-sam-app",
    "zq-handle-ryan", "zq-agent-ryan-old", "zq-handle-olive", "zq-agent-olive-ops", "zq-handle-lee",
    "zq-agent-lee-desk",
)
# Maya's own: shown to her only on her own pages (This computer, website Machines, owner warnings, /me, the inbox
# setting), never in conversations, lists, headers or message JSON labels.
OWN_STRINGS = ("zq-handle-maya", "zq-agent-maya-laptop", "zq-agent-maya-notes", "zq-handle-maya-ct")
PASSWORD = "correct horse battery"


def _machine(session, team, owner, handle, agents=(), *, seen_ago=0, status="ready"):
    """A machine with a fresh (or ``seen_ago`` seconds old) presence report and its agents' directory rows."""
    from raincli_server import identity
    from raincli_server.models import AgentPresence, KnownAgent, MachineAgent

    machine, token = identity.register_agent(session, team, owner, handle)
    seen = identity.now() - timedelta(seconds=seen_ago)
    session.add(AgentPresence(agent_id=machine.id, status=status, seen_at=seen, client_version="0.5.6",
                              update_mode="automatic", update_state="current"))
    machine.routing_capable_at = seen
    for name, reachability, agent_status in agents:
        session.add(MachineAgent(agent_id=machine.id, key=f"herdr:{name}", name=name, type="claude",
                                 status=agent_status, reachability=reachability, source="herdr", seen_at=seen))
        session.add(KnownAgent(agent_id=machine.id, name=name, type="claude", reachability=reachability,
                               last_seen_at=seen))
    session.flush()
    return machine, token


def _inbox(session, user, team, machine, name):
    from raincli_server.models import PersonInbox

    session.add(PersonInbox(user_id=user.id, team_id=team.id, agent_id=machine.id, agent_name=name, set_via="app"))


def _pair(session, team, x, y, kind="pair"):
    from raincli_server.models import Conversation

    if kind == "self":
        key = f"p:{x.id}"
        conv = Conversation(id=uuid.uuid4(), team_id=team.id, a_user_id=x.id, b_user_id=x.id, a_key=key,
                            b_key=f"self:{x.id}", kind="self", is_default=True)
    else:
        lo, hi = sorted((x, y), key=lambda u: f"p:{u.id}")
        conv = Conversation(id=uuid.uuid4(), team_id=team.id, a_user_id=lo.id, b_user_id=hi.id, a_key=f"p:{lo.id}",
                            b_key=f"p:{hi.id}", kind="pair", is_default=True)
    session.add(conv)
    session.flush()
    return conv


def _message(session, team, conv, *, body, route, sender_machine=None, sender_name=None, sender_user=None,
             recipient_machine=None, recipient_name=None, recipient_user=None, state="stored", kind="message",
             in_reply_to=None):
    from raincli_server.models import Message

    msg = Message(id=uuid.uuid4(), team_id=team.id, conversation_id=conv.id, body=body, kind=kind, route=route,
                  sender_agent_id=sender_machine.id if sender_machine else None, sender_agent_name=sender_name,
                  sender_user_id=sender_user.id if sender_user else None,
                  recipient_agent_id=recipient_machine.id if recipient_machine else None,
                  recipient_agent_name=recipient_name, recipient_user_id=recipient_user.id if recipient_user else None,
                  delivery_state=state, in_reply_to=in_reply_to)
    session.add(msg)
    session.flush()
    return msg


@pytest.fixture
def v06_seed(session):
    from raincli_server import identity
    from raincli_server.models import DeliveryEvent

    people = {name: identity.create_user(session, f"{name.lower()}@example.test", name, PASSWORD)
              for name in ("Olive", "Maya", "Jon", "Priya", "Sam", "Ryan", "Lee")}
    nw = identity.create_team(session, "northwind", "Northwind", people["Olive"])
    for name in ("Maya", "Jon", "Priya", "Sam", "Ryan"):
        identity.add_member(session, nw, people[name])
    ct = identity.create_team(session, "contoso", "Contoso", people["Lee"])
    identity.add_member(session, ct, people["Maya"])

    machines, tokens = {}, {}
    for key, team, owner, handle, agents, extra in (
        ("maya", nw, "Maya", "zq-handle-maya", [("zq-agent-maya-laptop", "instant", "idle"),
                                                ("zq-agent-maya-notes", "next-turn", "idle")], {}),
        ("jon", nw, "Jon", "zq-handle-jon", [("zq-agent-jon-api", "instant", "idle")], {}),
        ("priya", nw, "Priya", "zq-handle-priya", [("zq-agent-priya-web", "next-turn", "working")],
         {"status": "busy"}),
        ("sam", nw, "Sam", "zq-handle-sam-app", [], {}),
        ("ryan", nw, "Ryan", "zq-handle-ryan", [("zq-agent-ryan-old", "instant", "idle")],
         {"seen_ago": 2 * 86400}),
        ("olive", nw, "Olive", "zq-handle-olive", [("zq-agent-olive-ops", "instant", "idle")], {}),
        ("maya_ct", ct, "Maya", "zq-handle-maya-ct", [], {}),
        ("lee", ct, "Lee", "zq-handle-lee", [("zq-agent-lee-desk", "instant", "idle")], {}),
    ):
        machines[key], tokens[key] = _machine(session, team, people[owner], handle, agents, **extra)
    _inbox(session, people["Jon"], nw, machines["jon"], "zq-agent-jon-api")
    _inbox(session, people["Priya"], nw, machines["priya"], "zq-agent-priya-web")
    _inbox(session, people["Ryan"], nw, machines["ryan"], "zq-agent-ryan-old")
    _inbox(session, people["Olive"], nw, machines["olive"], "zq-agent-olive-ops")
    _inbox(session, people["Lee"], ct, machines["lee"], "zq-agent-lee-desk")

    maya, jon, sam, priya = people["Maya"], people["Jon"], people["Sam"], people["Priya"]
    with_jon = _pair(session, nw, maya, jon)
    ask = _message(session, nw, with_jon, body="Can you run the report?", route="inbox",
                   sender_machine=machines["maya"], sender_name="zq-agent-maya-laptop",
                   recipient_machine=machines["jon"], recipient_name="zq-agent-jon-api", state="replied")
    reply = _message(session, nw, with_jon, body="Here is the report.", route="reply",
                     sender_machine=machines["jon"], sender_name="zq-agent-jon-api",
                     recipient_machine=machines["maya"], recipient_name="zq-agent-maya-laptop", in_reply_to=ask.id)
    by_hand = _message(session, nw, with_jon, body="Written by Jon in the app.", route="person",
                       sender_user=jon, recipient_user=maya)
    with_sam = _pair(session, nw, maya, sam)
    to_sam = _message(session, nw, with_sam, body="Hello Sam.", route="person", sender_user=maya, recipient_user=sam)
    with_priya = _pair(session, nw, maya, priya)
    to_priya = _message(session, nw, with_priya, body="Busy?", route="inbox", sender_machine=machines["maya"],
                        sender_name="zq-agent-maya-laptop", recipient_machine=machines["priya"],
                        recipient_name="zq-agent-priya-web", state="held")
    session.add(DeliveryEvent(message_id=to_priya.id, state="held", detail="busy",
                              reported_by=machines["priya"].id))
    session.add(DeliveryEvent(message_id=ask.id, state="received", detail="zq-agent-jon-api picked it up",
                              reported_by=machines["jon"].id))  # a pre-v0.6 free-text detail: never rendered
    own = _pair(session, nw, maya, maya, kind="self")
    escalation = _message(session, nw, own, body="Need your OK.", route="self", kind="escalation",
                          sender_machine=machines["maya"], recipient_user=maya)
    person_tokens = {name: identity.issue_person_session(session, people[name], machines[key])
                     for name, key in (("Maya", "maya"), ("Jon", "jon"), ("Olive", "olive"), ("Sam", "sam"))}
    session.commit()
    return {
        "people": people, "teams": {"northwind": nw, "contoso": ct}, "machines": machines,
        "machine_tokens": tokens, "person_tokens": person_tokens,
        "conversations": {"jon": with_jon, "sam": with_sam, "priya": with_priya, "self": own},
        "messages": {"ask": ask, "reply": reply, "by_hand": by_hand, "to_sam": to_sam, "to_priya": to_priya,
                     "escalation": escalation},
        "teammate_strings": TEAMMATE_STRINGS, "own_strings": OWN_STRINGS,
    }
