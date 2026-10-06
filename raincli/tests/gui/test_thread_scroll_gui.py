"""Conversations open at the newest message (protocol §16.19 item 4), in app mode and on the website.

Headless Chromium against a real server: on open the newest message is in view; an ``#m-<id>`` anchor
wins; a reload keeps the reader at the newest message only when they were near it, else keeps their place
and shows "New messages"; without JavaScript, ``#latest`` reaches the end of the thread.
"""

from __future__ import annotations

import pytest

from .test_app_mode_gui import (PASSWORD, artifacts, browser, live, open_app, people,  # noqa: F401 - fixtures
                                person_send)

pytest.importorskip("playwright.sync_api")

MESSAGES = 30


def long_thread(people):
    """Bob sends Alice enough messages to scroll. Returns the message ids, oldest first."""
    body = "Line one\n\nLine two\n\nLine three"
    return [person_send(people["base"], people["bob"]["person_session"], {"person": "alice@example.test"},
                        f"Message {i}\n\n{body}")["id"] for i in range(MESSAGES)]


def in_view(page, selector, scroller=None):
    """True when ``selector``'s element is inside the scroll container's (or the window's) visible area."""
    return page.evaluate("""([selector, scroller]) => {
        const el = document.querySelector(selector);
        const box = el.getBoundingClientRect();
        const view = scroller ? document.querySelector(scroller).getBoundingClientRect()
                              : {top: 0, bottom: window.innerHeight};
        return box.bottom > view.top && box.top < view.bottom;
    }""", [selector, scroller])


def scroll_top(page, scroller):
    return page.evaluate("(s) => document.querySelector(s).scrollTop", scroller)


def test_app_mode_opens_at_the_newest_message_and_keeps_a_reader_who_scrolled_up(people, browser, artifacts):
    ids = long_thread(people)
    context, page = open_app(browser, people["base"], people["alice"]["person_session"])
    page.set_viewport_size({"width": 1180, "height": 700})
    page.click(".rc-conv")
    page.wait_for_selector(".rc-thread")
    assert page.url.endswith("#latest")
    scroller = ".rc-scroll"
    assert in_view(page, f"#m-{ids[-1]}", scroller) and not in_view(page, f"#m-{ids[0]}", scroller)
    page.screenshot(path=str(artifacts / "thread-open-at-newest.png"))

    # Near the bottom: a reload after a new message stays pinned to the newest one.
    newest = person_send(people["base"], people["bob"]["person_session"], {"person": "alice@example.test"},
                         "One more")["id"]
    page.reload()
    page.wait_for_selector(f"#m-{newest}")
    assert in_view(page, f"#m-{newest}", scroller) and page.is_hidden("[data-new-messages]")

    # Scrolled up: a reload keeps the place and offers "New messages".
    page.evaluate("(s) => { document.querySelector(s).scrollTop = 0; }", scroller)
    page.wait_for_timeout(200)
    later = person_send(people["base"], people["bob"]["person_session"], {"person": "alice@example.test"},
                        "Arrived while scrolled up")["id"]
    page.reload()
    page.wait_for_selector(f"#m-{later}", state="attached")
    assert scroll_top(page, scroller) < 50 and not in_view(page, f"#m-{later}", scroller)
    assert page.is_visible("[data-new-messages]")
    page.screenshot(path=str(artifacts / "thread-new-messages.png"))
    page.click("[data-new-messages]")
    page.wait_for_timeout(300)
    assert in_view(page, f"#m-{later}", scroller) and page.is_hidden("[data-new-messages]")
    context.close()


def test_an_anchor_takes_precedence(people, browser):
    ids = long_thread(people)
    context, page = open_app(browser, people["base"], people["alice"]["person_session"])
    page.set_viewport_size({"width": 1180, "height": 700})
    page.click(".rc-conv")
    page.wait_for_selector(".rc-thread")
    conversation = page.url.split("#")[0]
    page.goto(f"{conversation}#m-{ids[2]}")
    page.wait_for_selector(f"#m-{ids[2]}")
    assert in_view(page, f"#m-{ids[2]}", ".rc-scroll") and not in_view(page, f"#m-{ids[-1]}", ".rc-scroll")
    context.close()


def test_website_opens_at_the_newest_message_and_latest_works_without_javascript(people, browser):
    ids = long_thread(people)
    base = people["base"]
    for js in (True, False):
        context = browser.new_context(java_script_enabled=js, viewport={"width": 1100, "height": 700})
        page = context.new_page()
        page.goto(base + "/login")
        page.fill("#email", "alice@example.test")
        page.fill("#password", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url("**/app**")
        page.goto(base + "/app")
        href = page.get_attribute("a.conv", "href")
        assert href.endswith("#latest")
        page.goto(base + href if href.startswith("/") else href)
        page.wait_for_selector(".thread")
        assert in_view(page, f"#m-{ids[-1]}"), f"newest message not in view (javascript {js})"
        assert not in_view(page, f"#m-{ids[0]}")
        context.close()
