"""The app shell's height (the "everything at the top" bug, server-only defensive fix): nav, list and main always
fill the window, even with an unexpected direct child of <body> or a stale height, and recover after a resize or
visibility event. Screenshots go to RAINCLI_GUI_ARTIFACTS (``<RAINCLI_SHELL_PHASE>-<case>.png``)."""

from __future__ import annotations

import os

import pytest

from .test_app_mode_gui import artifacts, browser, live, open_app, people, person_send  # noqa: F401
from .test_live_gui import REVIEWER, agent_thread, open_thread, put_presence  # noqa: F401

pytest.importorskip("playwright.sync_api")
PHASE = os.environ.get("RAINCLI_SHELL_PHASE", "after")

SHELL = """() => {
  const b = (s) => { const e = document.querySelector(s); return e ? Math.round(e.getBoundingClientRect().bottom) : null; };
  return {h: innerHeight, body: Math.round(document.body.getBoundingClientRect().height),
          nav: b('.rc-nav'), list: b('section.rc-list'), main: b('main.rc-main'), compose: b('.rc-compose'),
          settings: b('.rc-nav a:last-of-type'), whoami: b('section.rc-list .rc-whoami'),
          appH: getComputedStyle(document.documentElement).getPropertyValue('--rc-app-h').trim()};
}"""


def assert_fills(m, list_shown=True):
    h = m["h"]
    assert abs(m["body"] - h) <= 1 and abs(m["main"] - h) <= 1 and abs(m["nav"] - h) <= 1, m
    assert m["compose"] is None or abs(m["compose"] - h) <= 1, m
    assert m["settings"] > h - 60, m  # Settings at the bottom of the nav
    if list_shown:
        assert abs(m["list"] - h) <= 1 and m["whoami"] > h - 40, m  # "Signed in as" at the list's foot


def shoot(page, artifacts, name):  # noqa: F811
    page.screenshot(path=str(artifacts / f"{PHASE}-{name}.png"))


@pytest.mark.parametrize("where", ["append", "prepend"])
def test_an_extra_body_child_leaves_the_shell_full_height(agent_thread, browser, artifacts, where):  # noqa: F811
    p = agent_thread
    context, page = open_thread(browser, p, p["cid"], viewport={"width": 910, "height": 512}, device_scale_factor=1.5)
    try:
        page.evaluate("""(where) => { const d = document.createElement('div'); d.className = 'injected-by-host';
            d.textContent = 'an element the host added'; d.style.height = '40px';  // CSSOM: allowed by the CSP
            document.body[where](d); }""", where)
        page.wait_for_timeout(100)
        shoot(page, artifacts, f"extra-child-{where}-910x512")
        assert_fills(page.evaluate(SHELL))
    finally:
        context.close()


def test_a_stale_height_recovers_on_resize_and_on_show(agent_thread, browser, artifacts):  # noqa: F811
    """The page laid out at a wrong height (as if the host's viewport were stale): the shell's own 100% is made
    wrong by pinning <html> to 300 px through CSSOM. After a resize, or the window being shown, the fallback sets
    --rc-app-h to innerHeight and the shell fills the window again."""
    p = agent_thread
    context, page = open_thread(browser, p, p["cid"], viewport={"width": 910, "height": 512}, device_scale_factor=1.5)
    try:
        page.evaluate("() => { document.documentElement.style.height = '300px'; document.body.style.height = '300px'; }")
        page.wait_for_timeout(100)
        shoot(page, artifacts, "stale-height-910x512")
        stale = page.evaluate(SHELL)
        assert stale["body"] < stale["h"] - 100  # the forced collapse really happened
        # Only the <body> pin is lifted (the host's stale viewport); <html> stays wrong, so only the fallback helps.
        page.evaluate("() => { document.body.style.removeProperty('height'); }")
        page.set_viewport_size({"width": 910, "height": 460})
        page.wait_for_timeout(200)
        shoot(page, artifacts, "stale-height-after-resize-910x460")
        m = page.evaluate(SHELL)
        assert_fills(m) and m["appH"] == "460px"
        # Stale again, then the window is shown: visibilitychange to visible recovers it too.
        page.evaluate("() => document.documentElement.style.setProperty('--rc-app-h', '250px')")
        assert page.evaluate(SHELL)["body"] == 250
        page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_timeout(100)
        assert_fills(page.evaluate(SHELL))
        page.evaluate("() => document.documentElement.style.setProperty('--rc-app-h', '250px')")
        page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        page.wait_for_timeout(100)
        assert_fills(page.evaluate(SHELL))
    finally:
        context.close()


@pytest.mark.parametrize("size", [(910, 512), (910, 460), (1366, 700), (760, 520), (1180, 780)])
def test_normal_windows_are_unchanged_and_the_fallback_stays_off(agent_thread, browser, size):  # noqa: F811
    p = agent_thread
    context, page = open_thread(browser, p, p["cid"], viewport={"width": size[0], "height": size[1]},
                                device_scale_factor=1.5)
    try:
        m = page.evaluate(SHELL)
        assert_fills(m) and m["appH"] == ""  # no mismatch, so the fallback never ran
        assert page.locator("[style]").count() == 0  # nothing set on the page when the fallback doesn't fire
        page.set_viewport_size({"width": size[0], "height": size[1] - 60})
        page.wait_for_timeout(150)
        assert_fills(page.evaluate(SHELL))
    finally:
        context.close()


