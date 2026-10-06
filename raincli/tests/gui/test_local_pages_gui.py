"""The app's bundled local pages in headless Chromium, with a fake js_api (protocol §16.10, §16.12 C4).

The pages are served from a loopback HTTP server, as pywebview serves them, and the fake api records
every call. Screenshots (light and dark) go to ``RAINCLI_GUI_ARTIFACTS`` or the test's temporary directory.
"""

from __future__ import annotations

import functools
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from raincli_agent.app.window import LOCAL_DIR  # noqa: E402

PASSWORD = "local page secret"
STATUS = {"connection": "connected", "machine": "alice-laptop", "team": "Acme", "version": "0.5.0",
          "updates": "automatic", "paused": False, "routing": "all",
          "agents": [{"name": "reviewer", "type": "claude", "status": "idle", "reachability": "instant"},
                     {"name": "scanned", "type": "other", "status": "unknown", "reachability": "listed"}]}
SETTINGS = {"routing": "all", "trust_mode": "team", "trusted_senders": ["bob-laptop", "@carol@example.test"],
            "update_mode": "automatic"}

# A stand-in for pywebview's bridge: every call is recorded; replies come from window.__replies.
FAKE_API = """
(() => {
  window.__calls = [];
  window.__replies = %s;
  const api = new Proxy({}, {get: (_, name) => (...args) => {
    window.__calls.push([name, ...args]);
    const reply = window.__replies[name];
    return Promise.resolve(typeof reply === "function" ? reply(...args) : reply);
  }});
  window.pywebview = {api};
  if (%s) {
    window.__rcNonce = "nonce-1";
    document.addEventListener("DOMContentLoaded", () => {
      window.dispatchEvent(new Event("pywebviewready"));
      window.dispatchEvent(new Event("rc-nonce"));
    });
  }
})();
"""


