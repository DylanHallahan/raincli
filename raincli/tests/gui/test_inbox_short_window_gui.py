"""The inbox bug (v0.5.4 hotfix): in a window shorter than the app's minimum (150% scaling on a small screen,
maximized), the conversation list kept at least three rows and the owner's notice stays one line.

Same harness as test_v054_list_gui.py. Skipped without Playwright or its browser.
"""

from __future__ import annotations

from .test_v054_list_gui import (api, browser, live, open_app, person_send, size, team)  # noqa: F401

ROWS = """() => { const l = document.querySelector('.rc-convs'), lr = l.getBoundingClientRect();
    return [...l.querySelectorAll('.rc-conv-item')].filter(i => { const r = i.getBoundingClientRect();
        return r.top >= lr.top - 0.5 && r.bottom <= lr.bottom + 0.5 && r.height > 0; }).length; }"""
SELECT_VISIBLE = """() => { const h = [...document.querySelectorAll('h2')].find(e => e.textContent.includes('Select a conversation'));
    const r = h.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }"""


def five_conversations(team):
    """§21: one conversation per person, so five teammates (two more than the team fixture's)."""
    from raincli_server import identity
    from raincli_server.models import Team

    s = team["session"]
    northwind = s.query(Team).filter_by(slug="northwind").one()
    for name in ("Lee", "Ana"):
        identity.add_member(s, northwind, identity.create_user(s, f"{name.lower()}@example.test", name, "correct horse battery"))
    s.commit()
    for email in ("jon", "priya", "sam", "lee", "ana"):
        person_send(team, team["maya"], {"person": f"{email}@example.test"}, "hello")


def stale_machines(team, count):
    from sqlalchemy import select, update

    from raincli_server import identity
    from raincli_server.models import Agent, AgentCredential, Team

    s = team["session"]
    northwind = s.scalar(select(Team).where(Team.slug == "northwind"))
    app_machine = s.scalar(select(Agent.id).where(Agent.handle == "maya-app"))  # the app's own: connected, as when running
    s.execute(update(AgentCredential).where(AgentCredential.agent_id == app_machine).values(last_used_at=identity.now()))
    for i in range(count):
        identity.register_agent(s, northwind, team["users"]["Maya"], f"maya-old-pc-{i}")  # never connected
    s.commit()


def test_a_short_window_keeps_three_conversation_rows_and_the_footer(team, browser):
    five_conversations(team)
    for w, h in ((910, 440), (760, 400)):
        for scheme in ("light", "dark"):
            context, page = open_app(browser, team, team["maya"], color_scheme=scheme, device_scale_factor=1.5, **size(w, h))
            assert page.evaluate(ROWS) >= 3, (w, h, scheme)
            footer = page.locator(".rc-list .rc-whoami").evaluate("e => e.getBoundingClientRect().bottom")
            assert footer <= h + 0.5, (w, h, footer)  # the footer stays on screen
            context.close()


def test_several_stale_machines_are_one_notice_and_the_list_keeps_three_rows(team, browser):
    five_conversations(team)
    stale_machines(team, 8)
    for scheme in ("light", "dark"):
        context, page = open_app(browser, team, team["maya"], color_scheme=scheme, device_scale_factor=1.5, **size(787, 520))
        # v0.5.5 (§20.6 M1): Inbox opens the latest conversation, and the notice sits at the top of the list.
        notices = page.locator(".rc-list .owner-warnings .sender-warning")
        assert notices.count() == 1
        text = notices.inner_text()
        assert text.startswith("8 of your machines aren't connecting: maya-old-pc-0") and "See This computer" in text
        assert notices.locator("a").get_attribute("href").endswith("/app/local/this-computer")
        assert page.evaluate(ROWS) >= 3
        context.close()


def test_one_stale_machine_keeps_todays_wording(team, browser):
    stale_machines(team, 1)
    five_conversations(team)
    context, page = open_app(browser, team, team["maya"], **size(1180, 780))
    notices = page.locator(".rc-list .owner-warnings .sender-warning")
    assert notices.count() == 1 and notices.inner_text() == "maya-old-pc-0 has never connected."
    context.close()
