"""The privacy scan, API half (protocol §21 C5.9, C13 M3, M7): fetched as a member, no teammate's machine handle or
agent name appears in any body or header; the owner sees their own only where C5.1 allows. The page half is
tests/gui/test_v06_privacy_pages.py (web)."""

from __future__ import annotations

import json
import re
import uuid

from fastapi.routing import APIRoute

from api_helpers import auth
from conftest_v06 import OWN_STRINGS, TEAMMATE_STRINGS, v06_seed  # noqa: F401  (fixture)
from test_groups_privacy_api import GROUP_DRIVEN, GROUP_GETS  # the group routes, scanned with group data there

# GET routes under /api/v1 and why each is covered; anything else fails the scan.
COVERED = {
    "/api/v1/me", "/api/v1/agents", "/api/v1/people", "/api/v1/inbox", "/api/v1/routing",
    "/api/v1/messages/{message_id}", "/api/v1/conversations", "/api/v1/conversations/{conversation_id}/messages",
    "/api/v1/me/inbox", "/api/v1/person/me", "/api/v1/person/inbox", "/api/v1/person/conversations",
    "/api/v1/person/conversations/{conversation_id}", "/api/v1/person/messages/{message_id}",
    "/api/v1/person/me/inbox",
}
EXEMPT = {
    "/api/v1/health": "no user data: the service's own status",
    # Attachment content is the sender's text (no attachments in the seed carry handles); its metadata is in the
    # message JSON, which is scanned.
    "/api/v1/messages/{message_id}/attachments/{attachment_id}": "attachment bytes, metadata scanned in messages",
    "/api/v1/person/messages/{message_id}/attachments/{ref}": "attachment bytes, metadata scanned in messages",
}
APP_PAGES = "the /app pages are tests/gui/test_v06_privacy_pages.py (web, C13 M7)"


def _routes(app):
    """Every ``(method, path)`` the app serves, mounted sub-apps included."""
    out = set()
    for app_route in getattr(app, "routes", []):
        mounted = getattr(app_route, "app", None)
        prefix = getattr(app_route, "path", "")
        for route in getattr(mounted, "routes", []) if mounted is not None and hasattr(mounted, "routes") else []:
            if isinstance(route, APIRoute):
                out |= {(m, prefix + route.path) for m in route.methods if m != "HEAD"}
        if isinstance(app_route, APIRoute):
            out |= {(m, app_route.path) for m in app_route.methods if m != "HEAD"}
    return out


def _leaks(text, strings):
    return [s for s in strings if s in text]


def _fetch_all(client, seed, *, machine, person):
    """Every API GET a member can make, plus refused sends (their errors), as (label, text) pairs."""
    mt, pt = seed["machine_tokens"][machine], seed["person_tokens"][person]
    convs = [str(c.id) for c in seed["conversations"].values()]
    msgs = [str(m.id) for m in seed["messages"].values()]
    gets = [("machine", mt, "/api/v1/me"), ("machine", mt, "/api/v1/agents"), ("machine", mt, "/api/v1/people"),
            ("machine", mt, "/api/v1/inbox?routing=1&include_acked=true"), ("machine", mt, "/api/v1/inbox"),
            ("machine", mt, "/api/v1/routing"), ("machine", mt, "/api/v1/conversations"),
            ("machine", mt, "/api/v1/me/inbox"), ("person", pt, "/api/v1/person/me"),
            ("person", pt, "/api/v1/person/inbox?include_acked=true"),
            ("person", pt, "/api/v1/person/conversations?archived=include"),
            ("person", pt, "/api/v1/person/me/inbox?team=northwind")]
    gets += [("machine", mt, f"/api/v1/messages/{m}") for m in msgs]
    gets += [("person", pt, f"/api/v1/person/messages/{m}") for m in msgs]
    gets += [("machine", mt, f"/api/v1/conversations/{c}/messages") for c in convs]
    gets += [("person", pt, f"/api/v1/person/conversations/{c}") for c in convs]
    out = []
    for kind, token, path in gets:
        r = client.get(path, headers=auth(token))
        out.append((f"GET {path} as {kind}", f"{r.status_code} {json.dumps(dict(r.headers))} {r.text}"))
    for to in ("zq-handle-jon", "zq-handle-jon/zq-agent-jon-api", {"machine": "zq-handle-priya"},
               {"machine": "zq-handle-priya", "agent": "zq-agent-priya-web"}, "zq-handle-ryan/zq-agent-ryan-old",
               "zq-handle-lee", "nobody"):
        for kind, token, path in (("machine", mt, "/api/v1/messages"), ("person", pt, "/api/v1/person/send")):
            body = {"id": str(uuid.uuid4()), "to": to, "body": "x"}
            if kind == "person":
                body["team"] = "northwind"
            r = client.post(path, headers=auth(token), json=body)
            out.append((f"POST {path} to {to} as {kind}", f"{r.status_code} {r.text}"))
    return out


def test_a_member_never_sees_a_teammates_machine_or_agent(client, v06_seed):  # noqa: F811
    leaks = [(label, _leaks(text, TEAMMATE_STRINGS)) for label, text in
             _fetch_all(client, v06_seed, machine="maya", person="Maya")]
    assert [leak for leak in leaks if leak[1]] == []


