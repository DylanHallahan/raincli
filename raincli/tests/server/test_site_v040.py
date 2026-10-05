"""The website reflects v0.4.0: the Windows app download and the headless `raincli login` path."""

from __future__ import annotations

import html as htmllib
import re

from test_web_app import app_csrf, login

DOWNLOAD = "https://github.com/DylanHallahan/raincli/releases/latest"
HEADLESS = "https://github.com/DylanHallahan/raincli/blob/main/SETUP.md#headless-linux-raincli-login"
WINDOWS_GUIDE = "https://github.com/DylanHallahan/raincli/blob/main/docs/windows-client.md"
LOGIN_STEPS = ("raincli login --email", "raincli runtime startup --config ~/.config/raincli/runtime.json",
               'loginctl enable-linger "$USER"')
NEVER = ("Ryan", "Shambhavi", "vault", "web-builder", "cli-builder", "main agent")


def text(page: str) -> str:
    return htmllib.unescape(page)


def test_home_page_offers_the_windows_download_and_headless_login(client):
    r = client.get("/")
    page = text(r.text)
    assert f'href="{DOWNLOAD}"' in r.text and "Download for Windows" in page
    # Release assets carry the version, so the page links the latest release, never one file.
    assert not re.search(r"releases/(?:latest/)?download/", r.text)
    assert '(Get-FileHash "$HOME\\Downloads\\RainCLI-Setup-X.Y.Z.exe" -Algorithm SHA256).Hash.ToLower()' in page
    assert "(Get-Content \"$HOME\\Downloads\\RainCLI-Setup-X.Y.Z.exe.sha256\").Split(' ')[0]" in page
    assert "not code-signed" in page and "Run anyway only for a file whose checksum matched" in page
    assert "sign in with your RainCLI email and password" in page
    for step in LOGIN_STEPS:
        assert step in page
    assert f'href="{HEADLESS}"' in r.text and f'href="{WINDOWS_GUIDE}"' in r.text
    assert "download its config" in page  # the connector path stays documented
    for word in NEVER:
        assert word not in page


def test_home_page_keeps_the_csp_and_loads_no_external_script(client):
    from raincli_server.web.middleware import CSP

    r = client.get("/")
    assert r.headers["content-security-policy"] == CSP
    assert not re.search(r"<script[^>]+src=\"https?://", r.text)
    assert "<script>" not in r.text


def test_machines_page_puts_the_app_and_login_first(client, world):
    login(client)
    page = text(client.get("/app/agents").text)
    assert "Set up a machine" in page and f'href="{DOWNLOAD}"' in page
    assert "raincli login --email alice@example.test" in page
    for step in LOGIN_STEPS[1:]:
        assert step in page
    assert page.index('id="set-up"') < page.index('id="add-machine"')
    assert "For an inbox or connector setup" in page  # the config download is the alternative
    for word in NEVER:
        assert word not in page


def test_empty_machines_page_names_both_paths(client, session):
    from raincli_server import identity

    user = identity.create_user(session, "new@example.test", "New", "correct horse battery")
    identity.create_team(session, "fresh", "Fresh", user)
    session.commit()
    login(client, email="new@example.test")
    page = text(client.get("/app/agents").text)
    assert "No machines yet. On Windows, install the RainCLI app and sign in" in page
    assert "raincli login" in page and "download its config" in page


def test_config_page_points_to_the_simpler_paths(client, world):
    login(client)
    r = client.post("/app/agents", data={"csrf_token": app_csrf(client), "team": "acme", "handle": "alice-two"})
    page = text(r.text)
    assert "This config is for an inbox or connector setup" in page
    assert f'href="{DOWNLOAD}"' in page and f'href="{HEADLESS}"' in page
    assert "Download raincli-alice-two.json" in page  # the config download itself is unchanged
