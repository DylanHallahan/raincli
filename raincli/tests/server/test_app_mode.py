"""The app handoff and app-mode sessions (protocol §16.9, §16.10, §16.12 C2, C3, C6), real PostgreSQL."""

from __future__ import annotations

import hashlib
import html as htmllib
import logging
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from api_helpers import auth
from raincli_server import identity
from raincli_server.models import HandoffCode, Message, PersonSession, WebSession
from test_web_app import PASSWORD, app_csrf, csrf_of, login

NAV = {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"}
# §16.14 S3: the app install token the webview sends in its User-Agent; the server sees only its hash.
APP_TOKEN = "t" * 20 + "Abc_-123" + "z" * 15
INSTALL_HASH = hashlib.sha256(APP_TOKEN.encode()).hexdigest()
WEBVIEW_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Edg/131.0 RainCLIApp/" + APP_TOKEN
WEBVIEW = {"User-Agent": WEBVIEW_UA}


def person_session(client, email="alice@example.test", machine="alice-laptop"):
    r = client.post("/api/v1/app/login", json={"email": email, "password": PASSWORD, "machine_name": machine,
                                               "person_session": True})
    assert r.status_code == 201, r.text
    return r.json()


def handoff_code(client, person, install_hash=INSTALL_HASH):
    r = client.post("/api/v1/app/handoff", headers=auth(person), json={"app_install_hash": install_hash})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"].startswith("rch_") and body["url"].endswith("/app/handoff?code=" + body["code"])
    return body["code"]


def open_app(app, person, client):
    """A fresh 'webview' that consumes a handoff code."""
    webview = TestClient(app, headers=WEBVIEW)
    code = handoff_code(client, person)
    r = webview.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/app/inbox"), r.text
    return webview, r


@pytest.fixture
def alice_app(app, client, world):
    signed = person_session(client)
    webview, response = open_app(app, signed["person_session"], client)
    yield {"webview": webview, "response": response, "person": signed["person_session"], "token": signed["token"],
           "world": world}
    webview.close()


# The handoff (C3) --------------------------------------------------------------------------

def test_handoff_sets_a_separate_strict_cookie(alice_app, session):
    r = alice_app["response"]
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("raincli_app=") and "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("Strict", "strict")
    assert "Path=/app/" in cookie and "raincli_session" not in cookie
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
    ws = session.scalar(select(WebSession).where(WebSession.app_mode.is_(True)))
    ps = session.scalar(select(PersonSession))
    assert ws.person_session_id == ps.id and ws.expires_at == identity.person_session_expires_at(ps)


@pytest.mark.parametrize("headers", [{}, {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate"},
                                     {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"},
                                     {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "cors"}])
def test_handoff_needs_a_user_started_navigation_and_consumes_nothing_otherwise(app, client, world, session, headers):
    person = person_session(client)["person_session"]
    code = handoff_code(client, person)
    with TestClient(app, headers=WEBVIEW) as webview:
        r = webview.get(f"/app/handoff?code={code}", headers=headers, follow_redirects=False)
        assert r.status_code == 400 and "raincli_app" not in r.headers.get("set-cookie", "")
        assert "can't be used" in htmllib.unescape(r.text)
        session.expire_all()
        assert session.scalar(select(HandoffCode.used_at)) is None
        assert webview.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False).status_code == 303


def test_handoff_codes_are_single_use_and_short_lived(app, client, world, session):
    person = person_session(client)["person_session"]
    code = handoff_code(client, person)
    with TestClient(app, headers=WEBVIEW) as webview:
        assert webview.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False).status_code == 303
    with TestClient(app, headers=WEBVIEW) as again:
        r = again.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False)
        assert r.status_code == 400 and "can't be used" in htmllib.unescape(r.text)
    code = handoff_code(client, person)
    session.execute(update(HandoffCode).where(HandoffCode.used_at.is_(None))
                    .values(expires_at=identity.now() - timedelta(seconds=1)))
    session.commit()
    with TestClient(app, headers=WEBVIEW) as late:
        assert late.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False).status_code == 400
    for bogus in ("", "rch_nope", "x" * 300, "rca_" + "a" * 40):
        with TestClient(app, headers=WEBVIEW) as other:
            assert other.get(f"/app/handoff?code={bogus}", headers=NAV, follow_redirects=False).status_code == 400


