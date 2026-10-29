"""Group chats over the API (protocol §22: G2 the gate, G3 posting and replies, G4 withdrawal, G5 reading), on the
group seed (tests/conftest_groups.py). Real PostgreSQL."""

from __future__ import annotations

import uuid

import pytest

from api_helpers import auth
from conftest_groups import groups_seed, make_capable  # noqa: F401  (fixture)
from conftest_v06 import _machine, v06_seed  # noqa: F401  (fixture)
from raincli_server import groups, identity, messaging
from raincli_server.models import AgentPresence, GroupMember, GroupPost, Message

UNKNOWN = str(uuid.uuid4())


def post(client, token, group, body="hello group", **extra):
    return client.post(f"/api/v1/groups/{group}/posts", headers=auth(token),
                       json={"id": str(uuid.uuid4()), "body": body, **extra})


def poll(client, token, *, groups_flag=True, after=0):
    q = {"routing": 1, "after": after, "include_acked": "true"}
    if groups_flag:
        q["groups"] = 1
    r = client.get("/api/v1/inbox", headers=auth(token), params=q)
    assert r.status_code == 200, r.text
    return r.json()["messages"]


def copies(session, post_id):
    session.expire_all()
    return {c.recipient_agent_id: c for c in session.query(Message).filter_by(post_id=uuid.UUID(str(post_id)))}


def same(a, b):
    return (a.status_code, a.json()) == (b.status_code, b.json())


@pytest.fixture
def g(groups_seed):  # noqa: F811
    s = groups_seed
    return {**s, "t": s["machine_tokens"], "checkout": str(s["groups"]["checkout"].id),
            "infra": str(s["groups"]["infra"].id)}


# G3: posting and fan-out -----------------------------------------------------------------------------------------

def test_a_post_reaches_every_other_member_taking_part(client, session, g):
    m = g["machines"]
    r = post(client, g["t"]["maya"], g["checkout"], "@Jon is /orders still paginated?", from_agent="zq-agent-maya-laptop")
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"post", "duplicate"} and body["duplicate"] is False
    p = body["post"]
    assert p["group"] == {"id": g["checkout"], "name": "checkout"} and p["recipients_m"] == 2
    assert p["mentions"] == [{"display_name": "Jon", "email": "jon@example.test"}]
    assert "copies" not in r.text and str(m["jon"].id) not in r.text  # never copy ids (M6) or machines
    made = copies(session, p["id"])
    assert set(made) == {m["jon"].id, m["priya"].id}  # not Maya (the poster), not Ryan (old client)
    assert {c.recipient_agent_name for c in made.values()} == {"zq-agent-jon-api", "zq-agent-priya-web"}
    jon = [x for x in poll(client, g["t"]["jon"]) if x.get("post_id") == p["id"]]
    assert len(jon) == 1
    copy = jon[0]
    assert copy["group"] == {"id": g["checkout"], "name": "checkout", "members_n": 4}
    assert copy["mentioned"] is True and copy["mentions"] == p["mentions"] and copy["from_kind"] == "agent"
    assert copy["from_endpoint"] == {"person": "maya@example.test", "display_name": "Maya"}
    assert copy["to_endpoint"] == {"machine": "zq-handle-jon", "agent": "zq-agent-jon-api"}  # its own endpoint
    priya = next(x for x in poll(client, g["t"]["priya"]) if x.get("post_id") == p["id"])
    assert priya["mentioned"] is False


def test_who_may_post(client, session, g):
    """L6: outside the team or not a member is the unknown answer; then a member's non-inbox machine is 403."""
    t, nw = g["t"], g["teams"]["northwind"]
    unknown = post(client, t["maya"], UNKNOWN)
    assert unknown.status_code == 404
    assert same(post(client, t["olive"], g["checkout"]), unknown)  # the team owner, not a member
    assert same(post(client, t["maya_ct"], g["checkout"]), unknown)  # Maya's machine in another team
    assert same(post(client, t["lee"], g["checkout"]), unknown)
    assert same(post(client, t["maya"], g["conversations"]["jon"].id), unknown)  # a 1:1 conversation isn't a group
    _second, second_token = _machine(session, nw, g["people"]["Maya"], "zq-handle-maya-2")
    session.commit()
    refused = post(client, second_token, g["checkout"])
    assert refused.status_code == 403 and refused.json()["error"] == {
        "code": "group_inbox_only", "message": "Post from the machine that holds your inbox."}
    sam = post(client, t["sam"], g["infra"])  # Sam has no inbox at all: the same 403
    assert sam.status_code == 403 and sam.json()["error"]["code"] == "group_inbox_only"
    ryan = post(client, t["ryan"], g["checkout"])  # an old client's inbox machine may post; it gets no copies
    assert ryan.status_code == 201 and ryan.json()["post"]["recipients_m"] == 3


