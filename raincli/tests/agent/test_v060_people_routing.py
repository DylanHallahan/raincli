"""§21 against the v0.6 fake (fake_server.py mirrors raincli_server.messaging/presentation): the client addresses
people, a refused machine or agent form is printed as the server's sentence, replies are routed from in_reply_to,
message JSON is per reader, and the inbox setting moves waiting and unpicked messages."""

import json

import pytest

from raincli_agent import cli
from raincli_agent.errors import Invalid

from .conftest import client_for, send

REVIEWER = {"key": "r" * 16, "name": "reviewer", "type": "claude", "status": "idle", "role": None,
            "reachability": "instant", "source": "herdr"}


# -- C2: addressing --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("to", ["bob", "bob/reviewer", {"machine": "bob"}, {"machine": "bob", "agent": "x"}])
def test_a_teammates_machine_form_is_refused_with_its_person(fake_api, to):
    with pytest.raises(Invalid) as err:
        send(fake_api, fake_api.alice, to)
    assert err.value.code == "address_people"
    assert str(err.value).endswith("Address people: send to Bob Example (bob@example.com)")
    assert fake_api.state.messages == {}


@pytest.mark.parametrize("to", ["alice", {"machine": "alice"}, "nobody", "nobody@example.com", "eve@example.com",
                                "eve"])
def test_own_unknown_and_other_team_forms_get_the_generic_refusal(fake_api, to):
    with pytest.raises(Invalid) as err:
        send(fake_api, fake_api.alice, to)
    assert err.value.code == "address_people"
    assert str(err.value).endswith("Address people: send to a teammate's name or email")


@pytest.mark.parametrize("to", ["bob@example.com", "@BOB@example.com", "Bob Example", " bob example ",
                                {"person": "bob@example.com"}, {"person_name": "Bob Example"}])
def test_every_person_form_reaches_bob(fake_api, to):
    msg = send(fake_api, fake_api.alice, to)
    [got], _ = client_for(fake_api, fake_api.bob).inbox()
    assert got["id"] == msg["id"] and msg["to"] == "@bob@example.com"


def test_the_cli_prints_the_refusal_and_the_ambiguous_people(fake_api, as_agent, capsys):
    st = fake_api.state
    st.add_agent("sam-laptop", owner="sam1@example.com", owner_name="Sam")
    st.add_agent("sam-desk", owner="sam2@example.com", owner_name="Sam")
    as_agent(fake_api.alice)
    assert cli.main(["send", "--to", "bob/reviewer", "--body", "x"]) == 1
    assert "Address people: send to Bob Example (bob@example.com)" in capsys.readouterr().err
    assert cli.main(["send", "--to", "sam", "--body", "x"]) == 1
    err = capsys.readouterr().err
    assert "More than one teammate is called Sam: use their email" in err
    assert "raincli send @sam1@example.com" in err and "raincli send @sam2@example.com" in err
    assert st.messages == {}


# -- C5.2: message JSON per reader -----------------------------------------------------------------------------

def test_message_json_shows_people_and_the_readers_own_machine(fake_api):
    msg = client_for(fake_api, fake_api.alice).send("bob@example.com", "hi", from_agent="planner")[0]
    # The sender's own view: its machine and stated agent, the recipient as a person.
    assert msg["from"] == "alice" and msg["from_endpoint"] == {"machine": "alice", "agent": "planner"}
    assert msg["to"] == "@bob@example.com"
    assert msg["to_endpoint"] == {"person": "bob@example.com", "display_name": "Bob Example"}
    assert msg["from_agent"] == "planner" and msg["from_kind"] == "agent" and msg["from_owner"] is None
    [got], _ = client_for(fake_api, fake_api.bob).inbox()
    assert got["from"] == "@alice@example.com"
    assert got["from_endpoint"] == {"person": "alice@example.com", "display_name": "Alice Example"}
    assert got["to"] == "bob" and got["to_endpoint"] == {"machine": "bob"}
    assert got["from_agent"] is None and got["from_owner"] is None and "planner" not in json.dumps(got)


# -- C4: replies -----------------------------------------------------------------------------------------------

def test_a_reply_goes_back_to_the_stated_agent_while_it_is_live(fake_api):
    alice, bob = client_for(fake_api, fake_api.alice), client_for(fake_api, fake_api.bob)
    parent = alice.send("bob@example.com", "question?", from_agent="planner")[0]
    reply = bob.send(None, "answer", in_reply_to=parent["id"])[0]
    assert reply["to"] == "@alice@example.com" and reply["conversation_id"] == parent["conversation_id"]
    assert alice.inbox()[0] == []  # not the machine's inbox: the planner's
    [got], _ = alice.inbox(routing=True)
    assert got["id"] == reply["id"] and got["to_endpoint"] == {"machine": "alice", "agent": "planner"}
    assert fake_api.state.messages[parent["id"]]["delivery_state"] == "replied"


