"""The v0.6.0 CLI (§21 C9.2) against the real v0.6 server: person addressing, ``people``, the ``agents`` alias,
``inbox show|set|clear``, replies without ``to``, and plain ``raincli inbox`` still listing messages.

A real uvicorn server on the throwaway test database (the ``live`` pattern of tests/gui/test_v054_list_gui.py),
seeded with the shared v0.6 seed (tests/conftest_v06.py). The CLI runs in-process with ``--config``.
"""

from __future__ import annotations

import io
import json
import socket
import threading
import time
from dataclasses import replace

import pytest

from conftest_v06 import TEAMMATE_STRINGS, _machine, v06_seed  # noqa: F401  (fixture)
from raincli_agent import cli
from raincli_agent.api import ApiClient
from raincli_agent.config import write_config


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
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.delenv("RAINCLI_CONFIG", raising=False)
    return path


@pytest.fixture
def cw(live, v06_seed, home, tmp_path):  # noqa: F811
    configs = {}
    for key, token in v06_seed["machine_tokens"].items():
        path = tmp_path / "machines" / key / "agent.json"
        write_config(str(path), live, token)
        assert (path.stat().st_mode & 0o777) == 0o600
        configs[key] = str(path)
    return {**v06_seed, "url": live, "configs": configs}