def test_a_reply_to_a_copy_is_a_post_in_its_group(client, session, g):
    ask = post(client, g["t"]["maya"], g["checkout"], "@Jon @Priya agree on the field name").json()["post"]
    copy = next(x for x in poll(client, g["t"]["jon"]) if x.get("post_id") == ask["id"])
    rid = str(uuid.uuid4())
    r = client.post("/api/v1/messages", headers=auth(g["t"]["jon"]),
                    json={"id": rid, "body": "total_cents", "in_reply_to": copy["id"], "from_agent": "zq-agent-jon-api"})
    assert r.status_code == 201, r.text
    reply = r.json()["post"]
    assert reply["id"] == rid and reply["in_reply_to_post"] == ask["id"] and reply["recipients_m"] == 2
    assert set(copies(session, rid)) == {g["machines"]["maya"].id, g["machines"]["priya"].id}
    assert session.get(Message, uuid.UUID(copy["id"])).delivery_state == "replied"  # that copy only
    # A retry is the same post: no second fan-out (M6).
    again = client.post("/api/v1/messages", headers=auth(g["t"]["jon"]),
                        json={"id": rid, "body": "total_cents", "in_reply_to": copy["id"],
                              "from_agent": "zq-agent-jon-api"})
    assert again.status_code == 200 and again.json() == {"post": reply, "duplicate": True}
    assert len(copies(session, rid)) == 2
    # A post id replied to directly works too, through the posting endpoint.
    direct = post(client, g["t"]["priya"], g["checkout"], "agreed on total_cents", in_reply_to=ask["id"])
    assert direct.status_code == 201 and direct.json()["post"]["in_reply_to_post"] == ask["id"]


def test_a_private_reply_goes_only_to_the_author(client, session, g):
    ask = post(client, g["t"]["maya"], g["checkout"], "@Jon what's your build number?",
               from_agent="zq-agent-maya-laptop").json()["post"]
    copy = next(x for x in poll(client, g["t"]["jon"]) if x.get("post_id") == ask["id"])
    r = client.post("/api/v1/messages", headers=auth(g["t"]["jon"]),
                    json={"id": str(uuid.uuid4()), "body": "1234", "in_reply_to": copy["id"], "private": True})
    assert r.status_code == 201, r.text
    msg = session.get(Message, uuid.UUID(r.json()["message"]["id"]))
    assert msg.route == "reply" and msg.post_id is None and msg.in_reply_to is None  # a new 1:1 thread (L3)
    assert msg.conversation_id == g["conversations"]["jon"].id  # their pair conversation, never the group
    assert msg.recipient_agent_id == g["machines"]["maya"].id  # PR1: the asking agent, live
    assert len(copies(session, ask["id"])) == 2 and session.query(GroupPost).count() == 4  # no group post


def test_replies_to_copies_that_arent_yours_are_unknown(client, session, g):
    ask = post(client, g["t"]["maya"], g["checkout"], "hi").json()["post"]
    jon_copy = next(x for x in poll(client, g["t"]["jon"]) if x.get("post_id") == ask["id"])
    reply = lambda token, ref, **x: client.post("/api/v1/messages", headers=auth(token),  # noqa: E731
                                                json={"id": str(uuid.uuid4()), "body": "x", "in_reply_to": ref, **x})
    unknown = reply(g["t"]["priya"], UNKNOWN)
    assert unknown.status_code == 404
    assert same(reply(g["t"]["priya"], jon_copy["id"]), unknown)  # Jon's copy, not Priya's
    assert same(reply(g["t"]["maya"], jon_copy["id"]), unknown)  # the poster never holds a copy
    assert same(reply(g["t"]["priya"], jon_copy["id"], private=True), unknown)
    person = client.post("/api/v1/person/send", headers=auth(g["person_tokens"]["Jon"]),
                         json={"id": str(uuid.uuid4()), "body": "x", "in_reply_to": jon_copy["id"]})
    assert person.status_code == 404  # no person posting path (C7)
    session.get(Message, uuid.UUID(jon_copy["id"])).withdrawn_at = identity.now()
    session.commit()
    assert same(reply(g["t"]["jon"], jon_copy["id"]), unknown)  # withdrawn: unknown


