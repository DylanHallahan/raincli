"""Person routing (protocol §21 C2-C4, C6, C8; C13 H1-H4, M1, M2, M4, M5, L1, L3, L4), through the API, on the shared
v0.6 seed (tests/conftest_v06.py)."""

from __future__ import annotations

import uuid

from sqlalchemy import select, update

from api_helpers import auth
from conftest_v06 import v06_seed  # noqa: F401  (fixture)
from raincli_server import identity
from raincli_server.models import Agent, MachineAgent, Message, PersonInbox


def machine_send(client, token, to, body="hello", **extra):
    return client.post("/api/v1/messages", headers=auth(token),
                       json={"id": str(uuid.uuid4()), **({"to": to} if to is not None else {}), "body": body, **extra})


def person_send(client, token, to, body="hello", **extra):
    team = {} if "in_reply_to" in extra else {"team": "northwind"}  # Maya is in two teams; a reply takes its parent's
    return client.post("/api/v1/person/send", headers=auth(token),
                       json={"id": str(uuid.uuid4()), **({"to": to} if to is not None else {}), "body": body,
                             **team, **extra})


def stored(session, message_id):
    session.expire_all()
    return session.get(Message, uuid.UUID(message_id))


def poll(client, token, after=0):
    r = client.get("/api/v1/inbox", headers=auth(token), params={"routing": 1, "after": after})
    assert r.status_code == 200, r.text
    return r.json()


# C2: addressing --------------------------------------------------------------------------------------------------

def test_a_person_by_email_or_unique_name(client, session, v06_seed):  # noqa: F811
    t = v06_seed["machine_tokens"]["maya"]
    for to in ("jon@example.test", "@jon@example.test", "Jon", {"person": "jon@example.test"}, {"person_name": "jon"}):
        r = machine_send(client, t, to)
        assert r.status_code == 201, (to, r.text)
        client.post(f"/api/v1/messages/{r.json()['message']['id']}/ack", headers=auth(v06_seed["machine_tokens"]["jon"]))
        msg = stored(session, r.json()["message"]["id"])
        assert (msg.recipient_agent_id, msg.recipient_agent_name, msg.route) == (
            v06_seed["machines"]["jon"].id, "zq-agent-jon-api", "inbox"), to
        assert msg.conversation_id == v06_seed["conversations"]["jon"].id  # the Maya-Jon pair, always


def test_machine_and_agent_forms_are_refused_naming_people_only(client, v06_seed):  # noqa: F811
    t = v06_seed["machine_tokens"]["maya"]
    for to in ("zq-handle-jon", "zq-handle-jon/zq-agent-jon-api", {"machine": "zq-handle-jon"},
               {"machine": "zq-handle-jon", "agent": "zq-agent-jon-api"}):
        r = machine_send(client, t, to)
        assert r.status_code == 400 and r.json()["error"]["code"] == "address_people", (to, r.text)
        assert r.json()["error"]["message"] == "Address people: send to Jon (jon@example.test)"
    # Your own handle and an unknown one are indistinguishable.
    answers = {machine_send(client, t, to).text.replace("\n", "") for to in (
        "zq-handle-maya", "zq-handle-nobody", {"machine": "zq-handle-maya", "agent": "zq-agent-maya-notes"},
        {"machine": "zq-handle-nobody", "agent": "x"}, "Nobody")}
    assert len(answers) == 1 and "address_people" in answers.pop()


def test_an_ambiguous_name_lists_the_people(client, session, v06_seed):  # noqa: F811
    other = identity.create_user(session, "jon.two@example.test", "Jon", "correct horse battery")
    identity.add_member(session, v06_seed["teams"]["northwind"], other)
    session.commit()
    r = person_send(client, v06_seed["person_tokens"]["Maya"], "Jon")
    assert r.status_code == 400 and r.json()["error"]["code"] == "ambiguous_person"
    assert {p["email"] for p in r.json()["people"]} == {"jon@example.test", "jon.two@example.test"}


# C3: resolution --------------------------------------------------------------------------------------------------