def test_the_no_list_pages_and_the_narrow_breakpoint_fill_the_window(people, browser):  # noqa: F811
    context, page = open_app(browser, people["base"], people["alice"]["person_session"],
                             viewport={"width": 910, "height": 512})
    try:
        page.goto(people["base"] + "/app/agents")  # a no-list page
        assert "no-list" in page.get_attribute("body", "class")
        m = page.evaluate(SHELL)
        assert abs(m["body"] - m["h"]) <= 1 and abs(m["main"] - m["h"]) <= 1 and abs(m["nav"] - m["h"]) <= 1, m
        page.set_viewport_size({"width": 600, "height": 500})  # below the breakpoint (browser only)
        page.goto(people["base"] + "/app/inbox")
        m = page.evaluate(SHELL)
        assert abs(m["body"] - m["h"]) <= 1 and abs(m["nav"] - m["h"]) <= 1, m
    finally:
        context.close()


def test_website_pages_are_untouched(live, browser):  # noqa: F811
    page = browser.new_page(viewport={"width": 910, "height": 512})
    try:
        page.goto(live + "/login")
        assert "app-mode" not in (page.get_attribute("body", "class") or "")
        sheets = page.evaluate("() => Array.from(document.styleSheets, (s) => s.href || '')")
        assert not any("app-mode" in s for s in sheets)
        assert page.evaluate("() => getComputedStyle(document.documentElement).getPropertyValue('--rc-app-h')") == ""
    finally:
        page.close()


# The "only when I click a chat" bug: the DOCUMENT scrolls. body.app-mode's overflow propagates to the viewport, and
# overflow: hidden leaves it programmatically scrollable, so a scrollIntoView() or focus() on the conversation page
# (the #latest anchor, the thread's scroll restore, a live message) moved the whole shell up when anything overflowed.
DOC = """() => { const s = document.scrollingElement;
  return {scrollY: scrollY, top: s.scrollTop, left: s.scrollLeft, height: s.scrollHeight, client: s.clientHeight,
          over: ['.rc-nav', 'section.rc-list', 'main.rc-main', '.rc-scroll', '.rc-thread']
                .map((q) => { const e = document.querySelector(q); if (!e) return null;
                              const r = e.getBoundingClientRect(); return q + ':' + Math.round(r.bottom) + '/' + e.scrollHeight; })}; }"""


@pytest.fixture
def long_thread(agent_thread):
    p = agent_thread
    from .test_live_gui import machine_send
    for i in range(30):
        machine_send(p["base"], p["bob"]["token"], {"person": "alice@example.test"},
                     f"Reply {i}: " + "more detail " * (4 + i % 6), p["cid"])
    return p


@pytest.mark.parametrize("size", [(1164, 741), (1000, 520), (760, 400), (910, 460)])
def test_clicking_a_chat_never_scrolls_the_document(long_thread, browser, artifacts, size):  # noqa: F811
    p = long_thread
    context, page = open_app(browser, p["base"], p["alice"]["person_session"],
                             viewport={"width": size[0], "height": size[1]}, device_scale_factor=1.5)
    try:
        page.bring_to_front()
        page.evaluate("() => window.focus()")
        page.click(".rc-convs .rc-conv")  # a real click on a chat (its link goes to #latest)
        page.wait_for_selector("[data-thread]")
        page.wait_for_timeout(600)
        doc = page.evaluate(DOC)
        shoot(page, artifacts, f"click-chat-{size[0]}x{size[1]}")
        print("DOC", size, page.evaluate("() => document.hasFocus()"), doc)
        assert doc["scrollY"] == 0 and doc["top"] == 0 and doc["left"] == 0, doc
        assert doc["height"] <= doc["client"], doc  # nothing overflows the document (the cause, not just the guard)
        assert page.evaluate("() => document.querySelectorAll('.rc-thread .sr-only').length") > 30  # the spans are there
        assert_fills(page.evaluate(SHELL))
        # A forced scrollIntoView on a message, and a focus without preventScroll, never move the document.
        page.evaluate("() => document.querySelector('.rc-thread .rc-msg').scrollIntoView({block: 'start'})")
        page.evaluate("() => document.querySelector('.rc-nav a:last-of-type').focus()")
        page.evaluate("() => document.querySelector('.rc-thread .rc-msg:last-child').scrollIntoView({block: 'end'})")
        page.wait_for_timeout(200)
        doc = page.evaluate(DOC)
        assert doc["scrollY"] == 0 and doc["top"] == 0, doc
        assert_fills(page.evaluate(SHELL))
    finally:
        context.close()


