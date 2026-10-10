"""Local time (protocol §17.1, §17.3 A2, A6) in headless Chromium: the website, app mode and the app's local pages.

Two forced zones and locales. Every expectation is computed with Intl in the same browser, never a literal
string; no "UTC" shows in a <time> or the page chrome while JavaScript runs; relative labels move with
Playwright's clock; times inserted after load are formatted too; without JavaScript the UTC text stays.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from .test_app_mode_gui import PASSWORD, browser, live, open_app, people, person_send  # noqa: F401 - fixtures
from .test_local_pages_gui import STATUS, local_origin, open_page  # noqa: F401 - fixtures

pytest.importorskip("playwright.sync_api")

ZONES = [("America/New_York", "en-US"), ("Asia/Kolkata", "en-GB")]
ALIASES = {"Asia/Kolkata": {"Asia/Kolkata", "Asia/Calcutta"}}  # ICU may report the older name

# Intl in the page: what localtime.js should show for a <time datetime>, and proof the forced zone is in use.
EXPECTED = """(zone) => Array.from(document.querySelectorAll("time[datetime]")).map((el) => {
    const when = new Date(el.getAttribute("datetime"));
    const options = {weekday: "long", year: "numeric", month: "long", day: "numeric", hour: "numeric",
                     minute: "2-digit", second: "2-digit", timeZoneName: "long"};
    return {
        text: el.textContent, title: el.getAttribute("title"), local: el.getAttribute("data-local"),
        expectedTitle: new Intl.DateTimeFormat(undefined, options).format(when),
        zoneTitle: new Intl.DateTimeFormat(undefined, {...options, timeZone: zone}).format(when),
        zoneName: new Intl.DateTimeFormat(undefined, {timeZoneName: "long", timeZone: zone}).formatToParts(when)
            .find((p) => p.type === "timeZoneName").value,
        recent: [new Intl.RelativeTimeFormat(undefined, {numeric: "auto"}).format(0, "second")].concat(
            [1, 2, 3, 4, 5].map((n) => new Intl.RelativeTimeFormat(undefined, {numeric: "auto"}).format(-n, "minute"))),
    };
})"""
RESOLVED = "() => [Intl.DateTimeFormat().resolvedOptions().timeZone, Intl.DateTimeFormat().resolvedOptions().locale]"
RELATIVE = "([n, unit]) => new Intl.RelativeTimeFormat(undefined, {numeric: 'auto'}).format(n, unit)"


def website(browser, base, js=True, **options):
    context = browser.new_context(java_script_enabled=js, viewport={"width": 1100, "height": 760}, **options)
    page = context.new_page()
    page.goto(base + "/login")
    page.fill("#email", "alice@example.test")
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url("**/app**")
    return context, page


def check_local_times(page, zone, locale, *, recent=True):
    resolved_zone, resolved_locale = page.evaluate(RESOLVED)
    assert resolved_zone in ALIASES.get(zone, {zone}) and resolved_locale == locale
    page.wait_for_function("document.querySelectorAll('time[datetime]:not([data-local])').length === 0")
    times = page.evaluate(EXPECTED, zone)
    assert times
    for t in times:
        assert t["local"] == "1" and t["title"] == t["expectedTitle"] == t["zoneTitle"], t
        assert t["zoneName"] in t["title"] and "UTC" not in t["title"] and "UTC" not in t["text"], t
        if recent:
            assert t["text"] in t["recent"], t
    assert "UTC" not in page.inner_text("body")


@pytest.mark.parametrize("zone,locale", ZONES)
def test_the_website_shows_local_times(people, browser, zone, locale):
    msg = person_send(people["base"], people["bob"]["person_session"], {"person": "alice@example.test"}, "Local time")
    context, page = website(browser, people["base"], timezone_id=zone, locale=locale)
    check_local_times(page, zone, locale)  # the inbox
    page.goto(people["base"] + f"/app/conversations/{msg['conversation_id']}")
    check_local_times(page, zone, locale)
    context.close()


@pytest.mark.parametrize("zone,locale", ZONES)
def test_app_mode_shows_local_times(people, browser, zone, locale):
    person_send(people["base"], people["bob"]["person_session"], {"person": "alice@example.test"}, "Local time")
    context, page = open_app(browser, people["base"], people["alice"]["person_session"],
                             timezone_id=zone, locale=locale)
    check_local_times(page, zone, locale)
    page.click(".rc-conv")
    page.wait_for_selector(".rc-thread")
    check_local_times(page, zone, locale)
    context.close()


@pytest.mark.parametrize("zone,locale", ZONES)
def test_local_pages_show_the_last_report_in_local_time(browser, local_origin, zone, locale):
    status = {**STATUS, "last_report": "2026-03-04T05:06:07Z"}
    context, page, errors = open_page(browser, local_origin, "this-computer", {"status": status, "hooks": []},
                                      timezone_id=zone, locale=locale)
    page.wait_for_selector("time#last-report[data-local]")
    check_local_times(page, zone, locale, recent=False)
    context.close()
    assert not errors


def test_relative_labels_move_with_the_clock(people, browser):
    msg = person_send(people["base"], people["bob"]["person_session"], {"person": "alice@example.test"}, "Tick")
    sent = datetime.fromisoformat(msg["created_at"].replace("Z", "+00:00"))
    context, page = website(browser, people["base"], timezone_id="Asia/Kolkata", locale="en-GB")
    page.clock.install(time=sent + timedelta(seconds=20))
    page.goto(people["base"] + f"/app/conversations/{msg['conversation_id']}")
    label = page.locator(f"#m-{msg['id']} time[datetime]").first
    page.wait_for_function("(el) => el.dataset.local === '1'", arg=label.element_handle())
    assert label.inner_text() == page.evaluate(RELATIVE, [0, "second"])
    page.clock.fast_forward(5 * 60 * 1000)  # the 30-second refresh fires
    page.wait_for_function("([el, want]) => el.textContent === want",
                           arg=[label.element_handle(), page.evaluate(RELATIVE, [-5, "minute"])])
    # A <time> added after load (a new message, a status refresh) is formatted as it appears.
    page.evaluate("""() => { const t = document.createElement("time"); t.id = "late";
        t.setAttribute("datetime", "2020-01-02T03:04:05Z"); t.textContent = "2020-01-02 03:04 UTC";
        document.querySelector("main").appendChild(t); }""")
    page.wait_for_selector("time#late[data-local]")
    late = page.evaluate("""() => { const d = new Date("2020-01-02T03:04:05Z");
        return [document.getElementById("late").textContent, new Intl.DateTimeFormat(undefined,
            {year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}).format(d)]; }""")
    assert late[0] == late[1] and "UTC" not in late[0]
    context.close()


def test_without_javascript_the_utc_fallback_stays(people, browser):
    msg = person_send(people["base"], people["bob"]["person_session"], {"person": "alice@example.test"}, "No JS")
    context, page = website(browser, people["base"], js=False, timezone_id="America/New_York", locale="en-US")
    page.goto(people["base"] + f"/app/conversations/{msg['conversation_id']}")
    elements = page.locator("time[datetime]")
    assert elements.count() > 0
    for i in range(elements.count()):
        el = elements.nth(i)
        stamp, text = el.get_attribute("datetime"), el.text_content()  # some sit in a closed <details>
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp) and el.get_attribute("data-local") is None
        assert text == stamp[:10] + " " + stamp[11:16] + " UTC"
    context.close()
