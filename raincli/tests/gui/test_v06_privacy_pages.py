"""The privacy scan, page half (protocol §21 C5.9, C13 M7): the app-mode and website pages in headless Chromium, on
the shared v0.6 seed, with JavaScript running (live updates, the picker, the warning fetches). As a member, no
teammate's machine handle or agent name appears in any rendered page, attribute, title or response header; a
teammate never sees the member's own; a team owner's Machines page shows handles and versions but no agent names.
Every route under /app, every method, is scanned here or exempted with a reason; the mutating ones the member uses
(the in-place send, the inbox setting, the refused sends) are driven and their responses scanned. And no read
receipt: what the sender can fetch is the same before and after the recipient reads in their app (H1). The API half
is tests/server/test_v06_privacy_api.py (core)."""

from __future__ import annotations

import difflib
import json
import re
import uuid

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Mount

from conftest_groups import groups_seed  # noqa: F401  (fixture)
from conftest_v06 import OWN_STRINGS, PASSWORD, TEAMMATE_STRINGS, v06_seed  # noqa: F401  (fixture)

from .test_app_mode_gui import INSTALL_HASH, api, app_user_agent, browser, live  # noqa: F401

pytest.importorskip("playwright.sync_api")

# Routes under /app, by method, and how this file covers them; anything else fails the scan.
COVERED = {f"GET {p}" for p in (
    "/app", "/app/inbox", "/app/people", "/app/compose", "/app/conversations/{conversation_id}", "/app/archived",
    "/app/live", "/app/recipients", "/app/person-warning", "/app/agents", "/app/teams/{slug}", "/app/team",
    "/app/account", "/app/local/{page}",
    "/app/groups/{group_id}")} | {  # §22 G6: scanned on the group seed (test_group_pages_*)
    "POST /app/send",  # driven: the member's in-place sends, their JSON (message_html) scanned
    "POST /app/inbox",  # driven: picking her inbox from the banner, and a teammate's machine (refused)
    "POST /app/conversations/{conversation_id}/send",  # driven: refused sends by handle; a website send
    "POST /app/ack",  # driven: the recipient reading in the app (the no-read-receipt diff)
    "POST /app/conversations/{conversation_id}/archive",  # driven, the redirect's page scanned
    "POST /app/conversations/{conversation_id}/unarchive",  # driven, the redirect's page scanned
}
EXEMPT = {
    "GET /app/handoff": "a one-time sign-in redirect: no page content",
    "GET /app/messages/{message_id}/attachments/{attachment_id}": "attachment bytes (no attachments in the seed); "
                                                                  "their metadata is on the conversation pages scanned",
    "GET /app/teams/{slug}/members/{user_id}/remove": "team-owner-only confirmation, about a member's own machines "
                                                      "(§21 C5.8 allows an owner to see machines)",
    "POST /app/teams/{slug}/members/{user_id}/remove": "team-owner-only; answers a redirect to the team page (scanned)",
    "POST /app/account/password": "the viewer's own password; redirects to the account page (scanned)",
    "POST /app/account/apps/{session_id}/revoke": "the viewer's own app sign-ins; redirects to account (scanned)",
    "POST /app/agents": "adds one of the viewer's own machines; its credential page is their own",
    "POST /app/agents/{agent_id}/rotate": "the viewer's own machine credential; their own page",
    "POST /app/agents/{agent_id}/revoke": "the viewer's own machine; redirects to Machines (scanned)",
    "POST /app/agents/{agent_id}/config": "the viewer's own machine's config file download",
    "POST /app/teams/{slug}/invitations": "team-owner-only; an invitation link, no teammate data",
    "POST /app/teams/{slug}/invitations/{invitation_id}/revoke": "team-owner-only; redirects to the team page",
    "POST /app/teams/{slug}/agents/{agent_id}/revoke": "team-owner-only (§21 C5.8); redirects to the team page",
    "POST /app/layout-diag": "the temporary layout diagnostic: accepts a report, answers 204 with no body",
}


