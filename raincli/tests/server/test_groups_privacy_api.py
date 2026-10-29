"""The privacy scan and the team-iso probe for groups (protocol §22, C19, C20): every group route and every copy, as
fetched by members, never shows a teammate's machine handle or agent name; another team's group activity changes
nothing a member can fetch. ``GROUP_GETS`` and ``GROUP_DRIVEN`` are the routes this file covers, for the route walk
in tests/server/test_v06_privacy_api.py. Real PostgreSQL."""

from __future__ import annotations

import json
import uuid

from api_helpers import auth
from conftest_groups import groups_seed, make_capable, make_group  # noqa: F401  (fixture)
from conftest_v06 import OWN_STRINGS, TEAMMATE_STRINGS, _inbox, v06_seed  # noqa: F401  (fixture)

GROUP_GETS = {"/api/v1/groups", "/api/v1/groups/{group_id}/members"}
GROUP_DRIVEN = {("POST", "/api/v1/groups/{group_id}/posts")}
UNKNOWN = str(uuid.uuid4())


def _fetch_all(client, s, machine):
    """Everything a member's machine can fetch about groups, posts and copies included, as text."""
    t = s["machine_tokens"][machine]
    texts = []
    for path in ["/api/v1/groups", "/api/v1/inbox?routing=1&groups=1&include_acked=true"] + \
            [f"/api/v1/groups/{g.id}/members" for g in s["groups"].values()]:
        r = client.get(path, headers=auth(t))
        texts.append(f"{path} {r.status_code} {json.dumps(dict(r.headers))} {r.text}")
    for copy in client.get("/api/v1/inbox", headers=auth(t),
                           params={"routing": 1, "groups": 1, "include_acked": "true"}).json()["messages"]:
        for path in (f"/api/v1/messages/{copy['id']}", f"/api/v1/messages/{copy['id']}/events"):
            texts.append(f"{path} {client.get(path, headers=auth(t)).text}")
    r = client.post(f"/api/v1/groups/{s['groups']['checkout'].id}/posts", headers=auth(t),
                    json={"id": str(uuid.uuid4()), "body": "@Jon @Priya @Maya scan"})
    texts.append(f"post {r.status_code} {r.text}")
    return texts


def _leaks(texts, strings):
    return [(t[:80], s) for t in texts for s in strings if s in t]


def test_members_never_see_a_teammates_machine_or_agent(client, groups_seed):  # noqa: F811
    for machine, own in (("maya", OWN_STRINGS), ("jon", ("zq-handle-jon", "zq-agent-jon-api")),
                         ("priya", ("zq-handle-priya", "zq-agent-priya-web")), ("ryan", ("zq-handle-ryan",))):
        teammates = [x for x in TEAMMATE_STRINGS + OWN_STRINGS if x not in own]
        assert _leaks(_fetch_all(client, groups_seed, machine), teammates) == [], machine


def _everything(client, s, machine, person):
    """Everything a northwind member can fetch, groups included (the team-iso diff probe)."""
    m, p = auth(s["machine_tokens"][machine]), auth(s["person_tokens"][person])
    out = {}
    for path in ("/api/v1/groups", "/api/v1/inbox?routing=1&groups=1&include_acked=true",
                 "/api/v1/conversations?archived=include", "/api/v1/people"):
        out[path] = client.get(path, headers=m).json()
    for group in out["/api/v1/groups"]["groups"]:
        out[group["id"]] = client.get(f"/api/v1/groups/{group['id']}/members", headers=m).json()
    for path in ("/api/v1/person/inbox?include_acked=true&routed=1", "/api/v1/person/conversations?archived=include"):
        out[path] = client.get(path, headers=p).json()
    return out


def test_another_teams_groups_change_nothing_a_member_can_fetch(client, session, groups_seed):  # noqa: F811
    s = groups_seed
    before = _everything(client, s, "jon", "Jon")
    ct, lee, maya = s["teams"]["contoso"], s["people"]["Lee"], s["people"]["Maya"]
    _inbox(session, maya, ct, s["machines"]["maya_ct"], "zq-agent-maya-ct")
    make_capable(session, s["machines"]["maya_ct"])
    make_capable(session, s["machines"]["lee"])
    group = make_group(session, ct, "checkout", [lee, maya])  # the same name, in another team
    session.commit()
    for i in range(5):
        r = client.post(f"/api/v1/groups/{group.id}/posts", headers=auth(s["machine_tokens"]["lee"]),
                        json={"id": str(uuid.uuid4()), "body": f"contoso {i}"})
        assert r.status_code == 201 and r.json()["post"]["recipients_m"] == 1
    assert _everything(client, s, "jon", "Jon") == before
    # And contoso's group is unknown to northwind, whoever asks.
    unknown = client.get(f"/api/v1/groups/{UNKNOWN}/members", headers=auth(s["machine_tokens"]["maya"]))
    mine = client.get(f"/api/v1/groups/{group.id}/members", headers=auth(s["machine_tokens"]["maya"]))
    assert (mine.status_code, mine.json()) == (unknown.status_code, unknown.json())  # her northwind machine
    post_unknown = client.post(f"/api/v1/groups/{UNKNOWN}/posts", headers=auth(s["machine_tokens"]["maya"]),
                               json={"id": str(uuid.uuid4()), "body": "x"})
    post_other = client.post(f"/api/v1/groups/{group.id}/posts", headers=auth(s["machine_tokens"]["maya"]),
                             json={"id": str(uuid.uuid4()), "body": "x"})
    assert (post_other.status_code, post_other.json()) == (post_unknown.status_code, post_unknown.json())