def test_the_inbox_mode_path_and_the_named_path(client, session, v06_seed):  # noqa: F811
    m, t = v06_seed["machines"], v06_seed["machine_tokens"]["maya"]
    named = stored(session, machine_send(client, t, "olive@example.test").json()["message"]["id"])
    assert (named.recipient_agent_id, named.recipient_agent_name) == (m["olive"].id, "zq-agent-olive-ops")
    # Olive's machine publishes her picked agent as its own inbox: today's inbox path (the machine endpoint).
    session.execute(update(MachineAgent).where(MachineAgent.agent_id == m["olive"].id).values(role="inbox"))
    session.commit()
    inbox_mode = stored(session, machine_send(client, t, "olive@example.test").json()["message"]["id"])
    assert (inbox_mode.recipient_agent_id, inbox_mode.recipient_agent_name, inbox_mode.route) == (
        m["olive"].id, None, "inbox")
    # So does a machine that accepts only its inbox.
    session.execute(update(MachineAgent).where(MachineAgent.agent_id == m["olive"].id).values(role=None))
    session.execute(update(Agent).where(Agent.id == m["olive"].id).values(routing="inbox-only"))
    session.commit()
    only = stored(session, machine_send(client, t, "olive@example.test").json()["message"]["id"])
    assert (only.recipient_agent_id, only.recipient_agent_name) == (m["olive"].id, None)


def test_no_inbox_waits_in_the_app_and_the_sender_sees_no_inbox(client, session, v06_seed):  # noqa: F811
    r = machine_send(client, v06_seed["machine_tokens"]["maya"], "sam@example.test")
    assert r.status_code == 201
    msg = stored(session, r.json()["message"]["id"])
    assert (msg.recipient_user_id, msg.recipient_agent_id, msg.route) == (v06_seed["people"]["Sam"].id, None, "person")
    shown = r.json()["message"]
    assert (shown["delivery_state"], shown["hold_reason"]) == ("held", "no_inbox")
    assert shown["to_endpoint"] == {"person": "sam@example.test", "display_name": "Sam"}


# H4: no read receipt ---------------------------------------------------------------------------------------------

def test_a_machines_pickup_is_shown_to_the_sender(client, session, v06_seed):  # noqa: F811
    """§21.6: only messages to a person (recipient_user_id) hide acked_at. One delivered to the person's inbox
    machine shows its pickup to the sender, as the trail's "received" step does."""
    maya = v06_seed["person_tokens"]["Maya"]
    r = person_send(client, maya, "jon@example.test", "for Jon's inbox")
    mid = r.json()["message"]["id"]
    assert stored(session, mid).recipient_agent_id == v06_seed["machines"]["jon"].id  # inbox-routed
    assert client.get(f"/api/v1/person/messages/{mid}", headers=auth(maya)).json()["message"]["acked_at"] is None
    picked = client.post(f"/api/v1/messages/{mid}/ack", headers=auth(v06_seed["machine_tokens"]["jon"]))
    assert picked.status_code == 200
    shown = client.get(f"/api/v1/person/messages/{mid}", headers=auth(maya)).json()["message"]
    assert shown["acked_at"] == picked.json()["message"]["acked_at"] and shown["delivery_state"] == "received"


def test_viewing_in_the_app_changes_nothing_the_sender_can_fetch(client, session, v06_seed):  # noqa: F811
    r = person_send(client, v06_seed["person_tokens"]["Maya"], "sam@example.test", "read me")
    mid = r.json()["message"]["id"]
    sam = v06_seed["person_tokens"]["Sam"]
    before = client.get(f"/api/v1/person/messages/{mid}", headers=auth(v06_seed["person_tokens"]["Maya"])).json()
    assert client.post(f"/api/v1/person/messages/{mid}/ack", headers=auth(sam)).status_code == 200
    after = client.get(f"/api/v1/person/messages/{mid}", headers=auth(v06_seed["person_tokens"]["Maya"])).json()
    keep = ("delivery_state", "hold_reason", "acked_at")
    assert {k: before["message"][k] for k in keep} == {k: after["message"][k] for k in keep}
    assert after["message"]["acked_at"] is None
    # Sam sees his own read state.
    own = client.get(f"/api/v1/person/messages/{mid}", headers=auth(sam)).json()["message"]
    assert own["acked_at"] is not None


# C4 and C13 M4: replies ------------------------------------------------------------------------------------------

def _ask(client, v06_seed, to="jon@example.test", from_agent="zq-agent-maya-laptop"):
    r = machine_send(client, v06_seed["machine_tokens"]["maya"], to, "a question", from_agent=from_agent)
    assert r.status_code == 201, r.text
    return r.json()["message"]["id"]


