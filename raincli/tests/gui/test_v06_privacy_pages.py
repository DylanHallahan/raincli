"""The privacy scan, page half (protocol §21 C5.9, C13 M7): the app-mode and website pages in headless Chromium, on
the shared v0.6 seed, with JavaScript running (live updates, the picker, the warning fetches). As a member, no
teammate's machine handle or agent name appears in any rendered page, attribute, title or response header; a
teammate never sees the member's own; a team owner's Machines page shows handles and versions but no agent names.
Every GET route under /app is scanned here or exempted with a reason. The API half is
tests/server/test_v06_privacy_api.py (core)."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.routing import APIRoute

from conftest_v06 import OWN_STRINGS, PASSWORD, TEAMMATE_STRINGS, v06_seed  # noqa: F401  (fixture)

from .test_app_mode_gui import INSTALL_HASH, api, app_user_agent, browser, live  # noqa: F401

pytest.importorskip("playwright.sync_api")

# GET routes under /app and how this file covers them; anything else fails the scan.
COVERED = {
    "/app", "/app/inbox", "/app/people", "/app/compose", "/app/conversations/{conversation_id}", "/app/archived",
    "/app/live", "/app/recipients", "/app/person-warning", "/app/agents", "/app/teams/{slug}", "/app/team",
    "/app/account", "/app/local/{page}",
}
EXEMPT = {
    "/app/handoff": "a one-time sign-in redirect: no page content",
    "/app/messages/{message_id}/attachments/{attachment_id}": "attachment bytes (no attachments in the seed); "
                                                              "their metadata is on the conversation pages scanned",
    "/app/teams/{slug}/members/{user_id}/remove": "team-owner-only confirmation, about a member's own machines "
                                                  "(§21 C5.8 allows an owner to see machines)",
}


def _app_routes(app) -> set[str]:
    return {r.path for r in app.routes if isinstance(r, APIRoute) and "GET" in r.methods and r.path.startswith("/app")}


class Scan:
    """Everything a viewer's browser received: response headers of every same-origin request, and each page's DOM
    (outerHTML, after scripts ran), title and every attribute value."""

    def __init__(self, base):
        self.base = base
        self.texts: list[tuple[str, str]] = []

    def watch(self, page):
        def on_response(r):
            if r.url.startswith(self.base):
                try:
                    body = r.text() if r.request.resource_type in ("document", "fetch", "xhr") else ""
                except Exception:  # noqa: BLE001 - a redirect has no body
                    body = ""
                self.texts.append((f"{r.request.method} {r.url}", json.dumps(dict(r.headers)) + " " + body))
        page.on("response", on_response)

    def snapshot(self, page, label):
        dom = page.evaluate("""() => document.documentElement.outerHTML + ' ' + document.title + ' ' +
            Array.from(document.querySelectorAll('*')).flatMap((e) => Array.from(e.attributes, (a) => a.value)).join(' ')""")
        self.texts.append((label, dom))

    def leaks(self, strings, allow=lambda label, text: text):
        return [(label, [s for s in strings if s in allow(label, text)]) for label, text in self.texts
                if any(s in allow(label, text) for s in strings)]


def _app(browser, base, token):  # noqa: F811
    context = browser.new_context(user_agent=app_user_agent(browser), viewport={"width": 1180, "height": 780})
    page = context.new_page()
    page.goto(api(base, "/api/v1/app/handoff", {"app_install_hash": INSTALL_HASH}, token)["url"])
    page.wait_for_load_state("networkidle")
    return context, page


def _website(browser, base, email):  # noqa: F811
    context = browser.new_context(viewport={"width": 1180, "height": 780})
    page = context.new_page()
    page.goto(base + "/login")
    page.fill("#email", email)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url("**/app**")
    return context, page


def _visit(scan, page, base, path, label):
    page.goto(base + path)
    page.wait_for_load_state("networkidle")
    scan.snapshot(page, label)


def _app_pages(scan, page, base, seed):  # noqa: F811
    convs = {k: str(c.id) for k, c in seed["conversations"].items()}
    for path in ("/app/inbox", "/app/people", "/app/compose", "/app/compose?to=jon@example.test", "/app/archived",
                 "/app/local/this-computer", "/app/agents"):
        _visit(scan, page, base, path, f"app {path}")
    for key, cid in convs.items():
        _visit(scan, page, base, f"/app/conversations/{cid}", f"app conversation {key}")
        for summary in page.locator("details.rc-trail-box > summary").all():  # every delivery history, opened
            summary.click()
        page.evaluate("() => window.rcLive && window.rcLive.poll()")
        page.wait_for_timeout(300)
        scan.snapshot(page, f"app conversation {key}, trails open, after a poll")
    for path in [f"/app/live?conversation={cid}&after=0" for cid in convs.values()] + [
            "/app/recipients", "/app/live"] + [f"/app/person-warning?to={q}" for q in (
            "jon@example.test", "Sam", "Ryan", "zq-handle-jon", "zq-handle-jon/zq-agent-jon-api", "zq-handle-lee",
            "nobody@example.test")]:
        r = page.request.get(base + path)
        scan.texts.append((f"GET {path}", json.dumps(dict(r.headers)) + " " + r.text()))
    # The picker, opened and filtered (its DOM is built from /app/recipients).
    _visit(scan, page, base, "/app/compose", "app compose")
    page.click("#to")
    page.wait_for_selector(".rc-picker-list:not([hidden])")
    scan.snapshot(page, "app picker open")
    page.fill("#to", "zq")
    scan.snapshot(page, "app picker filtered by a handle prefix")
    # Refused sends by handle: the error is the only place a typed handle may map to its person (C2.2).
    for to in ("zq-handle-jon", "zq-handle-jon/zq-agent-jon-api", "zq-handle-priya", "zq-handle-lee", "nobody"):
        r = page.request.post(base + "/app/conversations/new/send", form={
            "csrf_token": page.evaluate("() => document.body.getAttribute('data-csrf')"), "to": to, "body": "x",
            "message_id": "00000000-0000-4000-8000-000000000001"})
        # The refusal echoes nothing but presentation's text; the form keeps the typed value only in the input.
        body = re.sub(r'<input[^>]*name="to"[^>]*>', "", r.text())
        scan.texts.append((f"POST send to {to}", body))


def _website_pages(scan, page, base, seed):  # noqa: F811
    for path in ("/app/inbox", "/app/compose", "/app/compose?to=jon@example.test", "/app/archived", "/app/agents",
                 "/app/teams/northwind", "/app/team", "/app/account") + tuple(
            f"/app/conversations/{c.id}" for c in seed["conversations"].values()):
        _visit(scan, page, base, path, f"website {path}")


def _own_allowed(label, text):
    """C5.1: the owner's own handles and agent names appear only on their own pages (This computer, the website
    Machines and team pages, owner warnings, the inbox setting); remove those parts before scanning."""
    if "/app/agents" in label or "/app/teams/" in label or "/app/team" in label or "this-computer" in label:
        return ""
    text = re.sub(r'<div class="owner-warnings".*?</div>', "", text, flags=re.S)
    text = re.sub(r'<section class="inbox-banner".*?</section>', "", text, flags=re.S)
    return text


def test_a_member_never_sees_a_teammates_machine_or_agent(live, browser, v06_seed):  # noqa: F811
    scan = Scan(live)
    for opener, who in ((_app, v06_seed["person_tokens"]["Maya"]), (_website, "maya@example.test")):
        context, page = opener(browser, live, who)
        scan.watch(page)
        try:
            (_app_pages if opener is _app else _website_pages)(scan, page, live, v06_seed)
        finally:
            context.close()
    assert len(scan.texts) > 60
    assert scan.leaks(TEAMMATE_STRINGS) == []
    assert scan.leaks(OWN_STRINGS, _own_allowed) == []  # her own only on her own pages


def test_a_teammate_never_sees_the_members_machines_or_agents(live, browser, v06_seed):  # noqa: F811
    scan = Scan(live)
    context, page = _app(browser, live, v06_seed["person_tokens"]["Jon"])
    scan.watch(page)
    try:
        for path in ("/app/inbox", "/app/people", "/app/compose", "/app/archived") + tuple(
                f"/app/conversations/{v06_seed['conversations']['jon'].id}" for _ in (0,)):
            _visit(scan, page, live, path, f"jon app {path}")
        for summary in page.locator("details.rc-trail-box > summary").all():
            summary.click()
        scan.snapshot(page, "jon conversation, trails open")
        for q in ("maya@example.test", "zq-handle-maya", "zq-handle-maya/zq-agent-maya-laptop"):
            r = page.request.get(f"{live}/app/person-warning?to={q}")
            scan.texts.append((f"jon warning {q}", r.text()))
    finally:
        context.close()
    context, page = _website(browser, live, "jon@example.test")
    scan.watch(page)
    try:
        _visit(scan, page, live, "/app/agents", "jon website machines")
        _visit(scan, page, live, "/app/teams/northwind", "jon website team")
        _visit(scan, page, live, f"/app/conversations/{v06_seed['conversations']['jon'].id}", "jon website conversation")
    finally:
        context.close()
    assert scan.leaks(OWN_STRINGS) == []
    strangers = tuple(s for s in TEAMMATE_STRINGS if not s.startswith(("zq-handle-jon", "zq-agent-jon")))
    assert scan.leaks(strangers, _own_allowed) == []


def test_a_team_owner_sees_machines_and_versions_but_no_agents(live, browser, v06_seed):  # noqa: F811
    context, page = _website(browser, live, "olive@example.test")
    try:
        page.goto(live + "/app/agents")
        machines = page.content()
        page.goto(live + "/app/teams/northwind")
        team = page.content()
    finally:
        context.close()
    for handle in ("zq-handle-jon", "zq-handle-priya", "zq-handle-sam-app", "zq-handle-ryan", "zq-handle-maya"):
        assert handle in machines and handle in team, handle
    assert "v0.5.6" in machines or "0.5.6" in machines
    for agent in ("zq-agent-jon-api", "zq-agent-priya-web", "zq-agent-ryan-old", "zq-agent-maya-laptop",
                  "zq-agent-maya-notes"):
        assert agent not in machines and agent not in team, agent
    assert "zq-handle-lee" not in machines  # another team


def test_every_app_get_route_is_scanned_or_exempted(live):  # noqa: F811
    from raincli_server.app import create_app
    from raincli_server.config import load_settings

    import os

    app = create_app(load_settings({"RAINCLI_DATABASE_URL": os.environ.get("RAINCLI_TEST_DATABASE_URL", "postgresql://x"),
                                    "RAINCLI_SECRET_KEY": "s" * 48, "RAINCLI_PUBLIC_URL": "http://testserver"}))
    unknown = _app_routes(app) - COVERED - set(EXEMPT)
    assert unknown == set(), f"not in the page privacy scan: {sorted(unknown)}"
    assert not (set(EXEMPT) & COVERED)