def run(cw, who, *argv, stdin=None, monkeypatch=None, capsys=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = cli.main(["--config", cw["configs"][who], *argv])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


@pytest.fixture
def sent_bodies(monkeypatch):
    """Every request body the client sends to a send endpoint."""
    bodies = []
    real = ApiClient.request

    def spy(self, method, path, **kwargs):
        if method == "POST" and path in ("/messages", "/person/send"):
            bodies.append(kwargs.get("body"))
        return real(self, method, path, **kwargs)
    monkeypatch.setattr(ApiClient, "request", spy)
    return bodies


def stored(session, mid):
    from raincli_server.models import Message

    session.rollback()
    return session.get(Message, mid, populate_existing=True)


# -- raincli send <person> -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("to", ["jon@example.test", "@jon@example.test", "JON@example.test"])
def test_send_by_email(cw, session, sent_bodies, to, monkeypatch, capsys):
    rc, out, err = run(cw, "maya", "send", to, "--body-file", "-", stdin="Can you look?",
                       monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0, err
    assert sent_bodies[-1]["to"] == {"person": to.lstrip("@")}
    assert "to Jon (jon@example.test)" in out
    mid = out.split("sent: ", 1)[1].split()[0]
    msg = stored(session, mid)
    assert (msg.recipient_agent_id, msg.recipient_agent_name) == (cw["machines"]["jon"].id, "zq-agent-jon-api")


@pytest.mark.parametrize("to", ["Priya", "priya", " Priya "])
def test_send_by_unique_name(cw, session, sent_bodies, to, monkeypatch, capsys):
    rc, out, err = run(cw, "maya", "send", to, "--body-file", "-", stdin="Hi",
                       monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0, err
    assert sent_bodies[-1]["to"] == {"person_name": to.strip()}
    assert "to Priya (priya@example.test)" in out
    assert not any(s in out for s in TEAMMATE_STRINGS)


def test_send_to_a_person_without_an_inbox_waits_in_their_app(cw, monkeypatch, capsys):
    rc, out, err = run(cw, "jon", "send", "Sam", "--body-file", "-", "--json", stdin="Hi Sam",
                       monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0, err
    message = json.loads(out)["message"]
    assert (message["delivery_state"], message["hold_reason"]) == ("held", "no_inbox")


@pytest.mark.parametrize("to", ["zq-handle-jon", "zq-handle-jon/zq-agent-jon-api"])
def test_a_handle_is_refused_with_the_address_people_text(cw, sent_bodies, to, monkeypatch, capsys):
    rc, out, err = run(cw, "maya", "send", to, "--body-file", "-", stdin="x", monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 1
    assert "raincli: error: Address people: send to Jon (jon@example.test)" in err
    assert "400" not in err.splitlines()[-1]  # the server's sentence, not the protocol code
    assert "was not stored (address_people)" in err and "may be stored" not in err
    # The hidden older --to passes the text form; the server refuses it the same way.
    rc, out, err = run(cw, "maya", "send", "--to", to, "--body", "x", monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 1 and "Address people: send to Jon (jon@example.test)" in err


def test_an_unknown_name_and_your_own_handle_get_the_generic_text(cw, monkeypatch, capsys):
    for to in ("Nobody", "zq-handle-maya"):
        rc, out, err = run(cw, "maya", "send", to, "--body-file", "-", stdin="x",
                           monkeypatch=monkeypatch, capsys=capsys)
        assert rc == 1
        assert "Address people: send to a teammate's name or email" in err


def test_the_positional_person_takes_the_body_from_a_file_only(cw, monkeypatch, capsys):
    rc, out, err = run(cw, "maya", "send", "Jon", "--body", "x", monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 2 and "--body-file" in err


def test_an_ambiguous_name_lists_the_matching_people(cw, session, sent_bodies, monkeypatch, capsys):
    from raincli_server import identity

    other = identity.create_user(session, "jon.b@example.test", "Jon", "correct horse battery")
    identity.add_member(session, cw["teams"]["northwind"], other)
    session.commit()
    rc, out, err = run(cw, "maya", "send", "jon", "--body-file", "-", stdin="x", monkeypatch=monkeypatch,
                       capsys=capsys)
    assert rc == 1 and out == ""
    lines = err.splitlines()
    assert "raincli: error: More than one teammate is called Jon: use their email" in lines
    assert "raincli:   Jon (jon@example.test)  ->  raincli send @jon@example.test" in lines
    assert "raincli:   Jon (jon.b@example.test)  ->  raincli send @jon.b@example.test" in lines
    rc, out, err = run(cw, "maya", "send", "jon.b@example.test", "--body-file", "-", stdin="x",
                       monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0 and "(jon.b@example.test) (state: " in out


# -- raincli people / agents ------------------------------------------------------------------------------------

PEOPLE = {
    "Jon (jon@example.test)": "Ready · Instant",
    "Priya (priya@example.test)": "Busy · Next turn",
    "Ryan (ryan@example.test)": "Offline",
    "Sam (sam@example.test)": "No inbox yet",
    "Maya (maya@example.test)": "No inbox yet",
}


def _people_lines(out):
    found = {}
    for line in out.splitlines():
        for label, text in PEOPLE.items():
            if line.strip().startswith(label):
                found[label] = line[line.index(label) + len(label):].strip()
    return found


def test_people_shows_presence_and_speed(cw, monkeypatch, capsys):
    rc, out, err = run(cw, "maya", "people", monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0, err
    assert out.splitlines()[0] == "People in Northwind"
    assert _people_lines(out) == PEOPLE
    assert any(line.strip().startswith("Olive (olive@example.test)") for line in out.splitlines())
    assert not any(s in out for s in TEAMMATE_STRINGS)
    rc, out, err = run(cw, "maya", "people", "--json", monkeypatch=monkeypatch, capsys=capsys)
    people = {p["email"]: p for p in json.loads(out)["people"]}
    assert (people["jon@example.test"]["presence"], people["jon@example.test"]["speed"]) == ("ready", "instant")
    assert people["sam@example.test"]["presence"] == "no_inbox"
    assert "lee@example.test" not in people  # another team


def test_agents_is_an_alias_that_prints_people_with_a_notice(cw, monkeypatch, capsys):
    rc, out, err = run(cw, "maya", "agents", monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0, err
    lines = out.splitlines()
    assert lines[0] == "raincli agents now lists people: address people, not agents (see raincli people)"
    assert lines[1] == "People in Northwind"
    assert _people_lines(out) == PEOPLE
    assert not any(s in out for s in TEAMMATE_STRINGS)
    rc, out, err = run(cw, "maya", "agents", "--json", monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0
    assert {p["email"] for p in json.loads(out)["people"]} >= {"jon@example.test", "sam@example.test"}
    assert "address people, not agents" in err


# -- raincli inbox show / set / clear -----------------------------------------------------------------------------

def test_inbox_show(cw, monkeypatch, capsys):
    rc, out, err = run(cw, "maya", "inbox", "show", monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0, err
    lines = out.splitlines()
    assert lines[0] == "No inbox yet: messages to you wait in your app"
    assert lines[1] == "Waiting in your app: 1 message (raincli inbox set AGENT delivers them)"
    assert "Agents you can pick (raincli inbox set AGENT):" in lines
    assert any(line.startswith("  zq-agent-maya-laptop on zq-handle-maya (this computer)  (live")
               for line in lines)
    rc, out, err = run(cw, "jon", "inbox", "show", monkeypatch=monkeypatch, capsys=capsys)
    assert out.splitlines()[0] == "Your inbox: zq-agent-jon-api on zq-handle-jon (this computer)"
    rc, out, err = run(cw, "jon", "inbox", "show", "--json", monkeypatch=monkeypatch, capsys=capsys)
    data = json.loads(out)
    assert data["inbox"]["agent"] == "zq-agent-jon-api" and data["waiting"] == 0


def test_inbox_set_moves_and_delivers_then_clear(cw, session, monkeypatch, capsys):
    by_hand = cw["messages"]["by_hand"].id  # Jon wrote it in the app; it waits for Maya
    rc, out, err = run(cw, "maya", "inbox", "set", "zq-agent-maya-laptop", monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0, err
    assert out.splitlines() == [
        "Your inbox: zq-agent-maya-laptop on zq-handle-maya (this computer)",
        "moved 0 from your previous inbox, 0 stayed there (already picked up), delivered 1 waiting"]
    assert stored(session, by_hand).recipient_agent_name == "zq-agent-maya-laptop"
    rc, out, err = run(cw, "maya", "inbox", "set", "zq-agent-maya-notes", "--no-deliver-waiting", "--json",
                       monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0, err
    result = json.loads(out)
    assert result["inbox"]["agent"] == "zq-agent-maya-notes" and result["moved"] == 1
    assert stored(session, by_hand).recipient_agent_name == "zq-agent-maya-notes"
    rc, out, err = run(cw, "maya", "inbox", "clear", monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0, err
    assert out.splitlines() == ["No inbox: messages to you now wait in your app", "moved 1 message back to your app"]
    assert stored(session, by_hand).recipient_user_id == cw["people"]["Maya"].id
    rc, out, err = run(cw, "maya", "inbox", "show", monkeypatch=monkeypatch, capsys=capsys)
    assert out.splitlines()[0] == "No inbox yet: messages to you wait in your app"


def test_inbox_set_an_agent_that_cannot_receive_prints_the_server_message(cw, monkeypatch, capsys):
    rc, out, err = run(cw, "maya", "inbox", "set", "no-such-agent", monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 1
    assert ("raincli: error: zq-handle-maya can't be your inbox: pick one of your own agents that can receive "
            "messages") in err


def test_inbox_set_on_another_machine_needs_a_person_session(cw, session, monkeypatch, capsys):
    from raincli_agent import person

    second, _ = _machine(session, cw["teams"]["northwind"], cw["people"]["Maya"], "zq-handle-maya-two",
                         [("desk-agent", "instant", "idle")])
    session.commit()
    rc, out, err = run(cw, "maya", "inbox", "set", "desk-agent", "--machine", "zq-handle-maya-two",
                       monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 1
    assert "this machine's credential can pick only an agent on this machine" in err
    assert "raincli login --person" in err and "app" in err

    person.save_session(cw["configs"]["maya"], cw["person_tokens"]["Maya"])
    rc, out, err = run(cw, "maya", "inbox", "set", "desk-agent", "--machine", "zq-handle-maya-two",
                       monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0, err
    assert out.splitlines()[0] == "Your inbox: desk-agent on zq-handle-maya-two"
    rc, out, err = run(cw, "maya", "inbox", "show", monkeypatch=monkeypatch, capsys=capsys)
    assert out.splitlines()[0] == "Your inbox: desk-agent on zq-handle-maya-two"
    # A teammate's machine is refused, without saying whose it is.
    rc, out, err = run(cw, "maya", "inbox", "set", "zq-agent-jon-api", "--machine", "zq-handle-jon",
                       monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 1 and "That machine can't be your inbox" in err


def test_the_machine_api_refuses_another_machine(cw):
    """C13 L4 at the protocol level: the client never sends this, but the server refuses it."""
    from raincli_agent.errors import ApiError

    api = ApiClient(cw["url"], cw["machine_tokens"]["maya"])
    with pytest.raises(ApiError) as exc:
        api.set_my_inbox("zq-agent-maya-ct", machine="zq-handle-maya-ct")
    assert exc.value.code == "inbox_invalid"


# -- raincli reply / me reply: no ``to`` (C4) --------------------------------------------------------------------

def test_reply_sends_no_to_and_goes_back_to_the_agent_that_asked(cw, session, sent_bodies, monkeypatch, capsys):
    ask = cw["messages"]["ask"]  # Maya's zq-agent-maya-laptop asked Jon
    rc, out, err = run(cw, "jon", "reply", str(ask.id), "--body-file", "-", "--from-agent", "zq-agent-jon-api",
                       stdin="Done.", monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0, err
    body = sent_bodies[-1]
    assert "to" not in body and body["in_reply_to"] == str(ask.id) and body["from_agent"] == "zq-agent-jon-api"
    assert "to Maya (maya@example.test)" in out
    msg = stored(session, out.split("sent: ", 1)[1].split()[0])
    assert (msg.recipient_agent_id, msg.recipient_agent_name) == (cw["machines"]["maya"].id,
                                                                   "zq-agent-maya-laptop")
    assert msg.route == "reply"


def test_reply_to_an_app_message_goes_to_that_persons_inbox(cw, session, sent_bodies, monkeypatch, capsys):
    by_hand = cw["messages"]["by_hand"]  # Jon wrote it in the app
    rc, out, err = run(cw, "maya", "reply", str(by_hand.id), "--body", "Thanks", monkeypatch=monkeypatch,
                       capsys=capsys)
    assert rc == 0, err
    assert "to" not in sent_bodies[-1]
    msg = stored(session, out.split("sent: ", 1)[1].split()[0])
    assert (msg.recipient_agent_id, msg.recipient_agent_name) == (cw["machines"]["jon"].id, "zq-agent-jon-api")


def test_me_send_and_me_reply_address_people(cw, session, sent_bodies, monkeypatch, capsys):
    from raincli_agent import person

    person.save_session(cw["configs"]["maya"], cw["person_tokens"]["Maya"])
    rc, out, err = run(cw, "maya", "me", "send", "Jon", "--team", "northwind", "--body-file", "-",
                       stdin="From me", monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0, err
    assert sent_bodies[-1]["to"] == {"person_name": "Jon"} and "to Jon (jon@example.test)" in out
    rc, out, err = run(cw, "maya", "me", "send", "zq-handle-jon", "--team", "northwind", "--body-file", "-",
                       stdin="x", monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 1 and "Address people: send to Jon (jon@example.test)" in err
    assert "was not stored (address_people)" in err and "may be stored" not in err
    by_hand = cw["messages"]["by_hand"]
    rc, out, err = run(cw, "maya", "me", "reply", str(by_hand.id), "--body-file", "-", stdin="Reply as me",
                       monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0, err
    assert "to" not in sent_bodies[-1] and sent_bodies[-1]["in_reply_to"] == str(by_hand.id)
    msg = stored(session, out.split("sent: ", 1)[1].split()[0])
    assert msg.recipient_agent_name == "zq-agent-jon-api"


# -- plain raincli inbox still lists messages ----------------------------------------------------------------------

def test_plain_inbox_still_lists_received_messages(cw, session, monkeypatch, capsys):
    from raincli_server.models import Agent

    session.get(Agent, cw["machines"]["olive"].id).routing = "inbox-only"  # C3.1(a): the machine endpoint
    session.commit()
    rc, out, err = run(cw, "maya", "send", "Olive", "--body-file", "-", stdin="For the machine inbox",
                       monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0, err
    to_olive = out.split("sent: ", 1)[1].split()[0]
    rc, out, err = run(cw, "maya", "send", "Jon", "--body-file", "-", stdin="For the named agent",
                       monkeypatch=monkeypatch, capsys=capsys)
    to_jon = out.split("sent: ", 1)[1].split()[0]

    rc, out, err = run(cw, "olive", "inbox", monkeypatch=monkeypatch, capsys=capsys)
    assert rc == 0, err
    assert f"--- message {to_olive} " in out and "| For the machine inbox" in out
    rc, out, err = run(cw, "olive", "inbox", "--json", monkeypatch=monkeypatch, capsys=capsys)
    assert [json.loads(line)["id"] for line in out.splitlines()] == [to_olive]

    rc, out, err = run(cw, "jon", "inbox", "--agent", "zq-agent-jon-api", "--json", monkeypatch=monkeypatch,
                       capsys=capsys)
    assert rc == 0, err
    assert to_jon in [json.loads(line)["id"] for line in out.splitlines()]
    rc, out, err = run(cw, "jon", "inbox", monkeypatch=monkeypatch, capsys=capsys)
    assert to_jon not in out  # to the named agent, not the machine endpoint
