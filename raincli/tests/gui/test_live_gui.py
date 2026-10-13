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


BOB = {"person": "bob@example.test"}  # §21: people are addressed


def pick_inbox(base, token, agent):
    """§21 C8.1: a machine's owner picks one of its agents as their inbox (a machine credential may pick only its
    own agents, C13 L4)."""
    request = urllib.request.Request(base + "/api/v1/me/inbox", method="PUT",
                                     data=json.dumps({"agent": agent}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    urllib.request.urlopen(request, timeout=15).read()


@pytest.fixture
def agent_thread(people, session):
    """Alice writes to Bob, whose inbox is the reviewer agent his machine reports as instant and idle. Alice has no
    inbox (§21 C3.2), so what Bob's agent sends her is delivered in her app: the read state these tests are about."""
    from raincli_server.models import PersonInbox

    session.query(PersonInbox).filter(PersonInbox.user_id == people["world"]["users"]["alice"].id).delete()
    session.commit()
    put_presence(people["base"], people["bob"]["token"], [REVIEWER])
    pick_inbox(people["base"], people["bob"]["token"], "reviewer")
    # The machine's routing poll (§16.12 C1): it takes named messages, so nothing waits for an update.
    urllib.request.urlopen(urllib.request.Request(people["base"] + "/api/v1/inbox?routing=1&wait=0", headers={
        "Authorization": "Bearer " + people["bob"]["token"]}), timeout=15).read()
    first = person_send(people["base"], people["alice"]["person_session"], BOB, "Can you run the checkout flow?")
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
    assert who.startswith("Bob · agent · ") and " to " not in who  # §21 PR4: the person · agent · local time
    assert "via" not in who and "reviewer" not in who  # no agent name, no "via" (P1 deleted)
    link = theirs.locator(".rc-reply")
    assert float(link.evaluate("e => getComputedStyle(e).opacity")) == 0
    theirs.hover()
    page.wait_for_function("e => getComputedStyle(e).opacity === '1'", arg=link.element_handle())
    page.mouse.move(0, 0)
    link.focus()  # reachable by keyboard, and visible once focused
    page.wait_for_function("e => getComputedStyle(e).opacity === '1'", arg=link.element_handle())
    context.close()
    # What Bob writes in the app is "Bob", not "Bob · agent" (PR4).
    by_hand = person_send(p["base"], p["bob"]["person_session"], {"person": "alice@example.test"}, "by hand")
    context, page = open_thread(browser, p, by_hand["conversation_id"])
    who = page.locator(f"#m-{by_hand['id']} .rc-who").inner_text()
    assert who.startswith("Bob · ") and "agent" not in who
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
    # §21 C3.2, C6.1: with no inbox, a message waits in the person's app, held until they pick one; no chip.
    request = urllib.request.Request(p["base"] + "/api/v1/me/inbox", method="PUT", data=json.dumps({"none": True}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + p["bob"]["token"]})
    urllib.request.urlopen(request, timeout=15).read()
    person = person_send(p["base"], p["alice"]["person_session"], {"person": "bob@example.test"}, "Hi Bob")
    context, page = open_thread(browser, p, person["conversation_id"])
    assert steps(page, person["id"])[0] == ["stored", "held: waiting for Bob to pick an inbox"]
    assert page.locator("[data-live-header] .rc-chip").count() == 0
    assert "No inbox yet" in page.locator("[data-live-header]").inner_text()
    context.close()


def test_held_uncertain_and_rejected_show_their_reason(agent_thread, browser, artifacts):
    p = agent_thread
    msgs = [p["first"]] + [person_send(p["base"], p["alice"]["person_session"],
                                       BOB, f"m{i}") for i in range(2)]
    for m, (state, detail) in zip(msgs, (("held", "approval_required"), ("submission_uncertain", None),
                                         ("rejected", None))):
        ack(p["base"], p["bob"]["token"], m["id"])
        event(p["base"], p["bob"]["token"], m["id"], state, detail)
    context, page = open_thread(browser, p, p["cid"])
    held, uncertain, rejected = (page.locator(f'[data-trail="{m["id"]}"]') for m in msgs)
    assert "held: waiting for Bob's approval" in held.inner_text() and "rc-trail-warn" in held.get_attribute("class")
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
    assert "session" not in header.inner_text().lower()  # §21 C5.4: the person's presence · speed, no session words
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
    """§21 C7.4: the warning is about a person's inbox, by name; picking the person shows it."""
    from raincli_server import identity
    from raincli_server.models import PersonInbox, Team, User

    team = session.query(Team).filter_by(slug="acme").one()
    bob = session.query(User).filter_by(email="bob@example.test").one()
    machine, _ = identity.register_agent(session, team, bob, "old-laptop")  # never connected
    session.query(PersonInbox).filter(PersonInbox.user_id == bob.id).delete()
    session.add(PersonInbox(user_id=bob.id, team_id=team.id, agent_id=machine.id, agent_name="reviewer", set_via="app"))
    session.commit()
    expected = "Bob's inbox hasn't connected yet. They'll see your message in their app."
    context, page = open_app(browser, people["base"], people["alice"]["person_session"])
    page.goto(people["base"] + "/app/compose")
    page.click("#to")
    page.fill("#to", "Bob")
    page.wait_for_selector(".rc-picker-option")
    page.press("#to", "Enter")
    page.wait_for_selector("[data-recipient-warning] .sender-warning")
    assert page.inner_text("[data-recipient-warning]").strip() == expected
    assert "old-laptop" not in page.content()
    context.close()
    with browser.new_context() as website:  # the website compose page, in its own look (no JavaScript picker)
        site = website.new_page()
        site.goto(people["base"] + "/login")
        site.fill("#email", "alice@example.test")
        site.fill("#password", PASSWORD)
        site.click("button[type=submit]")
        site.wait_for_url("**/app**")
        site.goto(people["base"] + "/app/compose")
        site.select_option("#to", label="Bob")
        site.wait_for_selector("[data-recipient-warning] .sender-warning")
        assert site.inner_text("[data-recipient-warning]").strip() == expected

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
    # From the handoff on: Inbox opens this, the latest conversation, by itself (§20.6 M1), unfocused here.
    context, page = open_app(browser, p["base"], p["alice"]["person_session"],
                             init_script="window.__focus = false; document.hasFocus = () => window.__focus;")
    page.goto(p["base"] + f"/app/conversations/{p['cid']}#latest")
    page.wait_for_selector(f"#m-{reply['id']}")
    poll_now(page)
    page.wait_for_timeout(1500)
    session.expire_all()
    assert session.get(Message, uuid.UUID(reply["id"])).person_seen_at is None  # visible, but no focus: not read
    page.evaluate("() => { window.__focus = true; window.dispatchEvent(new Event('focus')); }")
    page.wait_for_function(f"() => !document.querySelector('#m-{reply['id']}').hasAttribute('data-unread')")
    session.expire_all()
    assert session.get(Message, uuid.UUID(reply["id"])).person_seen_at is not None
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
    box.press("Control+Enter")  # sent in place (§20.2): the message lands without a page load
    page.wait_for_function("() => document.querySelector('.rc-thread').innerText.includes('Sent with Ctrl+Enter')")
    assert page.input_value("#body") == "" and "notice=" not in page.url
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
    assert session.get(Message, uuid.UUID(reply["id"])).person_seen_at is not None
    context.close()


# §19.9 ---------------------------------------------------------------------------------------------------

def test_step_tooltips_are_local_also_when_inserted_live(agent_thread, browser):
    """F2: under a forced zone the trail tooltips show the local time, compared with Intl in the same browser."""
    p = agent_thread
    context, page = open_thread(browser, p, p["cid"], timezone_id="Asia/Kolkata", locale="en-GB")
    check = """() => [...document.querySelectorAll('[data-step-time]')].map(el => [el.getAttribute('title'),
        el.getAttribute('data-step-label') + ' ' + new Intl.DateTimeFormat(undefined, {weekday: 'long', year: 'numeric',
        month: 'long', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit', timeZoneName: 'long'})
        .format(new Date(el.getAttribute('data-step-time')))])"""
    page.wait_for_function("() => document.querySelector('[data-step-time][data-local]')")
    assert all(a == b and "UTC" not in a for a, b in page.evaluate(check))
    ack(p["base"], p["bob"]["token"], p["first"]["id"])  # a trail swapped in by the poll
    poll_now(page)
    page.wait_for_function(f"""() => document.querySelectorAll('[data-trail="{p["first"]["id"]}"] [data-step-time][data-local]').length == 2""")
    pairs = page.evaluate(check)
    assert len(pairs) >= 2 and all(a == b and "India" in a for a, b in pairs), pairs
    context.close()


def test_the_list_swap_keeps_focus_and_scroll(agent_thread, browser, session):
    """F3: a conversation link keeps keyboard focus, and the list its scroll, when a new message changes the list.
    (v0.5's version used the Team list, which §21 C7.6 removed.)"""
    from raincli_server import identity
    from raincli_server.models import Conversation, Message, Team, User

    p = agent_thread
    team = session.query(Team).filter_by(slug="acme").one()
    alice = session.query(User).filter_by(email="alice@example.test").one()
    for i in range(30):
        other = identity.create_user(session, f"mate{i:02d}@example.test", f"Mate {i:02d}", "correct horse battery")
        identity.add_member(session, team, other)
        lo, hi = sorted((alice, other), key=lambda u: f"p:{u.id}")
        conv = Conversation(id=uuid.uuid4(), team_id=team.id, a_user_id=lo.id, b_user_id=hi.id, a_key=f"p:{lo.id}",
                            b_key=f"p:{hi.id}", kind="pair", is_default=True)
        session.add(conv)
        session.flush()
        session.add(Message(id=uuid.uuid4(), team_id=team.id, conversation_id=conv.id, body=f"hello {i}",
                            kind="message", route="person", sender_user_id=other.id, recipient_user_id=alice.id,
                            delivery_state="stored"))
    session.commit()
    context, page = open_thread(browser, p, p["cid"], viewport={"width": 1180, "height": 600})
    convs = page.locator(".rc-list .rc-convs")
    assert convs.evaluate("e => e.scrollHeight > e.clientHeight")
    target = page.locator(".rc-convs .rc-conv").nth(12)
    href = target.get_attribute("href")
    target.focus()
    convs.evaluate("e => { e.scrollTop = 200; }")  # after focusing, which scrolls it into view
    before = page.evaluate("() => document.querySelector('.rc-list').outerHTML")
    machine_send(p["base"], p["bob"]["token"], {"person": "alice@example.test"}, "a new message", p["cid"])
    poll_now(page)
    page.wait_for_function("(before) => document.querySelector('.rc-list').outerHTML !== before", arg=before)
    assert page.evaluate("() => document.activeElement.getAttribute('href')") == href
    assert page.locator(".rc-list .rc-convs").evaluate("e => e.scrollTop") == 200
    context.close()


def test_an_unchanged_warning_is_not_replaced(agent_thread, browser, session):
    """F4: the role=status warning stays the same node across polls when nothing changed."""
    from raincli_server import identity
    from raincli_server.models import Agent, AgentCredential

    p = agent_thread
    from raincli_server.models import AgentPresence

    bob = session.query(Agent).filter_by(handle="bob-laptop").one()  # Bob's inbox (agent_thread)
    old = identity.now() - timedelta(days=3)
    session.execute(update(AgentCredential).where(AgentCredential.agent_id == bob.id).values(last_used_at=old))
    session.execute(update(AgentPresence).where(AgentPresence.agent_id == bob.id).values(seen_at=old))
    session.commit()
    context, page = open_thread(browser, p, p["cid"])
    page.wait_for_selector("[data-live-warning] .sender-warning")
    page.evaluate("() => { document.querySelector('[data-live-warning] .sender-warning').__kept = true; }")
    for _ in range(2):
        poll_now(page)
        page.wait_for_timeout(700)
    assert page.evaluate("() => document.querySelector('[data-live-warning] .sender-warning').__kept === true")
    assert "since" in page.inner_text("[data-live-warning]") and ":" not in page.inner_text("[data-live-warning] time")
    context.close()


def test_an_error_while_applying_never_stops_polling(agent_thread, browser):
    """F5."""
    p = agent_thread
    context, page = open_app(browser, p["base"], p["alice"]["person_session"])
    page.clock.install()
    page.goto(p["base"] + f"/app/conversations/{p['cid']}")
    page.evaluate("""() => { const real = DOMParser.prototype.parseFromString; let once = true;
        DOMParser.prototype.parseFromString = function () { if (once) { once = false; throw new Error('boom'); }
          return real.apply(this, arguments); }; }""")
    polls = []
    page.on("request", lambda r: polls.append(1) if "/app/live" in r.url else None)
    machine_send(p["base"], p["bob"]["token"], {"person": "alice@example.test"}, "first poll breaks", p["cid"])
    page.clock.run_for(5_500)
    page.wait_for_timeout(500)
    page.clock.run_for(5_500)
    page.wait_for_timeout(500)
    assert len(polls) >= 2
    page.wait_for_selector(".rc-msg:has-text('first poll breaks')", timeout=3000)
    context.close()


def test_a_failed_ack_keeps_the_message_unread(agent_thread, browser, session):
    """F8: a non-2xx ack leaves the message unread and observed, and a later ack succeeds."""
    from raincli_server.models import Message

    p = agent_thread
    reply = machine_send(p["base"], p["bob"]["token"], {"person": "alice@example.test"}, "ack me", p["cid"])
    failing = {"on": True}
    context, page = open_app(browser, p["base"], p["alice"]["person_session"], setup=lambda ctx: ctx.route(
        "**/app/ack", lambda route: route.fulfill(status=403, body="") if failing["on"] else route.continue_()))
    page.goto(p["base"] + f"/app/conversations/{p['cid']}#latest")
    page.wait_for_selector(f"#m-{reply['id']}")
    page.wait_for_timeout(1500)
    assert page.locator(f"#m-{reply['id']}").get_attribute("data-unread") is not None
    failing["on"] = False
    page.evaluate("() => window.dispatchEvent(new Event('focus'))")
    page.wait_for_function(f"() => !document.querySelector('#m-{reply['id']}').hasAttribute('data-unread')")
    session.expire_all()
    assert session.get(Message, uuid.UUID(reply["id"])).person_seen_at is not None
    context.close()
