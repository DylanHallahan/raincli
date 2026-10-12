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
    out = set()
    for app_route in getattr(app, "routes", []):
        mounted = getattr(app_route, "app", None)
        prefix = getattr(app_route, "path", "")
        for route in getattr(mounted, "routes", []) if mounted is not None and hasattr(mounted, "routes") else []:
            if isinstance(route, APIRoute) and "GET" in route.methods:
                out.add(prefix + route.path)
        if isinstance(app_route, APIRoute) and "GET" in app_route.methods:
            out.add(app_route.path)
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


def test_every_api_get_route_is_scanned_or_exempted(app):
    routes = {r for r in _routes(app) if r.startswith("/api/v1")}
    unknown = routes - COVERED - set(EXEMPT)
    assert unknown == set(), f"not in the privacy scan: {sorted(unknown)}"
    assert re.match(r"^the /app pages", APP_PAGES)
