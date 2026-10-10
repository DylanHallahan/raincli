"""v0.5.2 in the CLI and client (protocol §17): local time and archived conversations."""
import json

import pytest

from raincli_agent import cli
from raincli_agent.api import ApiClient
from raincli_agent.errors import UsageError

from .conftest import send

INSTANT = "2026-10-10T14:03:07Z"


@pytest.fixture
def zone(monkeypatch):
    """Inject the display zone as a tzinfo (§17.3 A2: Windows ignores TZ)."""
    zoneinfo = pytest.importorskip("zoneinfo")

    def set_zone(name):
        try:
            monkeypatch.setattr(cli, "_TZ", [zoneinfo.ZoneInfo(name)])
        except zoneinfo.ZoneInfoNotFoundError:
            pytest.skip("no time zone database here")
    return set_zone


@pytest.fixture
def utc_flag():
    yield
    cli._UTC[0] = False


# -- §17.1 local time --------------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("America/New_York", "2026-10-10 10:03:07 EDT"),
    ("Asia/Kolkata", "2026-10-10 19:33:07 IST"),
    ("Australia/Lord_Howe", "2026-10-11 01:03:07 +11:00"),  # no letters in the zone name: the offset
    ("UTC", "2026-10-10 14:03:07 UTC"),
])
def test_timestamps_print_in_the_local_zone(zone, name, expected):
    zone(name)
    assert cli.when(INSTANT) == expected


def test_the_zone_parameter_and_the_computers_own_zone():
    from datetime import datetime, timedelta, timezone
    assert cli.when(INSTANT, tz=timezone(timedelta(hours=-3))) == "2026-10-10 11:03:07 -03:00"
    local = datetime(2026, 10, 10, 14, 3, 7, tzinfo=timezone.utc).astimezone()  # this computer's zone
    assert cli.when(INSTANT).startswith(local.strftime("%Y-%m-%d %H:%M:%S"))


def test_utc_flag_and_unparseable_values(zone, utc_flag):
    zone("America/New_York")
    cli._UTC[0] = True
    assert cli.when(INSTANT) == INSTANT
    cli._UTC[0] = False
    assert cli.when("not a time\x1b[31m") == "not a time\\x1b[31m"  # escaped, never raises
    assert cli.when(None) == "" and cli.when("") == ""


def message_with_times(fake_api):
    msg = send(fake_api, fake_api.alice, "bob", "hello")
    record = fake_api.state.messages[msg["id"]]
    record["created_at"] = INSTANT
    record["delivery_updated_at"] = "2026-10-10T14:05:00Z"
    return msg


