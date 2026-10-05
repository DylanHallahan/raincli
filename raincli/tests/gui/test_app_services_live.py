"""The app window's services against a real server (protocol §16.10, §16.14 S3, §16.15), no pywebview.

A machine signs in as ``raincli login`` does, then the window's own path adds the person session
(``Services.sign_in``). The handoff URL it gets opens the hosted inbox in headless Chromium only with the
install token in the User-Agent, exactly as the WebView2 hook sends it. Real PostgreSQL and uvicorn.
"""

from __future__ import annotations

import json

import pytest

from .test_app_mode_gui import PASSWORD, api, browser, live, sign_in  # noqa: F401 - fixtures

pytest.importorskip("playwright.sync_api")


class Host:
    paused = False

    def __init__(self):
        self.calls = []

    def pause(self):
        self.calls.append("pause")

    def resume(self):
        self.calls.append("resume")

    def stop(self):
        self.calls.append("stop")


@pytest.fixture
def machine(live, world, tmp_path):
    from raincli_agent import login
    from raincli_agent.config import Secret

    agent_config = tmp_path / "config" / "agent.json"
    plan = login.prepare(str(agent_config))
    login.login("alice@example.test", Secret(PASSWORD), plan=plan, machine_name="alice-pc", api_url=live)
    runtime_config = login.runtime_config_path(str(agent_config))
    from raincli_agent.app.services import Services
    return Services(None, Host(), paths=lambda: (str(agent_config), runtime_config)), live


def webview_context(browser, token):
    probe = browser.new_page()
    base = probe.evaluate("navigator.userAgent")
    probe.close()
    return browser.new_context(user_agent=f"{base} RainCLIApp/{token}" if token else base)


def test_window_sign_in_handoff_and_inbox(machine, browser):
    from raincli_agent import person
    from raincli_agent.config import Secret

    services, base = machine
    assert services.signed_in() and not services.has_person_session()
    services.sign_in("alice@example.test", Secret(PASSWORD), machine_name="alice-pc")  # person_only
    assert services.has_person_session()
    token = services.app_install_token()
    assert token == person.app_install_token(services.agent_config) and len(token) == 43

    url = services.handoff_url()
    assert url.startswith(base + "/app/handoff?code=rch_")
    stranger = webview_context(browser, None)  # an ordinary browser: the code is refused, not used up
    page = stranger.new_page()
    page.goto(url)
    assert "can't be used" in page.inner_text("body").replace("’", "'")
    stranger.close()
    context = webview_context(browser, token)
    page = context.new_page()
    page.goto(url)
    assert page.url.endswith("/app/inbox"), page.url
    assert "Signed in as" in page.inner_text(".rc-whoami") and "(alice@example.test)" in page.inner_text(".rc-whoami")
    context.close()

    # A rotation (sign-in again, sign-out, or a new install) ends that app session's use of the old token.
    person.rotate_app_install_token(services.agent_config)
    old = webview_context(browser, token)
    page = old.new_page()
    page.goto(services.handoff_url())
    assert not page.url.endswith("/app/inbox")
    old.close()


def test_status_settings_and_toasts_carry_no_secrets(machine, live):
    from raincli_agent import person
    from raincli_agent.config import Secret, load_config

    services, base = machine
    services.sign_in("alice@example.test", Secret(PASSWORD), machine_name="alice-pc")
    status = services.status()
    assert status["machine"] == "alice-pc" and status["team"] and status["routing"] == "all"
    secrets = [load_config(services.agent_config).token.reveal(), person.load_session(services.agent_config).reveal(),
               services.app_install_token()]
    assert not any(s in json.dumps(status) for s in secrets)
    assert services.save_settings({"routing": "inbox-only"}).startswith("Saved")
    assert services.settings()["routing"] == "inbox-only"
    services.save_settings({"routing": "all"})

    bob = sign_in(live, "bob@example.test", "bob-laptop")
    sent = api(live, "/api/v1/person/send", {"id": "6d7b2b54-0000-4000-8000-000000000001",
                                             "to": {"person": "alice@example.test"}, "body": "TOP SECRET body"},
               bob["person_session"])["message"]
    summary = services.message_summary(sent["id"])
    assert summary["kind"] == "message" and summary["conversation_id"] == sent["conversation_id"]
    assert "TOP SECRET" not in json.dumps(summary) and summary["sender"]


def test_sign_out_revokes_and_the_handoff_stops(machine):
    from raincli_agent.config import Secret

    services, base = machine
    services.sign_in("alice@example.test", Secret(PASSWORD), machine_name="alice-pc")
    services.sign_out()
    assert not services.signed_in() and not services.has_person_session() and "stop" in services.host.calls
    with pytest.raises(Exception):
        services.handoff_url()
