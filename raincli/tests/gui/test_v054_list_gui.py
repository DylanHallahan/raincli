"""The v0.5.4 app-mode list in headless Chromium (protocol §19.5, §19.7; brief criteria 1, 2, 14, 15, 16).

Same harness as test_app_mode_gui.py: a real uvicorn server on the throwaway test database, signed in through a
real handoff with the app's install-token User-Agent. Skipped without Playwright or its browser.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
import urllib.request
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

PASSWORD = "correct horse battery"
APP_TOKEN = "gui-v054-install-token-" + "x" * 24
INSTALL_HASH = hashlib.sha256(APP_TOKEN.encode()).hexdigest()
CLIENT = {"version": "0.5.4", "update_mode": "automatic", "update_state": "current"}
# The app window's sizes: the minimum (window.py min_size) and larger ones up to a big monitor.
WINDOW_SIZES = [(760, 520), (800, 600), (1000, 700), (1180, 780), (1600, 1000)]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(settings, engine):
    import uvicorn

    from raincli_server.app import create_app

    port = _free_port()
    live_settings = replace(settings, public_url=f"http://127.0.0.1:{port}", max_pending=1000)
    app = create_app(live_settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    yield live_settings.public_url
    server.should_exit = True
    thread.join(10)
    app.state.engine.dispose()


@pytest.fixture
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except playwright_api.Error as exc:
            pytest.skip(f"headless Chromium is not installed: {exc}")
        yield b
        b.close()


def api(base, path, body=None, token=None, method=None):
    request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     method=method or ("POST" if body is not None else "GET"),
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def entry(name, key, status="idle"):
    return {"key": key, "name": name, "type": "claude", "status": status, "role": None,
            "reachability": "instant", "source": "herdr"}


@pytest.fixture
def team(live, session):
    """Maya (the viewer); Jon's api-agent ready, Priya's web-agent busy, Sam's ops-agent offline."""
    from sqlalchemy import update

    from raincli_server import identity
    from raincli_server.models import AgentPresence

    users = {n: identity.create_user(session, f"{n.lower()}@example.test", n, PASSWORD)
             for n in ("Maya", "Jon", "Priya", "Sam")}
    acme = identity.create_team(session, "northwind", "Northwind", users["Maya"])
    for n in ("Jon", "Priya", "Sam"):
        identity.add_member(session, acme, users[n])
    machines = {h: identity.register_agent(session, acme, users[o], h)
                for h, o in (("jon-laptop", "Jon"), ("priya-desktop", "Priya"), ("sam-server", "Sam"))}
    session.commit()
    for handle, name, status in (("jon-laptop", "api-agent", "idle"), ("priya-desktop", "web-agent", "working"),
                                 ("sam-server", "ops-agent", "idle")):
        api(live, "/api/v1/presence", {"status": "ready", "agents": [entry(name, "a" * 32, status)], "client": CLIENT},
            machines[handle][1], method="PUT")
    # §21: each teammate picks their agent as their inbox (with their machine's own credential, C13 L4), and the
    # machine polls with routing once, so named delivery needs no update.
    for handle, name, _ in (("jon-laptop", "api-agent", "idle"), ("priya-desktop", "web-agent", "working"),
                            ("sam-server", "ops-agent", "idle")):
        api(live, "/api/v1/me/inbox", {"agent": name}, machines[handle][1], method="PUT")
        api(live, "/api/v1/inbox?routing=1&wait=0", None, machines[handle][1])
    session.execute(update(AgentPresence).where(AgentPresence.agent_id == machines["sam-server"][0].id)
                    .values(seen_at=AgentPresence.seen_at - timedelta(minutes=30)))
    session.commit()
    apps = {}
    for name, machine in (("maya", "maya-app"), ("jon", "jon-app")):
        login = api(live, "/api/v1/app/login", {"email": f"{name}@example.test", "password": PASSWORD,
                                                "machine_name": machine, "person_session": True})
        # The app's own machine reports its client, as the running app does.
        api(live, "/api/v1/presence", {"status": "ready", "client": {**CLIENT, "version": "0.5.5"}}, login["token"],
            method="PUT")
        apps[name] = login["person_session"]
    maya, jon = apps["maya"], apps["jon"]
    return {"base": live, "session": session, "maya": maya, "jon": jon, "users": users, "machines": machines}


def person_send(team, token, to, body):
    return api(team["base"], "/api/v1/person/send", {"id": str(uuid.uuid4()), "to": to, "body": body}, token)["message"]


def settle_url(page, quiet=0.3, timeout=10):
    """Wait until the page has stopped navigating (the same URL, loaded, for ``quiet`` seconds)."""
    deadline, last, since = time.monotonic() + timeout, None, time.monotonic()
    while time.monotonic() < deadline:
        url = page.url
        if url != last:
            last, since = url, time.monotonic()
        elif time.monotonic() - since >= quiet:
            page.wait_for_load_state("load")
            return
        time.sleep(0.05)