def test_a_reply_goes_back_to_the_asking_agent_while_it_is_live(client, session, v06_seed):  # noqa: F811
    ask = _ask(client, v06_seed)
    r = machine_send(client, v06_seed["machine_tokens"]["jon"], None, "the answer", in_reply_to=ask)
    assert r.status_code == 201, r.text
    reply = stored(session, r.json()["message"]["id"])
    assert (reply.recipient_agent_id, reply.recipient_agent_name, reply.route) == (
        v06_seed["machines"]["maya"].id, "zq-agent-maya-laptop", "reply")
    # The replier sees a person, never the agent or machine.
    shown = r.json()["message"]
    assert shown["to_endpoint"] == {"person": "maya@example.test", "display_name": "Maya"} and shown["to"] == "@maya@example.test"
    assert stored(session, ask).delivery_state == "replied"


def test_a_reply_falls_back_to_the_senders_inbox_when_the_agent_is_gone(client, session, v06_seed):  # noqa: F811
    maya = v06_seed["people"]["Maya"]
    session.add(PersonInbox(user_id=maya.id, team_id=v06_seed["teams"]["northwind"].id,
                            agent_id=v06_seed["machines"]["maya"].id, agent_name="zq-agent-maya-notes", set_via="app"))
    session.commit()
    ask = _ask(client, v06_seed, from_agent="zq-agent-gone")  # stated, never reported live
    r = machine_send(client, v06_seed["machine_tokens"]["jon"], {"person": "maya@example.test"}, "answer",
                     in_reply_to=ask)
    reply = stored(session, r.json()["message"]["id"])
    assert (reply.recipient_agent_name, reply.route) == ("zq-agent-maya-notes", "inbox")


def test_a_reply_to_a_person_and_to_a_machine_with_no_name(client, session, v06_seed):  # noqa: F811
    by_hand = person_send(client, v06_seed["person_tokens"]["Maya"], "jon@example.test", "from the app")
    reply = machine_send(client, v06_seed["machine_tokens"]["jon"], None, "re", in_reply_to=by_hand.json()["message"]["id"])
    assert (stored(session, reply.json()["message"]["id"]).recipient_user_id) == v06_seed["people"]["Maya"].id  # no inbox
    plain = _ask(client, v06_seed, from_agent=None)
    reply = machine_send(client, v06_seed["machine_tokens"]["jon"], None, "re", in_reply_to=plain)
    msg = stored(session, reply.json()["message"]["id"])
    # Maya's machine is in machine mode (no inbox path), so the reply goes to Maya: no inbox, so her app (M1 review).
    assert (msg.recipient_user_id, msg.route) == (v06_seed["people"]["Maya"].id, "person")


def test_reply_edge_cases(client, session, v06_seed):  # noqa: F811
    ask = _ask(client, v06_seed)
    # The replier is the parent's own sender: it goes to the parent's recipient person, now.
    again = machine_send(client, v06_seed["machine_tokens"]["maya"], None, "and also", in_reply_to=ask)
    msg = stored(session, again.json()["message"]["id"])
    assert (msg.recipient_agent_id, msg.recipient_agent_name) == (v06_seed["machines"]["jon"].id, "zq-agent-jon-api")
    # A reply naming someone else is refused.
    wrong = machine_send(client, v06_seed["machine_tokens"]["jon"], "priya@example.test", "x", in_reply_to=ask)
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "invalid"
    # An old client naming the sender's machine is tolerated (that machine's person).
    old = machine_send(client, v06_seed["machine_tokens"]["jon"], "zq-handle-maya", "x", in_reply_to=ask)
    assert old.status_code == 201, old.text
    # The sending machine revoked, the person still a member: their inbox (none: the app).
    identity.revoke_agent(session, session.get(Agent, v06_seed["machines"]["maya"].id))
    session.commit()
    after_revoke = machine_send(client, v06_seed["machine_tokens"]["jon"], None, "x", in_reply_to=ask)
    assert stored(session, after_revoke.json()["message"]["id"]).recipient_user_id == v06_seed["people"]["Maya"].id
    # The sender person left the team: refused with the generic text.
    nw = v06_seed["teams"]["northwind"]
    identity.remove_member(session, nw, v06_seed["people"]["Maya"])
    session.commit()
    gone = machine_send(client, v06_seed["machine_tokens"]["jon"], None, "x", in_reply_to=ask)
    assert gone.status_code in (400, 404) and "zq-" not in gone.text


