"""v0.5.5 screenshot set: the v0.5.4 seed, with Maya's app on maya-laptop (first-run step 1 done), in Chromium.

Not collected by default (no ``test_`` prefix). Run explicitly::

    RAINCLI_SHOTS_PHASE=after RAINCLI_SHOTS_DIR=<dir> pytest tests/gui/shots_v055.py

Files are ``<phase>-<n>-<screen>-<light|dark>-<WxH>.png``. This script makes the trimmed scope's shots: 5 (the trail
opened), 6 (focus: Tab and click; the Chromium counterpart of the Windows capture), 7 (header heights), 9 (the empty
state with step 1 done), 10 (Inbox opened on the latest conversation) and 12 (the composer grown). Shot 4 (send in
place) is ``shots_v055_r2.py``. Frozen and not captured: 1-3 (the picker, R1), 8 (P1) and 11 (R7). The P1 seed
(``deploy_bot``) and the second team (``contoso``) stay below, unused, for when those return.
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path

import pytest

from .shots_v054 import (BIG, PASSWORD, PHASE, SMALL, Camera, api, browser_pw, converse, entry, go_offline,  # noqa: F401
                         jon_replies, live, presence, settle)

pytest.importorskip("playwright.sync_api")
OUT = Path(__import__("os").environ.get("RAINCLI_SHOTS_DIR") or "v055-screenshots")


@pytest.fixture
def seed(live, session):
    """The v0.5.4 seed, except that Maya's app signs in on maya-laptop: the app's own machine is this computer, as
    in real use, so first-run step 1 says "Connected: maya-laptop"."""
    from raincli_server import identity

    people = {n: identity.create_user(session, f"{n.lower()}@example.test", n, PASSWORD)
              for n in ("Maya", "Jon", "Priya", "Sam", "Ryan")}
    team = identity.create_team(session, "northwind", "Northwind", people["Maya"])
    for name in ("Jon", "Priya", "Sam", "Ryan"):
        identity.add_member(session, team, people[name])
    machines = {}
    for handle, owner in (("jon-laptop", "Jon"), ("priya-desktop", "Priya"), ("sam-server", "Sam"),
                          ("old-laptop", "Sam"), ("ryan-like", "Ryan")):
        machines[handle] = identity.register_agent(session, team, people[owner], handle)
    session.commit()
    tokens = {h: t for h, (_, t) in machines.items()}
    persons = {}
    for n, machine in (("Maya", "maya-laptop"), ("Priya", "priya-app")):
        login = api(live, "/api/v1/app/login", {"email": f"{n.lower()}@example.test", "password": PASSWORD,
                                                "machine_name": machine, "person_session": True})
        tokens[login["handle"]] = login["token"]
        persons[n] = login["person_session"]
    from raincli_server.models import Agent
    from sqlalchemy import select

    for handle in ("maya-laptop", "priya-app"):
        machines[handle] = (session.scalar(select(Agent).where(Agent.handle == handle)), tokens[handle])
    for handle, agents in (("maya-laptop", [entry("laptop-agent", "a" * 32)]), ("priya-app", [])):
        # The apps run 0.5.5 and report so, as the running app does.
        api(live, "/api/v1/presence", {"status": "ready", "agents": agents,
                                       "client": {"version": "0.5.5", "update_mode": "automatic",
                                                  "update_state": "current"}}, tokens[handle], method="PUT")
    presence(live, tokens["jon-laptop"], [entry("api-agent", "b" * 32)])
    presence(live, tokens["priya-desktop"], [entry("web-agent", "c" * 32, status="working")], status="busy")
    presence(live, tokens["sam-server"], [entry("ops-agent", "d" * 32)])
    presence(live, tokens["ryan-like"], [])  # connected, but it never picks up its messages
    go_offline(session, machines["sam-server"][0])
    return {"base": live, "session": session, "tokens": tokens, "persons": persons,
            "machines": {h: a for h, (a, _) in machines.items()}, "people": people}


def contoso(seed):
    """Team Contoso, where Lee is Maya's teammate (a second team: labels in the picker), with one conversation."""
    from raincli_server import identity

    s = seed["session"]
    lee = identity.create_user(s, "lee@example.test", "Lee", "correct horse battery")
    team = identity.create_team(s, "contoso", "Contoso", lee)
    identity.add_member(s, team, seed["people"]["Maya"])
    s.commit()
    api(seed["base"], "/api/v1/person/send", {"id": str(uuid.uuid4()), "to": {"person": "lee@example.test"},
                                              "team": "contoso", "body": "Are the Contoso invoices ready?"},
        seed["persons"]["Maya"])


