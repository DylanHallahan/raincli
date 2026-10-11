"""v0.5.4 screenshot set: the brief's fictional data, seeded on a real server, captured in Chromium.

Not collected by default (no ``test_`` prefix). Run explicitly::

    RAINCLI_SHOTS_PHASE=before|after RAINCLI_SHOTS_DIR=<dir> pytest tests/gui/shots_v054.py

Seed (fictional, matching the demo): Maya is the viewer (machine ``maya-laptop``, agent ``laptop-agent``);
Jon runs ``api-agent`` on ``jon-laptop``; Priya runs ``web-agent`` on ``priya-desktop``; Sam's ``ops-agent``
on ``sam-server`` is offline; ``old-laptop`` has never connected; Ryan's ``ryan-like`` holds a 2-day-old
``stored`` message. Files are ``<phase>-<n>-<screen>-<light|dark>-<WxH>.png``. The before set is shots 1, 3,
5, 6 and 7; the after set adds 2, 4, 8 and 9 (the demo's final frame, ``RAINCLI_SHOTS_DEMO``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import threading
import time
import urllib.request
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

PASSWORD = "correct horse battery"
PHASE = os.environ.get("RAINCLI_SHOTS_PHASE", "after")
OUT = Path(os.environ.get("RAINCLI_SHOTS_DIR") or "v054-screenshots")
DEMO = os.environ.get("RAINCLI_SHOTS_DEMO", "")
BIG, SMALL = (1180, 780), (760, 520)
APP_TOKEN = "shots-install-token-" + "x" * 28
INSTALL_HASH = hashlib.sha256(APP_TOKEN.encode()).hexdigest()
CLIENT = {"version": "0.5.4", "update_mode": "automatic", "update_state": "current"}
REPORT = (b"# Checkout audit\n\nTwo failures, both from the `total` -> `total_cents` rename.\n\n"
          b"| Step | Result |\n|---|---|\n| Cart | ok |\n| Pay | **fails**: `total` is undefined |\n"
          b"| Receipt | **fails**: shows 0.00 |\n")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(settings, engine):
    import uvicorn

    from raincli_server.app import create_app

    port = _free_port()
    live_settings = replace(settings, public_url=f"http://127.0.0.1:{port}", max_pending=1000)
    app = create_app(live_settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    yield live_settings.public_url
    server.should_exit = True
    thread.join(10)
    app.state.engine.dispose()


def api(base, path, body=None, token=None, method=None):
    request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     method=method or ("POST" if body is not None else "GET"),
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def entry(name, key, status="idle"):
    return {"key": key, "name": name, "type": "claude", "status": status, "role": None,
            "reachability": "instant", "source": "herdr"}


def presence(base, token, agents, status="ready"):
    api(base, "/api/v1/presence", {"status": status, "agents": agents, "client": CLIENT}, token, method="PUT")


def event(base, token, mid, state, detail=None):
    api(base, f"/api/v1/messages/{mid}/events", {"state": state, **({"detail": detail} if detail else {})}, token)


def receive(base, token, mid):
    api(base, f"/api/v1/messages/{mid}/ack", {}, token)


@pytest.fixture
def seed(live, session):
    """The team and machines only; messages are added by ``converse`` after the empty-inbox shot."""
    from raincli_server import identity

    people = {n: identity.create_user(session, f"{n.lower()}@example.test", n, PASSWORD)
              for n in ("Maya", "Jon", "Priya", "Sam", "Ryan")}
    team = identity.create_team(session, "northwind", "Northwind", people["Maya"])
    for name in ("Jon", "Priya", "Sam", "Ryan"):
        identity.add_member(session, team, people[name])
    machines = {}
    for handle, owner in (("maya-laptop", "Maya"), ("jon-laptop", "Jon"), ("priya-desktop", "Priya"),
                          ("sam-server", "Sam"), ("old-laptop", "Sam"), ("ryan-like", "Ryan")):
        machines[handle] = identity.register_agent(session, team, people[owner], handle)
    session.commit()
    tokens = {h: t for h, (_, t) in machines.items()}
    presence(live, tokens["maya-laptop"], [entry("laptop-agent", "a" * 32)])
    presence(live, tokens["jon-laptop"], [entry("api-agent", "b" * 32)])
    presence(live, tokens["priya-desktop"], [entry("web-agent", "c" * 32, status="working")], status="busy")
    presence(live, tokens["sam-server"], [entry("ops-agent", "d" * 32)])
    presence(live, tokens["ryan-like"], [])  # connected, but it never picks up its messages
    go_offline(session, machines["sam-server"][0])  # Sam's ops-agent: known, but not reported for 30 minutes
    persons = {}
    for n in ("Maya", "Priya"):  # the app's sign-in adds a machine, which the app's runtime keeps connected
        login = api(live, "/api/v1/app/login", {"email": f"{n.lower()}@example.test", "password": PASSWORD,
                                                "machine_name": f"{n.lower()}-app", "person_session": True})
        presence(live, login["token"], [])
        persons[n] = login["person_session"]
    return {"base": live, "session": session, "tokens": tokens, "persons": persons,
            "machines": {h: a for h, (a, _) in machines.items()}, "people": people}


def backdate(session, mids, when):
    from sqlalchemy import update

    from raincli_server.models import DeliveryEvent, Message

    ids = [uuid.UUID(m) for m in mids]
    session.execute(update(Message).where(Message.id.in_(ids)).values(created_at=when, delivery_updated_at=when))
    session.execute(update(DeliveryEvent).where(DeliveryEvent.message_id.in_(ids)).values(created_at=when))
    session.commit()


def go_offline(session, agent):
    from raincli_server.models import AgentPresence

    row = session.get(AgentPresence, agent.id)
    row.seen_at -= timedelta(minutes=30)
    session.commit()
    session.expire_all()


def converse(seed):
    """Every conversation the shots need. Returns the ids the shots open."""
    base, tok, maya = seed["base"], seed["tokens"], seed["persons"]["Maya"]

    def send(to, body, token=maya, **extra):
        path = "/api/v1/person/send" if token in seed["persons"].values() else "/api/v1/messages"
        return api(base, path, {"id": str(uuid.uuid4()), "to": to, "body": body, **extra}, token)["message"]

    now = datetime.now(timezone.utc)
    ids = {}
    # 2 days ago: a message ryan-like has not picked up (stored).
    old = send({"machine": "ryan-like"}, "Can you check the staging certs before Friday?")
    backdate(seed["session"], [old["id"]], now - timedelta(days=2, hours=1))
    ids["ryan"] = old["conversation_id"]
    # Yesterday: Priya (a person) -> stored, received, replied.
    question = send({"person": "priya@example.test"}, "Is the retry cap still 3 on the web side?")
    answer = send({"person": "maya@example.test"}, "Yes, 3, with backoff. I'll write it in the README.",
                  token=seed["persons"]["Priya"], in_reply_to=question["id"])
    api(base, f"/api/v1/person/messages/{question['id']}/ack", {}, seed["persons"]["Priya"])
    backdate(seed["session"], [question["id"]], now - timedelta(days=1, hours=2))
    backdate(seed["session"], [answer["id"]], now - timedelta(days=1, hours=1, minutes=40))
    ids["priya"] = question["conversation_id"]
    # Today: Priya's web-agent held one message for approval and rejected another.
    held = send({"machine": "priya-desktop", "agent": "web-agent"}, "Please bump the retry cap to 5 in prod.")
    receive(base, tok["priya-desktop"], held["id"])
    event(base, tok["priya-desktop"], held["id"], "held", "waiting for approval")
    rejected = send({"machine": "priya-desktop", "agent": "web-agent"}, "Delete the old checkout feature flag.")
    receive(base, tok["priya-desktop"], rejected["id"])
    event(base, tok["priya-desktop"], rejected["id"], "rejected", "declined by the user")
    ids["held"] = held["conversation_id"]
    # Today: Jon's api-agent, the demo conversation, first at submitted.
    ask = send({"machine": "jon-laptop", "agent": "api-agent"},
               "Can you run the checkout flow against the new orders API and tell me what breaks?")
    receive(base, tok["jon-laptop"], ask["id"])
    event(base, tok["jon-laptop"], ask["id"], "submitted")
    ids["jon"], ids["ask"] = ask["conversation_id"], ask["id"]
    return ids


def jon_replies(seed, ids):
    report = {"filename": "checkout-audit.md", "content_b64": base64.b64encode(REPORT).decode(),
              "sha256": hashlib.sha256(REPORT).hexdigest()}
    api(seed["base"], "/api/v1/messages",
        {"id": str(uuid.uuid4()), "to": {"person": "maya@example.test"}, "from_agent": "api-agent",
         "body": "Two failures, both from the total → total_cents rename. Details in the report.",
         "in_reply_to": ids["ask"], "attachments": [report]}, seed["tokens"]["jon-laptop"])


class Camera:
    def __init__(self, browser, base, person):
        self.browser, self.base, self.person = browser, base, person
        probe = browser.new_page()
        self.agent = probe.evaluate("navigator.userAgent") + " RainCLIApp/" + APP_TOKEN
        probe.close()
        self.saved = []

    def open(self, scheme, size, path):
        context = self.browser.new_context(color_scheme=scheme, user_agent=self.agent,
                                           viewport={"width": size[0], "height": size[1]})
        page = context.new_page()
        page.goto(api(self.base, "/api/v1/app/handoff", {"app_install_hash": INSTALL_HASH}, self.person)["url"])
        if path != "/app/inbox":
            page.goto(self.base + path)
        page.wait_for_load_state("networkidle")
        return context, page

    def shoot(self, n, screen, path, sizes=(BIG,), act=None):
        for scheme in ("light", "dark"):
            for size in sizes:
                context, page = self.open(scheme, size, path)
                if act:
                    act(page)
                name = f"{PHASE}-{n}-{screen}-{scheme}-{size[0]}x{size[1]}.png"
                page.screenshot(path=str(OUT / name))
                self.saved.append(name)
                context.close()


def test_capture_the_v054_set(seed, browser_pw):
    OUT.mkdir(parents=True, exist_ok=True)
    after = PHASE == "after"
    cam = Camera(browser_pw, seed["base"], seed["persons"]["Maya"])
    cam.shoot(6, "inbox-empty", "/app/inbox")
    ids = converse(seed)
    jon = f"/app/conversations/{ids['jon']}"
    cam.shoot(1, "conversation-submitted", jon, sizes=(BIG, SMALL))
    jon_replies(seed, ids)
    cam.shoot(1, "conversation-replied", jon, sizes=(BIG, SMALL))
    cam.shoot(3, "held-rejected", f"/app/conversations/{ids['held']}")
    cam.shoot(5, "inbox", "/app/inbox", sizes=(BIG, SMALL))
    for handle in ("old-laptop", "ryan-like"):
        cam.shoot(7, f"compose-{handle}", f"/app/compose?to={handle}",
                  act=lambda page: page.locator("#body").fill("Quick check, are you around?"))
    if after:
        cam.shoot(4, "person-trail", f"/app/conversations/{ids['priya']}")
        cam.shoot(8, "composer-empty", jon)
        cam.shoot(8, "composer-grown", jon, act=lambda page: page.locator("#body").fill(
            "\n".join(f"Line {i}: one more detail for the report." for i in range(1, 7))))
        go_offline(seed["session"], seed["machines"]["jon-laptop"])
        cam.shoot(2, "conversation-offline", jon)
        if DEMO:
            for scheme in ("light", "dark"):
                context = browser_pw.new_context(color_scheme=scheme, reduced_motion="reduce",
                                                 viewport={"width": BIG[0], "height": BIG[1]})
                page = context.new_page()
                page.goto(Path(DEMO).resolve().as_uri())
                name = f"{PHASE}-9-demo-final-{scheme}-{BIG[0]}x{BIG[1]}.png"
                page.screenshot(path=str(OUT / name), full_page=True)
                cam.saved.append(name)
                context.close()
    (OUT / f"{PHASE}-files.txt").write_text("\n".join(cam.saved) + "\n", "utf-8")
    assert cam.saved


@pytest.fixture
def browser_pw():
    with playwright_api.sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()