# H2: own traffic -------------------------------------------------------------------------------------------------

def test_an_escalation_and_its_answer_appear_unarchived_for_the_owner(client, session, v06_seed):  # noqa: F811
    t, p = v06_seed["machine_tokens"]["maya"], v06_seed["person_tokens"]["Maya"]
    esc = machine_send(client, t, {"person": "maya@example.test"}, "need your OK", kind="escalation")
    assert esc.status_code == 201, esc.text
    msg = stored(session, esc.json()["message"]["id"])
    assert msg.route == "self" and msg.conversation_id == v06_seed["conversations"]["self"].id
    answer = person_send(client, p, None, "OK", in_reply_to=str(msg.id))
    assert answer.status_code == 201, answer.text
    back = stored(session, answer.json()["message"]["id"])
    assert (back.recipient_agent_id, back.route) == (v06_seed["machines"]["maya"].id, "self")
    rows = client.get("/api/v1/person/conversations", headers=auth(p)).json()["conversations"]
    own = next(r for r in rows if r["id"] == str(v06_seed["conversations"]["self"].id))
    assert own["archived"] is False and own["peer"] == "From your agents"
    # A person may not write to themselves, nor a machine to its own machine.
    assert person_send(client, p, "maya@example.test").status_code == 400
    assert machine_send(client, t, "zq-handle-maya").json()["error"]["code"] == "address_people"


# C6, C8, C13 H1/M5/L1/L4: the inbox setting ----------------------------------------------------------------------

def test_picking_an_inbox_delivers_the_waiting_messages_but_not_answered_ones(client, session, v06_seed):  # noqa: F811
    sam_machine_token = v06_seed["machine_tokens"]["sam"]
    jon = v06_seed["machine_tokens"]["jon"]
    first = machine_send(client, jon, "sam@example.test", "one").json()["message"]["id"]
    answered = machine_send(client, jon, "sam@example.test", "two").json()["message"]["id"]
    person_send(client, v06_seed["person_tokens"]["Sam"], None, "answered in the app", in_reply_to=answered)
    third = machine_send(client, jon, "sam@example.test", "three").json()["message"]["id"]
    # "Answered" is any later message from Sam to Jon in the pair: the first two count as answered.
    session.add(MachineAgent(agent_id=v06_seed["machines"]["sam"].id, key="herdr:zq-agent-sam", name="zq-agent-sam",
                             type="claude", status="idle", reachability="instant", source="herdr",
                             seen_at=identity.now()))
    session.commit()
    me = client.get("/api/v1/me/inbox", headers=auth(sam_machine_token)).json()
    assert me["inbox"] is None and me["waiting"] == 2  # "three", and the seed's "Hello Sam" from Maya
    assert me["candidates"] == [{"machine": "zq-handle-sam-app", "agent": "zq-agent-sam", "live": True,
                                 "suggested": False}]
    cursor = poll(client, sam_machine_token)["cursor"]
    put = client.put("/api/v1/me/inbox", headers=auth(sam_machine_token), json={"agent": "zq-agent-sam"})
    assert put.status_code == 200, put.text
    assert (put.json()["delivered_waiting"], put.json()["moved"]) == (2, 0)
    moved = stored(session, third)
    assert (moved.recipient_agent_name, moved.route, moved.redeliver) == ("zq-agent-sam", "inbox", True)
    assert stored(session, first).recipient_user_id is not None  # answered: stays in the app
    # H1: the poll returns it below the cursor, once, and the cursor never goes back. The cursor is the team's
    # newest number (§21.13: one above anything the team issued would be another team's, answered from the start).
    high = max(cursor, stored(session, third).seq)
    page = poll(client, sam_machine_token, after=high)
    hello = str(v06_seed["messages"]["to_sam"].id)
    assert [m["id"] for m in page["messages"]] == [hello, third] and page["cursor"] == high
    for mid in (hello, third):
        assert client.post(f"/api/v1/messages/{mid}/ack", headers=auth(sam_machine_token)).status_code == 200
    assert poll(client, sam_machine_token, after=high)["messages"] == []
    # The same inbox again moves nothing.
    again = client.put("/api/v1/me/inbox", headers=auth(sam_machine_token), json={"agent": "zq-agent-sam"}).json()
    assert (again["moved"], again["stayed"], again["delivered_waiting"]) == (0, 0, 0)


