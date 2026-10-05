"""Machine agent directory, client report and team client targets (protocol §14, §14.7)."""

import io
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from api_helpers import auth, err
from raincli_server import admin, identity, presence
from raincli_server.models import AgentPresence, ClientTarget, MachineAgent

CLIENT = {"version": "0.3.0", "update_mode": "automatic", "update_state": "current", "error": None}


def agent(key="a" * 32, name="raincli-inbox", type="claude", status="idle", role=None, reachability=None,
          source="herdr", **extra):
    return {"key": key, "name": name, "type": type, "status": status, "role": role,
            "reachability": reachability, "source": source, **extra}


INBOX = agent(key="1" * 32, role="inbox", reachability="instant")


def put(client, token, body):
    return client.put("/api/v1/presence", headers=auth(token), json=body)


def entry(client, token, handle):
    rows = client.get("/api/v1/agents", headers=auth(token)).json()["agents"]
    return next(a for a in rows if a["handle"] == handle)


def test_report_is_stored_team_scoped_and_read_without_keys(client, world):
    body = {"status": "ready", "client": CLIENT,
            "agents": [agent(key="b" * 32, name="notes", type="codex", status="working", source="hook"),
                       INBOX, agent(key="c" * 32, name="gemini", type="gemini", status="unknown", source="scan")]}
    r = put(client, world["tokens"]["alice"], body)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"presence", "target"} and r.json()["target"] is None
    seen = entry(client, world["tokens"]["bob"], "alice-agent")
    assert seen["presence"]["status"] == "ready"
    assert seen["machine"] == {"client_version": "0.3.0", "update_mode": "automatic", "update_state": "current",
                               "error": None, "seen_at": seen["presence"]["seen_at"]}
    # The inbox comes first, then by name; keys are never returned.
    assert [a["name"] for a in seen["agents"]] == ["raincli-inbox", "gemini", "notes"]
    assert seen["agents"][0] == {"name": "raincli-inbox", "type": "claude", "status": "idle", "role": "inbox",
                                 "reachability": "instant", "source": "herdr", "ambiguous": False}
    assert all(set(a) == {"name", "type", "status", "role", "reachability", "source", "ambiguous"} for a in seen["agents"])
    assert "a" * 32 not in client.get("/api/v1/agents", headers=auth(world["tokens"]["bob"])).text
    # Bob has never reported: no machine block and no agents.
    bob = entry(client, world["tokens"]["alice"], "bob-agent")
    assert bob["machine"] is None and bob["agents"] == []
    # Another team never sees Alice's handle or her directory.
    other = client.get("/api/v1/agents", headers=auth(world["tokens"]["eve"])).text
    assert "alice-agent" not in other and "raincli-inbox" not in other


def test_snapshot_replaces_and_empty_list_clears(client, world, session):
    token, aid = world["tokens"]["alice"], world["agents"]["alice"].id
    put(client, token, {"status": "ready", "agents": [INBOX, agent(key="b" * 32, name="one")]})
    put(client, token, {"status": "busy", "agents": [agent(key="c" * 32, name="two"),
                                                     agent(key="d" * 32, name="three", role="inbox",
                                                           reachability="next-turn", source="hook")]})
    names = session.scalars(select(MachineAgent.name).where(MachineAgent.agent_id == aid).order_by(MachineAgent.name))
    assert list(names) == ["three", "two"]
    # An omitted agents list leaves the directory alone; an empty one clears it (graceful stop).
    put(client, token, {"status": "ready"})
    assert session.scalar(select(func.count()).select_from(MachineAgent).where(MachineAgent.agent_id == aid)) == 2
    put(client, token, {"status": "offline", "agents": []})
    assert entry(client, world["tokens"]["bob"], "alice-agent")["agents"] == []


def test_rejected_report_changes_nothing(client, world, session):
    token = world["tokens"]["alice"]
    put(client, token, {"status": "ready", "client": CLIENT, "agents": [INBOX]})
    bad = {"status": "busy", "client": {**CLIENT, "version": "0.4.0"},
           "agents": [agent(key="e" * 32, name="fine"), agent(key="f" * 32, name="\u202egnp.exe")]}
    assert err(put(client, token, bad)) == "invalid"
    session.expire_all()
    row = session.get(AgentPresence, world["agents"]["alice"].id)
    assert row.status == "ready" and row.client_version == "0.3.0"
    assert [a["name"] for a in entry(client, world["tokens"]["bob"], "alice-agent")["agents"]] == ["raincli-inbox"]


