"""The live thread under v0.6 (protocol §19, as changed by §21 C5, C7.4, C7.5, C13 H4): trails worded without agents,
person presence · speed in the header with the 120 s rule, person-keyed warnings, and polls that still cost the same
however many messages are in flight. These replace the v0.5 agent-addressed cases of test_live.py. Real PostgreSQL."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, update

from api_helpers import auth
from conftest_v06 import v06_seed  # noqa: F401  (fixture)
from raincli_server import identity
from raincli_server.models import AgentCredential, AgentPresence, MachineAgent, Message
from raincli_server.web import thread
from test_app_mode import NAV, WEBVIEW, handoff_code


@pytest.fixture
def maya(app, client, v06_seed):  # noqa: F811
    webview = TestClient(app, headers=WEBVIEW)
    r = webview.get(f"/app/handoff?code={handoff_code(client, v06_seed['person_tokens']['Maya'])}", headers=NAV,
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    yield webview
    webview.close()


def live(webview, **params):
    r = webview.get("/app/live", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def to_jon(session, v06_seed, body="to Jon", n=1):  # noqa: F811
    """Messages Maya writes in the app to Jon, stored as the inbox path stores them (Jon's machine and agent)."""
    s = v06_seed
    out = []
    for i in range(n):
        msg = Message(id=uuid.uuid4(), team_id=s["teams"]["northwind"].id,
                      conversation_id=s["conversations"]["jon"].id, body=f"{body} {i}", kind="message",
                      route="inbox", sender_user_id=s["people"]["Maya"].id, recipient_agent_id=s["machines"]["jon"].id,
                      recipient_agent_name="zq-agent-jon-api", delivery_state="stored")
        session.add(msg)
        out.append(msg)
    session.commit()
    return out


# The trail (§19.7, §21 C7.5) -----------------------------------------------------------------------

@pytest.mark.parametrize("kind,steps", [("agent", ["stored", "received", "submitted", "replied"]),
                                        ("machine", ["stored", "received", "submitted", "replied"]),
                                        ("person", ["stored", "received", "replied"])])
def test_the_trail_has_the_steps_of_the_recipient_kind(kind, steps):
    msg = SimpleNamespace(delivery_state="stored", acked_at=None, created_at=identity.now(), delivery_updated_at=None)
    trail = thread.trail_for(msg, SimpleNamespace(kind=kind), [], "stored", None, "Jon")
    assert [s.name for s in trail.steps] == steps and [s.reached for s in trail.steps] == [True] + [False] * (len(steps) - 1)
    assert trail.phrase == "Delivery: stored, not yet received"


def test_holds_and_outcomes_are_worded_without_agents():
    agent = SimpleNamespace(kind="agent")
    acked = SimpleNamespace(delivery_state="held", acked_at=identity.now(), created_at=identity.now(),
                            delivery_updated_at=None)
    held = thread.trail_for(acked, agent, [], "held", "approval_required", "Jon")
    assert [s.name for s in held.steps] == ["stored", "received"] and held.outcome == "held" and held.tone == "warn"
    assert held.reason == "waiting for Jon's approval" and held.phrase == "Delivery: held, waiting for Jon's approval"
    stored = SimpleNamespace(delivery_state="stored", acked_at=None, created_at=identity.now(), delivery_updated_at=None)
    assert thread.trail_for(stored, agent, [], "stored", "busy", "Jon").reason == "Jon's inbox is busy"
    assert thread.trail_for(stored, agent, [], "stored", "busy", "Jon", route="reply").reason == "Jon's agent is busy"
    assert thread.trail_for(stored, SimpleNamespace(kind="person"), [], "stored", "no_inbox", "Sam").reason == \
        "waiting for Sam to pick an inbox"
    for state, outcome, tone, words in (("submission_uncertain", "uncertain", "warn", "it may not have reached Jon's inbox"),
                                        ("rejected", "rejected", "danger", "Jon's inbox declined it")):
        msg = SimpleNamespace(delivery_state=state, acked_at=identity.now(), created_at=identity.now(),
                              delivery_updated_at=None)
        trail = thread.trail_for(msg, agent, [], state, None, "Jon")
        assert (trail.outcome, trail.tone, trail.reason) == (outcome, tone, words)
        assert "session" not in trail.reason
    replied = SimpleNamespace(delivery_state="replied", acked_at=identity.now(), created_at=identity.now(),
                              delivery_updated_at=None)
    assert all(s.reached for s in thread.trail_for(replied, agent, [], "replied", None, "Jon").steps)


def test_the_thread_shows_trails_only_under_your_messages(maya, v06_seed):  # noqa: F811
    page = maya.get(f"/app/conversations/{v06_seed['conversations']['jon'].id}").text
    ask, reply = v06_seed["messages"]["ask"], v06_seed["messages"]["reply"]
    assert f'data-trail="{ask.id}"' in page and f'data-trail="{reply.id}"' not in page
    assert '<summary class="rc-trail-steps">' in page  # §20.4 R5: the trail is the toggle


# Live states (§19.8 M1) and H4 ----------------------------------------------------------------------

def test_live_reports_state_changes_of_your_own_messages(maya, client, session, v06_seed):  # noqa: F811
    msg, = to_jon(session, v06_seed)
    cid = str(v06_seed["conversations"]["jon"].id)
    since = live(maya, conversation=cid, after=msg.seq)["now"]
    r = client.post(f"/api/v1/messages/{msg.id}/ack", headers=auth(v06_seed["machine_tokens"]["jon"]))
    assert r.status_code == 200, r.text
    out = live(maya, conversation=cid, after=msg.seq, states_since=since)
    states = {s["id"]: s for s in out["states"]}
    assert states[str(msg.id)]["state"] == "received"
    assert "Delivery: received, not yet submitted" in states[str(msg.id)]["html"]


def test_a_person_reading_in_their_app_changes_nothing_the_sender_sees(maya, client, session, v06_seed):  # noqa: F811
    """§21 C13 H4: a message delivered in Jon's app shows received to Maya, whether or not Jon has read it."""
    s = v06_seed
    msg = Message(id=uuid.uuid4(), team_id=s["teams"]["northwind"].id, conversation_id=s["conversations"]["jon"].id,
                  body="in his app", kind="message", route="person", sender_user_id=s["people"]["Maya"].id,
                  recipient_user_id=s["people"]["Jon"].id, delivery_state="stored")
    session.add(msg)
    session.commit()
    cid = str(s["conversations"]["jon"].id)
    before = maya.get(f"/app/conversations/{cid}").text
    session.execute(update(Message).where(Message.id == msg.id).values(acked_at=identity.now(), delivery_state="received"))
    session.commit()
    after = maya.get(f"/app/conversations/{cid}").text

    def trail(page):
        return re.search(rf'data-trail="{msg.id}" data-state="([a-z_]+)"', page).group(1)
    assert trail(before) == trail(after) == "received"

    def received_time(page):
        block = re.search(rf'data-trail="{msg.id}".*?</details>', page, re.S).group(0)
        return re.findall(r'data-step-time="([^"]+)"', block)
    assert received_time(before) == received_time(after)  # no read time leaks through the step times


# Header presence (§21 C5.4) -------------------------------------------------------------------------

def test_the_header_shows_the_persons_presence_and_speed_with_the_offline_rule(maya, session, v06_seed):  # noqa: F811
    cid = str(v06_seed["conversations"]["jon"].id)
    header = live(maya, conversation=cid)["header"]
    assert header["chip"] == "Instant" and header["word"] == "Ready"
    jon = v06_seed["machines"]["jon"].id
    session.execute(update(MachineAgent).where(MachineAgent.agent_id == jon).values(status="working"))
    session.commit()
    assert live(maya, conversation=cid)["header"]["word"] == "Busy"
    old = identity.now() - timedelta(seconds=121)
    session.execute(update(AgentPresence).where(AgentPresence.agent_id == jon).values(seen_at=old))
    session.commit()
    header = live(maya, conversation=cid)["header"]
    assert header["word"] == "Offline" and header["chip"] is None
    sam = live(maya, conversation=str(v06_seed["conversations"]["sam"].id))["header"]
    assert sam["word"] == "No inbox yet" and sam["chip"] is None
    own = live(maya, conversation=str(v06_seed["conversations"]["self"].id))["header"]
    assert own["word"] is None  # your own agents: no presence


def test_presence_follows_the_clock(session, v06_seed):  # noqa: F811
    jon, team = v06_seed["people"]["Jon"], v06_seed["teams"]["northwind"]
    seen = session.get(AgentPresence, v06_seed["machines"]["jon"].id).seen_at
    assert thread.header_presence(session, team.id, jon, now=seen + timedelta(seconds=119)).word == "Ready"
    late = thread.header_presence(session, team.id, jon, now=seen + timedelta(seconds=121))
    assert late.word == "Offline" and late.chip is None


# Warnings (§21 C7.4) ---------------------------------------------------------------------------------

def used(session, machine, when):
    session.execute(update(AgentCredential).where(AgentCredential.agent_id == machine.id).values(last_used_at=when))
    session.execute(update(AgentPresence).where(AgentPresence.agent_id == machine.id)
                    .values(seen_at=when or identity.now() - timedelta(days=30)))
    session.commit()


def test_warning_precedence_and_the_24_hour_clock(session, v06_seed):  # noqa: F811
    s = v06_seed
    team, jon, machine = s["teams"]["northwind"].id, s["people"]["Jon"], s["machines"]["jon"]
    for m in session.query(Message).filter(Message.recipient_agent_id == machine.id):
        m.acked_at = identity.now()
    session.commit()
    now = identity.now()
    used(session, machine, now)
    assert thread.person_warning(session, team, jon, now=now + timedelta(hours=23)) is None
    late = thread.person_warning(session, team, jon, now=now + timedelta(hours=25))
    assert late.kind == "not_connected" and late.name == "Jon" and late.since is not None
    session.execute(update(AgentCredential).where(AgentCredential.agent_id == machine.id).values(last_used_at=None))
    session.execute(update(AgentPresence).where(AgentPresence.agent_id == machine.id).values(seen_at=now))
    session.commit()
    assert thread.person_warning(session, team, jon, now=now) is None  # a presence report counts as contact
    assert thread.person_warning(session, team, s["people"]["Sam"]).kind == "no_inbox"
    assert thread.person_warning(session, s["teams"]["contoso"].id, jon).kind == "no_inbox"  # per team (H3)


def test_the_warning_shows_in_the_header_on_compose_and_on_the_website(app, maya, client, session, v06_seed):  # noqa: F811
    from test_web_app import login

    s = v06_seed
    used(session, s["machines"]["jon"], identity.now() - timedelta(days=3))
    cid = str(s["conversations"]["jon"].id)
    assert "<strong>Jon</strong>'s inbox hasn't connected since" in maya.get(f"/app/conversations/{cid}").text
    assert live(maya, conversation=cid)["warning"]["kind"] == "not_connected"
    assert "<strong>Jon</strong>'s inbox hasn't connected since" in maya.get("/app/compose?to=jon@example.test").text
    warning = live(maya, conversation=cid)["warning"]["html"]
    assert re.search(r'since <time datetime="[^"]+" data-format="date">\d{4}-\d\d-\d\d</time>\. They', warning)
    with TestClient(app) as browser:
        assert login(browser, "maya@example.test", "correct horse battery").status_code == 303
        assert "<strong>Jon</strong>'s inbox hasn't connected since" in browser.get(f"/app/conversations/{cid}").text


# Cost (§19.1, §19.8 M1) -----------------------------------------------------------------------------

def test_a_poll_costs_the_same_however_many_of_your_messages_are_in_flight(maya, app, session, v06_seed):  # noqa: F811
    counts = []
    cid = str(v06_seed["conversations"]["jon"].id)
    for n in (3, 30):
        sent = to_jon(session, v06_seed, f"in flight {n}", n)
        seen = []

        def count(conn, cursor, statement, *args):
            seen.append(statement)
        event.listen(app.state.engine, "before_cursor_execute", count)
        try:
            out = live(maya, conversation=cid, after=sent[-1].seq, states_since=identity.now().isoformat())
        finally:
            event.remove(app.state.engine, "before_cursor_execute", count)
        assert len(out["states"]) >= n
        counts.append(len(seen))
    assert counts[1] <= counts[0] + 2, counts
