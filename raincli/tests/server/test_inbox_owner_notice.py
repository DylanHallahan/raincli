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


def _stuck(client, session, world, handle):
    """A machine of alice's that connects but hasn't picked up a message for over 24 h (§19.6 b)."""
    import uuid as uuidlib
    from datetime import timedelta

    from api_helpers import auth
    from raincli_server.models import Agent, Message

    agent, token = identity.register_agent(session, world["teams"]["acme"], world["users"]["alice"], handle)
    session.commit()
    assert client.put("/api/v1/presence", headers=auth(token), json={"status": "ready"}).status_code == 200
    r = client.post("/api/v1/messages", headers=auth(world["tokens"]["bob"]),
                    json={"id": str(uuidlib.uuid4()), "to": {"machine": handle}, "body": "waiting"})
    assert r.status_code == 201, r.text
    session.execute(update(Message).where(Message.id == uuidlib.UUID(r.json()["message"]["id"]))
                    .values(created_at=identity.now() - timedelta(hours=25)))
    session.commit()
    return agent


def _notice(alice_app):
    page = htmllib.unescape(alice_app["webview"].get("/app/inbox").text)
    start = page.index('<div class="owner-warnings"')
    block = page[start:page.index("</div>", start)]
    assert block.count('class="sender-warning"') == 1
    return " ".join(block.split())


def test_the_combined_notice_splits_not_connecting_from_not_picking_up(client, world, alice_app, session):  # noqa: F811
    never_connected(session, world, extra=1)  # alice-agent, alice-laptop and alice-old-0: never connected
    _stuck(client, session, world, "alice-busy")
    text = _notice(alice_app)
    assert "3 of your machines aren't connecting: <strong>" in text and "<strong>alice-old-0</strong>" in text
    assert " · 1 hasn't picked up messages (update RainCLI): <strong>alice-busy</strong>." in text
    assert "alice-busy" not in text.split(" · ")[0] and "See them in Agents" in text


def test_a_notice_of_only_stale_pickups_says_so(client, world, alice_app, session):  # noqa: F811
    from raincli_server import identity as ident

    session.execute(update(AgentCredential).values(last_used_at=ident.now()))  # every machine connects
    session.commit()
    _stuck(client, session, world, "alice-busy-0")
    _stuck(client, session, world, "alice-busy-1")
    text = _notice(alice_app)
    assert "aren't connecting" not in text and "isn't connecting" not in text
    assert ("2 of your machines haven't picked up messages (update RainCLI): <strong>alice-busy-0</strong>, "
            "<strong>alice-busy-1</strong>.") in text