@pytest.mark.parametrize("agents", [
    [agent(key="short")],                                          # key too short
    [agent(key="A" * 32)],                                         # key not lowercase hex-like
    [agent(key="a" * 65)],
    [agent(), agent(name="dup")],                                  # keys unique within a report
    [agent(name="")], [agent(name="   ")], [agent(name="x" * 65)],
    [agent(name="line\nbreak")], [agent(name="tab\there")],
    [agent(name="rca_" + "A" * 43)], [agent(name="token rci_abcdefghijklmnopqrst-_")],  # token-shaped
    [agent(name="\u202egnp.exe")], [agent(name="a\u2066b")], [agent(name="\u200fx")],  # bidi controls
    [agent(name="zero\u200bwidth")], [agent(name="a\u200db")], [agent(name="\ufeffbom")],  # zero-width
    [agent(name="soft\u00adhyphen")], [agent(name="line\u2028sep")],
    [agent(type="vim")], [agent(status="ready")], [agent(source="ps")],
    [agent(role="main")],
    [agent(role="inbox")],                                         # the inbox needs a reachability
    [agent(role="inbox", reachability="soon")],
    [agent(reachability="soon")],                                  # §16.2: instant, next-turn or listed
    [agent(reachability="instant", ambiguous=True)],               # ambiguous names are listed
    [agent(reachability="listed", ambiguous="yes")],
    [INBOX, agent(key="b" * 32, role="inbox", reachability="next-turn")],  # a single inbox
    [agent(source="scan", status="idle")],                         # scan implies unknown
    [agent(cwd="/home/alice")], [agent(pid=1234)], [agent(title="t")],  # unknown keys, including paths
    [{"key": "a" * 32, "name": "x", "type": "claude", "status": "idle"}],  # missing source
    [agent(name=7)], [agent(status=None)], ["not-an-object"],
    {"key": "a" * 32}, "agents",
    [agent(key=f"{i:032d}") for i in range(presence.MAX_AGENTS + 1)],  # size bound
])
def test_invalid_agents_are_rejected_whole(client, world, agents):
    r = put(client, world["tokens"]["alice"], {"status": "ready", "agents": agents})
    assert r.status_code == 400 and err(r) == "invalid"


@pytest.mark.parametrize("client_block", [
    {**CLIENT, "version": "v0.3.0"}, {**CLIENT, "version": "0.3"}, {**CLIENT, "version": "٠.٣.٠"},
    {**CLIENT, "version": "12345.0.0"}, {**CLIENT, "version": "0.03.0"}, {**CLIENT, "version": "00.3.0"},
    {**CLIENT, "version": "0.3.00"}, {**CLIENT, "update_mode": "auto"}, {**CLIENT, "update_state": "ok"},
    {**CLIENT, "error": "Download failed: /home/alice/x"}, {**CLIENT, "error": "HTTPError"},
    {**CLIENT, "error": "x" * 65}, {**CLIENT, "error": ""}, {**CLIENT, "url": "https://evil.test"},
    {"version": "0.3.0", "update_mode": "automatic"}, "0.3.0", None,
])
def test_invalid_client_block_is_rejected(client, world, client_block):
    r = put(client, world["tokens"]["alice"], {"status": "ready", "client": client_block})
    assert r.status_code == 400 and err(r) == "invalid"


def test_client_block_bounds_and_omission(client, world, session):
    token, aid = world["tokens"]["alice"], world["agents"]["alice"].id
    failed = {"version": "9999.0.12", "update_mode": "manual", "update_state": "failed", "error": "urlerror:timeout"}
    assert put(client, token, {"status": "ready", "client": failed}).status_code == 200
    # Error is optional; an omitted client keeps the stored columns (protocol §14.7).
    assert put(client, token, {"status": "busy", "client": {k: v for k, v in CLIENT.items() if k != "error"}}).status_code == 200
    assert put(client, token, {"status": "ready"}).status_code == 200
    session.expire_all()
    row = session.get(AgentPresence, aid)
    assert (row.client_version, row.update_mode, row.update_state, row.update_error) == ("0.3.0", "automatic", "current", None)