def test_ids_share_one_namespace_with_messages(client, session, g):
    t = g["t"]
    pid = str(uuid.uuid4())
    first = client.post(f"/api/v1/groups/{g['checkout']}/posts", headers=auth(t["maya"]), json={"id": pid, "body": "a"})
    assert first.status_code == 201
    same_again = client.post(f"/api/v1/groups/{g['checkout']}/posts", headers=auth(t["maya"]),
                             json={"id": pid, "body": "a"})
    assert same_again.status_code == 200 and same_again.json() == {"post": first.json()["post"], "duplicate": True}
    changed = client.post(f"/api/v1/groups/{g['checkout']}/posts", headers=auth(t["maya"]),
                          json={"id": pid, "body": "b"})
    as_message = client.post("/api/v1/messages", headers=auth(t["jon"]),
                             json={"id": pid, "to": "maya@example.test", "body": "x"})
    msg_id = str(g["messages"]["ask"].id)
    over_message = client.post(f"/api/v1/groups/{g['checkout']}/posts", headers=auth(t["maya"]),
                               json={"id": msg_id, "body": "a"})
    other_message = client.post("/api/v1/messages", headers=auth(t["jon"]),
                                json={"id": msg_id, "to": "maya@example.test", "body": "different"})
    for r in (changed, as_message, over_message):
        assert r.status_code == 409 and r.json() == other_message.json()  # one answer for every conflict
    assert len(copies(session, pid)) == 2


def test_mentions(client, session, g):
    nw = g["teams"]["northwind"]
    twin = identity.create_user(session, "jon.two@example.test", "Jon", "correct horse battery")
    identity.add_member(session, nw, twin)
    session.add(GroupMember(conversation_id=g["groups"]["checkout"].id, user_id=twin.id, team_id=nw.id))
    session.commit()
    p = lambda body, **x: post(client, g["t"]["maya"], g["checkout"], body, **x)  # noqa: E731
    names = lambda r: [m["email"] for m in r.json()["post"]["mentions"]]  # noqa: E731
    assert names(p("@Jon please check")) == []  # two members called Jon: nobody, the text stays (L5)
    assert names(p("@jon@example.test and @PRIYA, then @everyone")) == ["jon@example.test", "priya@example.test"]
    assert names(p("@Maya @Priya @Priya")) == ["priya@example.test"]  # never the poster; once each
    assert names(p("ping", mentions=["Priya", "@jon.two@example.test"])) == ["priya@example.test",
                                                                             "jon.two@example.test"]
    for bad in (["Jon"], ["nobody@example.test"], ["Lee"], "Priya"):
        r = p("x", mentions=bad)
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid", bad
    assert "isn't one member of this group" in p("x", mentions=["Jon"]).json()["error"]["message"]


def test_no_limits_a_rapid_reply_chain_is_all_accepted(client, session, g):
    """D3, C9: no mention gate, chain depth, group rate or pause."""
    t, parent = g["t"], None
    for i in range(30):
        who = ("maya", "jon", "priya")[i % 3]
        r = post(client, t[who], g["checkout"], f"turn {i}", **({"in_reply_to": parent} if parent else {}))
        assert r.status_code == 201, (i, r.text)
        parent = r.json()["post"]["id"]
    assert session.query(GroupPost).filter_by(conversation_id=g["groups"]["checkout"].id).count() == 33