def deploy_bot(seed, ids):
    """P1: Jon's machine states another agent, deploy-bot. A stated from_agent is the sender endpoint (§16.1,
    §16.6), so the message opens its own conversation, "Jon · deploy-bot", not the api-agent one (the server
    refuses a conversation_id that doesn't match). Returns that conversation's id."""
    return api(seed["base"], "/api/v1/messages",
               {"id": str(uuid.uuid4()), "to": {"person": "maya@example.test"}, "from_agent": "deploy-bot",
                "body": "Deployed the fix to staging.", "in_reply_to": ids["ask"]}, seed["tokens"]["jon-laptop"]
               )["message"]["conversation_id"]


def tab_into(page, selector, limit=80):
    """Press Tab until keyboard focus is inside ``selector`` (a real keyboard focus, so :focus-visible applies)."""
    for _ in range(limit):
        page.keyboard.press("Tab")
        if page.evaluate(f"() => !!document.activeElement.closest({selector!r})"):
            return
    raise AssertionError(f"Tab never reached {selector}")


def stack(browser, paths, out):
    """The header crops one under another in a single image (shot 7)."""
    page = browser.new_page(viewport={"width": 1000, "height": 400})
    imgs = "".join(f'<img src="data:image/png;base64,{base64.b64encode(Path(p).read_bytes()).decode()}">' for p in paths)
    page.set_content(f"<body style='margin:0'><div id='stack' style='background:#888;display:inline-flex;"
                     f"flex-direction:column;gap:6px;padding:6px'>{imgs}</div></body>")
    page.locator("#stack").screenshot(path=str(out))
    page.close()


def test_capture_the_v055_set(seed, browser_pw):
    OUT.mkdir(parents=True, exist_ok=True)
    cam = Camera(browser_pw, seed["base"], seed["persons"]["Maya"])
    cam.agent += " RainCLIAppVersion/0.5.5"  # §20.7: a 0.5.5 window, so Inbox opens the latest conversation
    saved = []

    def shoot(n, screen, path, sizes=(BIG,), act=None, crop=None):
        files = []
        for scheme in ("light", "dark"):
            for size in sizes:
                context, page = cam.open(scheme, size, path)
                page.wait_for_load_state("networkidle")
                if act:
                    act(page)
                settle(page)
                name = f"{PHASE}-{n}-{screen}-{scheme}-{size[0]}x{size[1]}.png"
                (page.locator(crop) if crop else page).screenshot(path=str(OUT / name))
                saved.append(name)
                files.append(OUT / name)
                context.close()
        return files

    shoot(9, "empty-step1-done", "/app/inbox")
    ids = converse(seed)
    jon_replies(seed, ids)
    jon = f"/app/conversations/{ids['jon']}"
    shoot(10, "opened-latest", "/app/inbox")  # Inbox opens the latest conversation (§20.6 M1)
    shoot(5, "trail-open", jon, act=lambda page: page.locator(".rc-msg.rc-out .rc-trail-box > summary").first.click())
    shoot(6, "focus-tab", jon, act=lambda page: tab_into(page, ".rc-msg"))
    shoot(6, "focus-click", jon, act=lambda page: page.locator(".rc-msg .rc-msg-content").first.click())
    for scheme in ("light", "dark"):
        crops = []
        for key in ("jon", "priya", "ryan"):
            context, page = cam.open(scheme, BIG, f"/app/conversations/{ids[key]}")
            settle(page)
            path = OUT / f"_crop-{key}-{scheme}.png"
            page.locator(".rc-main-head").screenshot(path=str(path))
            crops.append(path)
            context.close()
        name = f"{PHASE}-7-header-heights-{scheme}-{BIG[0]}x{BIG[1]}.png"
        stack(browser_pw, crops, OUT / name)
        for path in crops:
            path.unlink()
        saved.append(name)
    shoot(12, "composer-grown", jon, act=lambda page: page.locator("#body").fill(
        "\n".join(f"Line {i}: one more detail for the report." for i in range(1, 7))))
    (OUT / f"{PHASE}-files.txt").write_text("\n".join(saved) + "\n", "utf-8")
    assert saved