@pytest.mark.parametrize("size", [(1164, 741), (1000, 520), (760, 400)])
def test_without_javascript_the_fragment_jump_cannot_scroll_the_document(long_thread, browser, size):  # noqa: F811
    """CSS alone: no app-mode.js guard, no app.js scroll restore; the native #latest jump scrolls only the pane."""
    p = long_thread
    context, page = open_app(browser, p["base"], p["alice"]["person_session"], java_script_enabled=False,
                             viewport={"width": size[0], "height": size[1]}, device_scale_factor=1.5)
    try:
        page.goto(p["base"] + f"/app/conversations/{p['cid']}#latest")
        page.wait_for_timeout(300)
        doc = page.evaluate(DOC)
        assert doc["top"] == 0 and doc["height"] <= doc["client"], doc
        assert page.evaluate("() => document.querySelector('.rc-scroll').scrollTop") > 0  # the pane did scroll
        assert_fills(page.evaluate(SHELL))
    finally:
        context.close()


def test_the_temp_layout_diagnostic_posts_the_expected_keys_at_most_three_times(agent_thread, browser):  # noqa: F811
    import json

    from raincli_server.web import routes

    p = agent_thread
    posts, statuses = [], []

    def setup(context):
        def handle(route):
            posts.append(json.loads(route.request.post_data))
            route.continue_()  # to the real server, which must accept it
        context.route("**/app/layout-diag", handle)
        context.on("response", lambda r: statuses.append(r.status) if r.url.endswith("/app/layout-diag") else None)

    context, page = open_thread(browser, p, p["cid"], viewport={"width": 910, "height": 512}, setup=setup)
    try:
        for size in ((910, 460), (1000, 600), (910, 512)):
            page.set_viewport_size({"width": size[0], "height": size[1]})
            page.wait_for_timeout(150)
        for _ in range(2):
            page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_timeout(150)
        page.wait_for_timeout(500)
        # The handoff page (Inbox) is a page load too; count the conversation page's reports.
        mine = [b for b in posts if b["layout"]["page"] == "conversation"]
        assert 1 <= len(mine) <= 3, posts
        events = [b["layout"]["event"] for b in mine]
        assert len(set(events)) == len(events) and set(events) <= {"load", "show", "resize"}, events
        for body in posts:
            assert set(body) == {"csrf_token", "layout"} and set(body["layout"]) == set(routes.LAYOUT_DIAG_FIELDS)
            assert all("/" not in str(v) or k == "dpr" for k, v in body["layout"].items() if isinstance(v, str))
        assert statuses and all(s == 204 for s in statuses), statuses
    finally:
        context.close()


@pytest.mark.parametrize("size", [(1164, 741), (1000, 520), (760, 400)])
def test_the_apps_own_open_of_the_latest_conversation_never_scrolls_the_document(long_thread, browser, artifacts, size):  # noqa: F811
    """P2 (Inbox opens the latest conversation, location.replace to its #latest link), as a cold --open does."""
    p = long_thread
    context, page = open_app(browser, p["base"], p["alice"]["person_session"],
                             viewport={"width": size[0], "height": size[1]}, device_scale_factor=1.5)
    try:
        page.wait_for_url("**/app/conversations/**#latest")
        page.wait_for_selector("[data-thread]")
        page.wait_for_timeout(600)
        shoot(page, artifacts, f"p2-open-{size[0]}x{size[1]}")
        doc = page.evaluate(DOC)
        assert doc["top"] == 0 and doc["height"] <= doc["client"], doc
        assert_fills(page.evaluate(SHELL))
    finally:
        context.close()


@pytest.mark.parametrize("start", ["hidden", "one-pixel"])
def test_a_page_loaded_hidden_or_at_almost_no_height_is_whole_when_shown(long_thread, browser, artifacts, start):  # noqa: F811
    """Loaded while hidden (visibilityState), or in a 1 px tall viewport (a host's zero-size control), then shown
    at 910x512: the shell fills the window and the document never scrolled."""
    p = long_thread
    hide = """(() => { let state = 'hidden';
        Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => state});
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => state !== 'visible'});
        window.__show = () => { state = 'visible'; document.dispatchEvent(new Event('visibilitychange')); }; })();"""
    height = 1 if start == "one-pixel" else 512
    context, page = open_app(browser, p["base"], p["alice"]["person_session"], init_script=hide if start == "hidden" else None,
                             viewport={"width": 910, "height": height}, device_scale_factor=1.5)
    try:
        page.goto(p["base"] + f"/app/conversations/{p['cid']}#latest")
        page.wait_for_selector("[data-thread]", state="attached")
        page.wait_for_timeout(300)
        if start == "one-pixel":
            page.set_viewport_size({"width": 910, "height": 512})
        else:
            page.evaluate("() => window.__show()")
        page.wait_for_timeout(300)
        shoot(page, artifacts, f"loaded-{start}-then-shown-910x512")
        doc = page.evaluate(DOC)
        assert doc["top"] == 0 and doc["height"] <= doc["client"], doc
        assert_fills(page.evaluate(SHELL))
    finally:
        context.close()
