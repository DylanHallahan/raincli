"""v0.5.5 app mode in headless Chromium (protocol §20.3, §20.4, §20.6; brief criteria 15-30, cli-builder's part).

Same harness as test_v054_list_gui.py: a real uvicorn server on the throwaway test database, signed in through a
real handoff with the app's install-token User-Agent. Skipped without Playwright or its browser.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

import pytest

from .test_app_mode_gui import _contrast
from .test_v054_list_gui import (APP_TOKEN, CLIENT, INSTALL_HASH, api, browser, entry, live, person_send, size,  # noqa: F401
                                 team)

STATIC = Path(__file__).resolve().parents[2] / "raincli_server" / "web" / "static"
NO_FOCUS = "window.__focus = false; document.hasFocus = () => window.__focus;"


def open_app(browser, team, person, path=None, init_script=None, setup=None, next_path=None, **options):
    """A fresh 'webview' profile signed in through the handoff; ``init_script`` and ``setup(context)`` apply from the
    handoff on. Inbox may move on to the latest conversation by itself (§20.6 M1)."""
    probe = browser.new_page()
    agent = probe.evaluate("navigator.userAgent") + " RainCLIApp/" + APP_TOKEN + " RainCLIAppVersion/0.5.5"  # §20.7: a 0.5.5 window
    probe.close()
    context = browser.new_context(user_agent=agent, **options)
    if init_script:
        context.add_init_script(init_script)
    if setup:
        setup(context)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    url = api(team["base"], "/api/v1/app/handoff", {"app_install_hash": INSTALL_HASH}, person)["url"]
    page.goto(url + (f"&next={next_path}" if next_path else ""))
    page.wait_for_load_state("networkidle")
    if path:
        page.goto(team["base"] + path)
        page.wait_for_load_state("networkidle")
    page.errors = errors
    return context, page


def acked(team, message_id):
    from raincli_server.models import Message

    team["session"].expire_all()
    return team["session"].get(Message, uuid.UUID(message_id)).acked_at is not None


def jon_writes(team, body):
    return person_send(team, team["jon"], {"person": "maya@example.test"}, body)


def priya(team):
    return api(team["base"], "/api/v1/app/login", {"email": "priya@example.test", "password": "correct horse battery",
                                                   "machine_name": "priya-app", "person_session": True})["person_session"]


def conv_path(message):
    return f"/app/conversations/{message['conversation_id']}"


# R4 / P2: the latest conversation (criteria 17-18) -------------------------------------------------------------

def test_inbox_opens_the_latest_conversation_selected_with_the_composer_ready(team, browser):
    person_send(team, team["maya"], {"machine": "jon-laptop", "agent": "api-agent"}, "older")
    latest = person_send(team, team["maya"], {"machine": "priya-desktop", "agent": "web-agent"}, "newest")
    context, page = open_app(browser, team, team["maya"], **size(1180, 780))
    assert page.url.endswith(conv_path(latest) + "#latest"), page.url
    current = page.locator(".rc-conv[aria-current=page]")
    assert current.count() == 1 and latest["conversation_id"] in current.get_attribute("href")
    assert page.evaluate("document.activeElement.id") == "body"  # the composer is ready
    assert page.evaluate("history.length") == 2  # replaced, not pushed: Back doesn't return to a bare Inbox
    page.goto(team["base"] + "/app/agents")
    page.click("nav.rc-nav a:has-text('Inbox')")  # the rail's Inbox does the same
    page.wait_for_url(re.compile(re.escape(conv_path(latest))))
    assert not page.errors
    context.close()


def test_below_the_breakpoint_and_with_no_conversations_inbox_stays(team, browser):
    context, page = open_app(browser, team, team["maya"], **size(1180, 780))
    assert page.url.endswith("/app/inbox") and page.locator(".rc-steps").is_visible()  # first run
    context.close()
    person_send(team, team["maya"], {"machine": "jon-laptop", "agent": "api-agent"}, "hello")
    context, page = open_app(browser, team, team["maya"], **size(600, 700))
    page.wait_for_timeout(300)
    assert page.url.endswith("/app/inbox") and page.locator("section.rc-list").is_visible()
    context.close()


def test_a_hidden_window_marks_nothing_read_until_shown_and_focused(team, browser):
    unread = jon_writes(team, "read me when you see me")
    context, page = open_app(browser, team, team["maya"], init_script=NO_FOCUS, **size(1180, 780))
    assert page.url.endswith(conv_path(unread) + "#latest")
    assert page.evaluate("document.hasFocus()") is False
    page.evaluate("() => window.rcLive && window.rcLive.poll()")
    page.wait_for_timeout(1500)
    assert not acked(team, unread["id"])
    assert page.evaluate("document.activeElement.id") != "body"  # no composer focus in an unfocused window
    page.evaluate("() => { window.__focus = true; window.dispatchEvent(new Event('focus')); }")
    page.wait_for_function(f"() => !document.querySelector('#m-{unread['id']}').hasAttribute('data-unread')")
    assert acked(team, unread["id"])
    context.close()


def test_a_toast_opens_its_thread_and_never_the_latest(team, browser):
    older = jon_writes(team, "the toast is about this")
    newer = person_send(team, priya(team), {"person": "maya@example.test"}, "newer, not the toast")
    context, page = open_app(browser, team, team["maya"], next_path=conv_path(older), **size(1180, 780))
    assert page.url.endswith(conv_path(older)), page.url
    page.wait_for_timeout(1500)
    assert acked(team, older["id"]) and not acked(team, newer["id"])
    context.close()


# R3: focus (criteria 15-16) ----------------------------------------------------------------------------------

def outlines(page):
    return page.evaluate("""() => [...document.querySelectorAll('.rc-msg')]
        .filter(m => getComputedStyle(m).outlineStyle !== 'none').map(m => m.id)""")


CONTROL_RING = """() => { const e = document.activeElement, s = getComputedStyle(e);
    return [e.matches('.rc-reply, .rc-trail-box > summary, .rc-file a, button'), s.outlineStyle, s.borderTopLeftRadius]; }"""


def test_no_ring_for_mouse_programmatic_or_live_focus_and_a_rounded_ring_for_the_keyboard(team, browser):
    person_send(team, team["maya"], {"person": "jon@example.test"}, "mine, with a trail")
    first = jon_writes(team, "first")
    for scheme in ("light", "dark"):
        context, page = open_app(browser, team, team["maya"], path=conv_path(first) + f"#m-{first['id']}",
                                 color_scheme=scheme, **size(1180, 780))
        assert page.evaluate("document.activeElement.closest('.rc-msg')") is None  # an #m- link focuses nothing
        assert outlines(page) == []
        page.locator(".rc-msg .rc-msg-content").first.click()
        assert outlines(page) == []
        later = jon_writes(team, f"live {scheme}")
        page.evaluate("() => window.rcLive && window.rcLive.poll()")
        page.wait_for_selector(f"#m-{later['id']}")
        page.wait_for_timeout(400)
        assert outlines(page) == []
        for _ in range(60):  # the keyboard: Tab until focus is inside a message
            page.keyboard.press("Tab")
            if page.evaluate("!!document.activeElement.closest('.rc-msg')"):
                break
        ringed = page.locator(".rc-msg:has(:focus-visible)")
        assert ringed.count() == 1
        style = ringed.evaluate("""m => { const s = getComputedStyle(m), p = document.createElement('i');
            p.style.color = getComputedStyle(document.documentElement).getPropertyValue('--rc-focus'); document.body.append(p);
            const want = getComputedStyle(p).color; p.remove();
            return {style: s.outlineStyle, color: s.outlineColor, want, radius: s.borderTopLeftRadius}; }""")
        assert style["style"] == "solid" and style["color"] == style["want"] and style["radius"] == "10px", style
        # Review L3: a button or chip inside the message (here Reply) has its own ring at --rc-radius-sm too.
        assert page.evaluate(CONTROL_RING) == [True, "solid", "6px"], page.evaluate(CONTROL_RING)
        for _ in range(40):  # on to a trail toggle (a role=button summary): its own ring as well
            page.keyboard.press("Tab")
            if page.evaluate("document.activeElement.matches('.rc-trail-box > summary')"):
                break
        assert page.evaluate(CONTROL_RING) == [True, "solid", "6px"], page.evaluate(CONTROL_RING)
        assert not page.errors
        context.close()


# R5: the trail is the toggle (criterion 19) ------------------------------------------------------------------

def test_the_trail_toggles_its_history_by_click_and_keyboard(team, browser):
    mine = person_send(team, team["maya"], {"machine": "jon-laptop", "agent": "api-agent"}, "trail me")
    context, page = open_app(browser, team, team["maya"], **size(1180, 780))
    summary = page.locator(f'[data-trail="{mine["id"]}"] summary')
    history = page.locator(f'[data-trail="{mine["id"]}"] .rc-history')
    assert summary.get_attribute("role") == "button" and summary.get_attribute("aria-expanded") == "false"
    assert not history.is_visible() and "Details" not in page.locator(".rc-thread").inner_text()
    assert summary.locator(".rc-chevron").count() == 1
    sel = f'[data-trail="{mine["id"]}"] summary'
    summary.click()  # the toggle event is asynchronous: wait for the attribute
    page.wait_for_selector(f'{sel}[aria-expanded="true"]')
    assert history.is_visible()
    summary.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector(f'{sel}[aria-expanded="false"]')
    assert not history.is_visible()
    page.keyboard.press(" ")
    page.wait_for_selector(f'{sel}[aria-expanded="true"]')
    assert history.is_visible()
    context.close()
    context, page = open_app(browser, team, team["maya"], path=conv_path(mine), java_script_enabled=False, **size(1180, 780))
    summary = page.locator(f'[data-trail="{mine["id"]}"] details > summary')
    assert "stored" in summary.inner_text() and not page.locator(".rc-history").is_visible()
    summary.click()
    assert page.locator(".rc-history").is_visible()  # a plain <details> without JavaScript
    context.close()


# R6: one header height (criteria 20-21) ----------------------------------------------------------------------

def test_headers_have_one_height_and_presence_and_archive_share_a_line(team, browser):
    agent = person_send(team, team["maya"], {"machine": "jon-laptop", "agent": "api-agent"}, "agent")
    person = person_send(team, team["maya"], {"person": "jon@example.test"}, "person")
    inbox = person_send(team, team["maya"], {"machine": "sam-server"}, "inbox")
    for w, h in ((1180, 780), (760, 520)):
        heights = {}
        context, page = open_app(browser, team, team["maya"], **size(w, h))
        for name, path in (("agent", conv_path(agent)), ("person", conv_path(person)), ("inbox", conv_path(inbox)),
                           ("compose", "/app/compose"), ("agents", "/app/agents")):
            page.goto(team["base"] + path)
            heights[name] = page.locator(".rc-main-head").evaluate("e => e.getBoundingClientRect().height")
            if name == "agent":
                centres = page.evaluate("""() => ['.rc-presence', '.rc-archive .rc-btn', '.rc-head-title h2'].map(s => {
                    const r = document.querySelector(s).getBoundingClientRect(); return r.top + r.height / 2; })""")
                assert max(centres) - min(centres) <= 1.5, (w, centres)
        assert len(set(heights.values())) == 1, (w, heights)
        context.close()


# R7: short windows (criteria 22-23) --------------------------------------------------------------------------

@pytest.mark.skip(reason="frozen for v0.5.5 (R7/P1/Team/M3)")
def test_team_starts_collapsed_below_600_tall_unless_the_viewer_chose(team, browser):
    key = "rc-team-open:" + str(team["users"]["Maya"].id)
    context, page = open_app(browser, team, team["maya"], **size(760, 520))
    assert page.locator("details.rc-team").evaluate("d => d.open") is False
    page.wait_for_timeout(200)
    assert page.evaluate(f"localStorage.getItem('{key}')") is None  # the automatic close is not a choice
    page.set_viewport_size({"width": 760, "height": 700})
    page.reload()
    assert page.locator("details.rc-team").evaluate("d => d.open") is True  # tall: open
    page.set_viewport_size({"width": 760, "height": 520})
    page.evaluate(f"localStorage.setItem('{key}', '1')")
    page.reload()
    assert page.locator("details.rc-team").evaluate("d => d.open") is True  # the viewer's choice wins
    context.close()


@pytest.mark.skip(reason="frozen for v0.5.5 (R7/P1/Team/M3)")
def test_an_overflowing_list_fades_at_the_bottom_with_no_custom_scrollbar(team, browser):
    from raincli_server import identity

    for target in ({"machine": "jon-laptop", "agent": "api-agent"}, {"person": "jon@example.test"},
                   {"machine": "sam-server"}, {"machine": "priya-desktop", "agent": "web-agent"}):
        person_send(team, team["maya"], target, "hello")
    for i in range(6):  # and six more teammates: ten conversations
        user = identity.create_user(team["session"], f"p{i}@example.test", f"Person {i}", "correct horse battery")
        identity.add_member(team["session"], _team_of(team), user)
        team["session"].commit()
        person_send(team, team["maya"], {"person": f"p{i}@example.test"}, f"hello {i}")
    context, page = open_app(browser, team, team["maya"], **size(760, 520))
    convs = page.locator(".rc-convs")
    assert convs.evaluate("l => l.scrollHeight > l.clientHeight")
    assert "rc-more" in convs.get_attribute("class")
    convs.evaluate("l => { l.scrollTop = l.scrollHeight; }")
    page.wait_for_function("() => !document.querySelector('.rc-convs').classList.contains('rc-more')")
    assert page.locator(".rc-day", has_text="Today").count() == 1
    css = (STATIC / "app-mode.css").read_text("utf-8")
    assert "::-webkit-scrollbar" not in css and "scrollbar-color" not in css and "scrollbar-width" not in css
    context.close()


def _team_of(team):
    from sqlalchemy import select

    from raincli_server.models import Team

    return team["session"].scalar(select(Team).where(Team.slug == "northwind"))


# R8: the small details (criteria 24-27) ----------------------------------------------------------------------

def test_the_to_label_stays_top_aligned_with_the_first_line(team, browser):
    mine = person_send(team, team["maya"], {"machine": "jon-laptop", "agent": "api-agent"}, "hi")
    context, page = open_app(browser, team, team["maya"], path=conv_path(mine), **size(1180, 780))
    page.fill("#body", "\n".join(f"line {i}" for i in range(6)))
    tops = page.evaluate("""() => { const to = document.querySelector('.rc-to').getBoundingClientRect(),
        ta = document.querySelector('#body'), r = ta.getBoundingClientRect(), pad = parseFloat(getComputedStyle(ta).paddingTop);
        return {to: to.top, text: r.top + pad, height: r.height, pad}; }""")
    assert tops["height"] > 100 and tops["pad"] >= 4  # grown, and the text keeps its top padding
    assert abs(tops["to"] - tops["text"]) <= 5, tops
    context.close()


def test_previews_say_you_only_for_your_own_and_keep_a_machine_inboxs_sender(team, browser):
    person_send(team, team["maya"], {"machine": "jon-laptop", "agent": "api-agent"}, "mine")
    jon_writes(team, "from Jon the person")
    api(team["base"], "/api/v1/messages", {"id": str(uuid.uuid4()), "to": {"person": "maya@example.test"},
                                          "body": "from Sam's machine"}, team["machines"]["sam-server"][1])
    context, page = open_app(browser, team, team["maya"], **size(1180, 780))
    previews = {page.locator(".rc-conv-name").nth(i).inner_text(): page.locator(".rc-conv-preview").nth(i).inner_text()
                for i in range(page.locator(".rc-conv").count())}
    clean = {k: v.replace("Last message from\n", "").replace("Last message:\n", "") for k, v in previews.items()}
    assert clean["Jon · api-agent"] == "You: mine"
    assert clean["Jon"] == "from Jon the person"
    assert clean["Sam · inbox"].endswith(": from Sam's machine") and clean["Sam · inbox"].startswith("Sam")
    context.close()


@pytest.mark.skip(reason="frozen for v0.5.5 (R7/P1/Team/M3)")
def test_the_team_section_is_pinned_at_the_bottom_with_or_without_conversations(team, browser):
    def team_gap(page):
        return page.evaluate("""() => { const t = document.querySelector('details.rc-team').getBoundingClientRect(),
            w = document.querySelector('.rc-list .rc-whoami').getBoundingClientRect(); return w.top - t.bottom; }""")
    context, page = open_app(browser, team, team["maya"], **size(1180, 780))
    empty_gap = team_gap(page)
    context.close()
    person_send(team, team["maya"], {"machine": "jon-laptop", "agent": "api-agent"}, "hi")
    context, page = open_app(browser, team, team["maya"], **size(1180, 780))
    assert abs(team_gap(page) - empty_gap) <= 1 and empty_gap < 20
    context.close()


# 29: tokens, no inline styles, AA ----------------------------------------------------------------------------

def test_no_inline_styles_and_the_new_parts_pass_aa(team, browser):
    mine = person_send(team, team["maya"], {"machine": "jon-laptop", "agent": "api-agent"}, "hi")
    api(team["base"], "/api/v1/presence", {"status": "ready", "agents": [], "client": CLIENT},
        team["machines"]["jon-laptop"][1], method="PUT")
    for scheme in ("light", "dark"):
        context, page = open_app(browser, team, team["maya"], color_scheme=scheme, **size(1180, 780))
        for path in (conv_path(mine), "/app/compose", "/app/agents"):
            page.goto(team["base"] + path)
            assert page.locator("body [style]").count() == 0, (scheme, path)
        page.goto(team["base"] + conv_path(mine))
        bg = page.evaluate("getComputedStyle(document.querySelector('.rc-main')).backgroundColor")
        chevron = page.locator(".rc-chevron").first.evaluate("e => getComputedStyle(e).color")
        rgb = lambda s: re.findall(r"\d+", s)[:3]  # noqa: E731
        assert _contrast(rgb(chevron), rgb(bg)) >= 3, (scheme, chevron, bg)  # a non-text indicator
        context.close()
    css = (STATIC / "app-mode.css").read_text("utf-8") + (STATIC / "app-mode.js").read_text("utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|https?://", css)


# The frozen items are off: v0.5.4's behaviour holds (P1, R7, the Team tooltip) -------------------------------

def test_v0_5_4_behaviour_holds_for_the_frozen_items(team, browser):
    """P1, R7 and the Team work are frozen for v0.5.5: "via" shows even when it equals the conversation's agent, the
    Team section stays open in a short window with no list fade, and the Team tooltip names the handle."""
    ask = person_send(team, team["maya"], {"machine": "jon-laptop", "agent": "api-agent"}, "Can you check?")
    api(team["base"], "/api/v1/messages", {"id": str(uuid.uuid4()), "to": {"person": "maya@example.test"},
                                          "from_agent": "api-agent", "body": "Done.", "in_reply_to": ask["id"]},
        team["machines"]["jon-laptop"][1])
    context, page = open_app(browser, team, team["maya"], path=conv_path(ask), **size(760, 520))
    assert "via api-agent (stated by the machine)" in page.locator(".rc-thread").inner_text()
    assert page.locator("details.rc-team").evaluate("d => d.open") is True  # no R7 collapse below 600 px tall
    assert "rc-more" not in (page.locator(".rc-convs").get_attribute("class") or "")
    tip = page.locator(".rc-team-agent", has_text="Jon · api-agent").get_attribute("title")
    assert tip == "New message to Jon · api-agent (jon-laptop/api-agent)"
    context.close()

