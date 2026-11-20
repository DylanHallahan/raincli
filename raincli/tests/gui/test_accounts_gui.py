"""The account switcher in headless Chromium (protocol §23 A9; brief AS7, C3, C4, C7; A14 L2), on the shared
accounts seed (tests/conftest_accounts.py), in a 0.6.2 app window: it opens from "Signed in as" and from the rail's
avatar, closes on Escape and outside clicks, keeps open across live updates with the counts moving, and asks the
window to switch, add or remove (and Ctrl+Shift+A to cycle) ONLY with the window's per-load nonce; without it,
nothing happens. Light and dark, 1180x780 and 760x520."""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest_accounts import accounts_seed  # noqa: F401  (fixture)
from conftest_v06 import v06_seed  # noqa: F401  (fixture)

from .test_app_mode_gui import APP_TOKEN, INSTALL_HASH, api, browser, live  # noqa: F401

pytest.importorskip("playwright.sync_api")

NONCE = "window.__rcNonce = 'n0nce-for-this-load';"


def open_window(browser, base, token, *, width=1180, height=780, scheme="light", nonce=True):  # noqa: F811
    probe = browser.new_page()
    agent = probe.evaluate("navigator.userAgent") + " RainCLIApp/" + APP_TOKEN + " RainCLIAppVersion/0.6.2"
    probe.close()
    context = browser.new_context(user_agent=agent, viewport={"width": width, "height": height}, color_scheme=scheme)
    if nonce:  # what the 0.6.2 window gives each hosted page it loads (A14 L2)
        context.add_init_script(NONCE)
    page = context.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto(api(base, "/api/v1/app/handoff", {"app_install_hash": INSTALL_HASH}, token)["url"])
    page.wait_for_load_state("networkidle")
    page.goto(base + "/app/compose")  # the list, without Inbox opening the newest
    page.wait_for_load_state("networkidle")
    return context, page


def sentinel_of(url):
    parts = urlsplit(url)
    return parts.path.rsplit("/", 1)[-1], {k: v[0] for k, v in parse_qs(parts.query).items()}


def test_the_switcher_opens_closes_and_names_the_accounts(live, browser, accounts_seed):  # noqa: F811
    context, page = open_window(browser, live, accounts_seed["person_tokens"]["Maya"])
    try:
        toggle = page.locator("[data-switcher-toggle]")
        panel = page.locator("[data-accounts]")
        assert toggle.get_attribute("aria-expanded") == "false" and panel.is_hidden()
        toggle.click()
        assert panel.is_visible() and toggle.get_attribute("aria-expanded") == "true"
        rows = panel.locator("[data-account-switch]")
        assert rows.count() == 2 and rows.nth(1).inner_text().split() == ["HD", "Help", "Desk", "helpdesk@example.test",
                                                                          "3", "unread"]
        assert page.get_by_role("button", name="Remove Help Desk from this app").count() == 1
        assert panel.locator("[data-account-add]").inner_text().split()[1:] == ["Billing", "billing@example.test"]
        page.keyboard.press("Escape")
        assert panel.is_hidden() and page.evaluate("document.activeElement.hasAttribute('data-switcher-toggle')")
        page.click("[data-switcher-open]")  # the rail's avatar
        assert panel.is_visible()
        page.click("main")  # a click elsewhere closes it
        assert panel.is_hidden()
        assert not page.errors
    finally:
        context.close()


def test_without_the_windows_nonce_nothing_happens(live, browser, accounts_seed):  # noqa: F811
    context, page = open_window(browser, live, accounts_seed["person_tokens"]["Maya"], nonce=False)
    try:
        before = page.url
        page.click("[data-switcher-toggle]")
        page.click("[data-account-switch='helpdesk@example.test']")
        page.click("[data-account-add='billing@example.test']")
        page.keyboard.press("Control+Shift+A")
        page.wait_for_timeout(500)
        assert page.url == before  # no sentinel without the window's nonce (A14 L2)
    finally:
        context.close()


@pytest.mark.parametrize("action", ["switch", "add", "remove", "cycle"])
def test_the_window_is_asked_only_with_its_nonce(live, browser, accounts_seed, action):  # noqa: F811
    context, page = open_window(browser, live, accounts_seed["person_tokens"]["Maya"])
    try:
        page.click("[data-switcher-toggle]")
        if action == "switch":
            page.click("[data-account-switch='helpdesk@example.test']")
            want = ("switch", {"account": "helpdesk@example.test"})
        elif action == "add":
            page.click("[data-account-add='billing@example.test']")
            want = ("add-account", {"account": "billing@example.test"})
        elif action == "remove":
            page.click("[data-account-remove='helpdesk@example.test']")
            confirm = page.locator("[data-remove-confirm]")
            assert confirm.is_visible() and "Remove Help Desk from this app?" in confirm.inner_text()
            page.click("[data-remove-no]")
            assert confirm.is_hidden() and "/app/local/" not in page.url  # cancelled: nothing asked
            page.click("[data-account-remove='helpdesk@example.test']")
            page.click("[data-remove-yes]")
            want = ("remove-account", {"account": "helpdesk@example.test"})
        else:
            page.keyboard.press("Escape")
            page.keyboard.press("Control+Shift+A")
            want = ("switch", {"next": "1"})
        page.wait_for_url("**/app/local/**")
        name, params = sentinel_of(page.url)
        assert (name, {k: v for k, v in params.items() if k != "nonce"}) == want
        assert params["nonce"] == "n0nce-for-this-load"
    finally:
        context.close()


def test_live_updates_keep_the_switcher_open_and_move_the_counts(live, browser, accounts_seed):  # noqa: F811
    s = accounts_seed
    context, page = open_window(browser, live, s["person_tokens"]["Maya"])
    try:
        page.click("[data-switcher-toggle]")
        count = "[data-account-switch='helpdesk@example.test'] .rc-count"
        assert page.inner_text(count).split()[0] == "3"
        api(live, "/api/v1/messages", {"id": str(uuid.uuid4()), "to": "helpdesk@example.test", "body": "Printer."},
            s["machine_tokens"]["jon"])
        page.evaluate("() => window.rcLive.poll()")
        page.wait_for_function(f"() => (document.querySelector(\"{count}\") || {{}}).textContent?.startsWith('4')")
        assert page.locator("[data-accounts]").is_visible()  # still open after the list swap
        assert not page.errors
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width,height", [(1180, 780), (760, 520)])
def test_the_switcher_and_the_cues_fit(live, browser, accounts_seed, width, height, scheme):  # noqa: F811
    s = accounts_seed
    for token, initials in ((s["person_tokens"]["Maya"], "MA"), (s["delegated_token"], "HD")):
        context, page = open_window(browser, live, token, width=width, height=height, scheme=scheme)
        try:
            assert page.inner_text(".rc-rail-avatar") == initials
            page.click("[data-switcher-toggle]")
            box = page.locator("[data-accounts]").bounding_box()
            assert box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["width"] <= width and box["y"] + box["height"] <= height, box
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            for b in page.locator("[data-accounts] button:visible").all():
                assert b.bounding_box()["height"] >= 44
            if initials == "HD":
                assert page.title().endswith(" · Help Desk")
                page.goto(live + f"/app/conversations/{s['conversations']['helpdesk_jon'].id}")
                assert page.inner_text(".rc-as") == "As: Help Desk"
            assert not page.errors
        finally:
            context.close()
