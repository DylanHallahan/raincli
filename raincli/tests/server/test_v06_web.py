"""v0.6 web (protocol §21 C5.7, C5.8, C7, C8.3; W1): app-mode and website pages show people, never machines or
agents, on the shared v0.6 seed. Real PostgreSQL."""

from __future__ import annotations

import html as htmllib
import re
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest_v06 import OWN_STRINGS, PASSWORD, TEAMMATE_STRINGS, v06_seed  # noqa: F401  (fixture)
from raincli_server import identity
from raincli_server.models import AgentPresence, Conversation, Message
from test_app_mode import NAV, WEBVIEW, handoff_code
from test_web_app import csrf_of, login


def text(page: str) -> str:
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", page)).split())


@pytest.fixture
def maya(app, client, v06_seed):  # noqa: F811
    """Maya's app window (app mode), signed in through a real handoff."""
    webview = TestClient(app, headers=WEBVIEW)
    r = webview.get(f"/app/handoff?code={handoff_code(client, v06_seed['person_tokens']['Maya'])}", headers=NAV,
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    yield webview
    webview.close()


def teammate_free(body: str, *, own_too: bool = True):
    """None of the teammates' strings anywhere. The viewer's own only inside their owner warnings (C5.1 allows
    those), so they're checked with that block removed."""
    for s in TEAMMATE_STRINGS:
        assert s not in body, s
    if own_too:
        rest = re.sub(r'<div class="owner-warnings".*?</div>', "", body, flags=re.S)
        for s in OWN_STRINGS:
            assert s not in rest, s


def conv(v06_seed, key):  # noqa: F811
    return str(v06_seed["conversations"][key].id)


# Conversations ----------------------------------------------------------------------------------

def test_the_conversation_with_jon_shows_people_presence_and_the_team(maya, v06_seed):  # noqa: F811
    page = maya.get(f"/app/conversations/{conv(v06_seed, 'jon')}").text
    words = text(page)
    assert "<h2>Jon</h2>" in page and "Northwind" in words
    assert re.search(r'rc-chip">Instant<', page) and ">Ready<" in page  # presence · speed from Jon's inbox
    assert "Maya · agent" in words and "Jon · agent" in words  # PR4: agent-sent messages
    assert re.search(r"<strong>Jon</strong> · ", page)  # what Jon wrote in the app
    assert "via" not in words and "stated by the machine" not in words  # P1 and the via line are gone
    assert "picked it up" not in words  # a pre-v0.6 free-text event detail is never rendered
    teammate_free(page)


def test_a_message_to_someone_without_an_inbox_is_held_and_warned(maya, v06_seed):  # noqa: F811
    page = maya.get(f"/app/conversations/{conv(v06_seed, 'sam')}").text
    words = text(page)
    assert "held: waiting for Sam to pick an inbox" in words
    assert "Sam hasn't picked an inbox yet. They'll see your message in their app." in words
    assert "No inbox yet" in words
    teammate_free(page)


def test_a_busy_inbox_hold_has_no_agent_words(maya, v06_seed):  # noqa: F811
    page = maya.get(f"/app/conversations/{conv(v06_seed, 'priya')}").text
    words = text(page)
    assert "held: Priya's inbox is busy" in words and "Busy · Next turn" in words or ">Busy<" in page
    assert "session" not in words.lower()
    teammate_free(page)


def test_your_own_agents_conversation_is_titled_and_takes_replies_only(maya, v06_seed):  # noqa: F811
    page = maya.get(f"/app/conversations/{conv(v06_seed, 'self')}").text
    words = text(page)
    assert "From your agents" in words and "Maya · agent" in words
    assert "Use Reply on one of them" in words and 'class="rc-reply"' in page
    assert 'name="to"' not in page  # no free composer: replies only (C13 H2)


def test_the_list_has_people_and_no_team_section(maya, v06_seed):  # noqa: F811
    page = maya.get(f"/app/conversations/{conv(v06_seed, 'jon')}").text
    names = re.findall(r'<span class="rc-conv-name">([^<]+)</span>', page)
    assert set(names) >= {"Jon", "Sam", "Priya", "From your agents"}
    assert "rc-team" not in page and "data-team" not in page and ">Team<" not in page
    teammate_free(page)


def test_the_website_conversation_is_the_same_people_view(client, v06_seed):  # noqa: F811
    login(client, "maya@example.test", PASSWORD)
    page = client.get(f"/app/conversations/{conv(v06_seed, 'jon')}").text
    words = text(page)
    assert "You ⇄ with Jon" in words and "Maya · agent" in words and "Jon · agent" in words
    assert "picked it up" not in words and "via" not in words
    teammate_free(page)
    sam = text(client.get(f"/app/conversations/{conv(v06_seed, 'sam')}").text)
    assert "held: waiting for Sam to pick an inbox" in sam


def test_an_old_merged_id_redirects_to_the_final_conversation(maya, session, v06_seed):  # noqa: F811
    final = v06_seed["conversations"]["jon"]
    team = v06_seed["teams"]["northwind"]
    maya_m, jon_m = v06_seed["machines"]["maya"], v06_seed["machines"]["jon"]
    old = Conversation(id=uuid.uuid4(), team_id=team.id, agent_a_id=maya_m.id, agent_b_id=jon_m.id,
                       a_key=f"m:{min(maya_m.id, jon_m.id)}", b_key=f"m:{max(maya_m.id, jon_m.id)}",
                       kind="pair", is_default=False, merged_into=final.id)
    if str(maya_m.id) > str(jon_m.id):
        old.agent_a_id, old.agent_b_id = jon_m.id, maya_m.id
        old.a_key, old.b_key = f"m:{jon_m.id}", f"m:{maya_m.id}"
    session.add(old)
    session.commit()
    r = maya.get(f"/app/conversations/{old.id}?reply_to=x", follow_redirects=False)
    assert r.status_code == 303 or r.status_code == 302
    assert r.headers["location"].endswith(f"/app/conversations/{final.id}?reply_to=x")
    assert maya.get(f"/app/conversations/{uuid.uuid4()}").status_code == 404


# People, the picker, warnings -----------------------------------------------------------------

def test_people_page_lists_teammates_with_presence_and_speed(maya, v06_seed):  # noqa: F811
    page = maya.get("/app/people").text
    words = text(page)
    for expected in ("Jon Ready · Instant", "Priya Busy · Next turn", "Sam No inbox yet", "Ryan Offline",
                     "Lee", "Contoso"):
        assert expected in words, expected
    assert "Maya" not in re.findall(r'rc-agent-name">.*?</span>([^<]+)</span>', page)
    assert 'href="/app/compose?to=jon%40example.test' in page
    teammate_free(page)
    r = maya.get("/app/agents", follow_redirects=False)
    assert r.status_code in (302, 303) and r.headers["location"].endswith("/app/people")


def test_recipients_are_people_only_recent_first(maya, v06_seed):  # noqa: F811
    r = maya.get("/app/recipients")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    data = r.json()
    people = data["recipients"]
    assert people and all(re.fullmatch(r"p\.[0-9a-f-]{36}\.[0-9a-f-]{36}", p["id"]) for p in people)
    recent = [p["name"] for p in people if p["group"] == "recent"]
    assert set(recent) == {"Jon", "Sam", "Priya"}  # the people Maya has conversations with
    assert [p["name"] for p in people[:len(recent)]] == recent
    jon = next(p for p in people if p["name"] == "Jon")
    assert jon["status"] == "Ready · Instant" and jon["team"] == "Northwind"  # several teams: the team shows
    assert "jon@example.test" in jon["match"]
    teammate_free(r.text)
    assert "Maya" not in [p["name"] for p in people]


def test_the_person_warning_is_uniform_for_anything_but_a_teammate(maya, v06_seed):  # noqa: F811
    sam = maya.get("/app/person-warning?to=sam@example.test").text
    assert "Sam</strong> hasn't picked an inbox yet. They'll see your message in their app." in sam
    ryan = maya.get("/app/person-warning?to=Ryan").text
    assert "<strong>Ryan</strong>'s inbox hasn't connected since" in ryan
    answers = {maya.get(f"/app/person-warning?to={q}").text for q in
               ("zq-handle-jon", "zq-handle-jon/zq-agent-jon-api", "zq-handle-maya", "nobody@example.test",
                "no-such-handle", "maya@example.test", "lee@example.test%20x")}
    assert len(answers) == 1  # the same empty answer: no handle, agent or outsider is confirmed
    assert maya.get("/app/person-warning?to=jon@example.test").headers["cache-control"] == "no-store"


def test_compose_picks_people_and_refuses_handles_in_presentations_words(maya, client, v06_seed):  # noqa: F811
    page = maya.get("/app/compose?to=jon@example.test").text
    nw, jon = v06_seed["teams"]["northwind"], v06_seed["people"]["Jon"]
    assert f'<option value="p.{nw.id}.{jon.id}" selected>Jon</option>' in page
    assert "<datalist" not in page and "data-recipient-warning" in page
    teammate_free(page)
    login(client, "maya@example.test", PASSWORD)
    form = client.get("/app/compose").text
    assert "<datalist" not in form
    teammate_free(form)
    csrf = csrf_of(form)

    def send(to):
        r = client.post("/app/conversations/new/send", data={"csrf_token": csrf, "to": to, "body": "hi",
                                                              "message_id": str(uuid.uuid4()), "team": "northwind"})
        return text(r.text)

    assert "Address people: send to Jon (jon@example.test)" in send("zq-handle-jon")
    assert "Address people: send to Jon (jon@example.test)" in send("zq-handle-jon/zq-agent-jon-api")
    generic = "Address people: send to a teammate's name or email"
    assert generic in send("zq-handle-maya") and generic in send("no-such-handle")  # own handle: generic (L3)


# W1: old clients that never confirm delivery -----------------------------------------------------

def _waiting(session, v06_seed, machine_key, days=2):  # noqa: F811
    """An unacknowledged message to ``machine_key``'s machine, ``days`` old."""
    team, maya = v06_seed["teams"]["northwind"], v06_seed["people"]["Maya"]
    machine = v06_seed["machines"][machine_key]
    for older in session.query(Message).filter(Message.recipient_agent_id == machine.id, Message.acked_at.is_(None)):
        older.acked_at = identity.now()  # only this message waits
    msg = Message(id=uuid.uuid4(), team_id=team.id, conversation_id=v06_seed["conversations"]["jon"].id,
                  body="waiting", kind="message", route="inbox", sender_user_id=maya.id,
                  recipient_agent_id=machine.id, recipient_agent_name=None, delivery_state="stored")
    session.add(msg)
    session.flush()
    msg.created_at = identity.now() - timedelta(days=days)
    session.commit()
    return machine


def _fresh(session, machine, version):
    row = session.get(AgentPresence, machine.id)
    row.seen_at = identity.now()
    row.client_version = version
    session.commit()


def test_w1_sender_warning_for_a_client_that_never_reported_a_version(maya, session, v06_seed):  # noqa: F811
    machine = _waiting(session, v06_seed, "jon")
    _fresh(session, machine, None)
    words = text(maya.get("/app/person-warning?to=jon@example.test").text)
    assert words == "Jon is on an old version of RainCLI that doesn't confirm delivery. Ask them to update."


def test_w1_current_client_keeps_the_not_picking_up_wording(maya, session, v06_seed):  # noqa: F811
    machine = _waiting(session, v06_seed, "jon")
    _fresh(session, machine, "0.5.6")
    page = maya.get("/app/person-warning?to=jon@example.test").text
    assert "<strong>Jon</strong>'s inbox hasn't picked up messages since" in page and "old version" not in page


def test_w1_owner_notice_for_their_own_old_machine(maya, session, v06_seed):  # noqa: F811
    from raincli_server.models import AgentCredential

    machine = _waiting(session, v06_seed, "maya")
    _fresh(session, machine, None)
    for key in ("maya", "maya_ct"):  # both connected today: the only notice is the old client's
        for cred in session.query(AgentCredential).filter_by(agent_id=v06_seed["machines"][key].id):
            cred.last_used_at = identity.now()
    session.commit()
    words = text(maya.get("/app/inbox").text)
    assert "zq-handle-maya : This machine runs an old RainCLI that doesn't confirm delivery; update it." in words
    # With a second notice the app combines them into one line; the old client keeps its own wording there.
    for cred in session.query(AgentCredential).filter_by(agent_id=v06_seed["machines"]["maya_ct"].id):
        cred.last_used_at = None
    session.commit()
    words = text(maya.get("/app/inbox").text)
    assert "1 runs an old RainCLI that doesn't confirm delivery; update it: zq-handle-maya" in words
    for cred in session.query(AgentCredential).filter_by(agent_id=v06_seed["machines"]["maya_ct"].id):
        cred.last_used_at = identity.now()
    session.commit()
    _fresh(session, machine, "0.5.6")
    words = text(maya.get("/app/inbox").text)
    assert "old RainCLI" not in words and "picked up messages" in words


# Website Machines (PR11) and the list digest -----------------------------------------------------

def test_a_member_sees_only_their_own_machines(client, v06_seed):  # noqa: F811
    login(client, "maya@example.test", PASSWORD)
    page = client.get("/app/agents").text
    assert "zq-handle-maya" in page and "zq-agent-maya-laptop" in page  # her own, with her agents
    teammate_free(page, own_too=False)
    team = client.get("/app/teams/northwind").text
    teammate_free(team, own_too=False)


def test_a_team_owner_sees_every_machine_but_no_agents(client, v06_seed):  # noqa: F811
    login(client, "olive@example.test", PASSWORD)
    page = client.get("/app/agents").text
    for handle in ("zq-handle-jon", "zq-handle-priya", "zq-handle-sam-app", "zq-handle-ryan", "zq-handle-maya"):
        assert handle in page, handle
    for agent in ("zq-agent-jon-api", "zq-agent-priya-web", "zq-agent-ryan-old", "zq-agent-maya-laptop"):
        assert agent not in page, agent
    assert "zq-agent-olive-ops" in page  # her own machine's agents
    assert "zq-handle-lee" not in page  # not her team
    team = client.get("/app/teams/northwind").text
    assert "zq-handle-jon" in team and "zq-agent-jon-api" not in team


def test_list_rev_ignores_a_teammates_presence(maya, session, v06_seed):  # noqa: F811
    first = maya.get("/app/live").json()["list_rev"]
    row = session.get(AgentPresence, v06_seed["machines"]["jon"].id)
    row.status = "busy"
    session.commit()
    assert maya.get("/app/live", params={"list_rev": first}).json()["list"] is None
