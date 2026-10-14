"""End-to-end: the person CLI (protocol §16.3, §16.4, §16.11, §16.12 C9, C10) against a real
uvicorn server and real PostgreSQL. Runs only when RAINCLI_TEST_DATABASE_URL is set."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import pytest

from raincli_agent import person
from raincli_agent.api import ApiClient
from raincli_agent.config import Secret

from test_two_agents import ROOT, server  # noqa: F401 - the real-server fixture (same directory)

PASSWORD = "correct horse battery"


@pytest.fixture
def machines(world, server, tmp_path):  # noqa: F811
    """One directory per machine, so each has its own person.json beside agent.json."""
    configs = {}
    for who in ("alice", "bob"):
        folder = tmp_path / who
        folder.mkdir(mode=0o700)
        path = folder / "agent.json"
        path.write_text(json.dumps({"api_url": server, "token": world["tokens"][who]}))
        path.chmod(0o600)
        configs[who] = path
    return configs


def cli(configs, who, *args, check=True, input=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("RAINCLI_")}
    env["RAINCLI_CONFIG"] = str(configs[who])
    proc = subprocess.run([sys.executable, "-m", "raincli_agent", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=90, input=input)
    if check and proc.returncode != 0:
        pytest.fail(f"raincli {args} as {who} -> {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc


def lines(proc):
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def add_person(configs, who):
    person.add_session(str(configs[who]), f"{who}@example.test", Secret(PASSWORD))
    return person.load_session(str(configs[who])).reveal()


def test_person_round_trip_send_inbox_read_reply_fetch(machines, tmp_path):
    session = add_person(machines, "alice")
    # §21 C6: with no inbox, a message to alice waits for her in her app (the person inbox).
    cli(machines, "alice", "inbox", "clear")
    # A teammate's machine messages alice as a person, with an attachment.
    note = tmp_path / "notes.md"
    note.write_bytes(b"# notes\n")
    mid = str(uuid.uuid4())
    cli(machines, "bob", "send", "@alice@example.test", "--body-file", "-", "--id", mid, "--attach", str(note),
        input="Could you look at the release notes?")
    [msg] = lines(cli(machines, "alice", "me", "inbox", "--json"))
    assert msg["id"] == mid and msg["from"] == "@bob@example.test" and msg["to_endpoint"]["person"] == "alice@example.test"
    assert msg["from_endpoint"] == {"person": "bob@example.test", "display_name": "Bob"} and msg["from_kind"] == "agent"
    # --watch prints it and never marks it read.
    watched = lines(cli(machines, "alice", "me", "inbox", "--watch", "--once", "--timeout", "10", "--json"))
    assert [m["id"] for m in watched] == [mid]
    shown = cli(machines, "alice", "me", "read", mid).stdout
    assert "| Could you look at the release notes?" in shown and "bob-agent" not in shown
    assert lines(cli(machines, "alice", "me", "inbox", "--json")) == []  # reading acked it
    dest = tmp_path / "got"
    cli(machines, "alice", "me", "fetch", mid, "--attachment", "1", "--to", str(dest))
    assert (dest / "notes.md").read_bytes() == b"# notes\n"
    assert cli(machines, "alice", "me", "fetch", mid, "--attachment", "2", check=False).returncode != 0
    # The reply goes back to the machine that wrote.
    rid = str(uuid.uuid4())
    cli(machines, "alice", "me", "reply", mid, "--body-file", "-", "--id", rid, input="On it.")
    reply = json.loads(cli(machines, "bob", "show", rid, "--json").stdout)["message"]
    assert reply["from"] == "@alice@example.test" and reply["in_reply_to"] == mid and reply["to"] == "bob-agent"
    # The machine replies to the person with raincli reply: alice has no inbox, so it waits in her app.
    cli(machines, "bob", "reply", rid, "--body-file", "-", input="Thanks!")
    assert [m["body"] for m in lines(cli(machines, "alice", "me", "inbox", "--json"))] == ["Thanks!"]
    # Nothing printed the person session.
    for args in (("me", "inbox"), ("me", "read", mid)):
        assert session not in cli(machines, "alice", *args).stdout


def test_person_sends_to_a_person_and_bodies_never_come_from_argv(machines):
    add_person(machines, "alice")
    cli(machines, "alice", "me", "send", "bob@example.test", "--body-file", "-", input="hello from Alice")
    [got] = lines(cli(machines, "bob", "inbox", "--json"))  # bob's inbox: his machine (the inbox-mode path)
    assert got["body"] == "hello from Alice" and got["from"] == "@alice@example.test" and got["from_kind"] == "app"
    refused = cli(machines, "alice", "me", "send", "bob-agent", "--body-file", "-", input="x", check=False)
    assert refused.returncode != 0 and "Address people: send to Bob (bob@example.test)" in refused.stderr
    usage = cli(machines, "alice", "me", "send", "bob@example.test", "--body", "x", check=False)
    assert usage.returncode == 2
    usage = cli(machines, "bob", "send", "@alice@example.test", "--body", "x", check=False)
    assert usage.returncode != 0 and "--body-file" in usage.stderr


def test_a_person_session_sends_in_its_machines_team(machines, world, session):
    """§21.13: alice is in acme and globex; her session, from her acme machine, covers acme. No --team is needed,
    and --team globex is answered like an unknown team."""
    from raincli_server import identity
    identity.add_member(session, world["teams"]["globex"], world["users"]["alice"])
    session.commit()
    add_person(machines, "alice")
    cli(machines, "alice", "me", "send", "bob@example.test", "--body-file", "-", input="hi")
    cli(machines, "alice", "me", "send", "bob@example.test", "--team", "acme", "--body-file", "-", input="hi")
    other = cli(machines, "alice", "me", "send", "eve@example.test", "--team", "globex", "--body-file", "-",
                input="hi", check=False)
    unknown = cli(machines, "alice", "me", "send", "eve@example.test", "--team", "nope", "--body-file", "-",
                  input="hi", check=False)
    error = lambda r: r.stderr.strip().splitlines()[-1]  # noqa: E731  (the line before names the message id)
    assert other.returncode != 0 and error(other) == error(unknown) == "raincli: error: 400 invalid: unknown team"
    # A reply needs no --team: the server takes the parent conversation's team (§16.16 (3)).
    mid = str(uuid.uuid4())
    cli(machines, "bob", "send", "@alice@example.test", "--body-file", "-", "--id", mid, input="question")
    rid = str(uuid.uuid4())
    cli(machines, "alice", "me", "reply", mid, "--body-file", "-", "--id", rid, input="answer")
    assert json.loads(cli(machines, "bob", "show", rid, "--json").stdout)["message"]["in_reply_to"] == mid


def test_an_agent_address_is_refused_with_its_person_only(machines, world, server):  # noqa: F811
    """§21 C2.2, INV F5: an agent form is refused as address_people, naming the machine's person and nothing about
    the agent (pre-§21 this was not_deliverable, which told a teammate the agent's state)."""
    bob = ApiClient(server, world["tokens"]["bob"], max_attempts=1)
    bob.report_presence("ready", agents=[{"key": "k" * 16, "name": "reviewer", "type": "claude", "status": "idle",
                                          "reachability": "listed", "role": None, "source": "herdr"}],
                        client={"version": "0.5.0", "update_mode": "manual", "update_state": "current"})
    for to in ("bob-agent/reviewer", "bob-agent/nobody"):
        refused = cli(machines, "alice", "send", to, "--body-file", "-", input="hi", check=False)
        assert refused.returncode != 0
        assert "Address people: send to Bob (bob@example.test)" in refused.stderr
        assert "listed" not in refused.stderr and "known agent" not in refused.stderr
    assert cli(machines, "bob", "inbox", "--json").stdout.strip() == ""


def test_routing_and_named_agent_send(machines, world, server):  # noqa: F811
    """§21 C3.1b: bob picks a named agent as his inbox; a message to bob reaches it. C4: the reply goes back to the
    stated agent only while it is live, else to alice's inbox."""
    bob = ApiClient(server, world["tokens"]["bob"], max_attempts=1)
    bob.report_presence("ready", agents=[{"key": "k" * 16, "name": "reviewer", "type": "claude", "status": "idle",
                                          "reachability": "instant", "role": None, "source": "herdr"}],
                        client={"version": "0.5.0", "update_mode": "manual", "update_state": "current"})
    bob.inbox(routing=True)  # bob's connector is routing-capable (S1)
    picked = json.loads(cli(machines, "bob", "inbox", "set", "reviewer", "--json").stdout)
    assert picked["inbox"]["machine"] == "bob-agent" and picked["inbox"]["agent"] == "reviewer"
    mid = str(uuid.uuid4())
    cli(machines, "alice", "send", "bob@example.test", "--from-agent", "planner", "--id", mid, "--body-file", "-",
        input="please review")
    [msg] = lines(cli(machines, "bob", "inbox", "--agent", "reviewer", "--json"))
    assert msg["to_endpoint"] == {"machine": "bob-agent", "agent": "reviewer"}
    assert msg["from_agent"] is None and "planner" not in json.dumps(msg)  # only the sender's owner sees it (C5.2)
    assert cli(machines, "bob", "inbox", "--json").stdout.strip() == ""  # S1: the machine's inbox only
    # alice's planner isn't live (alice's machine reports no agents), so bob's answer goes to alice's inbox.
    cli(machines, "bob", "reply", mid, "--from-agent", "reviewer", "--body-file", "-", input="looks good")
    [answer] = lines(cli(machines, "alice", "inbox", "--json"))
    assert answer["body"] == "looks good" and answer["to_endpoint"] == {"machine": "alice-agent"}
    # A machine that accepts only its inbox takes messages to bob there (the inbox-mode path, C3.1a).
    assert json.loads(cli(machines, "bob", "routing", "--inbox-only", "--json").stdout) == {"routing": "inbox-only"}
    cli(machines, "alice", "send", "bob@example.test", "--body-file", "-", input="again")
    assert [m["body"] for m in lines(cli(machines, "bob", "inbox", "--json"))] == ["again"]
    assert json.loads(cli(machines, "bob", "routing", "--all", "--json").stdout) == {"routing": "all"}


def test_person_sign_out_ends_the_session_on_the_server(machines):
    add_person(machines, "alice")
    cli(machines, "alice", "me", "sign-out")
    assert person.load_session(str(machines["alice"])) is None
    gone = cli(machines, "alice", "me", "inbox", check=False)
    assert gone.returncode != 0 and "login --person" in gone.stderr
    cli(machines, "alice", "whoami")  # the machine stays signed in