def _app_routes(app) -> set[str]:
    """Every "METHOD path" under /app. FastAPI 0.142 keeps an included router behind an _IncludedRouter whose
    routes aren't in app.routes, so this walks into it (and into mounts)."""
    found, stack = set(), [("", r) for r in app.routes]
    while stack:
        prefix, route = stack.pop()
        if isinstance(route, APIRoute):
            path = prefix + route.path
            if path.startswith("/app"):
                found |= {f"{m} {path}" for m in route.methods if m != "HEAD"}
            continue
        if isinstance(route, Mount):  # /api/v1 (the API scan's) and /static
            stack.extend((prefix + route.path, r) for r in getattr(route.app, "routes", None) or [])
            continue
        inner = getattr(route, "original_router", None)
        stack.extend((prefix, r) for r in getattr(inner, "routes", None) or [])
    return found


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
        # Attribute values inside the owner-only parts (owner warnings, the inbox banner) are wrapped in the same
        # markup, so the owner scan strips them with their part; the teammate scan reads everything.
        dom = page.evaluate("""() => document.documentElement.outerHTML + ' ' + document.title + ' ' +
            Array.from(document.querySelectorAll('*')).map((e) => {
              const values = Array.from(e.attributes, (a) => a.value).join(' ');
              return e.closest('.owner-warnings, .inbox-banner') ? '<section class="inbox-banner">' + values + '</section>' : values;
            }).join(' ')""")
        self.texts.append((label, dom))

    def leaks(self, strings, allow=lambda label, text: text):
        return [(label, [s for s in strings if s in allow(label, text)]) for label, text in self.texts
                if any(s in allow(label, text) for s in strings)]


def _app(browser, base, token, init_script=None):  # noqa: F811
    context = browser.new_context(user_agent=app_user_agent(browser), viewport={"width": 1180, "height": 780})
    if init_script:
        context.add_init_script(init_script)
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
    _app_writes(scan, page, base, convs)


def _post(scan, page, base, path, label, **form):
    form.setdefault("csrf_token", page.evaluate("() => document.body.getAttribute('data-csrf')"))
    r = page.request.post(base + path, form=form)
    scan.texts.append((f"POST {path} ({label}) -> {r.status} {r.url}", json.dumps(dict(r.headers)) + " " + r.text()))
    return r


def _app_writes(scan, page, base, convs):
    """The member's mutating requests, each response scanned (M2): an in-place send through the composer and into
    every pair conversation (the JSON's message_html), archive and unarchive, a refused inbox (a teammate's
    machine), and picking her own inbox from the banner, last (it moves her waiting messages)."""
    _visit(scan, page, base, f"/app/conversations/{convs['jon']}", "app conversation jon, before a send")
    page.fill(".rc-compose textarea#body", "typed in the app")
    with page.expect_response("**/app/send") as sent:
        page.click(".rc-compose button.rc-send")
    assert sent.value.status == 200 and "message_html" in sent.value.json()
    scan.texts.append(("POST /app/send (composer)", json.dumps(dict(sent.value.headers)) + " " + sent.value.text()))
    page.wait_for_timeout(300)
    scan.snapshot(page, "app conversation jon, after an in-place send")
    for key in ("sam", "priya", "jon"):
        r = _post(scan, page, base, "/app/send", f"into {key}", conversation_id=convs[key],
                  message_id=str(uuid.uuid4()), body=f"in place to {key}")
        assert r.status == 200 and "message_html" in r.json(), r.text()
    seq = page.evaluate("() => Math.max(...Array.from(document.querySelectorAll('[data-seq]'), (e) => +e.dataset.seq))")
    _post(scan, page, base, f"/app/conversations/{convs['priya']}/archive", "archive", archived_through_seq=str(seq))
    _post(scan, page, base, f"/app/conversations/{convs['priya']}/unarchive", "unarchive", back="archived")
    for machine, agent in (("zq-handle-jon", "zq-agent-jon-api"), ("zq-handle-nobody", "x")):
        _post(scan, page, base, "/app/inbox", f"refused {machine}", team="northwind", machine=machine, agent=agent)
    _visit(scan, page, base, "/app/inbox", "app inbox, the banner")
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/app/inbox")) as picked:
        page.click("section.inbox-banner[aria-labelledby=inbox-banner-northwind] button[type=submit]")
    assert "notice=inbox-set" in picked.value.headers.get("location", ""), picked.value.headers
    scan.texts.append(("POST /app/inbox (the banner)", json.dumps(dict(picked.value.headers))))
    page.wait_for_load_state("networkidle")
    scan.snapshot(page, "app after picking her inbox")


