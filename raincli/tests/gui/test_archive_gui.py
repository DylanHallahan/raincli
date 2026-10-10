"""Archive and unarchive in headless Chromium (protocol §17.2, §17.3 A3, A8), on the website and in app mode.

Archive asks for no confirmation; the conversation leaves the inbox, appears under Archived with its unread
count and who archived it, shows an "Archived" marker when opened directly, and Unarchive brings it back.
"""

from __future__ import annotations

import pytest

from .test_app_mode_gui import api, artifacts, browser, live, open_app, people, person_send  # noqa: F401 - fixtures
from .test_localtime_gui import website

pytest.importorskip("playwright.sync_api")


def test_archive_on_the_website(people, browser, artifacts):
    msg = person_send(people["base"], people["bob"]["person_session"], {"person": "alice@example.test"},
                      "Archive me on the website")
    context, page = website(browser, people["base"])
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(people["base"] + f"/app/conversations/{msg['conversation_id']}")
    page.click("button:has-text('Archive')")
    page.wait_for_url("**/app/inbox?notice=archived")
    assert not dialogs  # no confirmation
    assert "Conversation archived" in page.inner_text("body")
    assert page.locator(f"a[href*='{msg['conversation_id']}']").count() == 0
    page.click("a:has-text('Archived')")
    row = page.locator("li.conv-archived", has_text="Archived by you")
    assert row.count() == 1 and "new" not in row.inner_text()  # opened before archiving, so read
    page.screenshot(path=str(artifacts / "website-archived.png"))
    row.locator("a.conv").click()
    assert page.locator(".archived-marker").inner_text() == "Archived by you"
    page.click("button:has-text('Unarchive')")
    page.wait_for_url("**notice=unarchived**")
    assert page.locator(".archived-marker").count() == 0 and page.locator("button:has-text('Archive')").count() == 1
    page.goto(people["base"] + "/app/inbox")
    assert page.locator(f"a[href*='{msg['conversation_id']}']").count() == 1
    context.close()


def test_archive_in_app_mode(people, browser, artifacts):
    msg = person_send(people["base"], people["bob"]["person_session"], {"person": "alice@example.test"},
                      "Archive me in the app")
    context, page = open_app(browser, people["base"], people["alice"]["person_session"])
    page.click(".rc-conv")
    page.wait_for_selector(".rc-thread")
    page.click(".rc-archive button:has-text('Archive')")
    page.wait_for_url("**/app/inbox?notice=archived")
    assert page.locator(f".rc-conv[href*='{msg['conversation_id']}']").count() == 0
    # Bob archives a conversation Alice hasn't opened: it stays unread under Archived (§17.3 A3).
    unread = person_send(people["base"], people["bob"]["person_session"], {"machine": "alice-laptop"},
                         "Unread when archived")
    api(people["base"], f"/api/v1/person/conversations/{unread['conversation_id']}/archive", {},
        people["bob"]["person_session"])
    page.click("nav.rc-nav a:has-text('Archived')")
    assert page.get_attribute("nav.rc-nav a:has-text('Archived')", "aria-current") == "page"
    row = page.locator(".rc-archived-row", has_text="Archived by you")
    assert row.count() == 1 and row.locator(".rc-count").count() == 0  # read in the window before archiving
    other = page.locator(".rc-archived-row", has_text="Archived by Bob")
    assert other.count() == 1 and other.locator(".rc-count").inner_text() == "1"
    page.screenshot(path=str(artifacts / "app-mode-archived.png"))
    row.locator("button:has-text('Unarchive')").click()
    page.wait_for_url("**/app/archived?notice=unarchived")
    assert page.locator(".rc-archived-row").count() == 1  # only Bob's
    page.click("nav.rc-nav a:has-text('Inbox')")
    assert page.locator(f".rc-conv[href*='{msg['conversation_id']}']").count() == 1
    context.close()