def open_app(browser, team, person, path="/app/inbox", **options):
    probe = browser.new_page()
    agent = probe.evaluate("navigator.userAgent") + " RainCLIApp/" + APP_TOKEN + " RainCLIAppVersion/0.5.5"  # §20.7: a 0.5.5 window
    probe.close()
    context = browser.new_context(user_agent=agent, **options)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(api(team["base"], "/api/v1/app/handoff", {"app_install_hash": INSTALL_HASH}, person)["url"])
    settle_url(page)  # v0.5.5 §20.6 M1: Inbox may move on to the latest conversation by itself
    if path != "/app/inbox":
        page.goto(team["base"] + path)
    page.errors = errors
    return context, page


def size(w, h):
    return {"viewport": {"width": w, "height": h}}


# Criterion 2: the list at the minimum window size and every larger one ----------------------------------------

def test_the_list_shows_at_the_minimum_window_and_every_larger_size(team, browser):
    conv = person_send(team, team["maya"], {"person": "jon@example.test"},
                       "A long first line so the preview has to be cut off at the edge of the list " * 3)
    person_send(team, team["jon"], {"person": "maya@example.test"}, "Unread one")
    pages = ["/app/inbox", f"/app/conversations/{conv['conversation_id']}", "/app/compose"]
    for w, h in WINDOW_SIZES:
        for path in pages:
            context, page = open_app(browser, team, team["maya"], path, **size(w, h))
            listing = page.locator("section.rc-list")
            assert listing.is_visible(), (w, h, path)
            box = listing.bounding_box()
            assert box["width"] >= 200 and box["x"] + box["width"] < w, (w, h, path, box)
            for item in page.locator(".rc-conv-top").all():  # names, counts and times all stay inside the list
                for part in item.locator(".rc-conv-time, .rc-count").all():
                    b = part.bounding_box()
                    assert b and b["x"] + b["width"] <= box["x"] + box["width"], (w, path, b, box)
            assert page.locator(".rc-main").is_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not page.errors
            context.close()


def test_below_the_breakpoint_inbox_shows_the_list_and_other_pages_the_main_pane(team, browser):
    conv = person_send(team, team["maya"], {"person": "jon@example.test"}, "hello")
    context, page = open_app(browser, team, team["maya"], **size(600, 700))
    assert page.locator("section.rc-list").is_visible()
    assert not page.locator(".rc-main").is_visible()
    assert page.locator("text=Select a conversation").count() == 0 or not page.locator("text=Select a conversation").is_visible()
    page.click(".rc-conv")
    page.wait_for_url(f"**/app/conversations/{conv['conversation_id']}**")
    assert not page.locator("section.rc-list").is_visible() and page.locator(".rc-main").is_visible()
    context.close()


# Criterion 14: the Team section ----------------------------------------------------------------------------------



def test_the_page_works_when_local_storage_throws(team, browser):
    context, page = open_app(browser, team, team["maya"], **size(1180, 780))
    context.add_init_script("Object.defineProperty(window, 'localStorage', {get() { throw new Error('blocked'); }});")
    page.reload()
    assert page.locator(".rc-list").is_visible() and page.locator(".rc-team").count() == 0  # §21: no Team section
    assert not page.errors
    context.close()


# Criterion 15: day groups in the browser's local time ---------------------------------------------------------

def _label(when, zone, now):
    local, today = when.astimezone(zone).date(), now.astimezone(zone).date()
    return "Today" if local == today else "Yesterday" if local == today - timedelta(days=1) else "Earlier"


def test_day_groups_follow_the_viewers_zone_and_need_javascript(team, browser):
    from sqlalchemy import update

    from raincli_server.models import Message

    east, west = "Pacific/Kiritimati", "Pacific/Pago_Pago"  # UTC+14 and UTC-11: a day apart
    now = datetime.now(timezone.utc)
    # Ages (hours) whose labels differ between the two zones, plus a plain Earlier.
    ages = [h for h in range(1, 72) if _label(now - timedelta(hours=h), ZoneInfo(east), now)
            != _label(now - timedelta(hours=h), ZoneInfo(west), now)][:2] + [24 * 5]
    targets = [{"person": "jon@example.test"}, {"person": "priya@example.test"},
               {"person": "sam@example.test"}]
    sent = []
    for target, hours in zip(targets, sorted(ages, reverse=True)):  # oldest first: the list is in send order
        m = person_send(team, team["maya"], target, f"{hours} hours ago")
        sent.append((m["id"], hours))
    for mid, hours in sent:
        team["session"].execute(update(Message).where(Message.id == uuid.UUID(mid))
                                .values(created_at=now - timedelta(hours=hours)))
    team["session"].commit()
    seen = {}
    for zone in (east, west):
        context, page = open_app(browser, team, team["maya"], timezone_id=zone, **size(1180, 780))
        items = page.locator(".rc-convs > li").all()
        got = [("H:" + i.inner_text().strip()) if "rc-day" in (i.get_attribute("class") or "") else
               i.locator(".rc-conv-preview").inner_text().split(": ", 1)[1] for i in items]
        expected = []
        for _, hours in sorted(sent, key=lambda s: s[1]):
            label = "H:" + _label(now - timedelta(hours=hours), ZoneInfo(zone), now)
            if label not in expected:
                expected.append(label)
            expected.append(f"{hours} hours ago")
        assert got == expected, (zone, got, expected)
        assert page.locator(".rc-day h2").count() == len([e for e in expected if e.startswith("H:")])
        seen[zone] = [g for g in got if g.startswith("H:")]
        assert not page.errors
        context.close()
    assert seen[east] != seen[west]
    context, page = open_app(browser, team, team["maya"], java_script_enabled=False, **size(1180, 780))
    assert page.locator(".rc-day").count() == 0
    assert page.locator(".rc-convs > li.rc-conv-item").count() == 3
    context.close()