def test_changing_the_inbox_moves_unacked_and_keeps_acked(client, session, v06_seed):  # noqa: F811
    maya_t, jon_t = v06_seed["machine_tokens"]["maya"], v06_seed["machine_tokens"]["jon"]
    acked = machine_send(client, maya_t, "jon@example.test", "picked up").json()["message"]["id"]
    assert client.post(f"/api/v1/messages/{acked}/ack", headers=auth(jon_t)).status_code == 200
    unacked = machine_send(client, maya_t, "jon@example.test", "still waiting").json()["message"]["id"]
    session.add(MachineAgent(agent_id=v06_seed["machines"]["jon"].id, key="herdr:zq-agent-jon-two",
                             name="zq-agent-jon-two", type="claude", status="idle", reachability="instant",
                             source="herdr", seen_at=identity.now()))
    session.commit()
    r = client.put("/api/v1/me/inbox", headers=auth(jon_t), json={"agent": "zq-agent-jon-two"})
    assert (r.json()["moved"], r.json()["stayed"]) == (1, 1), r.text
    assert stored(session, unacked).recipient_agent_name == "zq-agent-jon-two"
    assert stored(session, acked).recipient_agent_name == "zq-agent-jon-api"
    # L4: a machine credential may pick only an agent on the calling machine.
    other = client.put("/api/v1/me/inbox", headers=auth(jon_t), json={"machine": "zq-handle-maya", "agent": "x"})
    assert other.status_code == 400 and other.json()["error"]["code"] == "inbox_invalid"
    # Removing it: unacked messages go back to waiting in the app.
    later = machine_send(client, maya_t, "jon@example.test", "after").json()["message"]["id"]
    assert client.put("/api/v1/me/inbox", headers=auth(jon_t), json={"none": True}).json()["moved"] == 2
    assert stored(session, later).recipient_user_id == v06_seed["people"]["Jon"].id


def test_an_ack_from_the_previous_recipient_is_a_no_op(client, session, v06_seed):  # noqa: F811
    maya_t, jon_t = v06_seed["machine_tokens"]["maya"], v06_seed["machine_tokens"]["jon"]
    mid = machine_send(client, maya_t, "jon@example.test", "x").json()["message"]["id"]
    assert client.put("/api/v1/me/inbox", headers=auth(jon_t), json={"none": True}).status_code == 200
    r = client.post(f"/api/v1/messages/{mid}/ack", headers=auth(jon_t))
    assert r.status_code == 200 and r.json()["acked"] is False


def test_revoking_the_inbox_machine_sends_its_messages_back_to_the_app(client, session, v06_seed):  # noqa: F811
    mid = machine_send(client, v06_seed["machine_tokens"]["maya"], "priya@example.test", "x").json()["message"]["id"]
    identity.revoke_agent(session, session.get(Agent, v06_seed["machines"]["priya"].id))
    session.commit()
    msg = stored(session, mid)
    assert (msg.recipient_user_id, msg.route, msg.redeliver) == (v06_seed["people"]["Priya"].id, "person", True)
    assert session.scalars(select(PersonInbox).where(PersonInbox.user_id == v06_seed["people"]["Priya"].id)).first() is None


def test_one_inbox_per_team(client, session, v06_seed):  # noqa: F811
    """H3: Maya picks in contoso; northwind still has none for her."""
    session.add(MachineAgent(agent_id=v06_seed["machines"]["maya_ct"].id, key="herdr:zq-agent-ct", name="zq-agent-ct",
                             type="claude", status="idle", reachability="instant", source="herdr",
                             seen_at=identity.now()))
    session.commit()
    r = client.put("/api/v1/me/inbox", headers=auth(v06_seed["machine_tokens"]["maya_ct"]), json={"agent": "zq-agent-ct"})
    assert r.status_code == 200, r.text
    person = client.get("/api/v1/person/me/inbox", headers=auth(v06_seed["person_tokens"]["Maya"]),
                        params={"team": "northwind"}).json()
    assert person["inbox"] is None
    lee = machine_send(client, v06_seed["machine_tokens"]["lee"], "maya@example.test", "contoso hi")
    assert stored(session, lee.json()["message"]["id"]).recipient_agent_name == "zq-agent-ct"


