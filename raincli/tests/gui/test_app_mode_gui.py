"""The website's app-mode UI in headless Chromium (protocol §16.9, §16.10; design §10 GUI coverage).

Test-only dependency: ``pip install -e '.[gui-test]' && python -m playwright install chromium``.
Skipped when Playwright or its browser is missing. Runs a real uvicorn server on the throwaway test
database and signs in through a real handoff, so the browser sends its own Sec-Fetch headers.
Screenshots (light and dark) go to ``RAINCLI_GUI_ARTIFACTS`` or the test's temporary directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import threading
import time
import urllib.request
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

PASSWORD = "correct horse battery"
XSS = [
    "[x](javascript:window.__xss=1)", "[x](JaVaScRiPt:window.__xss=1)", "[x](&#106;avascript:window.__xss=1)",
    "<img src=x onerror=window.__xss=1>", "<script>window.__xss=1</script>", "<svg onload=window.__xss=1>",
    "![x](javascript:window.__xss=1)", "<javascript:window.__xss=1>", "[x](data:text/html,<script>alert(1)</script>)",
    "[x](vbscript:msgbox(1))", "<a href=\"javascript:window.__xss=1\">x</a>",
]


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


@pytest.fixture
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except playwright_api.Error as exc:
            pytest.skip(f"headless Chromium is not installed: {exc}")
        yield b
        b.close()


@pytest.fixture
def artifacts(tmp_path) -> Path:
    path = Path(os.environ.get("RAINCLI_GUI_ARTIFACTS") or tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def api(base, path, body=None, token=None):
    request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     method="POST" if body is not None else "GET",
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def sign_in(base, email, machine):
    return api(base, "/api/v1/app/login", {"email": email, "password": PASSWORD, "machine_name": machine,
                                            "person_session": True})


# §16.14 S3: the app's webview appends its install token to the browser's own User-Agent.
APP_TOKEN = "gui-test-install-token-" + "x" * 24
INSTALL_HASH = hashlib.sha256(APP_TOKEN.encode()).hexdigest()


def app_user_agent(browser):
    probe = browser.new_page()
    try:
        return probe.evaluate("navigator.userAgent") + " RainCLIApp/" + APP_TOKEN
    finally:
        probe.close()


def open_app(browser, base, person, scheme="light"):
    """A fresh 'webview' profile, signed in through the handoff exactly as the app does."""
    context = browser.new_context(color_scheme=scheme, accept_downloads=True, user_agent=app_user_agent(browser))
    page = context.new_page()
    page.goto(api(base, "/api/v1/app/handoff", {"app_install_hash": INSTALL_HASH}, person)["url"])
    assert page.url.endswith("/app/inbox"), page.url
    return context, page


def person_send(base, person, to, body):
    return api(base, "/api/v1/person/send", {"id": str(uuid.uuid4()), "to": to, "body": body}, person)["message"]


@pytest.fixture
def people(live, world):
    alice = sign_in(live, "alice@example.test", "alice-laptop")
    bob = sign_in(live, "bob@example.test", "bob-laptop")
    return {"base": live, "alice": alice, "bob": bob, "world": world}


# Layout, light and dark -------------------------------------------------------------------------

def test_layout_rail_list_thread_compose_in_light_and_dark(people, browser, artifacts):
    base = people["base"]
    person_send(base, people["bob"]["person_session"], {"person": "alice@example.test"}, "Hello **Alice**")
    backgrounds = {}
    for scheme in ("light", "dark"):
        context, page = open_app(browser, base, people["alice"]["person_session"], scheme)
        rail = page.locator("nav.rc-nav a")
        assert [t.strip() for t in rail.all_inner_texts()] == ["Inbox", "Agents", "This computer", "Settings"]
        assert page.locator(".site-footer").count() == 0 and page.locator(".site-header").count() == 0
        page.click(".rc-conv")
        assert page.locator(".rc-thread .rc-msg .md strong").inner_text() == "Alice"
        assert page.locator(".rc-compose textarea#body").is_visible()
        backgrounds[scheme] = page.evaluate("getComputedStyle(document.body).backgroundColor")
        page.screenshot(path=str(artifacts / f"app-mode-thread-{scheme}.png"), full_page=True)
        page.locator("nav.rc-nav").screenshot(path=str(artifacts / f"app-mode-rail-logo-{scheme}.png"))
        assert page.locator("nav.rc-nav img.rc-logo").evaluate("img => img.complete && img.naturalWidth > 0")
        context.close()
    assert backgrounds["light"] != backgrounds["dark"]


# Send, reply and attachments --------------------------------------------------------------------

def test_send_reply_and_attachments(people, browser, tmp_path, artifacts):
    base = people["base"]
    report = tmp_path / "report.md"
    report.write_text("# Report\n\nAll green.\n", "utf-8")
    alice_ctx, alice = open_app(browser, base, people["alice"]["person_session"])
    alice.click("text=New")
    alice.screenshot(path=str(artifacts / "app-mode-compose-light.png"))
    alice.fill("#to", "@bob@example.test")
    alice.fill("#body", "Here is the *report*")
    alice.set_input_files("input[name=files]", str(report))
    alice.click("button:has-text('Send')")
    alice.wait_for_url(re.compile(r"/app/conversations/"))
    assert alice.locator(".rc-msg .md em").inner_text() == "report"
    with alice.expect_download() as download:
        alice.click(".rc-attachments a:has-text('report.md')")
    assert Path(download.value.path()).read_text("utf-8") == report.read_text("utf-8")

    bob_ctx, bob = open_app(browser, base, people["bob"]["person_session"])
    bob.click(".rc-conv")
    bob.click(".rc-msg a:has-text('Reply')")
    bob.fill("#body", "Thanks, looks good")
    bob.click("button:has-text('Send')")
    bob.wait_for_url(re.compile(r"notice=sent"))
    assert "Thanks, looks good" in bob.locator(".rc-thread").inner_text()

    alice.reload()
    assert "Thanks, looks good" in alice.locator(".rc-thread").inner_text()
    assert "replied" in alice.locator(".rc-msg").first.inner_text()
    alice.screenshot(path=str(artifacts / "app-mode-attachment-reply-light.png"))
    alice_ctx.close()
    bob_ctx.close()


# Markdown never executes ----------------------------------------------------------------------

def test_markdown_xss_corpus_does_not_execute(people, browser):
    base = people["base"]
    for body in XSS:
        person_send(base, people["bob"]["person_session"], {"person": "alice@example.test"}, body)
    context, page = open_app(browser, base, people["alice"]["person_session"])
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.click(".rc-conv")
    page.wait_for_selector(".rc-msg")
    assert page.locator(".rc-msg").count() == len(XSS)
    for link in page.locator(".rc-msg .md a").all():  # click every link that survived; none may run script
        href = link.get_attribute("href") or ""
        assert re.match(r"^(https?:|mailto:|/app/conversations/)", href), href
    assert page.evaluate("window.__xss") is None and not dialogs
    assert page.locator(".rc-msg .md script, .rc-msg .md img, .rc-msg .md svg, .rc-msg .md iframe").count() == 0
    context.close()


# The agents picker refuses listed and ambiguous agents --------------------------------------------

def test_agents_picker_and_listed_agents(people, browser, artifacts):
    base = people["base"]
    bob_token = people["bob"]["token"]
    request = urllib.request.Request(base + "/api/v1/presence", method="PUT", data=json.dumps({"status": "ready", "agents": [
        {"key": "a" * 32, "name": "reviewer", "type": "claude", "status": "idle", "role": None,
         "reachability": "instant", "source": "herdr"},
        {"key": "b" * 32, "name": "scanned", "type": "other", "status": "unknown", "role": None,
         "reachability": "listed", "source": "scan"},
        {"key": "c" * 32, "name": "twin", "type": "claude", "status": "idle", "role": None,
         "reachability": "listed", "source": "hook", "ambiguous": True}]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + bob_token})
    urllib.request.urlopen(request, timeout=15).read()
    context, page = open_app(browser, base, people["alice"]["person_session"])
    page.click("nav.rc-nav a:has-text('Agents')")
    page.screenshot(path=str(artifacts / "app-mode-agents-light.png"))
    card = page.locator("section.rc-card", has_text="bob-laptop")
    reviewer = card.locator("li", has_text="reviewer")
    assert reviewer.locator("a:has-text('Message')").count() == 1
    assert reviewer.locator(".badge.reach-instant").inner_text() == "Instant"  # the website's reach_badge
    for name, reason in (("scanned", "listed only"), ("twin", "ambiguous")):
        row = card.locator("li", has_text=name)
        assert row.locator("a").count() == 0 and f"can't receive messages ({reason})" in row.inner_text()
    reviewer.locator("a:has-text('Message')").click()
    assert page.input_value("#to") == "bob-laptop/reviewer"
    page.fill("#to", "bob-laptop/scanned")
    page.fill("#body", "hello")
    page.click("button:has-text('Send')")
    assert "can't receive messages" in page.locator(".rc-flash-error").inner_text()
    context.close()


# The sentinel and app-mode refusals ------------------------------------------------------------

def test_a_stolen_app_cookie_does_nothing_in_a_normal_browser(people, browser):
    """§16.14 S3: the raincli_app cookie only works with the app install's User-Agent token."""
    base = people["base"]
    context, page = open_app(browser, base, people["alice"]["person_session"])
    assert "Signed in as" in page.inner_text(".rc-whoami") and "(alice@example.test)" in page.inner_text(".rc-whoami")
    stolen = [c for c in context.cookies() if c["name"] == "raincli_app"]
    assert stolen
    thief = browser.new_context()
    thief.add_cookies(stolen)
    page2 = thief.new_page()
    page2.goto(base + "/app/inbox")
    assert page2.locator(".rc-nav").count() == 0 and "/login" in page2.url
    thief.close()
    context.close()