def _website_pages(scan, page, base, seed):  # noqa: F811
    for path in ("/app/inbox", "/app/compose", "/app/compose?to=jon@example.test", "/app/archived", "/app/agents",
                 "/app/teams/northwind", "/app/team", "/app/account") + tuple(
            f"/app/conversations/{c.id}" for c in seed["conversations"].values()):
        _visit(scan, page, base, path, f"website {path}")
    jon = str(seed["conversations"]["jon"].id)
    _visit(scan, page, base, f"/app/conversations/{jon}", "website conversation jon, before a send")
    page.fill("textarea#body", "typed on the website")
    with page.expect_navigation():
        page.click("form[action$='/send'] button[type=submit]")
    scan.snapshot(page, "website conversation jon, after a send")


def _own_allowed(label, text):
    """C5.1: the owner's own handles and agent names appear only on their own pages (This computer, the website
    Machines, team and account pages, owner warnings, the inbox setting); remove those parts before scanning."""
    if any(own in label for own in ("/app/agents", "/app/teams/", "/app/team", "this-computer", "/app/account")):
        return ""
    text = text.replace('\\"', '"').replace("\\n", "\n").replace("\\u003c", "<").replace("\\u003e", ">")  # JSON-escaped HTML
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
    own = scan.leaks(OWN_STRINGS, _own_allowed)
    assert own == [], [(label, found) for label, found in own][:5]  # her own only on her own pages


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


# §22 G6 (C19): the group pages, on the group seed ------------------------------------------------------------

def _group_pages(scan, page, base, seed, label):
    """The viewer's Inbox (which may open a group), every group page with its member panel open, after a poll, and
    the live JSON of each group."""
    _visit(scan, page, base, "/app/inbox", f"{label} inbox")
    for name, group in seed["groups"].items():
        _visit(scan, page, base, f"/app/groups/{group.id}", f"{label} group {name}")
        if page.locator(".rc-members > summary").count():
            page.click(".rc-members > summary")
        page.evaluate("() => window.rcLive && window.rcLive.poll()")
        page.wait_for_timeout(300)
        scan.snapshot(page, f"{label} group {name}, members open, after a poll")
        r = page.request.get(f"{base}/app/live?group={group.id}&after=0")
        scan.texts.append((f"{label} GET /app/live?group={name}", json.dumps(dict(r.headers)) + " " + r.text()))


def test_group_pages_never_show_a_members_machine_or_agent(live, browser, groups_seed):  # noqa: F811
    """As the poster (Maya) and as a teammate in the group (Jon): posts show "<Name> · agent", never the machine, its
    handle or the stated agent name (zq-agent-maya-laptop is the first post's author_agent_name); the member panel
    shows people only."""
    seed = groups_seed
    scan = Scan(live)
    context, page = _app(browser, live, seed["person_tokens"]["Maya"])
    scan.watch(page)
    try:
        _group_pages(scan, page, live, seed, "maya")
    finally:
        context.close()
    assert len(scan.texts) > 8 and any("Delivered to 2 of 2" in t for _, t in scan.texts)
    assert scan.leaks(TEAMMATE_STRINGS) == []
    assert scan.leaks(OWN_STRINGS, _own_allowed) == []
    jon = Scan(live)
    context, page = _app(browser, live, seed["person_tokens"]["Jon"])
    jon.watch(page)
    try:
        _group_pages(jon, page, live, seed, "jon")
    finally:
        context.close()
    assert any("Maya · agent" in t for _, t in jon.texts)
    assert jon.leaks(OWN_STRINGS) == []  # Maya's machine and agent names, never on Jon's pages
    strangers = tuple(s for s in TEAMMATE_STRINGS if not s.startswith(("zq-handle-jon", "zq-agent-jon")))
    assert jon.leaks(strangers, _own_allowed) == []