# C5.5, C13 M1/M2 ------------------------------------------------------------------------------------------------

def test_event_details_are_codes_only(client, session, v06_seed):  # noqa: F811
    mid = machine_send(client, v06_seed["machine_tokens"]["maya"], "jon@example.test").json()["message"]["id"]
    jon = v06_seed["machine_tokens"]["jon"]
    client.post(f"/api/v1/messages/{mid}/ack", headers=auth(jon))
    bad = client.post(f"/api/v1/messages/{mid}/events", headers=auth(jon), json={"state": "held", "detail": "zq free text"})
    assert bad.status_code == 400
    ok = client.post(f"/api/v1/messages/{mid}/events", headers=auth(jon), json={"state": "held", "detail": "busy"})
    assert ok.status_code == 200
    # The sender sees a coarse code; the recipient machine's owner sees the raw one.
    sender_view = client.get(f"/api/v1/messages/{mid}", headers=auth(v06_seed["machine_tokens"]["maya"])).json()
    owner_view = client.get(f"/api/v1/messages/{mid}", headers=auth(jon)).json()
    assert (sender_view["message"]["hold_reason"], owner_view["message"]["hold_reason"]) == ("inbox_busy", "busy")


def test_agents_lists_only_your_own_machines_and_people_lists_people(client, v06_seed):  # noqa: F811
    t = v06_seed["machine_tokens"]["maya"]
    agents = client.get("/api/v1/agents", headers=auth(t)).json()["agents"]
    assert [a["handle"] for a in agents] == ["zq-handle-maya"]
    people = client.get("/api/v1/people", headers=auth(t)).json()["people"]
    shown = {p["display_name"]: (p["presence"], p["speed"]) for p in people}
    assert shown["Jon"] == ("ready", "instant") and shown["Sam"] == ("no_inbox", None)
    assert shown["Priya"] == ("busy", "next_turn") and shown["Ryan"] == ("offline", None)
    assert "Lee" not in shown


def test_a_machine_reads_and_archives_its_owners_conversations(client, session, v06_seed):  # noqa: F811
    t = v06_seed["machine_tokens"]["maya"]
    conv = str(v06_seed["conversations"]["jon"].id)
    rows = client.get("/api/v1/conversations", headers=auth(t)).json()["conversations"]
    assert conv in {r["id"] for r in rows}
    msgs = client.get(f"/api/v1/conversations/{conv}/messages", headers=auth(t)).json()["messages"]
    assert {m["body"] for m in msgs} >= {"Written by Jon in the app.", "Can you run the report?"}
    assert client.post(f"/api/v1/conversations/{conv}/archive", headers=auth(t)).status_code == 200
    # send --conversation must be the sender's pair with that person.
    ok = machine_send(client, t, "jon@example.test", "in the pair", conversation_id=conv)
    assert ok.status_code == 201, ok.text
    wrong = machine_send(client, t, "priya@example.test", "not that pair", conversation_id=conv)
    assert wrong.status_code == 400


def test_the_apps_feed_sees_messages_routed_to_your_inbox(client, v06_seed):  # noqa: F811
    """PR6: with routed=1 the person feed also returns what reached Jon's inbox machine (acked or not), never
    what Jon or his machines sent."""
    jon_person, jon_machine = v06_seed["person_tokens"]["Jon"], v06_seed["machine_tokens"]["jon"]
    mid = person_send(client, v06_seed["person_tokens"]["Maya"], "jon@example.test", "by hand").json()["message"]["id"]
    assert client.post(f"/api/v1/messages/{mid}/ack", headers=auth(jon_machine)).status_code == 200
    plain = client.get("/api/v1/person/inbox", headers=auth(jon_person)).json()["messages"]
    assert mid not in {m["id"] for m in plain}
    routed = client.get("/api/v1/person/inbox", headers=auth(jon_person), params={"routed": 1}).json()["messages"]
    seen = {m["id"]: m for m in routed}
    assert seen[mid]["from_kind"] == "app" and seen[mid]["from_endpoint"]["person"] == "maya@example.test"
    own = machine_send(client, jon_machine, "maya@example.test", "from jon's agent").json()["message"]["id"]
    again = client.get("/api/v1/person/inbox", headers=auth(jon_person), params={"routed": 1}).json()["messages"]
    assert own not in {m["id"] for m in again}