def test_group_copies_never_fill_the_1_to_1_inbox(client, session, g, settings):
    """M5: copies don't count toward inbox_full; fan-out always makes them."""
    for i in range(settings.max_pending + 3):
        assert post(client, g["t"]["maya"], g["checkout"], f"post {i}").status_code == 201
    r = client.post("/api/v1/messages", headers=auth(g["t"]["olive"]),
                    json={"id": str(uuid.uuid4()), "to": "jon@example.test", "body": "1:1 still works"})
    assert r.status_code == 201, r.text


# G2: the gate ----------------------------------------------------------------------------------------------------

def test_copies_go_only_to_a_groups_poll(client, session, g):
    pid = post(client, g["t"]["maya"], g["checkout"], "gated").json()["post"]["id"]
    plain = poll(client, g["t"]["priya"], groups_flag=False)  # as an old client polls
    assert all(x.get("post_id") is None for x in plain) and all("group" not in x for x in plain)
    session.expire_all()
    assert g["machines"]["priya"].groups_capable_at is None  # that poll ended the capability (H1)
    assert copies(session, pid)[g["machines"]["priya"].id].withdrawn_at is not None
    assert not [x for x in poll(client, g["t"]["priya"]) if x.get("post_id") == pid]  # withdrawn: never again
    later = post(client, g["t"]["maya"], g["checkout"], "after").json()["post"]
    assert g["machines"]["priya"].id in copies(session, later["id"])  # capable again after a groups=1 poll


def test_an_older_client_ends_the_capability(client, session, g):
    pid = post(client, g["t"]["maya"], g["checkout"], "x").json()["post"]["id"]
    r = client.put("/api/v1/presence", headers=auth(g["t"]["jon"]), json={
        "status": "ready", "client": {"version": "0.6.0", "update_mode": "automatic", "update_state": "current"}})
    assert r.status_code == 200, r.text
    session.expire_all()
    assert g["machines"]["jon"].groups_capable_at is None
    assert copies(session, pid)[g["machines"]["jon"].id].withdrawn_at is not None
    later = post(client, g["t"]["maya"], g["checkout"], "y").json()["post"]
    assert later["recipients_m"] == 1 and g["machines"]["jon"].id not in copies(session, later["id"])
    # A groups=1 poll from a machine still reporting 0.6.0 doesn't qualify it.
    poll(client, g["t"]["jon"])
    session.expire_all()
    assert g["machines"]["jon"].groups_capable_at is None


def test_a_withdrawn_copy_ack_is_a_no_op(client, session, g):
    pid = post(client, g["t"]["maya"], g["checkout"], "x").json()["post"]["id"]
    copy = copies(session, pid)[g["machines"]["jon"].id]
    copy.withdrawn_at = identity.now()
    session.commit()
    r = client.post(f"/api/v1/messages/{copy.id}/ack", headers=auth(g["t"]["jon"]))
    assert r.status_code == 200 and r.json()["acked"] is False
    session.expire_all()
    assert session.get(Message, copy.id).acked_at is None


# G4: withdrawal and moves ----------------------------------------------------------------------------------------

def _pending(session, user_machine_ids):
    session.expire_all()
    return [c for c in session.query(Message).filter(Message.route == "group", Message.acked_at.is_(None))
            if c.recipient_agent_id in user_machine_ids]


def test_an_inbox_change_moves_copies_only_to_a_groups_inbox(client, session, g):
    nw, jon = g["teams"]["northwind"], g["people"]["Jon"]
    pid = post(client, g["t"]["maya"], g["checkout"], "x").json()["post"]["id"]
    other, _ = _machine(session, nw, jon, "zq-handle-jon-2", [("zq-agent-jon-2", "instant", "idle")])
    session.commit()
    messaging.set_person_inbox(session, jon, nw.id, other.id, "zq-agent-jon-2", via="api")  # not a groups machine
    session.commit()
    assert copies(session, pid)[g["machines"]["jon"].id].withdrawn_at is not None
    pid2 = post(client, g["t"]["maya"], g["checkout"], "y").json()["post"]["id"]
    assert g["machines"]["jon"].id not in copies(session, pid2) and other.id not in copies(session, pid2)
    make_capable(session, other)
    session.commit()
    pid3 = post(client, g["t"]["maya"], g["checkout"], "z").json()["post"]["id"]
    assert copies(session, pid3)[other.id].recipient_agent_name == "zq-agent-jon-2"
    messaging.set_person_inbox(session, jon, nw.id, g["machines"]["jon"].id, "zq-agent-jon-api", via="api")
    session.commit()
    moved = copies(session, pid3)[g["machines"]["jon"].id]  # back to a groups machine: moved, redelivered
    assert moved.withdrawn_at is None and moved.redeliver and moved.recipient_agent_name == "zq-agent-jon-api"