def test_show_is_local_with_utc_and_json_unchanged(fake_api, as_agent, zone, utc_flag, capsys):
    msg = message_with_times(fake_api)
    as_agent(fake_api.bob)
    zone("Asia/Kolkata")
    assert cli.main(["show", msg["id"]]) == 0
    text = capsys.readouterr().out
    assert "at: 2026-10-10 19:33:07 IST" in text and "(2026-10-10 19:35:00 IST)" in text and "UTC" not in text
    for argv in (["--utc", "show", msg["id"]], ["show", msg["id"], "--utc"]):
        assert cli.main(argv) == 0
        assert f"at: {INSTANT}" in capsys.readouterr().out
        cli._UTC[0] = False
    assert cli.main(["show", msg["id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["message"]["created_at"] == INSTANT  # --json stays UTC ISO


def test_conversations_last_at_is_local(fake_api, as_agent, zone, capsys):
    message_with_times(fake_api)
    as_agent(fake_api.bob)
    zone("America/New_York")
    assert cli.main(["conversations"]) == 0
    assert "last_at 2026-10-10 10:03:07 EDT" in capsys.readouterr().out


# -- §17.2 archived conversations -------------------------------------------------------------------

def test_archive_unarchive_and_the_lists(fake_api, as_agent, capsys):
    msg = send(fake_api, fake_api.alice, "bob", "hello")
    cid = msg["conversation_id"]
    as_agent(fake_api.bob)
    assert cli.main(["archive", cid]) == 0
    assert "archived conversation" in capsys.readouterr().out
    assert cli.main(["archive", cid]) == 0  # idempotent
    capsys.readouterr()
    assert cli.main(["conversations"]) == 0
    assert capsys.readouterr().out.strip() == "No conversations."
    assert cli.main(["conversations", "--archived"]) == 0
    line = capsys.readouterr().out
    assert cid in line and "archived " in line and "by bob" in line
    assert cli.main(["conversations", "--all", "--json"]) == 0
    [conv] = json.loads(capsys.readouterr().out)["conversations"]
    assert conv["archived_at"] and conv["archived_by"] == {"machine": "bob"}
    # The other side sees it archived too, and may unarchive it for both.
    as_agent(fake_api.alice)
    assert cli.main(["conversations", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["conversations"] == []
    assert cli.main(["unarchive", cid]) == 0
    assert "back in the main list" in capsys.readouterr().out
    as_agent(fake_api.bob)
    assert cli.main(["conversations", "--json"]) == 0
    assert [c["id"] for c in json.loads(capsys.readouterr().out)["conversations"]] == [cid]
    with pytest.raises(SystemExit):
        cli.main(["conversations", "--archived", "--all"])  # one or the other


def test_archive_records_the_newest_seq_seen(fake_api, as_agent, capsys):
    """§17.3 A1: the archive carries the conversation's last_seq as listed, or --through-seq."""
    first = send(fake_api, fake_api.alice, "bob", "one")
    second = send(fake_api, fake_api.alice, "bob", "two")
    seqs = sorted(m["seq"] for m in fake_api.state.messages.values())
    as_agent(fake_api.bob)
    assert cli.main(["archive", first["conversation_id"], "--through-seq", str(seqs[0])]) == 0
    assert fake_api.state.archive_calls[-1][3] == seqs[0]
    capsys.readouterr()
    assert cli.main(["conversations", "--json"]) == 0  # a newer message exists: still listed
    assert [c["id"] for c in json.loads(capsys.readouterr().out)["conversations"]] == [second["conversation_id"]]
    assert cli.main(["archive", first["conversation_id"]]) == 0
    assert fake_api.state.archive_calls[-1][3] == seqs[-1]  # last_seq as listed
    assert "through message seq" in capsys.readouterr().out
    assert cli.main(["conversations", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["conversations"] == []


def test_the_cli_always_sends_archived(fake_api, as_agent):
    """§17.3 A4: a machine credential sending none would get include."""
    send(fake_api, fake_api.alice, "bob", "one")
    as_agent(fake_api.bob)
    for argv, expected in ((["conversations"], "exclude"), (["conversations", "--archived"], "only"),
                           (["conversations", "--all"], "include")):
        assert cli.main(argv + ["--json"]) == 0
        assert fake_api.state.conversation_queries[-1] == expected


def test_a_new_message_brings_an_archived_conversation_back(fake_api, as_agent):
    msg = send(fake_api, fake_api.alice, "bob", "first")
    api = ApiClient(fake_api.url, fake_api.bob)
    api.archive(msg["conversation_id"])
    assert api.conversations() == []
    send(fake_api, fake_api.alice, "bob", "second")
    [conv] = api.conversations()
    assert conv["id"] == msg["conversation_id"] and conv["archived_at"] is None


def test_client_query_and_outsiders(fake_api):
    msg = send(fake_api, fake_api.alice, "bob", "hello")
    eve = fake_api.state.add_agent("eve")
    from raincli_agent.errors import NotFound
    with pytest.raises(NotFound):
        ApiClient(fake_api.url, eve, max_attempts=1).archive(msg["conversation_id"])
    with pytest.raises(UsageError):
        ApiClient(fake_api.url, fake_api.bob).conversations(archived="sometimes")
    assert ApiClient(fake_api.url, fake_api.bob).conversations(archived="include")


# -- §17.2 "Delivery is never affected" ---------------------------------------------------------------

def test_the_connector_delivers_to_an_archived_conversation_exactly_as_to_an_active_one(fake_api, connector_env):
    """Two conversations with bob: one archived, one active. A new message in each is received,
    acked, framed, submitted and reported the same way, and the archived one returns to the list."""
    archived_first = send(fake_api, fake_api.alice, "bob", "old thread")
    carol = fake_api.state.add_agent("carol")
    active_first = send(fake_api, carol, "bob", "active thread")
    conn = connector_env.connector(trusted_senders=["alice", "carol"])
    for _ in range(3):  # one submission per iteration: deliver both first messages
        conn.run_once()
    bob_api = ApiClient(fake_api.url, fake_api.bob)
    bob_api.archive(archived_first["conversation_id"])
    assert [c["id"] for c in bob_api.conversations()] == [active_first["conversation_id"]]
    prompts_before = len(connector_env.herdr.prompts)
    connector_env.herdr.set_status("bob-claude", "idle")  # the same conditions for both messages
    to_archived = send(fake_api, fake_api.alice, "bob", "to the archived thread")
    assert to_archived["conversation_id"] == archived_first["conversation_id"]
    for _ in range(3):  # one at a time, so the agent is idle for each
        conn.run_once()
    connector_env.herdr.set_status("bob-claude", "idle")
    to_active = send(fake_api, carol, "bob", "to the active thread")
    for _ in range(3):
        conn.run_once()

    def outcome(mid):
        record = conn.queue.get(mid)
        events = [e[1] for e in fake_api.state.events if e[0] == mid]
        server = fake_api.state.messages[mid]
        return record["state"], record["acked"], events, server["delivery_state"], bool(server["acked_at"])
    assert outcome(to_archived["id"]) == outcome(to_active["id"]) == ("submitted", True, ["submitted"],
                                                                      "submitted", True)
    prompts = [p[1] for p in connector_env.herdr.prompts[prompts_before:]]
    assert len(prompts) == 2 and any("to the archived thread" in p for p in prompts)
    # A new message brought the archived conversation back for both sides.
    assert sorted(c["id"] for c in bob_api.conversations()) == sorted(
        [archived_first["conversation_id"], active_first["conversation_id"]])
