"""Send in place in headless Chromium against a real server (protocol §20.2, §20.6 M2, M4, L4, L5): no page load,
the message merged once by id, the message id kept across retries of an unknown outcome, and inline errors that
keep the draft."""

from __future__ import annotations

import json

import pytest

from .test_app_mode_gui import api, artifacts, browser, live, open_app, people, person_send  # noqa: F401
from .test_live_gui import REVIEWER, agent_thread, open_thread, poll_now, put_presence  # noqa: F401

pytest.importorskip("playwright.sync_api")

COUNT = "(text) => Array.from(document.querySelectorAll('.rc-thread .rc-msg .md')).filter((e) => e.innerText.trim() === text).length"


def mark(page):
    """A marker that a page load would wipe."""
    page.evaluate("() => { window.__noReload = true; }")


def still_same_page(page):
    return page.evaluate("() => window.__noReload === true")


def message_id(page):
    return page.input_value("input[name=message_id]")


def type_and_send(page, text):
    page.fill("#body", text)
    page.click("button.rc-send")


def landed(page, text):
    page.wait_for_function(f"() => ({COUNT})({json.dumps(text)}) === 1")


def test_two_sends_in_a_row_without_a_page_load(agent_thread, browser, artifacts):
    p = agent_thread
    context, page = open_thread(browser, p, p["cid"])
    try:
        mark(page)
        first_id = message_id(page)
        type_and_send(page, "first in place")
        landed(page, "first in place")
        assert page.input_value("#body") == "" and message_id(page) != first_id  # a new id after a success
        second_id = message_id(page)
        type_and_send(page, "second in place")
        landed(page, "second in place")
        assert message_id(page) not in (first_id, second_id)
        assert still_same_page(page) and "notice=" not in page.url
        assert page.locator(".rc-flash").count() == 0  # no "Message stored on the server." banner (§20.2)
        assert page.is_enabled("button.rc-send")  # app.js's double-submit guard never sticks
        # The poll merges by id: nothing is doubled, and the list moved at once (the immediate poll).
        poll_now(page)
        page.wait_for_timeout(600)
        assert page.evaluate(COUNT, "first in place") == 1 and page.evaluate(COUNT, "second in place") == 1
        page.wait_for_function("() => document.querySelector('.rc-list').innerText.includes('second in place')")
        ids = page.evaluate("() => Array.from(document.querySelectorAll('.rc-thread .rc-msg'), (e) => e.id)")
        assert ids[-2:] == [f"m-{first_id}", f"m-{second_id}"]
        # The viewer's own message is scrolled into view.
        assert page.evaluate("""() => { const r = document.getElementById(%s).getBoundingClientRect();
                                        const s = document.querySelector('[data-thread-scroll]');
                                        const b = s ? s.getBoundingClientRect().bottom : innerHeight;
                                        return r.bottom <= b + 1; }""" % json.dumps(f"m-{second_id}"))
        assert page.locator("[style]").count() == 0
        page.screenshot(path=str(artifacts / "send-in-place.png"))
    finally:
        context.close()


def test_a_retry_after_a_dropped_response_sends_once(agent_thread, browser):
    """The request reaches the server but the response is lost: the draft stays with the same id, and the retry is
    a replay (M2)."""
    p = agent_thread
    context, page = open_thread(browser, p, p["cid"])
    try:
        mark(page)
        drop = {"on": True}

        def handler(route):
            if drop["on"]:
                route.fetch()  # the server stores it ...
                drop["on"] = False
                route.abort()  # ... and the window never hears back
            else:
                route.continue_()

        page.route("**/app/send", handler)
        attempt_id = message_id(page)
        type_and_send(page, "lost then retried")
        page.wait_for_selector(".rc-send-status:not([hidden])")
        assert page.inner_text(".rc-send-status") == "Couldn't send. Check your connection and try again."
        assert page.input_value("#body") == "lost then retried" and message_id(page) == attempt_id
        assert page.evaluate(COUNT, "lost then retried") == 0
        page.click("button.rc-send")
        landed(page, "lost then retried")
        assert page.is_hidden(".rc-send-status") and page.input_value("#body") == ""
        assert page.locator(f"#m-{attempt_id}").count() == 1
        poll_now(page)
        page.wait_for_timeout(600)
        assert page.evaluate(COUNT, "lost then retried") == 1 and still_same_page(page)
    finally:
        context.close()


def test_an_edited_retry_shows_the_earlier_version(agent_thread, browser):
    p = agent_thread
    context, page = open_thread(browser, p, p["cid"])
    try:
        state = {"drop": True}

        def handler(route):
            if state["drop"]:
                route.fetch()
                state["drop"] = False
                route.abort()
            else:
                route.continue_()

        page.route("**/app/send", handler)
        attempt_id = message_id(page)
        type_and_send(page, "version one")
        page.wait_for_selector(".rc-send-status:not([hidden])")
        page.fill("#body", "version two")
        page.click("button.rc-send")
        landed(page, "version one")
        assert page.inner_text(".rc-send-status") == "Your earlier version was already sent."
        assert page.input_value("#body") == "version two" and message_id(page) != attempt_id  # the edit stays
        page.click("button.rc-send")
        landed(page, "version two")
        assert page.evaluate(COUNT, "version one") == 1
    finally:
        context.close()


def test_errors_are_one_inline_line_and_keep_the_draft(agent_thread, browser, tmp_path):
    p = agent_thread
    report = tmp_path / "notes.md"
    report.write_text("# Notes\n", "utf-8")
    context, page = open_thread(browser, p, p["cid"])
    try:
        replies = [
            {"status": 429, "content_type": "application/json",
             "body": json.dumps({"error": {"code": "rate_limited", "retry_after": 7,
                                           "message": "You're sending too fast. Wait 7 seconds and try again."}})},
            {"status": 502, "content_type": "text/html", "body": "<h1>Bad gateway</h1>"},
            {"status": 400, "content_type": "application/json",
             "body": json.dumps({"error": {"code": "invalid", "message": "Attach Markdown files only."}})},
        ]
        page.route("**/app/send", lambda route: route.fulfill(**replies.pop(0)) if replies else route.continue_())
        attempt_id = message_id(page)
        page.set_input_files("input[name=files]", str(report))
        type_and_send(page, "with notes")
        page.wait_for_function("() => document.querySelector('.rc-send-status').innerText.includes('too fast')")
        assert message_id(page) == attempt_id  # a 429 is not a definite answer: the same id
        assert page.inner_text(".rc-files") == "notes.md" and page.input_value("#body") == "with notes"
        page.click("button.rc-send")
        page.wait_for_function("() => document.querySelector('.rc-send-status').innerText.startsWith(\"Couldn't send\")")
        assert message_id(page) == attempt_id  # not JSON: unknown outcome, the same id
        page.click("button.rc-send")
        page.wait_for_function("() => document.querySelector('.rc-send-status').innerText === 'Attach Markdown files only.'")
        assert message_id(page) != attempt_id  # a definite rejection: a new id may be used
        assert page.get_attribute(".rc-send-status", "role") == "status"
        assert page.locator(".rc-send-status").count() == 1  # one line, replaced each time
        page.click("button.rc-send")  # now for real, with the file
        landed(page, "with notes")
        page.wait_for_selector(".rc-thread .rc-attachments a:has-text('notes.md')")
        assert page.inner_text(".rc-files") == "" and page.is_hidden(".rc-send-status")
    finally:
        context.close()
