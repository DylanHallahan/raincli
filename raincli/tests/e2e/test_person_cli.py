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
    # A teammate's machine messages alice as a person, with an attachment.
    note = tmp_path / "notes.md"
    note.write_bytes(b"# notes\n")
    mid = str(uuid.uuid4())
    cli(machines, "bob", "send", "@alice@example.test", "--body-file", "-", "--id", mid, "--attach", str(note),
        input="Could you look at the release notes?")
    [msg] = lines(cli(machines, "alice", "me", "inbox", "--json"))
    assert msg["id"] == mid and msg["from"] == "bob-agent" and msg["to_endpoint"]["person"] == "alice@example.test"
    # --watch prints it and never marks it read.
    watched = lines(cli(machines, "alice", "me", "inbox", "--watch", "--once", "--timeout", "10", "--json"))
    assert [m["id"] for m in watched] == [mid]
    shown = cli(machines, "alice", "me", "read", mid).stdout
    assert "| Could you look at the release notes?" in shown and "from: bob-agent" in shown
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
    # The machine replies to the person with raincli reply.
    cli(machines, "bob", "reply", rid, "--body-file", "-", input="Thanks!")
    assert [m["body"] for m in lines(cli(machines, "alice", "me", "inbox", "--json"))] == ["Thanks!"]
    # Nothing printed the person session.
    for args in (("me", "inbox"), ("me", "read", mid)):
        assert session not in cli(machines, "alice", *args).stdout


def test_person_sends_to_a_machine_and_bodies_never_come_from_argv(machines):
    add_person(machines, "alice")
    cli(machines, "alice", "me", "send", "bob-agent", "--body-file", "-", input="hello from Alice")
    usage = cli(machines, "alice", "me", "send", "bob-agent", "--body", "x", check=False)
    assert usage.returncode == 2
    usage = cli(machines, "bob", "send", "@alice@example.test", "--body", "x", check=False)
    assert usage.returncode != 0 and "--body-file" in usage.stderr


def test_team_required_names_the_teams(machines, world, session):
    from raincli_server import identity
    identity.add_member(session, world["teams"]["globex"], world["users"]["alice"])
    session.commit()
    add_person(machines, "alice")
    refused = cli(machines, "alice", "me", "send", "bob-agent", "--body-file", "-", input="hi", check=False)
    assert refused.returncode != 0 and "--team" in refused.stderr and "acme" in refused.stderr
    cli(machines, "alice", "me", "send", "bob-agent", "--team", "acme", "--body-file", "-", input="hi")
    # A reply needs no --team: the client finds the parent's team.
    mid = str(uuid.uuid4())
    cli(machines, "bob", "send", "@alice@example.test", "--body-file", "-", "--id", mid, input="question")
    rid = str(uuid.uuid4())
    cli(machines, "alice", "me", "reply", mid, "--body-file", "-", "--id", rid, input="answer")
    assert json.loads(cli(machines, "bob", "show", rid, "--json").stdout)["message"]["in_reply_to"] == mid


def test_not_deliverable_offers_the_machine_endpoint(machines, world, server):  # noqa: F811
    bob = ApiClient(server, world["tokens"]["bob"], max_attempts=1)
    bob.report_presence("ready", agents=[{"key": "k" * 16, "name": "reviewer", "type": "claude", "status": "idle",
                                          "reachability": "listed", "role": None, "source": "herdr"}],
                        client={"version": "0.5.0", "update_mode": "manual", "update_state": "current"})
    refused = cli(machines, "alice", "send", "bob-agent/reviewer", "--body-file", "-", input="hi", check=False)
    assert refused.returncode != 0
    assert "can't receive messages (listed only)" in refused.stderr
    assert "send to bob-agent" in refused.stderr  # offered, never done silently (C10)
    assert cli(machines, "bob", "inbox", "--json").stdout.strip() == ""


def test_routing_and_named_agent_send(machines, world, server):  # noqa: F811
    bob = ApiClient(server, world["tokens"]["bob"], max_attempts=1)
    bob.report_presence("ready", agents=[{"key": "k" * 16, "name": "reviewer", "type": "claude", "status": "idle",
                                          "reachability": "instant", "role": None, "source": "herdr"}],
                        client={"version": "0.5.0", "update_mode": "manual", "update_state": "current"})
    bob.inbox(routing=True)  # bob's connector is routing-capable (S1)
    cli(machines, "alice", "send", "bob-agent/reviewer", "--from-agent", "planner", "--body-file", "-",
        input="please review")
    [msg] = lines(cli(machines, "bob", "inbox", "--agent", "reviewer", "--json"))
    assert msg["to_endpoint"] == {"machine": "bob-agent", "agent": "reviewer"} and msg["from_agent"] == "planner"
    assert cli(machines, "bob", "inbox", "--json").stdout.strip() == ""  # S1: the machine's inbox only
    assert json.loads(cli(machines, "bob", "routing", "--inbox-only", "--json").stdout) == {"routing": "inbox-only"}
    refused = cli(machines, "alice", "send", "bob-agent/reviewer", "--body-file", "-", input="again", check=False)
    assert refused.returncode != 0
    assert json.loads(cli(machines, "bob", "routing", "--all", "--json").stdout) == {"routing": "all"}


def test_person_sign_out_ends_the_session_on_the_server(machines):
    add_person(machines, "alice")
    cli(machines, "alice", "me", "sign-out")
    assert person.load_session(str(machines["alice"])) is None
    gone = cli(machines, "alice", "me", "inbox", check=False)
    assert gone.returncode != 0 and "login --person" in gone.stderr
    cli(machines, "alice", "whoami")  # the machine stays signed in