def test_every_site_withdraws(client, session, g):
    """M1: clear inbox, machine revoke, team removal, disable, capability loss (group-remove: test_groups_admin)."""
    nw, p, m = g["teams"]["northwind"], g["people"], g["machines"]
    post(client, g["t"]["maya"], g["checkout"], "a")
    messaging.clear_person_inbox(session, p["Jon"], nw.id)
    session.commit()
    assert _pending(session, {m["jon"].id}) and all(c.withdrawn_at for c in _pending(session, {m["jon"].id}))
    assert all(c.recipient_user_id is None for c in session.query(Message).filter_by(route="group"))  # never app
    identity.revoke_agent(session, m["priya"])
    session.commit()
    assert all(c.withdrawn_at for c in _pending(session, {m["priya"].id}))
    post(client, g["t"]["maya"], g["checkout"], "b")
    identity.remove_member(session, nw, p["Maya"])  # out of the team: out of its groups too
    session.commit()
    assert not session.query(GroupMember).filter_by(user_id=p["Maya"].id).count()
    identity.set_user_active(session, p["Sam"], False)
    session.commit()
    assert not session.query(GroupMember).filter_by(user_id=p["Sam"].id).count()


def test_removal_and_disable_withdraw_copies(client, session, g):
    nw, p, m = g["teams"]["northwind"], g["people"], g["machines"]
    post(client, g["t"]["maya"], g["checkout"], "to jon and priya")
    assert _pending(session, {m["jon"].id})
    identity.remove_member(session, nw, p["Jon"])
    session.commit()
    assert all(c.withdrawn_at for c in _pending(session, {m["jon"].id}))
    assert not session.query(GroupMember).filter_by(user_id=p["Jon"].id).count()
    post(client, g["t"]["maya"], g["checkout"], "to priya")
    identity.set_user_active(session, p["Priya"], False)
    session.commit()
    assert all(c.withdrawn_at for c in _pending(session, {m["priya"].id}))


# G5: reading, visibility (M3, M4) and no reads (C18) ----------------------------------------------------------------

def test_the_read_api(client, session, g):
    r = client.get("/api/v1/groups", headers=auth(g["t"]["maya"]))
    assert r.json() == {"groups": [
        {"id": g["checkout"], "name": "checkout", "members_n": 4, "taking_part": True},
        {"id": g["infra"], "name": "infra", "members_n": 2, "taking_part": True}]}
    assert client.get("/api/v1/groups", headers=auth(g["t"]["olive"])).json() == {"groups": []}
    assert client.get("/api/v1/groups", headers=auth(g["t"]["maya_ct"])).json() == {"groups": []}  # her contoso machine
    members = client.get(f"/api/v1/groups/{g['checkout']}/members", headers=auth(g["t"]["ryan"])).json()
    assert members["members"] == [
        {"display_name": "Jon", "email": "jon@example.test", "taking_part": True},
        {"display_name": "Maya", "email": "maya@example.test", "taking_part": True},
        {"display_name": "Priya", "email": "priya@example.test", "taking_part": True},
        {"display_name": "Ryan", "email": "ryan@example.test", "taking_part": False}]
    assert members["group"]["taking_part"] is False  # Ryan's own
    unknown = client.get(f"/api/v1/groups/{UNKNOWN}/members", headers=auth(g["t"]["olive"]))
    for token in ("olive", "maya_ct", "sam", "lee"):
        assert same(client.get(f"/api/v1/groups/{g['checkout']}/members", headers=auth(g["t"][token])), unknown)