def test_v020_body_stays_valid(client, world):
    r = put(client, world["tokens"]["alice"], {"status": "busy"})
    assert r.status_code == 200 and r.json()["presence"]["status"] == "busy" and r.json()["target"] is None
    seen = entry(client, world["tokens"]["bob"], "alice-agent")
    assert seen["presence"]["status"] == "busy" and seen["machine"] is None and seen["agents"] == []


def test_directory_rows_expire_after_ttl(client, world, session):
    put(client, world["tokens"]["alice"], {"status": "ready", "client": CLIENT, "agents": [INBOX]})
    for row in session.scalars(select(MachineAgent)):
        row.seen_at -= timedelta(seconds=presence.TTL_SECONDS - 5)
    session.commit()
    assert len(entry(client, world["tokens"]["bob"], "alice-agent")["agents"]) == 1
    for row in session.scalars(select(MachineAgent)):
        row.seen_at -= timedelta(seconds=5)
    session.get(AgentPresence, world["agents"]["alice"].id).seen_at -= timedelta(seconds=presence.TTL_SECONDS)
    session.commit()
    seen = entry(client, world["tokens"]["bob"], "alice-agent")
    assert seen["agents"] == [] and seen["presence"]["status"] == "offline"
    assert seen["machine"]["client_version"] == "0.3.0"  # the last known version stays visible


def test_revoked_handle_returns_no_agents_and_cannot_report(client, world, session):
    token = world["tokens"]["alice"]
    put(client, token, {"status": "ready", "client": CLIENT, "agents": [INBOX]})
    identity.revoke_agent(session, world["agents"]["alice"])
    session.commit()
    seen = entry(client, world["tokens"]["bob"], "alice-agent")
    assert seen["active"] is False and seen["agents"] == [] and seen["machine"] is None
    assert seen["presence"]["status"] == "offline"
    assert put(client, token, {"status": "ready", "agents": [INBOX]}).status_code == 401


def test_reply_carries_the_team_target_only(client, world, session):
    session.add(ClientTarget(team_id=world["teams"]["acme"].id, version="v0.3.1", allow_downgrade=True))
    session.commit()
    set_at = session.get(ClientTarget, world["teams"]["acme"].id).set_at
    r = put(client, world["tokens"]["alice"], {"status": "ready"})
    target = r.json()["target"]
    assert target == {"version": "v0.3.1", "allow_downgrade": True, "set_at": set_at.isoformat()}
    assert datetime.fromisoformat(target["set_at"]).tzinfo is not None  # ISO 8601 with an offset
    assert not {"url", "repository", "host"} & set(target)
    # Another team's target is not visible to globex.
    assert put(client, world["tokens"]["eve"], {"status": "ready"}).json()["target"] is None


def test_database_enforces_directory_invariants(session, world):
    from sqlalchemy.exc import IntegrityError

    aid = world["agents"]["alice"].id
    now = identity.now()
    base = dict(agent_id=aid, name="x", type="claude", status="idle", source="herdr", seen_at=now)
    for bad in ([MachineAgent(key="a" * 32, role="inbox", **base)],
                [MachineAgent(key="a" * 32, role="inbox", reachability="instant", **base),
                 MachineAgent(key="b" * 32, role="inbox", reachability="next-turn", **base)]):
        session.add_all(bad)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_version_tuple_compares_numerically():
    assert presence.version_tuple("v0.10.0") > presence.version_tuple("0.9.9")
    assert presence.version_tuple("v0.3.0") == presence.version_tuple("0.3.0") == presence.MIN_TARGET
    for bad in ("v0.3", "x0.3.0", "v٠.3.0", "vv0.3.0", "v0.03.0", "v01.0.0"):
        with pytest.raises(ValueError):
            presence.version_tuple(bad)