@pytest.fixture(scope="module")
def local_origin():
    handler = functools.partial(_QuietHandler, directory=str(LOCAL_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"headless Chromium is not installed: {exc}")
        yield b
        b.close()


@pytest.fixture
def artifacts(tmp_path) -> Path:
    path = Path(os.environ.get("RAINCLI_GUI_ARTIFACTS") or tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def open_page(browser, origin, page_name, replies, *, scheme="light", nonce=True, query=""):
    context = browser.new_context(color_scheme=scheme, viewport={"width": 1180, "height": 780})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(FAKE_API % (json.dumps(replies), "true" if nonce else "false"))
    page.goto(f"{origin}{page_name}.html{query}")
    return context, page, errors


def calls(page):
    return page.evaluate("window.__calls")


def test_sign_in_sends_the_password_only_to_sign_in(browser, local_origin, artifacts):
    replies = {"sign_in_defaults": {"machine_name": "alice-laptop"},
               "sign_in": {"ok": False, "message": "Choose a team.", "code": "team_required",
                           "teams": [{"slug": "acme", "name": "Acme"}, {"slug": "beta", "name": "Beta"}]}}
    for scheme in ("light", "dark"):
        context, page, errors = open_page(browser, local_origin, "sign-in", replies, scheme=scheme)
        page.wait_for_function("document.getElementById('machine').value === 'alice-laptop'")
        page.screenshot(path=str(artifacts / f"local-sign-in-{scheme}.png"))
        context.close()
        assert not errors
    context, page, errors = open_page(browser, local_origin, "sign-in", replies)
    page.wait_for_function("document.getElementById('machine').value === 'alice-laptop'")
    page.fill("#email", "alice@example.test")
    page.fill("#password", PASSWORD)
    page.click("#submit")
    page.wait_for_selector("#team-field:not(.hidden)")
    assert page.input_value("#password") == ""  # cleared before the call
    sign_ins = [c for c in calls(page) if c[0] == "sign_in"]
    assert sign_ins == [["sign_in", "nonce-1", {"email": "alice@example.test", "machine_name": "alice-laptop",
                                                 "team": None, "replace": False, "again": False,
                                                 "new_machine": False}, PASSWORD]]
    assert all(PASSWORD not in json.dumps(c) for c in calls(page) if c[0] != "sign_in")
    assert page.locator("#team option").all_inner_texts() == ["Acme", "Beta"]
    assert "Choose a team." in page.inner_text("#message")
    context.close()
    assert not errors


def test_sign_in_offers_replace_on_name_in_use(browser, local_origin):
    replies = {"sign_in_defaults": {"machine_name": "alice-laptop"},
               "sign_in": {"ok": False, "message": "That name is in use.", "code": "name_in_use"}}
    context, page, _ = open_page(browser, local_origin, "sign-in", replies, query="?again=1")
    page.wait_for_function("document.getElementById('machine').value === 'alice-laptop'")
    assert "Sign in again" in page.inner_text("#intro")
    page.fill("#email", "alice@example.test")
    page.fill("#password", PASSWORD)
    page.click("#submit")
    page.wait_for_selector("#replace-field:not(.hidden)")
    assert "Replace machine alice-laptop" in page.inner_text("#replace-field")
    assert [c for c in calls(page) if c[0] == "sign_in"][0][2]["again"] is True
    context.close()


def test_no_call_is_made_without_a_nonce(browser, local_origin):
    context, page, _ = open_page(browser, local_origin, "this-computer", {"status": STATUS}, nonce=False)
    page.evaluate("window.dispatchEvent(new Event('pywebviewready'))")
    page.wait_for_timeout(300)
    assert calls(page) == [] and page.inner_text("#machine") == "…"
    context.close()


def test_this_computer_shows_status_and_actions(browser, local_origin, artifacts):
    replies = {"status": STATUS, "toggle_pause": True, "open_log": True,
               "sign_out": {"ok": False, "message": "Sign-out cancelled."}, "open": {"ok": True}}
    for scheme in ("light", "dark"):
        context, page, errors = open_page(browser, local_origin, "this-computer", replies, scheme=scheme)
        page.wait_for_function("document.getElementById('machine').textContent === 'alice-laptop'")
        page.screenshot(path=str(artifacts / f"local-this-computer-{scheme}.png"))
        context.close()
        assert not errors
    context, page, _ = open_page(browser, local_origin, "this-computer", replies)
    page.wait_for_function("document.getElementById('machine').textContent === 'alice-laptop'")
    assert page.inner_text("#team") == "Acme" and page.inner_text("#routing") == "all"
    assert "reviewer" in page.inner_text("#agents") and "listed" in page.inner_text("#agents")
    page.click("#pause")
    page.click("#open-log")
    page.click("#sign-out")
    page.wait_for_function("document.getElementById('message').textContent.length > 0")
    page.click("nav button:has-text('Inbox')")
    page.wait_for_function("window.__calls.some(c => c[0] === 'open')")
    names = [c[0] for c in calls(page)]
    assert {"toggle_pause", "open_log", "sign_out", "open"} <= set(names)
    assert all(c[1] == "nonce-1" for c in calls(page))
    assert ["open", "nonce-1", "inbox"] in calls(page)
    context.close()


def test_settings_saves_one_change_at_a_time(browser, local_origin, artifacts):
    replies = {"settings": SETTINGS, "status": STATUS, "save_settings": {"ok": True, "message": "Saved."}}
    for scheme in ("light", "dark"):
        context, page, errors = open_page(browser, local_origin, "settings", replies, scheme=scheme)
        page.wait_for_selector("#trusted li")
        page.screenshot(path=str(artifacts / f"local-settings-{scheme}.png"), full_page=True)
        context.close()
        assert not errors
    context, page, _ = open_page(browser, local_origin, "settings", replies)
    page.wait_for_selector("#trusted li")
    assert page.is_checked("#routing-all") and page.is_checked("#trust-team") and page.is_checked("#update-automatic")
    page.click("#routing-inbox-only")
    page.click("#trust-list")
    page.click("#update-manual")
    page.fill("#trust-add", "dave-desktop")
    page.click("#trust-add-button")
    page.locator("#trusted li", has_text="bob-laptop").locator("button").click()
    page.wait_for_function("window.__calls.filter(c => c[0] === 'save_settings').length === 5")
    saved = [c[2] for c in calls(page) if c[0] == "save_settings"]
    assert saved == [{"routing": "inbox-only"}, {"trust_mode": "list"}, {"update_mode": "manual"},
                     {"trust_add": "dave-desktop"}, {"trust_remove": "bob-laptop"}]
    context.close()


def test_offline_retry(browser, local_origin, artifacts):
    replies = {"retry": {"ok": False, "message": "Still can't reach the service."}}
    for scheme in ("light", "dark"):
        context, page, errors = open_page(browser, local_origin, "offline", replies, scheme=scheme)
        page.screenshot(path=str(artifacts / f"local-offline-{scheme}.png"))
        context.close()
        assert not errors
    context, page, _ = open_page(browser, local_origin, "offline", replies)
    page.click("#retry")
    page.wait_for_function("document.getElementById('message').textContent.startsWith('Still')")
    assert calls(page) == [["retry", "nonce-1"]]
    context.close()


def test_sign_in_offers_a_new_machine_and_asks_for_the_password_again(browser, local_origin, artifacts):
    """§16.17 item 4, §16.18 V7: the offer is the user's choice; the password is typed again."""
    sentence = "This computer's saved RainCLI setup belongs to a machine that was revoked."
    replies = {"sign_in_defaults": {"machine_name": "old-pc"},
               "sign_in": {"ok": False, "message": sentence, "code": "stale_credential", "reason": "invalid",
                           "offer_new_machine": True, "machine_name": "old-pc-new"}}
    context, page, errors = open_page(browser, local_origin, "sign-in", replies, query="?person=1")
    page.wait_for_function("document.getElementById('machine').value === 'old-pc'")
    assert not page.is_visible("#new-machine")
    page.fill("#email", "alice@example.test")
    page.fill("#password", "first-password")
    page.click("#submit")
    page.wait_for_selector("#new-machine", state="visible")
    assert page.inner_text("#message") == sentence and page.input_value("#machine") == "old-pc-new"
    shot = artifacts / "local-sign-in-new-machine-offer.png"
    page.screenshot(path=str(shot))
    page.click("#new-machine")
    assert page.inner_text("#submit") == "Set up as a new machine" and not page.is_visible("#new-machine")
    assert "password again" in page.inner_text("#message") and page.input_value("#password") == ""
    page.click("#submit")  # without the password: nothing is sent
    assert "Enter your password." in page.inner_text("#message")
    assert len([c for c in calls(page) if c[0] == "sign_in"]) == 1
    page.evaluate("window.__replies.sign_in = {ok: true, message: 'Signed in.'}")
    page.fill("#password", "second-password")
    page.click("#submit")
    page.wait_for_function("window.__calls.filter(c => c[0] === 'sign_in').length === 2")
    first, second = [c for c in calls(page) if c[0] == "sign_in"]
    assert first[2]["new_machine"] is False and first[3] == "first-password"
    assert second[2]["new_machine"] is True and second[3] == "second-password"
    assert second[2]["machine_name"] == "old-pc-new"
    context.close()
    assert not errors


def test_this_computer_connects_coding_agents_only_on_click(browser, local_origin, artifacts):
    """§16.19 item 3: each agent's state; Connect or Disconnect only when the user clicks."""
    approve = ("Connected. Approve the RainCLI hooks once in Codex (type /hooks in a Codex session), "
               "then start a new Codex session.")
    replies = {"status": STATUS,
               "hooks": [{"kind": "codex", "name": "Codex", "state": "not_connected"},
                         {"kind": "claude", "name": "Claude Code", "state": "not_installed_agent"}],
               "connect_hooks": {"ok": True, "message": approve, "state": "needs_approval"}}
    context, page, errors = open_page(browser, local_origin, "this-computer", replies)
    page.wait_for_selector("#hooks li[data-agent=codex] button")
    assert page.inner_text("#hooks li[data-agent=codex] button") == "Connect"
    assert page.locator("#hooks li[data-agent=claude] button").count() == 0
    assert "Not installed" in page.inner_text("#hooks li[data-agent=claude]")
    assert not [c for c in calls(page) if c[0] == "connect_hooks"]  # nothing happens by itself
    page.screenshot(path=str(artifacts / "local-this-computer-connect.png"), full_page=True)
    page.evaluate("window.__replies.hooks = [{kind: 'codex', name: 'Codex', state: 'needs_approval'},"
                  " {kind: 'claude', name: 'Claude Code', state: 'not_installed_agent'}]")
    page.click("#hooks li[data-agent=codex] button")
    page.wait_for_function("document.getElementById('hooks-message').textContent.includes('/hooks')")
    assert ["connect_hooks", "nonce-1", "codex", True] in calls(page)
    assert page.inner_text("#hooks-message") == approve
    page.wait_for_selector("#hooks li[data-agent=codex] [data-state=needs_approval]")
    assert page.inner_text("#hooks li[data-agent=codex] button") == "Disconnect"
    context.close()
    assert not errors


def test_a_read_whose_reply_is_dropped_is_asked_again(browser, local_origin):
    """pywebview re-injects its bridge when the app's cancelled sentinel navigation completes, dropping the
    reply to a call in flight (seen in the Windows e2e). One-shot reads (rc.read) ask again after 8 s."""
    context = browser.new_context(viewport={"width": 1180, "height": 780})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(FAKE_API % (json.dumps({"status": STATUS}), "true"))
    page.add_init_script("""(() => {
        let first = true;
        window.__replies.hooks = () => {
            if (first) { first = false; return new Promise(() => {}); }  // the dropped reply
            return [{kind: "codex", name: "Codex", state: "not_connected"}];
        };
    })();""")
    page.goto(f"{local_origin}this-computer.html")
    page.wait_for_selector("#hooks li[data-agent=codex] [data-state=not_connected]", timeout=20000)
    assert len([c for c in calls(page) if c[0] == "hooks"]) == 2
    context.close()
    assert not errors