def test_a_teammate_never_sees_mayas_machine_or_agents(client, v06_seed):  # noqa: F811
    leaks = [(label, _leaks(text, OWN_STRINGS)) for label, text in
             _fetch_all(client, v06_seed, machine="jon", person="Jon")]
    assert [leak for leak in leaks if leak[1]] == []


def test_the_owner_sees_their_own_only_where_allowed(client, v06_seed):  # noqa: F811
    mt, pt = v06_seed["machine_tokens"]["maya"], v06_seed["person_tokens"]["Maya"]
    assert "zq-handle-maya" in client.get("/api/v1/me", headers=auth(mt)).text
    assert "zq-handle-maya" in client.get("/api/v1/agents", headers=auth(mt)).text
    # Her machine reading its own message keeps its own endpoint and stated agent; her person view is people.
    ask = str(v06_seed["messages"]["ask"].id)
    own = client.get(f"/api/v1/messages/{ask}", headers=auth(mt)).json()["message"]
    assert own["from_endpoint"] == {"machine": "zq-handle-maya", "agent": "zq-agent-maya-laptop"}
    assert own["from_agent"] == "zq-agent-maya-laptop" and own["to_endpoint"]["person"] == "jon@example.test"
    as_person = client.get(f"/api/v1/person/messages/{ask}", headers=auth(pt)).json()["message"]
    assert as_person["from_endpoint"] == {"person": "maya@example.test", "display_name": "Maya"}
    assert as_person["from_kind"] == "agent" and as_person["from_owner"] is None


def test_api_fields_are_coarse_for_non_owners(client, v06_seed):  # noqa: F811
    """C13 M3: hold codes, peers and archived_by."""
    to_priya = str(v06_seed["messages"]["to_priya"].id)
    seen = client.get(f"/api/v1/person/messages/{to_priya}", headers=auth(v06_seed["person_tokens"]["Maya"])).json()
    assert (seen["message"]["delivery_state"], seen["message"]["hold_reason"]) == ("held", "inbox_busy")
    rows = client.get("/api/v1/person/conversations?archived=include",
                      headers=auth(v06_seed["person_tokens"]["Maya"])).json()["conversations"]
    peers = {r["peer"] for r in rows}
    assert peers == {"Jon", "Sam", "Priya", "From your agents"}
    assert all(set(r["peer_endpoint"]) == {"person", "display_name"} for r in rows)


# Mutating routes, each driven (and its response scanned) by _drive_mutations, or exempted with a reason.
DRIVEN = {
    ("POST", "/api/v1/messages"), ("POST", "/api/v1/messages/{message_id}/ack"),
    ("POST", "/api/v1/messages/{message_id}/events"), ("POST", "/api/v1/conversations/{conversation_id}/archive"),
    ("POST", "/api/v1/conversations/{conversation_id}/unarchive"), ("POST", "/api/v1/person/send"),
    ("POST", "/api/v1/person/messages/{message_id}/ack"),
    ("POST", "/api/v1/person/conversations/{conversation_id}/archive"),
    ("POST", "/api/v1/person/conversations/{conversation_id}/unarchive"),
    ("PUT", "/api/v1/me/inbox"), ("PUT", "/api/v1/person/me/inbox"),
}
MUTATING_EXEMPT = {
    ("PUT", "/api/v1/presence"): "the caller's own machine report; the reply names only it",
    ("PUT", "/api/v1/routing"): "the caller's own machine's routing policy",
    ("POST", "/api/v1/app/login"): "machine sign-in; name_taken is an accepted exception (§21)",
    ("POST", "/api/v1/app/sign-out"): "the caller's own sign-out; no other endpoint in the reply",
    ("POST", "/api/v1/person/sign-out"): "the caller's own session",
    ("POST", "/api/v1/app/handoff"): "a single-use URL for the caller",
}