def test_copies_are_visible_only_to_their_recipient(client, session, g):
    """M3: the poster, other members and removed members get the unknown answer, events included."""
    pid = post(client, g["t"]["maya"], g["checkout"], "x").json()["post"]["id"]
    jon_copy = copies(session, pid)[g["machines"]["jon"].id]
    path, events = f"/api/v1/messages/{jon_copy.id}", f"/api/v1/messages/{jon_copy.id}/events"
    unknown = client.get(f"/api/v1/messages/{UNKNOWN}", headers=auth(g["t"]["maya"]))
    unknown_events = client.get(f"/api/v1/messages/{UNKNOWN}/events", headers=auth(g["t"]["maya"]))
    assert client.get(path, headers=auth(g["t"]["jon"])).status_code == 200
    assert client.get(f"/api/v1/person/messages/{jon_copy.id}", headers=auth(g["person_tokens"]["Jon"])).status_code \
        == 200
    for token in ("maya", "priya", "ryan", "olive"):
        assert same(client.get(path, headers=auth(g["t"][token])), unknown), token
        assert same(client.get(events, headers=auth(g["t"][token])), unknown_events), token
    person_unknown = client.get(f"/api/v1/person/messages/{UNKNOWN}", headers=auth(g["person_tokens"]["Maya"]))
    assert same(client.get(f"/api/v1/person/messages/{jon_copy.id}", headers=auth(g["person_tokens"]["Maya"])),
                person_unknown)
    identity.remove_member(session, g["teams"]["northwind"], g["people"]["Ryan"])  # unrelated: Jon stays
    session.query(GroupMember).filter_by(user_id=g["people"]["Jon"].id).delete()  # Jon leaves the group
    session.commit()
    assert same(client.get(path, headers=auth(g["t"]["jon"])),
                client.get(f"/api/v1/messages/{UNKNOWN}", headers=auth(g["t"]["jon"])))


def test_one_to_one_routes_answer_a_group_id_like_an_unknown_one(client, g):
    """M4: members included; groups never appear in 1:1 lists."""
    gid, t, pt = g["checkout"], g["t"]["maya"], g["person_tokens"]["Maya"]
    for method, path, token in (("GET", "/api/v1/conversations/{}/messages", t),
                                ("POST", "/api/v1/conversations/{}/archive", t),
                                ("POST", "/api/v1/conversations/{}/unarchive", t),
                                ("GET", "/api/v1/person/conversations/{}", pt),
                                ("POST", "/api/v1/person/conversations/{}/archive", pt),
                                ("POST", "/api/v1/person/conversations/{}/unarchive", pt)):
        call = lambda ident: client.request(method, path.format(ident), headers=auth(token))  # noqa: E731
        assert same(call(gid), call(UNKNOWN)), path
    for path, token in (("/api/v1/conversations?archived=include", t),
                        ("/api/v1/person/conversations?archived=include", pt)):
        assert gid not in client.get(path, headers=auth(token)).text


def test_groups_have_no_reads(client, session, g):
    """C18: no unread, no person feed, no person ack."""
    pid = post(client, g["t"]["jon"], g["checkout"], "@Maya FYI").json()["post"]["id"]
    maya_copy = copies(session, pid)[g["machines"]["maya"].id]
    assert not messaging.is_unread_for(session, g["people"]["Maya"].id, maya_copy)
    unread = session.query(Message).filter(messaging.unread_for(g["people"]["Maya"].id),
                                           Message.route == "group").count()
    assert unread == 0
    feed = client.get("/api/v1/person/inbox", headers=auth(g["person_tokens"]["Maya"]),
                      params={"routed": 1, "include_acked": "true"}).json()["messages"]
    assert not [x for x in feed if x["id"] == str(maya_copy.id)]
    r = client.post(f"/api/v1/person/messages/{maya_copy.id}/ack", headers=auth(g["person_tokens"]["Maya"]))
    session.expire_all()
    assert session.get(Message, maya_copy.id).person_seen_at is None and r.status_code == 403


def test_presence_rows_for_groups(session, g):
    """The gate reads the machine's CURRENT presence: no report at all is no groups client."""
    session.delete(session.get(AgentPresence, g["machines"]["priya"].id))
    session.commit()
    assert not groups.taking_part(session, g["teams"]["northwind"].id, g["people"]["Priya"].id)