def test_group_pages_show_non_members_nothing(live, browser, groups_seed):  # noqa: F811
    """Sam (in infra only) and Olive (the team owner, in neither): checkout reads like an unknown id, with nothing of
    its members or posts."""
    seed = groups_seed
    for who in ("Sam", "Olive"):
        scan = Scan(live)
        context, page = _app(browser, live, seed["person_tokens"][who])
        scan.watch(page)
        try:
            checkout = seed["groups"]["checkout"]
            r = page.request.get(f"{live}/app/groups/{checkout.id}")
            unknown = page.request.get(f"{live}/app/groups/{uuid.uuid4()}")
            assert (r.status, r.text()) == (unknown.status, unknown.text()) and r.status == 404, who
            _visit(scan, page, live, "/app/inbox", f"{who} inbox")
        finally:
            context.close()
        for text in (t for _, t in scan.texts):
            assert "checkout" not in text and str(checkout.id) not in text and "orders API" not in text, who
        assert scan.leaks(tuple(s for s in TEAMMATE_STRINGS + OWN_STRINGS
                                if not s.startswith((f"zq-handle-{who.lower()}", f"zq-agent-{who.lower()}")))
                          , _own_allowed) == [], who


# The poll's clock and the compose form's fresh message id differ on every fetch.
VOLATILE = r'data-states-since="[^"]*"|name="message_id" value="[^"]*"'


def _read_by_recipient(session, message_id) -> bool:
    """The recipient person's own read state (§21 H2)."""
    from raincli_server.models import Message

    session.expire_all()
    return session.get(Message, uuid.UUID(message_id)).person_seen_at is not None


def test_no_read_receipt_on_the_senders_pages(live, browser, v06_seed, session):  # noqa: F811
    """H1: Maya's conversation page with Sam, /app/live and her trail as the poll renders it are the same before and
    after Sam reads her message in his app (focused, the message on screen, so live.js acks it), apart from the
    poll's clock."""
    s = v06_seed
    cid, msg = str(s["conversations"]["sam"].id), str(s["messages"]["to_sam"].id)
    context, maya = _app(browser, live, s["person_tokens"]["Maya"])

    def snapshot():
        page = maya.request.get(f"{live}/app/conversations/{cid}").text()
        feed = maya.request.get(f"{live}/app/live?conversation={cid}&after=0").json()
        feed.pop("now")
        _visit(Scan(live), maya, live, f"/app/conversations/{cid}", "")
        maya.evaluate("() => window.rcLive && window.rcLive.poll()")
        maya.wait_for_timeout(400)
        trail = maya.locator(f'[data-trail="{msg}"]').evaluate("(e) => e.outerHTML")
        return [re.sub(VOLATILE, "", page), json.dumps(feed, sort_keys=True), trail]

    try:
        snapshot()  # Maya's own reading settles first
        before = snapshot()
        assert not _read_by_recipient(session, msg)
        sam_context, sam = _app(browser, live, s["person_tokens"]["Sam"], init_script="document.hasFocus = () => true;")
        try:
            sam.goto(f"{live}/app/conversations/{cid}#latest")
            sam.wait_for_selector(f"#m-{msg}")
            sam.evaluate("() => window.dispatchEvent(new Event('focus'))")
            sam.wait_for_function(f"() => !document.querySelector('#m-{msg}').hasAttribute('data-unread')")
            sam.wait_for_load_state("networkidle")
            assert _read_by_recipient(session, msg)
        finally:
            sam_context.close()
        after = snapshot()
        for b, a in zip(before, after):
            assert a == b, "\n".join(difflib.unified_diff(b.splitlines(), a.splitlines(), "before", "after",
                                                           lineterm=""))
    finally:
        context.close()


def test_every_app_route_is_scanned_or_exempted(live):  # noqa: F811
    from raincli_server.app import create_app
    from raincli_server.config import load_settings

    import os

    app = create_app(load_settings({"RAINCLI_DATABASE_URL": os.environ.get("RAINCLI_TEST_DATABASE_URL", "postgresql://x"),
                                    "RAINCLI_SECRET_KEY": "s" * 48, "RAINCLI_PUBLIC_URL": "http://testserver"}))
    routes = _app_routes(app)
    assert len(routes) > 30 and "POST /app/send" in routes  # the walk found the web router (never vacuous)
    unknown = routes - COVERED - set(EXEMPT)
    assert unknown == set(), f"not in the page privacy scan: {sorted(unknown)}"
    assert not (set(EXEMPT) & COVERED)
    assert (COVERED | set(EXEMPT)) <= routes, sorted((COVERED | set(EXEMPT)) - routes)  # no stale entries