# Build review H2, M1, L1 ---------------------------------------------------------------------------------------

def test_picking_an_inbox_delivers_read_and_unread_waiting_messages_alike(client, session, v06_seed):  # noqa: F811
    """H2: reading a waiting message in the app changes nothing the sender sees, before or after an inbox is picked;
    read and unread waiting messages are both delivered to the new inbox, and stay read in the person's app."""
    jon, sam_p, sam_m = v06_seed["machine_tokens"]["jon"], v06_seed["person_tokens"]["Sam"], v06_seed["machine_tokens"]["sam"]
    read = machine_send(client, jon, "sam@example.test", "one").json()["message"]["id"]
    unread = machine_send(client, jon, "sam@example.test", "two").json()["message"]["id"]
    assert client.post(f"/api/v1/person/messages/{read}/ack", headers=auth(sam_p)).status_code == 200

    def jon_view(mid):
        m = client.get(f"/api/v1/messages/{mid}", headers=auth(jon)).json()["message"]
        return {k: v for k, v in m.items() if k not in ("id", "body", "seq", "created_at", "delivery_updated_at")}
    assert jon_view(read) == jon_view(unread)
    session.add(MachineAgent(agent_id=v06_seed["machines"]["sam"].id, key="herdr:zq-agent-sam", name="zq-agent-sam",
                             type="claude", status="idle", reachability="instant", source="herdr",
                             seen_at=identity.now()))
    session.commit()
    put = client.put("/api/v1/me/inbox", headers=auth(sam_m), json={"agent": "zq-agent-sam"}).json()
    assert put["delivered_waiting"] == 3  # both of Jon's, and the seed's "Hello Sam" from Maya
    for mid in (read, unread):
        msg = stored(session, mid)
        assert (msg.recipient_agent_name, msg.route, msg.redeliver, msg.acked_at) == ("zq-agent-sam", "inbox", True, None)
    assert jon_view(read) == jon_view(unread)
    # The inbox fetches both; Sam's app keeps "one" read and "two" unread.
    fetched = {m["id"] for m in poll(client, sam_m, after=10_000)["messages"]}
    assert {read, unread} <= fetched
    assert stored(session, read).person_seen_at is not None and stored(session, unread).person_seen_at is None


def test_a_reply_to_a_machine_mode_sender_with_no_name_goes_to_the_person(client, session, v06_seed):  # noqa: F811
    """M1: a machine with no inbox path (machine mode: no role=inbox agent, not inbox-only) never takes
    machine-endpoint messages, so a reply to its unnamed message goes to the person's inbox, else their app."""
    ask = machine_send(client, v06_seed["machine_tokens"]["maya"], "jon@example.test", "no name").json()["message"]["id"]
    reply = machine_send(client, v06_seed["machine_tokens"]["jon"], None, "answer", in_reply_to=ask)
    msg = stored(session, reply.json()["message"]["id"])
    assert (msg.recipient_user_id, msg.recipient_agent_id, msg.route) == (v06_seed["people"]["Maya"].id, None, "person")
    # With an inbox, it goes there.
    session.add(PersonInbox(user_id=v06_seed["people"]["Maya"].id, team_id=v06_seed["teams"]["northwind"].id,
                            agent_id=v06_seed["machines"]["maya"].id, agent_name="zq-agent-maya-notes", set_via="app"))
    session.commit()
    reply2 = machine_send(client, v06_seed["machine_tokens"]["jon"], None, "answer 2", in_reply_to=ask)
    msg2 = stored(session, reply2.json()["message"]["id"])
    assert (msg2.recipient_agent_name, msg2.route) == ("zq-agent-maya-notes", "inbox")
    # A machine with an inbox path (a reported inbox agent) still gets the reply itself.
    session.add(MachineAgent(agent_id=v06_seed["machines"]["maya"].id, key="herdr:inbox", name="inbox", type="claude",
                             status="idle", role="inbox", reachability="instant", source="herdr", seen_at=identity.now()))
    session.commit()
    reply3 = machine_send(client, v06_seed["machine_tokens"]["jon"], None, "answer 3", in_reply_to=ask)
    msg3 = stored(session, reply3.json()["message"]["id"])
    assert (msg3.recipient_agent_id, msg3.recipient_agent_name, msg3.route) == (v06_seed["machines"]["maya"].id, None, "reply")


