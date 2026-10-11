"""The owner's "not connected" notice (§19.6) after the inbox hotfix, real PostgreSQL: app mode combines two or more
machines into one line; the website keeps one line per machine (its shared partial is unchanged)."""

from __future__ import annotations

import html as htmllib

from fastapi.testclient import TestClient
from sqlalchemy import update

from raincli_server import identity
from raincli_server.models import AgentCredential
from test_app_mode import alice_app  # noqa: F401  (fixture)
from test_web_app import login


def never_connected(session, world, extra=1):
    for i in range(extra):
        identity.register_agent(session, world["teams"]["acme"], world["users"]["alice"], f"alice-old-{i}")
    session.execute(update(AgentCredential).values(last_used_at=None))
    session.commit()


def test_app_mode_combines_the_notice(client, world, alice_app, session):  # noqa: F811
    never_connected(session, world, extra=2)
    page = htmllib.unescape(alice_app["webview"].get("/app/inbox").text)
    block = page[page.index('<div class="owner-warnings"'):page.index("</div>", page.index('<div class="owner-warnings"'))]
    assert block.count('class="sender-warning"') == 1
    assert "of your machines aren't connecting:" in block and "<strong>alice-old-0</strong>" in block
    assert "See them in Agents" in block


def test_the_website_keeps_one_line_per_machine(client, world, session):
    never_connected(session, world, extra=2)
    with TestClient(client.app) as site:
        login(site)
        page = htmllib.unescape(site.get("/app/inbox").text)
    assert page.count("has never connected.") >= 3 and "of your machines aren't connecting" not in page
