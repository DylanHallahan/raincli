"""The live app thread in headless Chromium against a real server (protocol §19; v0.5.4 brief criteria 1, 3, 4, 7,
9-13, 17 and 18)."""

from __future__ import annotations

import json
import re
import time
import urllib.request
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import update

from .test_app_mode_gui import (PASSWORD, _contrast, api, artifacts, browser, live, open_app, people,  # noqa: F401
                                person_send)

pytest.importorskip("playwright.sync_api")

REVIEWER = {"key": "a" * 32, "name": "reviewer", "type": "claude", "status": "idle", "role": None,
            "reachability": "instant", "source": "herdr"}
CLIENT = {"version": "0.5.4", "update_mode": "manual", "update_state": "current"}


def put_presence(base, token, agents, status="ready"):
    request = urllib.request.Request(base + "/api/v1/presence", method="PUT",
                                     data=json.dumps({"status": status, "agents": agents, "client": CLIENT}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    urllib.request.urlopen(request, timeout=15).read()


def machine_send(base, token, to, body, conversation_id=None, from_agent="reviewer"):
    """Bob's machine replies into the agent thread as its reviewer agent (the thread's endpoint)."""
    payload = {"id": str(uuid.uuid4()), "to": to, "body": body}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    if from_agent:
        payload["from_agent"] = from_agent
    return api(base, "/api/v1/messages", payload, token)["message"]


def event(base, token, message_id, state, detail=None):
    return api(base, f"/api/v1/messages/{message_id}/events", {"state": state, "detail": detail}, token)


def ack(base, token, message_id):
    return api(base, f"/api/v1/messages/{message_id}/ack", {}, token)


def open_thread(browser, people, conversation_id, scheme="light", **options):
    context, page = open_app(browser, people["base"], people["alice"]["person_session"], scheme, **options)
    page.goto(people["base"] + f"/app/conversations/{conversation_id}#latest")
    page.wait_for_selector("[data-thread]")
    return context, page


def poll_now(page):
    page.evaluate("() => window.rcLive && window.rcLive.poll()")


@pytest.fixture
def agent_thread(people):
    """Alice writes to Bob's reviewer agent, which Bob's machine reports as instant and idle."""
    put_presence(people["base"], people["bob"]["token"], [REVIEWER])
    first = person_send(people["base"], people["alice"]["person_session"],
                        {"machine": "bob-laptop", "agent": "reviewer"}, "Can you run the checkout flow?")
    return {**people, "first": first, "cid": first["conversation_id"]}


# 1. Avatar colours ---------------------------------------------------------------------------------------

def test_only_your_own_messages_use_the_accent_avatar(agent_thread, browser):
    p = agent_thread
    machine_send(p["base"], p["bob"]["token"], {"person": "alice@example.test"}, "Two failures.", p["cid"])
    for scheme in ("light", "dark"):
        context, page = open_thread(browser, p, p["cid"], scheme)
        colours = page.evaluate("""() => { const s = getComputedStyle(document.documentElement);
            const probe = (v) => { const el = document.createElement('span'); el.style.color = s.getPropertyValue(v);
              document.body.appendChild(el); const c = getComputedStyle(el).color; el.remove(); return c; };
            const av = (sel) => getComputedStyle(document.querySelector(sel)).backgroundColor;
            return {mine: av('.rc-msg.rc-out .rc-avatar'), theirs: av('.rc-msg:not(.rc-out) .rc-avatar'),
                    soft: probe('--rc-accent-soft'), surface: probe('--rc-surface'),
                    border: getComputedStyle(document.querySelector('.rc-msg:not(.rc-out) .rc-avatar')).borderTopColor,
                    bordertoken: probe('--rc-border')}; }""")
        assert colours["mine"] == colours["soft"] and colours["theirs"] == colours["surface"], (scheme, colours)
        assert colours["border"] == colours["bordertoken"]
        context.close()


# 3. Meta line and Reply -------------------------------------------------------------------------------------

def test_meta_line_names_and_reply_on_hover_and_focus(agent_thread, browser):
    p = agent_thread
    reply = machine_send(p["base"], p["bob"]["token"], {"person": "alice@example.test"}, "Done.", p["cid"])
    context, page = open_thread(browser, p, p["cid"])
    theirs = page.locator(f"#m-{reply['id']}")
    who = theirs.locator(".rc-who").inner_text()
    assert who.startswith("Bob · reviewer · ") and " to " not in who  # name · local time; no "to" here
    assert "via reviewer (stated by the machine)" in who
    link = theirs.locator(".rc-reply")
    assert float(link.evaluate("e => getComputedStyle(e).opacity")) == 0
    theirs.hover()
    page.wait_for_function("e => getComputedStyle(e).opacity === '1'", arg=link.element_handle())
    page.mouse.move(0, 0)
    link.focus()  # reachable by keyboard, and visible once focused
    page.wait_for_function("e => getComputedStyle(e).opacity === '1'", arg=link.element_handle())
    context.close()
    # "to <recipient>" only in machine-inbox conversations.
    inbox = person_send(p["base"], p["alice"]["person_session"], {"machine": "bob-laptop"}, "to the inbox")
    context, page = open_thread(browser, p, inbox["conversation_id"])
    assert "to Bob · inbox" in page.locator(f"#m-{inbox['id']} .rc-who").inner_text()
    context.close()


# 4. The delivery trail ------------------------------------------------------------------------------------------

def steps(page, message_id):
    trail = page.locator(f'[data-trail="{message_id}"]')
    return ([s.inner_text() for s in trail.locator(".rc-tstep").all()],
            [s.inner_text() for s in trail.locator(".rc-tstep.rc-treached").all()],
            trail.locator(".sr-only").first.inner_text())


def test_the_trail_for_each_recipient_kind_and_none_on_incoming(agent_thread, browser):
    p = agent_thread
    ack(p["base"], p["bob"]["token"], p["first"]["id"])
    event(p["base"], p["bob"]["token"], p["first"]["id"], "submitted")
    reply = machine_send(p["base"], p["bob"]["token"], {"person": "alice@example.test"}, "Reply", p["cid"])
    context, page = open_thread(browser, p, p["cid"])
    names, reached, phrase = steps(page, p["first"]["id"])
    assert names == ["stored", "received", "submitted", "replied"] and reached == ["stored", "received", "submitted"]
    assert phrase == "Delivery: submitted, not yet replied"
    title = page.locator(f'[data-trail="{p["first"]["id"]}"] .rc-tstep.rc-treached').first.get_attribute("title")
    assert title.startswith("Stored ")
    assert page.locator(f"#m-{reply['id']} [data-trail]").count() == 0  # incoming: no state
    reached_colour = page.evaluate("""() => [getComputedStyle(document.querySelector('.rc-tstep.rc-treached')).color,
        (() => { const el = document.createElement('span'); el.style.color = getComputedStyle(document.documentElement)
          .getPropertyValue('--rc-ok'); document.body.appendChild(el); const c = getComputedStyle(el).color; el.remove();
          return c; })()]""")
    assert reached_colour[0] == reached_colour[1]
    page.locator(f'[data-trail="{p["first"]["id"]}"] summary').click()
    assert "submitted" in page.locator(f'[data-trail="{p["first"]["id"]}"] details ol').inner_text()
    context.close()
    person = person_send(p["base"], p["alice"]["person_session"], {"person": "bob@example.test"}, "Hi Bob")
    context, page = open_thread(browser, p, person["conversation_id"])
    assert steps(page, person["id"])[0] == ["stored", "received", "replied"]
    assert page.locator("[data-live-header] .rc-chip").count() == 0  # a person: no chip
    context.close()


def test_held_uncertain_and_rejected_show_their_reason(agent_thread, browser, artifacts):
    p = agent_thread
    msgs = [p["first"]] + [person_send(p["base"], p["alice"]["person_session"],
                                       {"machine": "bob-laptop", "agent": "reviewer"}, f"m{i}") for i in range(2)]
    for m, (state, detail) in zip(msgs, (("held", "approval_required"), ("submission_uncertain", None),
                                         ("rejected", None))):
        ack(p["base"], p["bob"]["token"], m["id"])
        event(p["base"], p["bob"]["token"], m["id"], state, detail)
    context, page = open_thread(browser, p, p["cid"])
    held, uncertain, rejected = (page.locator(f'[data-trail="{m["id"]}"]') for m in msgs)
    assert "held: waiting for approval" in held.inner_text() and "rc-trail-warn" in held.get_attribute("class")
    assert "uncertain:" in uncertain.inner_text() and "rc-trail-warn" in uncertain.get_attribute("class")
    assert "rejected:" in rejected.inner_text() and "rc-trail-danger" in rejected.get_attribute("class")
    assert held.locator(".rc-tstep", has_text="submitted").count() == 0  # the outcome replaces the rest
    page.screenshot(path=str(artifacts / "live-held-rejected.png"))
    context.close()


# 7 and 18. Header chip and presence, with the offline rule ---------------------------------------------------

def test_header_chip_and_presence_follow_the_live_directory(agent_thread, browser, session):
    p = agent_thread
    context, page = open_thread(browser, p, p["cid"])
    header = page.locator("[data-live-header]")
    assert header.locator(".rc-chip").inner_text() == "Instant" and "Ready" in header.inner_text()
    assert page.locator(".rc-pill").count() == 0
    put_presence(p["base"], p["bob"]["token"], [{**REVIEWER, "status": "working"}])
    poll_now(page)
    page.wait_for_function("() => document.querySelector('[data-live-header]').innerText.includes('Busy')")
    assert "working at its last report" in header.locator(".rc-presence").get_attribute("title")
    from raincli_server.models import Agent, AgentPresence, MachineAgent
    bob = session.query(Agent).filter_by(handle="bob-laptop").one()
    old = __import__("raincli_server").identity.now() - timedelta(seconds=125)
    session.execute(update(AgentPresence).where(AgentPresence.agent_id == bob.id).values(seen_at=old))
    session.execute(update(MachineAgent).where(MachineAgent.agent_id == bob.id).values(seen_at=old))
    session.commit()
    poll_now(page)
    page.wait_for_function("() => document.querySelector('[data-live-header]').innerText.includes('Offline')")
    assert header.locator(".rc-chip").count() == 0  # never "Instant" past 2 minutes
    text = page.inner_text("body").lower()
    assert "typing" not in text and "is working on" not in text
    context.close()


# 9. The sender warning --------------------------------------------------------------------------------------------

def test_the_sender_warning_on_compose_and_in_the_thread(people, browser, session):
    from raincli_server import identity
    from raincli_server.models import Team, User

    team = session.query(Team).filter_by(slug="acme").one()
    bob = session.query(User).filter_by(email="bob@example.test").one()
    identity.register_agent(session, team, bob, "old-laptop")  # never connected
    session.commit()
    context, page = open_app(browser, people["base"], people["alice"]["person_session"])
    page.goto(people["base"] + "/app/compose")
    page.fill("#to", "old-laptop")
    page.wait_for_selector("[data-recipient-warning] .sender-warning")
    assert page.inner_text("[data-recipient-warning]").strip() == "old-laptop has never connected."
    page.fill("#to", "@bob@example.test")
    page.wait_for_function("() => !document.querySelector('[data-recipient-warning] .sender-warning')")
    context.close()
    with browser.new_context() as website:  # the website compose page, in its own look
        site = website.new_page()
        site.goto(people["base"] + "/login")
        site.fill("#email", "alice@example.test")
        site.fill("#password", PASSWORD)
        site.click("button[type=submit]")
        site.wait_for_url("**/app**")
        site.goto(people["base"] + "/app/compose")
        site.fill("#to", "old-laptop")
        site.wait_for_selector("[data-recipient-warning] .sender-warning")


# 10-13. Polling, read state, scrolling and motion ------------------------------------------------------------------

def test_new_messages_and_state_changes_arrive_within_five_seconds(agent_thread, browser):
    p = agent_thread
    context, page = open_thread(browser, p, p["cid"])
    started = time.monotonic()
    reply = machine_send(p["base"], p["bob"]["token"], {"person": "alice@example.test"}, "**live** reply", p["cid"])
    page.wait_for_selector(f"#m-{reply['id']}", timeout=7000)
    assert time.monotonic() - started < 6.5
    assert page.locator(f"#m-{reply['id']} .md strong").inner_text() == "live"
    assert "rc-arrived" in page.locator(f"#m-{reply['id']}").get_attribute("class")
    started = time.monotonic()
    ack(p["base"], p["bob"]["token"], p["first"]["id"])
    page.wait_for_function(f"""() => document.querySelector('[data-trail="{p["first"]["id"]}"]')
        .getAttribute('data-state') !== 'stored'""", timeout=7000)
    assert time.monotonic() - started < 6.5
    context.close()


def test_polling_stops_while_hidden_and_resumes_at_once_when_shown(agent_thread, browser):
    p = agent_thread
    context, page = open_thread(browser, p, p["cid"])
    polls = []
    page.on("request", lambda r: polls.append(time.monotonic()) if "/app/live" in r.url else None)
    page.evaluate("""() => { Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});
        document.dispatchEvent(new Event('visibilitychange')); }""")
    time.sleep(0.5)
    polls.clear()
    time.sleep(11)
    assert polls == []  # nothing while hidden
    page.evaluate("""() => { Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});
        document.dispatchEvent(new Event('visibilitychange')); }""")
    shown = time.monotonic()
    page.wait_for_function("() => true")
    deadline = time.monotonic() + 3
    while not polls and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert polls and polls[0] - shown < 1.5  # an immediate refresh
    context.close()


def test_network_errors_back_off_to_thirty_seconds_without_a_dialog(agent_thread, browser):
    p = agent_thread
    context, page = open_app(browser, p["base"], p["alice"]["person_session"])
    page.clock.install()
    page.goto(p["base"] + f"/app/conversations/{p['cid']}")
    dialogs, polls = [], []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.route("**/app/live**", lambda route: (polls.append(1), route.abort()))
    page.clock.run_for(5_500)
    page.wait_for_timeout(300)
    assert len(polls) == 1
    page.clock.run_for(20_000)
    page.wait_for_timeout(300)
    assert len(polls) == 1  # not every 5 s after a failure
    page.clock.run_for(11_000)
    page.wait_for_timeout(300)
    assert len(polls) == 2 and dialogs == []
    context.close()


def test_a_message_counts_as_read_only_when_focused_and_half_visible(agent_thread, browser, session):
    from raincli_server.models import Message

    p = agent_thread
    reply = machine_send(p["base"], p["bob"]["token"], {"person": "alice@example.test"}, "read me", p["cid"])
    context, page = open_app(browser, p["base"], p["alice"]["person_session"])
    page.add_init_script("window.__focus = false; document.hasFocus = () => window.__focus;")
    page.goto(p["base"] + f"/app/conversations/{p['cid']}#latest")
    page.wait_for_selector(f"#m-{reply['id']}")
    poll_now(page)
    page.wait_for_timeout(1500)
    session.expire_all()
    assert session.get(Message, uuid.UUID(reply["id"])).acked_at is None  # visible, but no focus: not read
    page.evaluate("() => { window.__focus = true; window.dispatchEvent(new Event('focus')); }")
    page.wait_for_function(f"() => !document.querySelector('#m-{reply['id']}').hasAttribute('data-unread')")
    session.expire_all()
    assert session.get(Message, uuid.UUID(reply["id"])).acked_at is not None
    context.close()


def test_new_messages_scroll_in_at_the_bottom_or_offer_new_messages_when_scrolled_up(agent_thread, browser):
    p = agent_thread
    for i in range(25):
        machine_send(p["base"], p["bob"]["token"], {"person": "alice@example.test"}, f"line {i}\n\nmore\n\ntext", p["cid"])
    context, page = open_thread(browser, p, p["cid"])
    page.set_viewport_size({"width": 1180, "height": 700})
    late = machine_send(p["base"], p["bob"]["token"], {"person": "alice@example.test"}, "at the bottom", p["cid"])
    poll_now(page)
    page.wait_for_selector(f"#m-{late['id']}")
    page.wait_for_timeout(300)
    assert page.evaluate(f"""() => {{ const el = document.getElementById('m-{late['id']}').getBoundingClientRect();
        return el.bottom <= document.querySelector('.rc-scroll').getBoundingClientRect().bottom + 2; }}""")
    page.evaluate("() => { document.querySelector('.rc-scroll').scrollTop = 0; }")
    page.wait_for_timeout(200)
    later = machine_send(p["base"], p["bob"]["token"], {"person": "alice@example.test"}, "while away", p["cid"])
    poll_now(page)
    page.wait_for_selector(f"#m-{later['id']}", state="attached")
    assert page.is_visible("[data-new-messages]") and page.evaluate("() => document.querySelector('.rc-scroll').scrollTop") < 50
    context.close()


def test_motion_is_short_and_absent_under_reduced_motion(agent_thread, browser):
    p = agent_thread
    for motion, expect in (("no-preference", True), ("reduce", False)):
        context, page = open_thread(browser, p, p["cid"], reduced_motion=motion)
        reply = machine_send(p["base"], p["bob"]["token"], {"person": "alice@example.test"}, f"m {motion}", p["cid"])
        poll_now(page)
        page.wait_for_selector(f"#m-{reply['id']}")
        name, duration = page.evaluate(f"""() => {{ const s = getComputedStyle(document.getElementById('m-{reply['id']}'));
            return [s.animationName, parseFloat(s.animationDuration)]; }}""")
        if expect:
            assert name == "rc-rise" and 0.15 <= duration <= 0.3
        else:
            assert name == "none"
        context.close()


# 17. Tokens and contrast -------------------------------------------------------------------------------------------

def test_new_colour_pairs_pass_aa_in_light_and_dark(agent_thread, browser):
    p = agent_thread
    for scheme in ("light", "dark"):
        context, page = open_thread(browser, p, p["cid"], scheme)
        rgb = page.evaluate("""() => { const s = getComputedStyle(document.documentElement);
            const probe = (v) => { const el = document.createElement('span'); el.style.color = s.getPropertyValue(v);
              document.body.appendChild(el); const c = getComputedStyle(el).color; el.remove(); return c.match(/\\d+/g).slice(0, 3); };
            return Object.fromEntries(['--rc-bg','--rc-ok','--rc-warn','--rc-warn-bg','--rc-danger','--rc-muted',
              '--rc-accent','--rc-accent-soft','--rc-out-bg'].map(v => [v, probe(v)])); }""")
        for fg, bg in (("--rc-ok", "--rc-bg"), ("--rc-warn", "--rc-bg"), ("--rc-danger", "--rc-bg"),
                       ("--rc-warn", "--rc-warn-bg"), ("--rc-accent", "--rc-accent-soft"), ("--rc-muted", "--rc-bg"),
                       ("--rc-ok", "--rc-out-bg")):
            assert _contrast(rgb[fg], rgb[bg]) >= 4.5, (scheme, fg, bg, _contrast(rgb[fg], rgb[bg]))
        page.fill("#body", "one\ntwo\nthree")
        assert page.locator("[style]").count() == 0  # no inline styles, even once the composer grows (§19.8 L1)
        context.close()


def test_the_thread_fits_the_minimum_window(agent_thread, browser, artifacts):
    """At 760x520 the main pane is narrow: nothing in the thread or composer overflows it, and Send stays in view."""
    p = agent_thread
    machine_send(p["base"], p["bob"]["token"], {"person": "alice@example.test"}, "A reply with `code` " * 6, p["cid"])
    for scheme in ("light", "dark"):
        context, page = open_thread(browser, p, p["cid"], scheme, viewport={"width": 760, "height": 520})
        fits = page.evaluate("""() => { const main = document.querySelector('.rc-main').getBoundingClientRect();
            const out = [...document.querySelectorAll('.rc-main *')].filter(el => {
              const r = el.getBoundingClientRect();
              return r.width && (r.right > main.right + 1 || r.left < main.left - 1); }).map(el => el.className);
            const send = document.querySelector('.rc-send').getBoundingClientRect();
            return {out: out.slice(0, 5), send: send.right <= main.right && send.bottom <= window.innerHeight}; }""")
        assert fits["out"] == [] and fits["send"], (scheme, fits)
        page.screenshot(path=str(artifacts / f"live-thread-760x520-{scheme}.png"))
        context.close()



def test_the_composer_grows_to_eight_lines_and_ctrl_enter_sends(agent_thread, browser):
    p = agent_thread
    context, page = open_thread(browser, p, p["cid"])
    box = page.locator("#body")
    one = box.bounding_box()["height"]
    box.fill("\n".join(f"line {i}" for i in range(6)))
    six = box.bounding_box()["height"]
    box.fill("\n".join(f"line {i}" for i in range(20)))
    capped = box.bounding_box()["height"]
    line = float(page.evaluate("() => parseFloat(getComputedStyle(document.querySelector('#body')).lineHeight)"))
    assert one < 2 * line and six > 5 * line and capped <= 8 * line + 16, (one, six, capped, line)
    assert page.get_attribute("#body", "placeholder") == "Message… Markdown and .md files supported"
    assert page.locator(".rc-hint").count() == 0
    box.fill("Sent with Ctrl+Enter")
    box.press("Enter")
    assert "\n" in page.input_value("#body")  # Enter adds a new line
    box.press("Control+Enter")
    page.wait_for_url("**notice=sent**")
    assert "Sent with Ctrl+Enter" in page.inner_text(".rc-thread")
    context.close()


def test_a_long_message_counts_as_seen_once_it_fills_half_the_view(agent_thread, browser, session):
    """§19.8 M3: seen at min(half the message, half the viewport) of visible height, while focused."""
    from raincli_server.models import Message

    p = agent_thread
    long_body = "\n\n".join(f"paragraph {i}" for i in range(120))
    reply = machine_send(p["base"], p["bob"]["token"], {"person": "alice@example.test"}, long_body, p["cid"])
    machine_send(p["base"], p["bob"]["token"], {"person": "alice@example.test"}, "after it", p["cid"])
    context, page = open_app(browser, p["base"], p["alice"]["person_session"], viewport={"width": 1180, "height": 700})
    page.goto(p["base"] + f"/app/conversations/{p['cid']}")
    page.wait_for_selector(f"#m-{reply['id']}")
    tall, view = page.evaluate(f"""() => [document.getElementById('m-{reply['id']}').getBoundingClientRect().height,
        document.querySelector('.rc-scroll').getBoundingClientRect().height]""")
    assert tall > 2 * view  # never half on screen
    page.evaluate(f"() => document.getElementById('m-{reply['id']}').scrollIntoView({{block: 'start'}})")
    page.wait_for_function(f"() => !document.querySelector('#m-{reply['id']}').hasAttribute('data-unread')")
    session.expire_all()
    assert session.get(Message, uuid.UUID(reply["id"])).acked_at is not None
    context.close()