def _drive_mutations(client, seed, *, machine, person, peer_person_token, peer_machine_token, peer_email):
    """Every mutating API call a member makes, as (label, text) pairs: sends, acks, events, archive and unarchive,
    the inbox setting. The member is ``machine``/``person``; the peer answers so there is something to ack."""
    import uuid as _uuid
    mt, pt = seed["machine_tokens"][machine], seed["person_tokens"][person]
    out = []

    def record(label, r):
        out.append((label, f"{r.status_code} {json.dumps(dict(r.headers))} {r.text}"))
        return r
    sent = record("send", client.post("/api/v1/messages", headers=auth(mt), json={
        "id": str(_uuid.uuid4()), "to": peer_email, "body": "x"})).json()["message"]
    record("person send", client.post("/api/v1/person/send", headers=auth(pt), json={
        "id": str(_uuid.uuid4()), "to": peer_email, "body": "y", "team": "northwind"}))
    back = client.post("/api/v1/messages", headers=auth(peer_machine_token), json={
        "id": str(_uuid.uuid4()), "body": "z", "in_reply_to": sent["id"]}).json()["message"]
    by_hand = client.post("/api/v1/person/send", headers=auth(peer_person_token), json={
        "id": str(_uuid.uuid4()), "body": "w", "in_reply_to": sent["id"]}).json()["message"]
    record("ack", client.post(f"/api/v1/messages/{back['id']}/ack", headers=auth(mt)))
    record("event", client.post(f"/api/v1/messages/{back['id']}/events", headers=auth(mt),
                                json={"state": "submitted", "detail": "delivered"}))
    record("person ack", client.post(f"/api/v1/person/messages/{by_hand['id']}/ack", headers=auth(pt)))
    conv = sent["conversation_id"]
    for kind, token, prefix in (("machine", mt, ""), ("person", pt, "/person")):
        record(f"{kind} archive", client.post(f"/api/v1{prefix}/conversations/{conv}/archive", headers=auth(token)))
        record(f"{kind} unarchive", client.post(f"/api/v1{prefix}/conversations/{conv}/unarchive",
                                               headers=auth(token)))
    record("put inbox", client.put("/api/v1/me/inbox", headers=auth(mt), json={"none": True}))
    record("put person inbox", client.put("/api/v1/person/me/inbox", headers=auth(pt),
                                          json={"team": "northwind", "none": True}))
    return out


def test_mutating_calls_never_show_a_teammates_machine_or_agent(client, v06_seed):  # noqa: F811
    for machine, person, peer, peer_email, strings in (("maya", "Maya", "Jon", "jon@example.test", TEAMMATE_STRINGS),
                                                      ("jon", "Jon", "Maya", "maya@example.test", OWN_STRINGS)):
        peer_key = peer.lower()
        leaks = [(label, _leaks(text, strings)) for label, text in _drive_mutations(
            client, v06_seed, machine=machine, person=person, peer_person_token=v06_seed["person_tokens"][peer],
            peer_machine_token=v06_seed["machine_tokens"][peer_key], peer_email=peer_email)]
        assert [leak for leak in leaks if leak[1]] == [], machine


def _sender_snapshot(client, seed, token_kind, sender):
    """Everything the sender can fetch over the API, as one comparable structure (the poll's own cursor aside)."""
    token = seed["machine_tokens"][sender] if token_kind == "machine" else seed["person_tokens"][sender]
    paths = (["/api/v1/conversations?archived=include", "/api/v1/inbox?routing=1&include_acked=true", "/api/v1/people"]
             if token_kind == "machine" else
             ["/api/v1/person/conversations?archived=include", "/api/v1/person/inbox?include_acked=true&routed=1",
              "/api/v1/person/me/inbox?team=northwind"])
    convs = [str(c.id) for c in seed["conversations"].values()]
    paths += ([f"/api/v1/conversations/{c}/messages" for c in convs] if token_kind == "machine"
              else [f"/api/v1/person/conversations/{c}" for c in convs])
    return {p: client.get(p, headers=auth(token)).json() for p in paths}


def test_no_read_receipt_anything_the_sender_can_fetch_is_unchanged(client, v06_seed):  # noqa: F811
    """PR5, C13 H4, build review H1/H2: the recipient reading a message (to them in the app, or routed to their
    inbox) changes nothing the sender can fetch over the API."""
    import uuid as _uuid
    maya_m, maya_p = v06_seed["machine_tokens"]["maya"], v06_seed["person_tokens"]["Maya"]
    to_sam = client.post("/api/v1/person/send", headers=auth(maya_p), json={
        "id": str(_uuid.uuid4()), "to": "sam@example.test", "body": "read me", "team": "northwind"}).json()["message"]
    to_jon = client.post("/api/v1/messages", headers=auth(maya_m), json={
        "id": str(_uuid.uuid4()), "to": "jon@example.test", "body": "to jon's inbox"}).json()["message"]
    before = {k: _sender_snapshot(client, v06_seed, k, s) for k, s in (("machine", "maya"), ("person", "Maya"))}
    for token, mid in ((v06_seed["person_tokens"]["Sam"], to_sam["id"]), (v06_seed["person_tokens"]["Jon"], to_jon["id"])):
        assert client.post(f"/api/v1/person/messages/{mid}/ack", headers=auth(token)).status_code == 200
    after = {k: _sender_snapshot(client, v06_seed, k, s) for k, s in (("machine", "maya"), ("person", "Maya"))}
    assert after == before


def test_every_api_route_is_scanned_or_exempted(app):
    routes = {r for r in _routes(app) if r[1].startswith("/api/v1")}
    assert len(routes) >= len(COVERED) + len(DRIVEN), "the route walk found too few routes: it would pass vacuously"
    gets = {p for m, p in routes if m == "GET"}
    unknown_gets = gets - COVERED - GROUP_GETS - set(EXEMPT)
    assert unknown_gets == set(), f"GET routes not in the privacy scan: {sorted(unknown_gets)}"
    mutating = {r for r in routes if r[0] != "GET"}
    unknown = mutating - DRIVEN - GROUP_DRIVEN - set(MUTATING_EXEMPT)
    assert unknown == set(), f"mutating routes not in the privacy scan: {sorted(unknown)}"
    assert re.match(r"^the /app pages", APP_PAGES)