def test_handoff_bound_to_a_revoked_session_fails(app, client, world):
    person = person_session(client)["person_session"]
    code = handoff_code(client, person)
    assert client.post("/api/v1/person/sign-out", headers=auth(person)).status_code == 200
    with TestClient(app, headers=WEBVIEW) as webview:
        assert webview.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False).status_code == 400


def test_handoff_never_replaces_another_users_app_session(alice_app, app, client):
    bob = person_session(client, "bob@example.test", "bob-laptop")["person_session"]
    webview = alice_app["webview"]
    code = handoff_code(client, bob)
    r = webview.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False)
    assert r.status_code == 400
    assert "Alice" in webview.get("/app/compose").text  # still Alice
    # The same user may hand off again.
    again = handoff_code(client, alice_app["person"])
    assert webview.get(f"/app/handoff?code={again}", headers=NAV, follow_redirects=False).status_code == 303


def test_handoff_codes_need_a_person_session(client, world):
    assert client.post("/api/v1/app/handoff", headers=auth(world["tokens"]["alice"])).status_code == 401
    assert client.post("/api/v1/app/handoff").status_code == 401


# App-mode scope (C2) ----------------------------------------------------------------------------

def test_app_mode_layout_and_allowed_routes(alice_app, session):
    webview, world = alice_app["webview"], alice_app["world"]
    inbox = webview.get("/app/inbox")
    assert inbox.status_code == 200 and 'class="app-mode' in inbox.text and "rc-nav" in inbox.text
    assert "site-footer" not in inbox.text and "How it works" not in inbox.text  # no marketing chrome
    # The raincli_app cookie is scoped to /app/, so the rail links to /app/inbox, never /app.
    assert webview.get("/app", follow_redirects=False).status_code == 303
    for path, marker in (("/app/compose", "New message"), ("/app/agents", "Agents"),
                         ("/app/local/this-computer", "Open the RainCLI app")):
        r = webview.get(path)
        assert r.status_code == 200 and marker in r.text, path
    agents = webview.get("/app/agents").text
    assert "Add a machine" not in agents and "Rotate" not in agents and "bob-agent" in agents
    # Compose and reply as the person.
    csrf = csrf_of(webview.get("/app/compose").text)
    r = webview.post("/app/conversations/new/send", data={"csrf_token": csrf, "message_id": str(uuid.uuid4()),
                                                          "to": "@bob@example.test", "body": "from the app"},
                     follow_redirects=False)
    assert r.status_code == 303
    msg = session.scalar(select(Message).where(Message.body == "from the app"))
    assert msg.sender_user_id == world["users"]["alice"].id
    thread = webview.get(f"/app/conversations/{msg.conversation_id}")
    assert thread.status_code == 200 and "rc-compose" in thread.text and "from the app" in thread.text


@pytest.mark.parametrize("method,path", [
    ("get", "/app/account"), ("get", "/app/team"), ("get", "/app/teams/acme"),
    ("post", "/app/account/password"), ("post", "/app/agents"), ("post", "/app/teams/acme/invitations"),
])
def test_app_mode_refuses_administration(alice_app, method, path):
    webview = alice_app["webview"]
    csrf = csrf_of(webview.get("/app/compose").text)
    r = getattr(webview, method)(path, **({"data": {"csrf_token": csrf}} if method == "post" else {}))
    assert r.status_code == 403 and "Open on the website" in r.text and "http://testserver" + path in r.text


def test_app_mode_refuses_machine_and_app_management(alice_app, session):
    webview, world = alice_app["webview"], alice_app["world"]
    csrf = csrf_of(webview.get("/app/compose").text)
    aid = world["agents"]["alice"].id
    for path in (f"/app/agents/{aid}/rotate", f"/app/agents/{aid}/revoke", f"/app/agents/{aid}/config"):
        assert webview.post(path, data={"csrf_token": csrf}).status_code == 403, path
    ps = session.scalar(select(PersonSession))
    assert webview.post(f"/app/account/apps/{ps.id}/revoke", data={"csrf_token": csrf}).status_code == 403