def test_stayed_counts_only_messages_still_in_progress(client, session, v06_seed):  # noqa: F811
    """L1: "N messages are with your previous inbox" counts picked-up messages still received or held."""
    maya_t, jon_t = v06_seed["machine_tokens"]["maya"], v06_seed["machine_tokens"]["jon"]
    done, busy = (machine_send(client, maya_t, "jon@example.test", b).json()["message"]["id"] for b in ("done", "busy"))
    for mid in (done, busy):
        assert client.post(f"/api/v1/messages/{mid}/ack", headers=auth(jon_t)).status_code == 200
    assert client.post(f"/api/v1/messages/{done}/events", headers=auth(jon_t),
                       json={"state": "submitted", "detail": "delivered"}).status_code == 200
    session.add(MachineAgent(agent_id=v06_seed["machines"]["jon"].id, key="herdr:zq-agent-jon-two",
                             name="zq-agent-jon-two", type="claude", status="idle", reachability="instant",
                             source="herdr", seen_at=identity.now()))
    session.commit()
    r = client.put("/api/v1/me/inbox", headers=auth(jon_t), json={"agent": "zq-agent-jon-two"}).json()
    assert r["stayed"] == 1, r


def test_unread_counts_your_agents_escalations_and_routed_messages(client, session, v06_seed):  # noqa: F811
    """H2 helpers: an escalation from your own machine to you counts as unread until you see it; a message routed to
    your inbox machine counts too; what you or your machines sent never does."""
    from raincli_server import messaging

    maya = v06_seed["people"]["Maya"]
    esc = machine_send(client, v06_seed["machine_tokens"]["maya"], {"person": "maya@example.test"}, "need you",
                       kind="escalation").json()["message"]["id"]
    to_jon = machine_send(client, v06_seed["machine_tokens"]["maya"], "jon@example.test", "mine").json()["message"]["id"]

    def unread(user):
        session.expire_all()
        return set(map(str, session.scalars(select(Message.id).where(messaging.unread_for(user.id)))))
    assert esc in unread(maya) and to_jon not in unread(maya)
    assert messaging.is_unread_for(session, maya.id, stored(session, esc))
    assert to_jon in unread(v06_seed["people"]["Jon"])
    assert client.post(f"/api/v1/person/messages/{esc}/ack", headers=auth(v06_seed["person_tokens"]["Maya"])).status_code == 200
    assert esc not in unread(maya) and not messaging.is_unread_for(session, maya.id, stored(session, esc))


# R1: the capacity cap never depends on read state ------------------------------------------------------------------

def test_the_capacity_answer_is_the_same_before_and_after_the_person_reads(client, session, settings, v06_seed):  # noqa: F811
    """§21 C13 H4, PR5: a person's cap (inbox_full) counts the messages waiting for them that they haven't answered,
    never what they've read; answering frees it."""
    maya, sam = v06_seed["person_tokens"]["Maya"], v06_seed["person_tokens"]["Sam"]
    sent = []
    while True:
        r = person_send(client, maya, {"person": "sam@example.test"}, f"note {len(sent)}")
        if r.status_code == 429:
            break
        assert r.status_code == 201, r.text
        sent.append(r.json()["message"]["id"])
        assert len(sent) <= settings.max_pending
    full = (r.status_code, r.json())
    assert full[1]["error"]["code"] == "inbox_full"
    for mid in sent + [str(v06_seed["messages"]["to_sam"].id)]:
        assert client.post(f"/api/v1/person/messages/{mid}/ack", headers=auth(sam)).status_code == 200
    assert stored(session, sent[0]).person_seen_at is not None
    again = person_send(client, maya, {"person": "sam@example.test"}, "after he read them")
    assert (again.status_code, again.json()) == full  # reading changed nothing the sender can see
    answer = person_send(client, sam, None, "got them", in_reply_to=sent[-1])
    assert answer.status_code == 201, answer.text
    assert person_send(client, maya, {"person": "sam@example.test"}, "after he answered").status_code == 201