# Admin CLI ---------------------------------------------------------------------

def run(database_url, *argv):
    out, errs = io.StringIO(), io.StringIO()
    code = admin.main(list(argv), env={"RAINCLI_DATABASE_URL": database_url}, stdin=io.StringIO(),
                      stdout=out, stderr=errs)
    return code, out.getvalue(), errs.getvalue()


def test_set_client_version_and_clear(database_url, client, world, session):
    acme = world["teams"]["acme"].id
    code, out, errs = run(database_url, "set-client-version", "--team", "acme", "v0.3.1")
    assert code == 0 and out.strip() == "client target for acme is v0.3.1" and errs == ""
    first = session.get(ClientTarget, acme)
    assert (first.version, first.allow_downgrade) == ("v0.3.1", False)
    first_set = first.set_at
    session.commit()

    code, out, errs = run(database_url, "set-client-version", "--team", "acme", "0.3.0", "--allow-downgrade")
    assert code == 0 and "v0.3.0 (downgrade allowed)" in out
    assert errs.startswith("warning: --allow-downgrade") and "only while this target is set" in errs
    session.expire_all()
    target = session.get(ClientTarget, acme)
    assert (target.version, target.allow_downgrade) == ("v0.3.0", True)
    assert target.set_at > first_set  # the upsert refreshes set_at (protocol §14.7)
    session.commit()
    reply = put(client, world["tokens"]["alice"], {"status": "ready"}).json()
    assert reply["target"] == {"version": "v0.3.0", "allow_downgrade": True, "set_at": target.set_at.isoformat()}

    # Re-setting the same target re-arms it: the reply's set_at changes (protocol §14.8).
    assert run(database_url, "set-client-version", "--team", "acme", "v0.3.0", "--allow-downgrade")[0] == 0
    again = put(client, world["tokens"]["alice"], {"status": "ready"}).json()["target"]
    assert again["version"] == "v0.3.0" and again["allow_downgrade"] is True
    assert datetime.fromisoformat(again["set_at"]) > target.set_at

    # Re-setting without the flag turns the downgrade permission off again.
    assert run(database_url, "set-client-version", "--team", "acme", "v0.3.0")[0] == 0
    assert put(client, world["tokens"]["alice"], {"status": "ready"}).json()["target"]["allow_downgrade"] is False

    code, out, _ = run(database_url, "set-client-version", "--team", "acme", "--clear")
    assert code == 0 and "cleared" in out
    assert put(client, world["tokens"]["alice"], {"status": "ready"}).json()["target"] is None
    assert run(database_url, "set-client-version", "--team", "acme", "--clear")[1].strip() == "team acme has no client target"


@pytest.mark.parametrize("argv", [
    ["v0.2.0"], ["v0.2.9"], ["0.1.0"],                                    # below the first target-aware version
    ["latest"], ["v0.3"], ["v0.03.0"], ["v0.4.00"], ["v00.3.0"], ["0.3.01"], ["v0.3.0-rc1"], ["https://example.test/v0.3.0"], ["v٠.3.0"],
    ["--clear", "--allow-downgrade"],
])
def test_set_client_version_rejects(database_url, world, session, argv):
    code, out, errs = run(database_url, "set-client-version", "--team", "acme", *argv)
    assert code == 1 and errs.startswith("raincli-admin: ") and out == ""
    assert session.get(ClientTarget, world["teams"]["acme"].id) is None


def test_set_client_version_requires_one_of_version_or_clear(database_url, world):
    for argv in ([], ["v0.3.0", "--clear"]):
        with pytest.raises(SystemExit):
            run(database_url, "set-client-version", "--team", "acme", *argv)
    assert run(database_url, "set-client-version", "--team", "nope", "v0.3.0")[2].strip() == "raincli-admin: no team nope"


