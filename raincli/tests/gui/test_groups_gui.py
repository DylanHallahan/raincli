"""The app's group pages (protocol §22 G6, D9, D12; C14–C18) in headless Chromium, on the shared group seed
(tests/conftest_groups.py), with JavaScript running: open latest picks a group when its post is the newest activity
and a 1:1 conversation otherwise (G12 L4), a hidden launch onto a group acks nothing, the live poll brings new posts
and "Delivered to N of M", the member panel opens and is read-only, and the page fits a small window."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from conftest_groups import groups_seed, make_post  # noqa: F401  (fixture)
from conftest_v06 import v06_seed  # noqa: F401  (fixture)

from .test_app_mode_gui import INSTALL_HASH, api, app_user_agent, browser, live  # noqa: F401

pytest.importorskip("playwright.sync_api")

NO_FOCUS = "window.__focus = false; document.hasFocus = () => window.__focus;"


def open_app(browser, base, token, *, width=1180, height=780, init_script=None, scheme="light"):  # noqa: F811
    """An app window (a 0.5.5+ User-Agent: Inbox opens the latest, §20.7), signed in through the handoff. It records
    every POST /app/ack the page makes."""
    context = browser.new_context(user_agent=app_user_agent(browser), viewport={"width": width, "height": height},
                                  color_scheme=scheme)
    if init_script:
        context.add_init_script(init_script)
    page = context.new_page()
    page.acks, page.errors = [], []
    page.on("request", lambda r: page.acks.append(r.post_data) if r.url.endswith("/app/ack") else None)
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto(api(base, "/api/v1/app/handoff", {"app_install_hash": INSTALL_HASH}, token)["url"])
    page.wait_for_load_state("networkidle")
    return context, page


def group_url(seed, name="checkout"):
    return f"/app/groups/{seed['groups'][name].id}"


def test_inbox_opens_the_group_when_its_post_is_newest(live, browser, groups_seed):  # noqa: F811
    s = groups_seed
    context, page = open_app(browser, live, s["person_tokens"]["Maya"])
    try:
        assert page.url.endswith(group_url(s) + "#latest"), page.url
        assert page.get_attribute("nav.rc-nav a[aria-current=page]", "aria-label") == "Inbox"  # a group page is in Inbox
        current = page.locator(".rc-conv-group[aria-current=page]")
        assert current.count() == 1 and current.inner_text().startswith("checkout")
        assert page.locator(".rc-post").count() == 3 and page.locator("textarea").count() == 0
        assert page.evaluate("history.length") == 1 or page.evaluate("history.length") == 2
        assert not page.errors
    finally:
        context.close()


def test_inbox_opens_the_conversation_when_a_1to1_message_is_newest(live, browser, groups_seed):  # noqa: F811
    s = groups_seed
    sent = api(live, "/api/v1/messages", {"id": str(uuid.uuid4()), "to": "maya@example.test", "body": "Newer."},
               s["machine_tokens"]["jon"])["message"]
    context, page = open_app(browser, live, s["person_tokens"]["Maya"])
    try:
        assert page.url.endswith(f"/app/conversations/{sent['conversation_id']}#latest"), page.url
        assert page.locator(".rc-conv-group[aria-current=page]").count() == 0
    finally:
        context.close()


def test_a_hidden_launch_onto_a_group_acks_nothing(live, browser, groups_seed, session):  # noqa: F811
    """G12 L4, added to C17's checks: the window starts hidden and unfocused, opens the newest (a group), polls, then
    gains focus. Nothing is acked: no POST /app/ack at all, and no copy or message changes."""
    from raincli_server.models import Message

    s = groups_seed
    snapshot = lambda: sorted((str(m.id), m.acked_at, m.person_seen_at, m.delivery_state)  # noqa: E731
                              for m in session.scalars(select(Message)).all())
    session.expire_all()
    before = snapshot()
    context, page = open_app(browser, live, s["person_tokens"]["Maya"], init_script=NO_FOCUS)
    try:
        assert page.url.endswith(group_url(s) + "#latest")
        page.evaluate("() => window.rcLive && window.rcLive.poll()")
        page.wait_for_timeout(800)
        page.evaluate("() => { window.__focus = true; window.dispatchEvent(new Event('focus')); }")
        page.evaluate("() => window.rcLive && window.rcLive.poll()")
        page.wait_for_timeout(800)
        assert page.acks == []
        session.expire_all()
        assert snapshot() == before
    finally:
        context.close()


def test_new_posts_and_delivered_counts_arrive_live(live, browser, groups_seed, session):  # noqa: F811
    s = groups_seed
    context, page = open_app(browser, live, s["person_tokens"]["Maya"])
    try:
        page.goto(live + group_url(s))
        page.wait_for_load_state("networkidle")
        mine, copies = make_post(session, s["groups"]["checkout"], s["machines"]["maya"], "zq-agent-maya-laptop",
                                 "@Jon does `/orders` return `currency` now?", mentions=[s["people"]["Jon"]])
        theirs, _ = make_post(session, s["groups"]["checkout"], s["machines"]["jon"], "zq-agent-jon-api",
                              "Yes, since this morning.", in_reply_to=mine)
        session.commit()
        page.evaluate("() => window.rcLive.poll()")
        page.wait_for_selector(f"#p-{theirs.id}")
        assert page.locator(".rc-post").count() == 5
        assert page.locator(f"#p-{mine.id} .rc-delivered").inner_text() == "Delivered to 0 of 2"
        assert page.locator(f"#p-{theirs.id}").inner_text().startswith("JO\nJon · agent")
        assert page.locator(f"#p-{theirs.id} .rc-post-reply").inner_text() == "Reply to Maya · agent"
        assert page.locator(f"#p-{mine.id} .rc-mention").all_inner_texts() == ["@Jon"]
        for c in copies:
            c.delivery_state = "received"
        session.commit()
        page.evaluate("() => window.rcLive.poll()")
        page.wait_for_function(f"() => document.querySelector('[data-delivered=\"{mine.id}\"]').textContent"
                               " === 'Delivered to 2 of 2'")
        assert page.acks == [] and not page.errors
    finally:
        context.close()


@pytest.mark.parametrize("width,height", [(1180, 780), (760, 520)])
def test_the_member_panel_opens_read_only_and_fits(live, browser, groups_seed, width, height):  # noqa: F811
    s = groups_seed
    context, page = open_app(browser, live, s["person_tokens"]["Jon"], width=width, height=height)
    try:
        page.goto(live + group_url(s))
        page.wait_for_load_state("networkidle")
        page.click(".rc-members > summary")
        panel = page.locator(".rc-members-panel")
        assert panel.is_visible()
        assert panel.locator("li").all_inner_texts() == ["JO\nJon", "MA\nMaya", "PR\nPriya", "RY\nRyan\nnot taking part"]
        assert "Ask Dylan to change who's in this group." in panel.inner_text()
        assert panel.locator("button, input, select, textarea, a, form").count() == 0
        box = panel.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width and box["y"] + box["height"] <= height, box
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.keyboard.press("Escape")
        assert not page.errors
    finally:
        context.close()
