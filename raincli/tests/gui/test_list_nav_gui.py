"""The app's list and rail polish (brief "RainCLI app list and nav polish", 2026-10-30) in headless Chromium, on the
shared group seed: groups are rows of the one conversation list, ordered with the 1:1 conversations by newest
activity (and kept so by the live poll), and the rail is icons only, with each link named by its aria-label and the
same word as its tooltip, 44px targets inside the 64px rail, the current page visible and keyboard focus rings, in
light and dark at 1180x780 and 760x520."""

from __future__ import annotations

import uuid

import pytest

from conftest_groups import groups_seed, make_post  # noqa: F401  (fixture)
from conftest_v06 import v06_seed  # noqa: F401  (fixture)

from .test_app_mode_gui import api, browser, live  # noqa: F401
from .test_groups_gui import open_app

pytest.importorskip("playwright.sync_api")

WORDS = ("Inbox", "People", "Archived", "This computer", "Settings")
ROWS = """() => Array.from(document.querySelectorAll('.rc-convs .rc-conv')).map((a) =>
    [a.classList.contains('rc-conv-group') ? 'group' : 'conversation', a.querySelector('.rc-conv-name').innerText.replace(/\\s+/g, ' ').trim()])"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width,height", [(1180, 780), (760, 520)])
def test_the_rail_is_icons_only_named_and_fits(live, browser, groups_seed, width, height, scheme):  # noqa: F811
    context, page = open_app(browser, live, groups_seed["person_tokens"]["Maya"], width=width, height=height,
                             scheme=scheme)
    try:
        page.goto(live + "/app/people")
        page.wait_for_load_state("networkidle")
        nav = page.locator("nav.rc-nav")
        rail = nav.locator("a")
        assert [t.strip() for t in rail.all_inner_texts()] == [""] * 5  # no visible words
        for word in WORDS:  # the accessible name, and the same word as the tooltip
            link = page.get_by_role("link", name=word, exact=True)
            assert link.count() == 1 and link.get_attribute("title") == word, word
        box = nav.bounding_box()
        assert round(box["width"]) == 64
        for i in range(5):
            b = rail.nth(i).bounding_box()
            assert b["width"] >= 44 and b["height"] >= 44, (i, b)  # 44px targets
            assert box["x"] <= b["x"] and b["x"] + b["width"] <= box["x"] + box["width"], (i, b)  # inside the rail
            assert b["y"] + b["height"] <= height, (i, b)
        assert nav.evaluate("n => n.scrollHeight <= n.clientHeight")  # nothing hidden below the rail
        # The current page stays visible: its own background and the accent icon.
        current = page.locator("nav.rc-nav a[aria-current=page]")
        assert current.get_attribute("aria-label") == "People"
        style = lambda loc: loc.evaluate("a => [getComputedStyle(a).backgroundColor, getComputedStyle(a).color]")  # noqa
        other = page.locator("nav.rc-nav a[aria-label='Archived']")
        assert style(current)[0] != style(other)[0] and style(current)[1] != style(other)[1]
        # Keyboard focus shows the ring.
        page.evaluate("document.activeElement && document.activeElement.blur()")
        for _ in range(12):
            page.keyboard.press("Tab")
            if page.evaluate("!!document.activeElement.closest('nav.rc-nav')"):
                break
        ring = page.evaluate("[getComputedStyle(document.activeElement).outlineStyle, "
                             "getComputedStyle(document.activeElement).outlineWidth]")
        assert page.evaluate("!!document.activeElement.closest('nav.rc-nav')") and ring == ["solid", "2px"], ring
        assert not page.errors
    finally:
        context.close()


def test_groups_are_rows_of_the_list_and_the_live_poll_keeps_the_order(live, browser, groups_seed, session):  # noqa: F811
    s = groups_seed
    context, page = open_app(browser, live, s["person_tokens"]["Maya"])
    try:
        page.goto(live + "/app/compose")  # the list, without Inbox opening the newest
        page.wait_for_load_state("networkidle")
        assert page.locator("#groups-title, .rc-list-sub").count() == 0 and page.locator(".rc-convs").count() == 1
        rows = page.evaluate(ROWS)
        assert rows[0] == ["group", "checkout (group)"] and rows[-1] == ["group", "infra (group)"], rows
        assert page.locator(".rc-conv-group .rc-count").count() == 0
        assert page.locator(".rc-conv-group").first.locator(".rc-conv-preview").inner_text().split() == (
            "Last post from Priya · agent: Updated the web totals too.".split())  # "Last post from" is for screen readers
        assert page.get_by_role("link", name="checkout (group)").count() == 1  # the group is named as one
        api(live, "/api/v1/messages", {"id": str(uuid.uuid4()), "to": "maya@example.test", "body": "Newest now."},
            s["machine_tokens"]["jon"])
        page.evaluate("() => window.rcLive.poll()")
        page.wait_for_function("() => !document.querySelector('.rc-convs .rc-conv').classList.contains('rc-conv-group')")
        assert page.evaluate(ROWS)[1] == ["group", "checkout (group)"]
        make_post(session, s["groups"]["checkout"], s["machines"]["jon"], "zq-agent-jon-api", "And `currency` too.")
        session.commit()
        page.evaluate("() => window.rcLive.poll()")
        page.wait_for_function("() => document.querySelector('.rc-convs .rc-conv').classList.contains('rc-conv-group')")
        assert page.locator(".rc-conv-group").first.locator(".rc-conv-preview").inner_text().split() == (
            "Last post from Jon · agent: And `currency` too.".split())
        assert not page.errors
    finally:
        context.close()