def test_client_status_lists_machines(database_url, client, world, session):
    put(client, world["tokens"]["alice"], {"status": "ready", "agents": [INBOX], "client": {
        "version": "0.3.0", "update_mode": "automatic", "update_state": "failed", "error": "urlerror"}})
    put(client, world["tokens"]["bob"], {"status": "ready"})
    code, out, _ = run(database_url, "client-status", "--team", "acme")
    lines = [line.split("\t") for line in out.strip().splitlines()]
    assert code == 0 and lines[0] == ["target", "none"]
    assert lines[1] == ["handle", "version", "update_mode", "update_state", "error", "seen_at"]
    alice = next(line for line in lines if line[0] == "alice-agent")
    assert alice[1:5] == ["0.3.0", "automatic", "failed", "urlerror"]
    bob = next(line for line in lines if line[0] == "bob-agent")
    assert bob[1:5] == ["-", "-", "-", "-"] and bob[5] != "never"
    assert "eve-agent" not in out and "raincli-inbox" not in out and "1" * 32 not in out

    run(database_url, "set-client-version", "--team", "acme", "v0.3.2", "--allow-downgrade")
    assert run(database_url, "client-status", "--team", "acme")[1].splitlines()[0] == "target\tv0.3.2 allow-downgrade"
    identity.revoke_agent(session, world["agents"]["bob"])
    session.commit()
    assert "bob-agent" not in run(database_url, "client-status", "--team", "acme")[1]


@pytest.mark.parametrize("name", [
    "team/api", "C:\\Users\\alice", "orca_tools", "merci_bot", "circa_2024", "rca_short",
    "my.project.v2", "..", "~", "🚀 launch", "東京-agent", "naïve café", "🙂" * 64, "a" * 64,
])
def test_ordinary_names_are_accepted(client, world, name):
    r = put(client, world["tokens"]["alice"], {"status": "ready", "agents": [agent(name=name)]})
    assert r.status_code == 200, r.text
    assert entry(client, world["tokens"]["bob"], "alice-agent")["agents"][0]["name"] == name


def test_client_normalized_names_always_pass():
    """Whatever the client's normalizer produces, the server accepts (review 1, finding 1)."""
    from raincli_agent.runtime.sessions import normalize_name

    raw = ["/home/u/orca_tools", "team/api", "‮gnp.exe", "zero\u200bwidth", "👨‍👩‍👧 family",
           "line\nbreak\ttab", "\x00\x1f", "   ", "x" * 200, "C:\\Users\\bob\\proj", "\ud800", "\ue000pua",
           "\u2028\u2029", "a\u00a0b", "🙂" * 80]
    for value in raw:
        name = normalize_name(value, "claude")
        assert presence.valid_agent_name(name), (value, name)
    assert not presence.valid_agent_name("\ud800")  # a lone surrogate can't even travel as UTF-8


def test_contract_maximal_report_fits_the_body_limit(client, world):
    """100 agents with 64 astral-plane names, as Python's json.dumps escapes them (review 1, finding 9)."""
    import json

    agents = [agent(key=f"{i:064d}", name="😀" * 64, type="opencode", status="unknown", source="scan")
              for i in range(presence.MAX_AGENTS)]
    agents[0].update(role="inbox", reachability="next-turn", source="hook")
    body = {"status": "blocked", "agents": agents, "client": {
        "version": "9999.9999.9999", "update_mode": "automatic", "update_state": "rolled_back", "error": "e" * 64}}
    raw = json.dumps(body).encode()
    assert 90_000 < len(raw) < 128 * 1024 * 3 // 4  # at least a quarter of the limit to spare
    r = client.put("/api/v1/presence", headers={**auth(world["tokens"]["alice"]), "Content-Type": "application/json"},
                   content=raw)
    assert r.status_code == 200, r.text
    assert len(entry(client, world["tokens"]["bob"], "alice-agent")["agents"]) == presence.MAX_AGENTS
    # The larger limit applies to PUT /presence only.
    big = b'{"status": "ready", "pad": "' + b"x" * (128 * 1024) + b'"}'
    r = client.put("/api/v1/presence", headers={**auth(world["tokens"]["alice"]), "Content-Type": "application/json"},
                   content=big)
    assert r.status_code == 413
    r = client.post("/api/v1/messages/00000000-0000-4000-8000-000000000000/events", headers={
        **auth(world["tokens"]["alice"]), "Content-Type": "application/json"}, content=b"x" * (70 * 1024))
    assert r.status_code == 413