# Criterion 16: first run ---------------------------------------------------------------------------------------

def test_first_run_inbox_shows_three_steps_with_the_first_teammate(team, browser):
    for w, h in ((1180, 780), (760, 520), (600, 700)):
        context, page = open_app(browser, team, team["maya"], **size(w, h))
        steps = page.locator(".rc-steps .rc-step")
        assert steps.count() == 3 and steps.first.is_visible(), (w, h)
        assert [s.locator("strong").inner_text() for s in steps.all()] == [
            "Pick your inbox agent", "See who's on your team", "Send your first message"]  # §21 C7.6
        hrefs = [s.get_attribute("href") for s in steps.all()]
        assert hrefs[0].endswith("/app/local/this-computer") and hrefs[1].endswith("/app/people")
        assert hrefs[2].endswith("/app/compose?to=jon%40example.test")  # step 3 pre-fills a person
        context.close()
    context, page = open_app(browser, team, team["maya"], **size(1180, 780))
    page.click(".rc-step:has-text('Send your first message')")
    page.wait_for_selector(".rc-chip-picked:not([hidden])")  # the picker shows the person, pre-picked
    assert "Jon" in page.locator(".rc-chip-picked").inner_text()
    context.close()


def test_first_run_without_teammates_opens_a_blank_new_message(team, browser):
    """§21 C7.6: step 3 pre-fills a person; with no teammates it opens a blank New message."""
    from raincli_server.models import Membership, Team, User

    session = team["session"]
    northwind = session.query(Team).filter_by(slug="northwind").one()
    maya = session.query(User).filter_by(email="maya@example.test").one()
    session.query(Membership).filter(Membership.team_id == northwind.id, Membership.user_id != maya.id).delete()
    session.commit()
    context, page = open_app(browser, team, team["maya"], **size(1180, 780))
    assert page.locator(".rc-team").count() == 0
    assert page.locator(".rc-step").nth(2).get_attribute("href").endswith("/app/compose")
    context.close()


# Criterion 1, list side: the list and Team section show no accent-coloured avatars ----------------------------

def test_the_list_uses_no_accent_avatars(team, browser):
    person_send(team, team["jon"], {"person": "maya@example.test"}, "hi")
    context, page = open_app(browser, team, team["maya"], **size(1180, 780))
    assert page.locator(".rc-list .rc-avatar").count() == 0
    accent = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--rc-accent-soft').trim()")
    assert accent
    backgrounds = page.locator(".rc-list .rc-conv, .rc-list .rc-team-agent").evaluate_all(
        "els => els.map(e => getComputedStyle(e).backgroundColor)")
    probe = page.evaluate(f"() => {{ const p = document.createElement('i'); p.style.color = '{accent}'; document.body.append(p);"
                          " const c = getComputedStyle(p).color; p.remove(); return c; }")
    assert probe not in backgrounds
    context.close()


# Criterion 8, list side: the server's conversation names, also in the preview ---------------------------------

def test_the_list_names_people(team, browser):
    ask = person_send(team, team["maya"], {"person": "jon@example.test"}, "Can you check?")
    api(team["base"], "/api/v1/messages", {"id": str(uuid.uuid4()), "to": {"person": "maya@example.test"},
                                          "from_agent": "api-agent", "body": "Two failures.", "in_reply_to": ask["id"]},
        team["machines"]["jon-laptop"][1])
    person_send(team, team["maya"], {"person": "priya@example.test"}, "Retry cap?")
    context, page = open_app(browser, team, team["maya"], **size(1180, 780))
    names = [t.strip() for t in page.locator(".rc-conv-name").all_inner_texts()]
    previews = [t.strip() for t in page.locator(".rc-conv-preview").all_inner_texts()]
    assert names == ["Priya", "Jon"]  # §21 C5.1: people, never their agents
    # §20.4 R8: "You:" only for your own messages; otherwise the text alone.
    assert previews[0].endswith("You: Retry cap?") and previews[1].endswith("Two failures.")
    assert "api-agent" not in previews[1] and "Jon · agent:" not in previews[1]
    context.close()



# §19.8 L3 (the Team dots) and L1 (no inline styles in the sidebar) ---------------------------------------------