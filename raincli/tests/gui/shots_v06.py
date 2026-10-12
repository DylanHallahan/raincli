"""v0.6 screenshots 1-5 and 7-13 (brief "RainCLI v0.6 person routing", Screenshots), captured in Chromium.

Not collected by default (no ``test_`` prefix). Run explicitly, with a checkout of main (v0.5.6) for shot 8's before::

    RAINCLI_SHOTS_DIR=<dir> RAINCLI_SHOTS_MAIN=<checkout of origin/main>/raincli pytest tests/gui/shots_v06.py

One scratch database, migrated for real:
1. Migration 0009 and main's (v0.5.6) code seed the pre-migration data the brief names: Maya's conversation with
   Jon's api-agent, one with Jon's machine inbox, and one between Maya's own two machines. Main's own server, in a
   subprocess, shows that list (shot 8, before).
2. This branch's migration 0010 runs on it; this branch's server shows the merged list (shot 8, after) and the
   archived "Your agents" (shot 9).
3. The brief's seed is completed with this branch's API: Jon's inbox is api-agent (instant, ready), Priya's
   web-agent (next turn, busy), Ryan's ryan-agent (not picking up for 2 days), Sam has none, Lee is in Contoso, and
   Maya has three messages waiting with no inbox (shots 5, 7), picks laptop-agent, and the rest follow.
Shots 6 and 14 are the Windows window and the CLI (client v0.6.0, after the server deploy).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

OUT = Path(os.environ.get("RAINCLI_SHOTS_DIR") or "v06-screenshots")
MAIN = os.environ.get("RAINCLI_SHOTS_MAIN", "")
PASSWORD = "correct horse battery"
APP_TOKEN = "shots-v06-install-token-" + "x" * 24
INSTALL_HASH = hashlib.sha256(APP_TOKEN.encode()).hexdigest()
BIG, SMALL = (1180, 780), (760, 520)
CLIENT = {"version": "0.5.6", "update_mode": "automatic", "update_state": "current"}
REPORT = (b"# Checkout audit\n\nTwo failures, both from the `total` -> `total_cents` rename.\n\n"
          b"| Step | Result |\n|---|---|\n| Cart | ok |\n| Pay | **fails** |\n| Receipt | **fails** |\n")

# Main's (v0.5.6) seed: run in a subprocess with main's code first on the path. Prints the tokens as JSON.
MAIN_SEED = r'''
import json, sys, uuid
from sqlalchemy.orm import Session
from raincli_server.db import make_engine
from raincli_server import identity, messaging, presence
url = sys.stdin.read().strip()  # never in argv
s = Session(make_engine(url))
P = "correct horse battery"
u = {n: identity.create_user(s, f"{n.lower()}@example.test", n, P) for n in ("Maya", "Jon", "Priya", "Sam", "Ryan", "Lee")}
nw = identity.create_team(s, "northwind", "Northwind", u["Maya"])
for n in ("Jon", "Priya", "Sam", "Ryan"):
    identity.add_member(s, nw, u[n])
ct = identity.create_team(s, "contoso", "Contoso", u["Lee"])
identity.add_member(s, ct, u["Maya"])
machines = {}
for handle, owner, team in (("maya-laptop", "Maya", nw), ("maya-desktop", "Maya", nw), ("jon-laptop", "Jon", nw),
                            ("priya-desktop", "Priya", nw), ("sam-pc", "Sam", nw), ("ryan-like", "Ryan", nw),
                            ("lee-desk", "Lee", ct)):
    machines[handle] = identity.register_agent(s, team, u[owner], handle)
s.commit()
agent = lambda name, reach="instant", status="idle", role=None: {"key": uuid.uuid4().hex, "name": name, "type": "claude",
    "status": status, "role": role, "reachability": reach, "source": "herdr"}
client = {"version": "0.5.6", "update_mode": "automatic", "update_state": "current"}
presence.publish(s, machines["jon-laptop"][0], {"status": "ready", "agents": [agent("api-agent"), agent("inbox", role="inbox")], "client": client})
s.commit()
messaging.send_as_person(s, u["Maya"], nw.id, id=str(uuid.uuid4()), to={"machine": "jon-laptop", "agent": "api-agent"},
                         body="Can you run the checkout flow against the new orders API?", max_pending=1000)
messaging.send_as_person(s, u["Maya"], nw.id, id=str(uuid.uuid4()), to={"machine": "jon-laptop"},
                         body="Is the staging deploy still on for Friday?", max_pending=1000)
messaging.send_message(s, machines["maya-laptop"][0], id=str(uuid.uuid4()), to={"machine": "maya-desktop"},
                       body="Sync the notes from this morning.", max_pending=1000)
s.commit()
person = identity.issue_person_session(s, u["Maya"], machines["maya-laptop"][0])
s.commit()
print(json.dumps({"maya_person": person, "tokens": {h: t for h, (_, t) in machines.items()}}))
'''


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def api(base, path, body=None, token=None, method=None):
    request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     method=method or ("POST" if body is not None else "GET"),
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise AssertionError(f"{method or 'POST/GET'} {path}: {exc.code} {exc.read()[:400]!r}") from None


def _wait_up(base, process=None):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError("the server exited")
        try:
            urllib.request.urlopen(base + "/api/v1/health", timeout=2).read()
            return
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.2)
    raise RuntimeError("the server did not start")


def _main_server(url, code_root, port):
    env = {**os.environ, "PYTHONPATH": code_root, "RAINCLI_DATABASE_URL": url,
           "RAINCLI_SECRET_KEY": "shots-secret-key-" + "x" * 40, "RAINCLI_PUBLIC_URL": f"http://127.0.0.1:{port}",
           "RAINCLI_COOKIE_SECURE": "0", "RAINCLI_RATE_LIMIT_PER_MIN": "10000"}
    return subprocess.Popen([sys.executable, "-m", "uvicorn", "raincli_server.app:app_from_env", "--factory",
                             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"], env=env, cwd=code_root)


class Camera:
    def __init__(self, browser, base, person):
        self.browser, self.base, self.person = browser, base, person
        probe = browser.new_page()
        self.agent = probe.evaluate("navigator.userAgent") + " RainCLIApp/" + APP_TOKEN + " RainCLIAppVersion/0.6.0"
        probe.close()

    def app(self, scheme, size, path):
        context = self.browser.new_context(color_scheme=scheme, user_agent=self.agent, reduced_motion="reduce",
                                           viewport={"width": size[0], "height": size[1]})
        page = context.new_page()
        page.goto(api(self.base, "/api/v1/app/handoff", {"app_install_hash": INSTALL_HASH}, self.person)["url"])
        page.wait_for_load_state("networkidle")
        if path:
            page.goto(self.base + path)
            page.wait_for_load_state("networkidle")
        return context, page

    def website(self, scheme, size, email, path):
        context = self.browser.new_context(color_scheme=scheme, reduced_motion="reduce",
                                           viewport={"width": size[0], "height": size[1]})
        page = context.new_page()
        page.goto(self.base + "/login")
        page.fill("#email", email)
        page.fill("#password", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url("**/app**")
        page.goto(self.base + path)
        page.wait_for_load_state("networkidle")
        return context, page


def settle(page):
    page.wait_for_timeout(250)


@pytest.fixture
def scratch_url():
    from sqlalchemy import create_engine, text

    admin_url = os.environ["RAINCLI_TEST_DATABASE_URL"]
    psycopg = "postgresql+psycopg://" + admin_url.split("://", 1)[1]
    name = f"raincli_shots_v06_{uuid.uuid4().hex[:10]}"
    server = create_engine(psycopg, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    yield psycopg.rsplit("/", 1)[0] + "/" + name
    with server.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    server.dispose()


@pytest.fixture
def browser_pw():
    with playwright_api.sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


def test_capture_the_v06_set(scratch_url, browser_pw):
    assert MAIN and Path(MAIN, "raincli_server").is_dir(), "RAINCLI_SHOTS_MAIN must point at main's raincli/ dir"
    OUT.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []

    def shoot(page, name, locator=None, full_page=False):
        settle(page)
        if locator:
            page.locator(locator).screenshot(path=str(OUT / name))
        else:
            page.screenshot(path=str(OUT / name), full_page=full_page)
        saved.append(name)

    # 1. Before: migration 0009 and main's code; main's own server.
    env_main = {**os.environ, "PYTHONPATH": MAIN}

    def run_main(code):
        """Main's code in a subprocess; the database URL (with its password) goes on stdin, never in argv."""
        done = subprocess.run([sys.executable, "-c", code], env=env_main, input=scratch_url, capture_output=True,
                              text=True, cwd=MAIN)  # main's checkout first on sys.path (its cwd), not this one
        if done.returncode:
            raise AssertionError("main's code failed:\n" + done.stderr[-3000:].replace(scratch_url, "<url>"))
        return done.stdout
    assert MAIN in run_main("import raincli_server; print(raincli_server.__file__)")  # really main's code
    run_main("import sys; from raincli_server.migrate import upgrade; upgrade(sys.stdin.read().strip())")
    seeded = json.loads(run_main(MAIN_SEED).strip().splitlines()[-1])
    port = _port()
    process = _main_server(scratch_url, MAIN, port)
    try:
        base = f"http://127.0.0.1:{port}"
        _wait_up(base, process)
        cam = Camera(browser_pw, base, seeded["maya_person"])
        for scheme in ("light", "dark"):
            context, page = cam.app(scheme, BIG, "/app/inbox")
            shoot(page, f"before-8-conversation-list-{scheme}-1180x780.png", ".rc-list")
            context.close()
    finally:
        process.terminate()
        process.wait(20)

    # 2. This branch's migration 0010 on the same data, then this branch's server (in this process).
    from dataclasses import replace

    import uvicorn

    from raincli_server.app import create_app
    from raincli_server.config import load_settings
    from raincli_server.migrate import upgrade

    upgrade(scratch_url)
    port = _port()
    settings = replace(load_settings({"RAINCLI_DATABASE_URL": scratch_url, "RAINCLI_SECRET_KEY": "s" * 48,
                                      "RAINCLI_PUBLIC_URL": f"http://127.0.0.1:{port}", "RAINCLI_COOKIE_SECURE": "0",
                                      "RAINCLI_RATE_LIMIT_PER_MIN": "10000"}))
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_up(base)
        tokens = seeded["tokens"]
        cam = Camera(browser_pw, base, seeded["maya_person"])
        for scheme in ("light", "dark"):
            context, page = cam.app(scheme, BIG, "/app/inbox")
            shoot(page, f"after-8-conversation-list-{scheme}-1180x780.png", ".rc-list")
            page.goto(base + "/app/archived")
            page.wait_for_load_state("networkidle")
            shoot(page, f"after-9-archived-your-agents-{scheme}-1180x780.png")
            context.close()

        # 3. The brief's seed, through this branch's API.
        def agent(name, reach="instant", status="idle", role=None):
            return {"key": hashlib.sha256(name.encode()).hexdigest()[:32], "name": name, "type": "claude",
                    "status": status, "role": role, "reachability": reach, "source": "herdr"}

        for handle, agents, status in (
                ("maya-laptop", [agent("laptop-agent", role="inbox"), agent("notes-agent", "next-turn")], "ready"),
                ("jon-laptop", [agent("api-agent")], "ready"),
                ("priya-desktop", [agent("web-agent", "next-turn", "working")], "busy"),
                ("ryan-like", [agent("ryan-agent")], "ready"), ("sam-pc", [], "ready")):
            api(base, "/api/v1/presence", {"status": status, "agents": agents, "client": CLIENT}, tokens[handle], "PUT")
            api(base, "/api/v1/inbox?routing=1&wait=0", None, tokens[handle])
        for handle, name in (("jon-laptop", "api-agent"), ("priya-desktop", "web-agent"), ("ryan-like", "ryan-agent")):
            api(base, "/api/v1/me/inbox", {"agent": name}, tokens[handle], "PUT")
        # Ryan: his inbox hasn't picked up a message for two days (his machine connects, and reports its version).
        to_ryan = api(base, "/api/v1/person/send", {"id": str(uuid.uuid4()), "to": {"person": "ryan@example.test"},
                                                    "team": "northwind",
                                                    "body": "Can you check the staging certs before Friday?"},
                      seeded["maya_person"])["message"]
        from sqlalchemy import create_engine, text
        engine = create_engine(scratch_url)
        with engine.begin() as conn:
            conn.execute(text("UPDATE messages SET created_at = now() - interval '2 days 1 hour' WHERE id = :i"),
                         {"i": to_ryan["id"]})
        api(base, "/api/v1/presence", {"status": "ready", "agents": [agent("ryan-agent")], "client": CLIENT},
            tokens["ryan-like"], "PUT")
        persons = {}
        for name in ("Jon", "Priya", "Sam"):
            login = api(base, "/api/v1/app/login", {"email": f"{name.lower()}@example.test", "password": PASSWORD,
                                                    "machine_name": f"{name.lower()}-app", "person_session": True})
            persons[name] = login["person_session"]
        for name, body in (("Jon", "I wrote this one myself: the README has the new endpoint."),
                           ("Priya", "The retry cap is still 3 on the web side."),
                           ("Sam", "Can you add me to the release channel?")):
            api(base, "/api/v1/person/send", {"id": str(uuid.uuid4()), "to": {"person": "maya@example.test"},
                                              "body": body}, persons[name])

        # Shots 5 and 7: Maya has no inbox and three messages waiting.
        for scheme in ("light", "dark"):
            context, page = cam.app(scheme, BIG, "/app/inbox")
            page.goto(base + "/app/inbox")  # the banner itself (not the latest conversation P2 would open)
            page.wait_for_load_state("networkidle")
            shoot(page, f"after-5-inbox-first-run-banner-{scheme}-1180x780.png")
            shoot(page, f"after-7-pick-with-waiting-messages-{scheme}-1180x780.png", "section.inbox-banner[aria-labelledby=inbox-banner-northwind]")
            context.close()
        context, page = cam.app("light", BIG, "/app/inbox")
        page.goto(base + "/app/inbox")
        page.click("section.inbox-banner[aria-labelledby=inbox-banner-northwind] button[type=submit]")  # laptop-agent is pre-selected: one more click
        page.wait_for_url("**notice=inbox-set**")
        context.close()

        # Shot 3: Maya's agent asks Jon; Jon's agent answers with a report; and Jon's own message from the app.
        ask = api(base, "/api/v1/messages", {"id": str(uuid.uuid4()), "to": {"person": "jon@example.test"},
                                             "from_agent": "laptop-agent",
                                             "body": "Can you run the checkout flow against the new `orders` API "
                                                     "and tell me what breaks?"}, tokens["maya-laptop"])["message"]
        api(base, f"/api/v1/messages/{ask['id']}/ack", {}, tokens["jon-laptop"])
        api(base, f"/api/v1/messages/{ask['id']}/events", {"state": "submitted"}, tokens["jon-laptop"])
        report = {"filename": "checkout-audit.md", "content_b64": base64.b64encode(REPORT).decode(),
                  "sha256": hashlib.sha256(REPORT).hexdigest()}
        api(base, "/api/v1/messages", {"id": str(uuid.uuid4()), "in_reply_to": ask["id"], "from_agent": "api-agent",
                                       "body": "Two failures, both from the `total` → `total_cents` rename. "
                                               "Details in the report.", "attachments": [report]}, tokens["jon-laptop"])
        jon_conv = ask["conversation_id"]
        # Shot 4: a message to Sam, who hasn't picked an inbox.
        to_sam = api(base, "/api/v1/person/send", {"id": str(uuid.uuid4()), "to": {"person": "sam@example.test"},
                                                   "team": "northwind", "body": "Here are the release notes for review."},
                     seeded["maya_person"])["message"]

        for scheme in ("light", "dark"):
            for size in (BIG, SMALL):
                tag = f"{scheme}-{size[0]}x{size[1]}"
                context, page = cam.app(scheme, size, "/app/people")
                shoot(page, f"after-1-people-{tag}.png")
                page.goto(base + f"/app/conversations/{jon_conv}")
                page.wait_for_load_state("networkidle")
                shoot(page, f"after-3-conversation-jon-{tag}.png")
                context.close()
            context, page = cam.app(scheme, BIG, "/app/compose")
            page.click("#to")
            page.wait_for_selector(".rc-picker-option")
            shoot(page, f"after-2-picker-open-{scheme}-1180x780.png")
            page.goto(base + f"/app/conversations/{to_sam['conversation_id']}")
            page.wait_for_load_state("networkidle")
            shoot(page, f"after-4-conversation-sam-{scheme}-1180x780.png")
            page.goto(base + "/app/compose?to=ryan@example.test")
            page.wait_for_selector("[data-recipient-warning] .sender-warning")
            shoot(page, f"after-12-compose-to-ryan-{scheme}-1180x780.png")
            context.close()
            context, page = cam.app(scheme, SMALL, "/app/inbox")
            shoot(page, f"after-13-sidebar-short-{scheme}-760x520.png")
            context.close()
            context, page = cam.website(scheme, BIG, "maya@example.test", "/app/agents")
            shoot(page, f"after-10-website-machines-team-owner-{scheme}-1180-full.png", full_page=True)
            context.close()
            context, page = cam.website(scheme, BIG, "jon@example.test", "/app/agents")
            shoot(page, f"after-11-website-machines-member-{scheme}-1180-full.png", full_page=True)
            context.close()
        engine.dispose()
    finally:
        server.should_exit = True
        thread.join(10)
        app.state.engine.dispose()
    (OUT / "files.txt").write_text("\n".join(saved) + "\n", "utf-8")
