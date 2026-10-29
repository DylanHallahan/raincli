"""The client tests' fake server (tests/agent/fake_server.py) answers the group routes as the real server does
(protocol §22, G12 L7): the same statuses, error codes and JSON shapes (keys and value types, nested), for the same
scenarios. Real PostgreSQL for the server; the fake in-process."""

from __future__ import annotations

import uuid

from agent.fake_server import ApiFail, FakeState
from api_helpers import auth
from conftest_groups import groups_seed  # noqa: F401  (fixture)
from conftest_v06 import _machine, v06_seed  # noqa: F401  (fixture)
from test_v060_cli import live  # noqa: F401  (fixture: a real uvicorn server)


def shape(value):
    """Keys and value types, nested; list items by their first element's shape."""
    if isinstance(value, dict):
        return {k: shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return type(value).__name__ if value is not None else "null"


def answer(status, body):
    if status >= 400:
        return status, body["error"]["code"]
    return status, shape(body)


class Fake:
    """The fake, set up like the group seed's "checkout": Maya, Jon and Priya taking part (Maya posts)."""

    def __init__(self):
        st = self.st = FakeState()
        self.tokens = {}
        for key, email, name in (("maya", "maya@example.test", "Maya"), ("jon", "jon@example.test", "Jon"),
                                 ("priya", "priya@example.test", "Priya"), ("olive", "olive@example.test", "Olive")):
            self.tokens[key] = st.add_agent(f"zq-{key}", team="northwind", owner=email, owner_name=name)
            if key != "olive":
                st.set_inbox(email, "northwind", f"zq-{key}", f"agent-{key}")
                st.make_groups_capable(f"zq-{key}", "northwind")
        self.tokens["maya_2"] = st.add_agent("zq-maya-2", team="northwind", owner="maya@example.test")
        self.group = st.add_group("northwind", "checkout", ["maya@example.test", "jon@example.test",
                                                            "priya@example.test"])

    def caller(self, key):
        return self.st.agents[self.st.tokens[self.tokens[key]]]

    def locked(self, fn, *args):
        with self.st.lock:  # as the fake's HTTP handler calls it
            return fn(*args)

    def call(self, fn, *args):
        try:
            return answer(*self.locked(fn, *args)[:2])
        except ApiFail as exc:
            return exc.status, exc.code

    def post(self, key, body, gid=None):
        return self.call(self.st.group_post, self.caller(key), gid or self.group, body)

    def poll(self, key):
        return self.locked(self.st.inbox, self.caller(key), {"routing": ["1"], "groups": ["1"],
                                                             "include_acked": ["true"]})[1]


def test_the_fake_answers_like_the_server(client, session, groups_seed):  # noqa: F811
    s, fake = groups_seed, Fake()
    t, gid = s["machine_tokens"], str(s["groups"]["checkout"].id)
    _second, second = _machine(session, s["teams"]["northwind"], s["people"]["Maya"], "zq-handle-maya-2")
    session.commit()
    real_post = lambda token, body, g=gid: answer(*(lambda r: (r.status_code, r.json()))(  # noqa: E731
        client.post(f"/api/v1/groups/{g}/posts", headers=auth(token), json=body)))
    pid = str(uuid.uuid4())
    first = {"id": pid, "body": "@Jon please check", "from_agent": "an-agent"}
    assert real_post(t["maya"], first) == fake.post("maya", first)  # 201 and the post's shape
    assert real_post(t["maya"], first) == fake.post("maya", first)  # 200 duplicate
    changed = dict(first, body="different")
    assert real_post(t["maya"], changed) == fake.post("maya", changed) == (409, "id_conflict")
    other = {"id": str(uuid.uuid4()), "body": "x"}
    assert real_post(second, other) == fake.post("maya_2", other) == (403, "group_inbox_only")
    assert real_post(t["olive"], other) == fake.post("olive", other) == (404, "not_found")
    assert real_post(t["maya"], other, str(uuid.uuid4())) == fake.post("maya", other, str(uuid.uuid4())) \
        == (404, "not_found")
    bad = {"id": str(uuid.uuid4()), "body": "x", "mentions": ["Nobody"]}
    assert real_post(t["maya"], bad) == fake.post("maya", bad) == (400, "invalid")
    assert real_post(t["maya"], {"id": "nope", "body": "x"}) == fake.post("maya", {"id": "nope", "body": "x"}) \
        == (400, "invalid")

    # A copy, as its recipient polls it: the same fields and types.
    real_copy = next(m for m in client.get("/api/v1/inbox", headers=auth(t["jon"]),
                                           params={"routing": 1, "groups": 1}).json()["messages"]
                     if m.get("post_id") == pid)
    fake_copy = next(m for m in fake.poll("jon")["messages"] if m.get("post_id") == pid)
    assert shape(real_copy) == shape(fake_copy)
    assert {k: real_copy[k] for k in ("mentioned", "from_kind")} == {k: fake_copy[k] for k in ("mentioned", "from_kind")}

    # Replies through POST /messages: a group post, and a private 1:1 reply.
    reply = {"id": str(uuid.uuid4()), "body": "done", "in_reply_to": None}
    real_reply = client.post("/api/v1/messages", headers=auth(t["jon"]), json=dict(reply, in_reply_to=real_copy["id"]))
    fake_reply = fake.call(fake.st.send, fake.caller("jon"), dict(reply, in_reply_to=fake_copy["id"]))
    assert answer(real_reply.status_code, real_reply.json()) == fake_reply
    private = {"id": str(uuid.uuid4()), "body": "just you", "private": True}
    real_private = client.post("/api/v1/messages", headers=auth(t["jon"]), json=dict(private, in_reply_to=real_copy["id"]))
    fake_private = fake.call(fake.st.send, fake.caller("jon"), dict(private, in_reply_to=fake_copy["id"]))
    assert real_private.status_code == fake_private[0] == 201
    assert set(real_private.json()["message"]) == set(fake_private[1]["message"])
    stray = {"id": str(uuid.uuid4()), "body": "x", "in_reply_to": real_copy["id"]}
    assert client.post("/api/v1/messages", headers=auth(t["priya"]), json=stray).status_code == 404
    assert fake.call(fake.st.send, fake.caller("priya"), dict(stray, in_reply_to=fake_copy["id"])) == (404, "not_found")

    # The read routes.
    real_groups = client.get("/api/v1/groups", headers=auth(t["maya"])).json()
    me = fake.st.owners[fake.caller("maya")["id"]]
    fake_groups = {"groups": [fake.st.group_json(fake.group, me)]}
    assert shape(real_groups) == shape(fake_groups)
    real_members = client.get(f"/api/v1/groups/{gid}/members", headers=auth(t["maya"])).json()
    fake_members = {"group": fake.st.group_json(fake.group, me), "members": fake.st.members_json(fake.group)}
    assert shape(real_members) == shape(fake_members)

    # The gate: a poll without groups=1 takes nothing and ends the capability, on both.
    plain = client.get("/api/v1/inbox", headers=auth(t["priya"]), params={"routing": 1}).json()["messages"]
    fake_plain = fake.locked(fake.st.inbox, fake.caller("priya"), {"routing": ["1"]})[1]["messages"]
    assert [m for m in plain if m.get("post_id")] == [m for m in fake_plain if m.get("post_id")] == []
    later = {"id": str(uuid.uuid4()), "body": "after"}
    assert real_post(t["maya"], later)[1]["post"]["recipients_m"] == "int"
    real_m = client.post(f"/api/v1/groups/{gid}/posts", headers=auth(t["maya"]),
                         json={"id": str(uuid.uuid4()), "body": "m"}).json()["post"]["recipients_m"]
    fake_m = fake.locked(fake.st.group_post, fake.caller("maya"), fake.group, {"id": str(uuid.uuid4()), "body": "m"})[1]
    assert real_m == fake_m["post"]["recipients_m"] == 1  # Jon only: Priya's poll ended her capability


def test_the_real_client_polls_keep_the_capability(live, client, session, groups_seed):  # noqa: F811
    """On the real server: the 0.6.1 client's polls (with and without routing, paging, include_acked) all send
    groups=1, so a pending copy stays and the machine keeps taking part."""
    from raincli_agent.api import ApiClient
    from raincli_server.models import Agent, Message

    s = groups_seed
    pid = str(uuid.uuid4())
    client.post(f"/api/v1/groups/{s['groups']['checkout'].id}/posts", headers=auth(s["machine_tokens"]["maya"]),
                json={"id": pid, "body": "pending"})
    jon = ApiClient(live, s["machine_tokens"]["jon"])
    jon.inbox(after=0)
    jon.inbox(after=0, routing=True, include_acked=True, limit=1)
    session.expire_all()
    copy = session.query(Message).filter_by(post_id=uuid.UUID(pid), recipient_agent_id=s["machines"]["jon"].id).one()
    assert copy.withdrawn_at is None and session.get(Agent, s["machines"]["jon"].id).groups_capable_at is not None
    messages, _ = jon.inbox(after=0, routing=True)
    assert str(copy.id) in [m["id"] for m in messages]