def test_local_sentinel_and_refused_pages(people, browser):
    base = people["base"]
    context, page = open_app(browser, base, people["alice"]["person_session"])
    page.click("nav.rc-nav a:has-text('This computer')")
    assert "Open the RainCLI app" in page.inner_text("main")
    response = page.goto(base + "/app/account")
    assert response.status == 403 and page.locator("a:has-text('Open on the website')").count() == 1
    context.close()
    plain = browser.new_page()
    plain.goto(base + "/app/local/settings")
    assert "Open the RainCLI app" in plain.inner_text("main") and plain.locator(".rc-nav").count() == 0
    plain.close()


# Accessibility basics ---------------------------------------------------------------------------

def _luminance(rgb):
    def channel(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(int(x)) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_labels_focus_order_and_token_contrast(people, browser):
    base = people["base"]
    for scheme in ("light", "dark"):
        context, page = open_app(browser, base, people["alice"]["person_session"], scheme)
        page.goto(base + "/app/compose")
        unlabeled = page.evaluate("""() => [...document.querySelectorAll('input:not([type=hidden]), textarea, select')]
            .filter(el => !(el.labels && el.labels.length) && !el.getAttribute('aria-label')).map(el => el.name)""")
        assert unlabeled == []
        page.keyboard.press("Tab")
        order = []
        for _ in range(4):
            order.append(page.evaluate("document.activeElement.textContent.trim()"))
            page.keyboard.press("Tab")
        assert order == ["Inbox", "Agents", "This computer", "Settings"]
        tokens = page.evaluate("""() => { const s = getComputedStyle(document.documentElement);
            const probe = (v) => { const el = document.createElement('span'); el.style.color = s.getPropertyValue(v);
              document.body.appendChild(el); const c = getComputedStyle(el).color; el.remove(); return c; };
            return Object.fromEntries(['--rc-bg','--rc-surface','--rc-text','--rc-text-2','--rc-muted','--rc-accent',
              '--rc-on-accent'].map(v => [v, probe(v)])); }""")
        rgb = {k: re.findall(r"\d+", v)[:3] for k, v in tokens.items()}
        for fg, bg in (("--rc-text", "--rc-bg"), ("--rc-text-2", "--rc-bg"), ("--rc-muted", "--rc-bg"),
                       ("--rc-muted", "--rc-surface"), ("--rc-on-accent", "--rc-accent"), ("--rc-accent", "--rc-bg")):
            assert _contrast(rgb[fg], rgb[bg]) >= 4.5, (scheme, fg, bg, _contrast(rgb[fg], rgb[bg]))
        context.close()