def test_a_reply_to_an_agent_that_is_gone_goes_to_the_persons_inbox(fake_api):
    fake_api.state.set_offline("alice", "planner")
    alice, bob = client_for(fake_api, fake_api.alice), client_for(fake_api, fake_api.bob)
    parent = alice.send("bob@example.com", "question?", from_agent="planner")[0]
    reply = bob.send(None, "answer", in_reply_to=parent["id"])[0]
    [got], _ = alice.inbox()
    assert got["id"] == reply["id"] and got["to_endpoint"] == {"machine": "alice"}


def test_a_replys_to_must_name_the_other_person(fake_api):
    bob = client_for(fake_api, fake_api.bob)
    parent = send(fake_api, fake_api.alice, "bob@example.com", "question?")
    assert bob.send("alice", "old client form", in_reply_to=parent["id"])[0]["to"] == "@alice@example.com"
    with pytest.raises(Invalid, match="a reply goes to the other person"):
        bob.send("mallory@example.com", "x", in_reply_to=parent["id"])


# -- C3, C6, C8: no inbox, waiting, picking an inbox -----------------------------------------------------------

def test_no_inbox_waits_then_picking_one_delivers_it(fake_api, as_agent, capsys):
    alice, bob = client_for(fake_api, fake_api.alice), client_for(fake_api, fake_api.bob)
    assert bob.clear_my_inbox() == {"inbox": None, "moved": 0, "stayed": 0, "delivered_waiting": 0}
    msg = send(fake_api, fake_api.alice, "bob@example.com", "while you were out")
    assert (msg["delivery_state"], msg["hold_reason"]) == ("held", "no_inbox")
    people = {p["email"]: p for p in alice.people()}
    assert (people["bob@example.com"]["presence"], people["bob@example.com"]["speed"]) == ("no_inbox", None)
    _, cursor = bob.inbox()
    assert bob.my_inbox() == {"inbox": None, "candidates": [], "waiting": 1}
    bob.report_presence("ready", agents=[REVIEWER])
    as_agent(fake_api.bob)
    assert cli.main(["inbox", "set", "reviewer"]) == 0
    out = capsys.readouterr().out
    assert "Your inbox: reviewer on bob (this computer)" in out and "delivered 1 waiting" in out
    [got], _ = bob.inbox(after=cursor + 5, routing=True)  # C13 H1: redelivered below the cursor
    assert got["id"] == msg["id"] and got["to_endpoint"] == {"machine": "bob", "agent": "reviewer"}
    assert alice.get_message(msg["id"])["delivery_state"] == "stored"
    people = {p["email"]: p for p in alice.people()}
    assert (people["bob@example.com"]["presence"], people["bob@example.com"]["speed"]) == ("ready", "instant")
    # The same pick again moves nothing.
    assert bob.set_my_inbox("reviewer")["moved"] == 0


def test_an_inbox_on_another_machine_or_an_unknown_agent_is_refused(fake_api, as_agent, capsys):
    bob = client_for(fake_api, fake_api.bob)
    with pytest.raises(Invalid) as err:
        bob.set_my_inbox("reviewer", machine="alice")
    assert err.value.code == "inbox_invalid" and "alice can't be your inbox" in str(err.value)
    with pytest.raises(Invalid, match="bob can't be your inbox"):
        bob.set_my_inbox("not-running")


def test_agents_lists_only_the_callers_own_machines(fake_api):
    fake_api.state.add_agent("bob-desk", owner="bob@example.com")
    handles = [a["handle"] for a in client_for(fake_api, fake_api.bob).agents()]
    assert handles == ["bob", "bob-desk"]


def test_event_details_are_codes_only(fake_api):
    msg = send(fake_api, fake_api.alice, "bob@example.com")
    bob = client_for(fake_api, fake_api.bob)
    bob.ack(msg["id"])
    with pytest.raises(Invalid, match="event codes"):
        bob.event(msg["id"], "held", "herdr says reviewer is busy in /home/bob")
    held = bob.event(msg["id"], "held", "busy")
    assert held["hold_reason"] == "busy"  # bob owns the recipient machine: the raw code
    assert client_for(fake_api, fake_api.alice).get_message(msg["id"])["hold_reason"] == "inbox_busy"