@pytest.mark.parametrize("trigger", ["person_sign_out", "app_sign_out", "password_change", "apps_list"])
def test_app_session_ends_with_its_person_session(app, alice_app, client, session, trigger):
    webview = alice_app["webview"]
    assert webview.get("/app/inbox", follow_redirects=False).status_code == 200
    if trigger == "person_sign_out":
        client.post("/api/v1/person/sign-out", headers=auth(alice_app["person"]))
    elif trigger == "app_sign_out":
        client.post("/api/v1/app/sign-out", headers=auth(alice_app["token"]))
    elif trigger == "password_change":
        login(client)
        client.post("/app/account/password", data={"csrf_token": app_csrf(client), "current_password": PASSWORD,
                                                   "password": "another long passphrase",
                                                   "password_confirm": "another long passphrase"})
    elif trigger == "apps_list":
        login(client)
        ps = session.scalar(select(PersonSession))
        page = client.get("/app/account").text
        assert "Signed-in apps" in page and "alice-laptop" in page
        r = client.post(f"/app/account/apps/{ps.id}/revoke", data={"csrf_token": app_csrf(client)},
                        follow_redirects=False)
        assert r.status_code == 303 and "app-signed-out" in r.headers["location"]
    r = webview.get("/app/inbox", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_apps_list_is_the_owners_own(client, world, session):
    person_session(client)
    ps = session.scalar(select(PersonSession))
    login(client, email="bob@example.test")
    assert "alice-laptop" not in client.get("/app/account").text
    r = client.post(f"/app/account/apps/{ps.id}/revoke", data={"csrf_token": app_csrf(client)})
    assert r.status_code == 404
    session.expire_all()
    assert session.get(PersonSession, ps.id).revoked_at is None


# The sentinel, branding and the per-user web send limit ------------------------------------------------

def test_local_sentinel_in_a_browser(client):
    r = client.get("/app/local/settings")
    assert r.status_code == 200 and "Open the RainCLI app" in r.text and "app-mode" not in r.text
    assert client.get("/app/local/Bad_Name").status_code == 404


def test_product_name_and_logo_are_settings(settings, engine, world):
    from dataclasses import replace

    from raincli_server.app import create_app
    from raincli_server.config import ConfigError, load_settings

    app = create_app(replace(settings, product_name="Acme Relay"))
    try:
        with TestClient(app) as c:
            login(c)
            page = c.get("/app/compose").text
            assert "Compose · Acme Relay" in page and ">Acme Relay</span>" in page
            assert "Open the Acme Relay app" in c.get("/app/local/settings").text
    finally:
        app.state.engine.dispose()
    base = {"RAINCLI_DATABASE_URL": "postgresql://x/y", "RAINCLI_SECRET_KEY": "k" * 40}
    for bad in ({"RAINCLI_PRODUCT_NAME": ""}, {"RAINCLI_PRODUCT_LOGO": "../x.svg"},
                {"RAINCLI_PRODUCT_LOGO": "logo.js"}):
        with pytest.raises(ConfigError):
            load_settings({**base, **bad})


def test_web_sends_are_rate_limited_per_user(settings, engine, world, session):
    from dataclasses import replace

    from raincli_server.app import create_app

    app = create_app(replace(settings, rate_limit_per_min=2))
    try:
        with TestClient(app) as c:
            login(c)
            csrf = app_csrf(c)
            codes = [c.post("/app/conversations/new/send", data={
                "csrf_token": csrf, "message_id": str(uuid.uuid4()), "to": "bob-agent", "body": f"m{i}"},
                follow_redirects=False).status_code for i in range(3)]
            assert codes == [303, 303, 429]
    finally:
        app.state.engine.dispose()


def test_tokens_css_has_light_and_dark_rc_tokens(client):
    css = client.get("/static/tokens.css").text
    assert "--rc-bg:" in css and "--rc-accent:" in css and "prefers-color-scheme: dark" in css
    app_mode = client.get("/static/app-mode.css").text
    import re
    # app-mode.css uses only tokens: no raw colours outside tokens.css.
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", app_mode)


# §16.14 S3: the handoff and app-mode sessions are bound to the app install ---------------------------

@pytest.mark.parametrize("user_agent", [
    None, "Mozilla/5.0 Chrome/131.0 Edg/131.0",  # no token: an ordinary browser
    WEBVIEW_UA.replace(APP_TOKEN, "w" * 43),  # another install's token
    WEBVIEW_UA + " RainCLIApp/" + APP_TOKEN,  # two tokens
    WEBVIEW_UA.replace("RainCLIApp/", "RainCLIApp/ "),  # malformed
    WEBVIEW_UA.replace("RainCLIApp/", "XRainCLIApp/"),
])
def test_handoff_needs_the_bound_install_token_and_consumes_nothing_otherwise(app, client, world, session,
                                                                             user_agent):
    person = person_session(client)["person_session"]
    code = handoff_code(client, person)
    with TestClient(app) as browser:
        headers = dict(NAV, **({"User-Agent": user_agent} if user_agent else {"User-Agent": ""}))
        r = browser.get(f"/app/handoff?code={code}", headers=headers, follow_redirects=False)
        assert r.status_code == 400 and "raincli_app" not in r.headers.get("set-cookie", "")
        assert "can't be used" in htmllib.unescape(r.text)
    session.expire_all()
    assert session.scalar(select(HandoffCode.used_at)) is None
    with TestClient(app, headers=WEBVIEW) as webview:  # the right install still can
        assert webview.get(f"/app/handoff?code={code}", headers=NAV, follow_redirects=False).status_code == 303


def test_handoff_request_must_carry_a_well_formed_install_hash(client, world):
    person = person_session(client)["person_session"]
    for body in (None, {}, {"app_install_hash": "A" * 64}, {"app_install_hash": "a" * 63}, {"app_install_hash": 1},
                 {"app_install_hash": INSTALL_HASH, "extra": 1}):
        r = client.post("/api/v1/app/handoff", headers=auth(person), **({"json": body} if body is not None else {}))
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid", body


def test_a_stolen_app_cookie_is_useless_in_another_browser(alice_app, app):
    cookie = alice_app["webview"].cookies.get("raincli_app")
    assert cookie and alice_app["webview"].get("/app/inbox").status_code == 200
    for user_agent in ("Mozilla/5.0 Chrome/131.0", WEBVIEW_UA.replace(APP_TOKEN, "w" * 43)):
        with TestClient(app, headers={"User-Agent": user_agent}) as thief:
            thief.cookies.set("raincli_app", cookie, path="/app/")
            r = thief.get("/app/inbox", follow_redirects=False)
            assert r.status_code in (303, 401) and "rc-nav" not in r.text, user_agent
            assert thief.get("/app/conversations/new", follow_redirects=False).status_code != 200


def test_the_app_rail_says_who_is_signed_in(alice_app):
    webview = alice_app["webview"]
    for path in ("/app/inbox", "/app/agents"):
        page = htmllib.unescape(webview.get(path).text)
        assert 'class="rc-whoami"' in page and "Signed in as" in page and "(alice@example.test)" in page, path


def test_the_install_token_is_never_logged(alice_app, caplog):
    webview = alice_app["webview"]
    with caplog.at_level(logging.DEBUG):
        webview.get("/app/inbox")
        webview.get("/app/handoff?code=rch_bogus", headers=NAV)
        webview.get("/app/does-not-exist")
        webview.get("/api/v1/person/me", headers={"Authorization": "Bearer rps_bogus"})
    text = "\n".join(r.getMessage() + " " + str(r.__dict__) for r in caplog.records)
    assert APP_TOKEN not in text and "RainCLIApp" not in text


# §16.14 S4: the handoff URL keeps the root path -------------------------------------------------------

def test_handoff_url_includes_the_root_path(settings, engine, world):
    from dataclasses import replace

    from raincli_server.app import create_app

    app = create_app(replace(settings, root_path="/raincli"))
    try:
        with TestClient(app, root_path="/raincli") as c:
            r = c.post("/raincli/api/v1/app/login", json={"email": "alice@example.test", "password": PASSWORD,
                                                          "machine_name": "alice-laptop", "person_session": True})
            person = r.json()["person_session"]
            r = c.post("/raincli/api/v1/app/handoff", headers=auth(person), json={"app_install_hash": INSTALL_HASH})
            assert r.status_code == 200, r.text
            url = r.json()["url"]
            assert url == f"{settings.public_url}/raincli/app/handoff?code={r.json()['code']}"
    finally:
        app.state.engine.dispose()


def test_the_app_mode_rail_shows_the_mark(alice_app):
    """v0.5.1: the rail's logo is the product logo setting, by default the new mark."""
    import re
    html = alice_app["webview"].get("/app/inbox").text
    assert re.search(r'<img class="rc-logo" src="/static/mark\.svg\?v=[0-9a-f]{12}"', html)
    assert re.search(r'<link rel="icon" href="/static/favicon\.ico\?v=[0-9a-f]{12}" sizes="any">', html)
