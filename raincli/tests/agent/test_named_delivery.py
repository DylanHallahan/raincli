"""Delivery to named agents on this machine (protocol §16.7, §16.12 C1, C12, §16.14 S1)."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from raincli_agent.api import ApiClient
from raincli_agent.connector import queue as q
from raincli_agent.connector.config import load_connector_config
from raincli_agent.connector.herdr import FakeHerdr
from raincli_agent.connector.queue import Queue
from raincli_agent.connector.runner import EXPIRE_OFFLINE, Connector, frame_record
from raincli_agent.runtime import discovery, floors, hook, sessions

from .conftest import body_of, write_agent_config

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def no_agent_process(monkeypatch):
    monkeypatch.setattr(hook, "agent_pid", lambda agent_type: None)


class Env:
    def __init__(self, tmp_path, fake_api):
        self.api, self.tmp = fake_api, tmp_path
        self.state = tmp_path / "runtime-state"
        self.state.mkdir(mode=0o700)
        self.salt = sessions.ensure_salt(str(self.state))
        sessions.sessions_dir(str(self.state), create=True)
        self.agent_cfg = write_agent_config(tmp_path / "bob-agent.json", fake_api.url, fake_api.bob)
        self.herdr = FakeHerdr()
        self.herdr.add("bob-claude", status="idle", pane_id="w9:p1")  # the configured inbox
        self.now = [1_000_000.0]

    def connector(self, **overrides):
        data = {"agent_config": self.agent_cfg, "herdr_agent": "bob-claude", "expect_pane_id": "w9:p1",
                "state_dir": str(self.tmp / "queue"), "trusted_senders": ["alice"], "prompt_timeout": 5}
        data.update(overrides)
        path = self.tmp / "connector.json"
        path.write_text(json.dumps({k: v for k, v in data.items() if v is not None}))
        cfg = load_connector_config(str(path))
        return Connector(cfg, ApiClient(self.api.url, self.api.bob), self.herdr, Queue(cfg.state_dir),
                         log=lambda line: None, sleep=lambda s: None, clock=lambda: self.now[0],
                         sessions_state=str(self.state))

    def send(self, agent, body="hello", sender=None):
        to = {"machine": "bob", "agent": agent} if agent else "bob"
        return ApiClient(self.api.url, sender or self.api.alice).send(to, body)[0]["id"]

    def hook_session(self, session_id, name, event="SessionStart", kind="claude"):
        out = io.BytesIO()
        data = {"session_id": session_id, "cwd": f"/w/{name}", "hook_event_name": event, "prompt": "hi"}
        hook.handle(kind, event, name, str(self.state), io.BytesIO(json.dumps(data).encode()), out, now=self.now[0])
        if not out.getvalue():
            return None
        return json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]

    def events(self, mid):
        return [(state, detail) for m, state, detail in self.api.state.events if m == mid]


@pytest.fixture
def env(tmp_path, fake_api):
    return Env(tmp_path, fake_api)


def local(conn, mid):
    return conn.queue.get(mid)


# -- C1: local states, routing=1 ------------------------------------------------------------

def test_named_message_lives_in_agent_states_and_polls_with_routing(env):
    env.herdr.add("reviewer", status="working")
    mid = env.send("reviewer")
    conn = env.connector()
    conn.run_once()
    rec = local(conn, mid)
    assert (rec["state"], rec["hold_reason"], rec["target"]) == (q.AGENT_HELD, "busy", "reviewer")
    assert env.api.state.inbox_queries and all(routing for _, routing in env.api.state.inbox_queries)
    assert floors.routing_capable(env.state) and floors.routing_capable(conn.queue.state_dir)


def test_a_v04_connector_delivers_none_of_the_agent_messages(env, tmp_path):
    """§16.12 C1: the real v0.4.0 connector code, run on a queue with every agent_* state
    (and pending attachments), prompts nothing and hands nothing over."""
    env.herdr.add("reviewer", status="working")
    ids = [env.send("reviewer", f"m{i}") for i in range(4)]
    conn = env.connector()
    conn.run_once()
    states = (q.AGENT_RECEIVED, q.AGENT_HELD, q.AGENT_SUBMITTING, q.AGENT_HANDED_OVER)
    with conn.queue.lock():
        for mid, state in zip(ids, states):
            rec = conn.queue.get(mid)
            rec["state"], rec["hold_reason"] = state, ("offline" if state == q.AGENT_HELD else None)
            rec["attachments_pending"] = state == q.AGENT_RECEIVED
            conn.queue.save(rec)
    old = tmp_path / "v040"
    old.mkdir()
    archive = subprocess.run(["git", "archive", "v0.4.0", "raincli/raincli_agent"], cwd=REPO,
                             capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(old, filter="data")
    script = f"""
import json, sys
sys.path.insert(0, {str(old / 'raincli')!r})
import raincli_agent
assert raincli_agent.__version__ == "0.4.0", raincli_agent.__version__
from raincli_agent.connector.config import load_connector_config
from raincli_agent.connector.herdr import FakeHerdr
from raincli_agent.connector.queue import Queue
from raincli_agent.connector.runner import Connector
cfg = load_connector_config({str(env.tmp / 'connector.json')!r})
herdr = FakeHerdr()
herdr.add("bob-claude", status="idle", pane_id="w9:p1")
herdr.add("reviewer", status="idle")
conn = Connector(cfg, None, herdr, Queue(cfg.state_dir), identity={{"handle": "bob", "team": "alpha"}},
                 log=lambda line: None, sleep=lambda s: None)
conn.recover()
conn.process()
print(json.dumps({{"prompts": len(herdr.prompts),
                  "states": sorted(r["state"] for r in conn.queue.all())}}))
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60,
                            env={**os.environ, "PYTHONPATH": ""})
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["prompts"] == 0
    assert out["states"] == sorted(states)


# -- §16.7 Herdr names ------------------------------------------------------------------------

def test_two_named_herdr_agents_each_get_their_own(env, fake_api):
    env.herdr.add("reviewer", status="idle", pane_id="w2:p1")
    env.herdr.add("planner", status="idle", pane_id="w3:p1")
    a = env.send("reviewer", "review it")
    conn = env.connector()
    conn.run_once()
    b = env.send("planner", "plan it")
    conn.run_once()
    assert [(n, body_of(t)) for n, t, _ in env.herdr.prompts] == [("reviewer", "review it"), ("planner", "plan it")]
    for mid, name in ((a, "reviewer"), (b, "planner")):
        assert local(conn, mid)["state"] == q.SUBMITTED
        assert env.events(mid) == [("submitted", f"handed to herdr agent {name}")]
    text = env.herdr.prompts[0][1]
    assert "\nTo: reviewer on bob\n" in text and '--from-agent "reviewer"' in text


def test_inbox_pins_never_apply_to_named_agents(env):
    env.herdr.add("reviewer", status="idle", pane_id="w7:p7", cwd="/somewhere/else")
    mid = env.send("reviewer")
    env.connector(expect_cwd="/work/bob").run_once()
    assert local(env.connector(), mid)["state"] == q.SUBMITTED


def test_named_holds_blocked_busy_offline_and_one_prompt_per_iteration(env):
    env.herdr.add("blocked-one", status="blocked")
    env.herdr.add("busy-one", status="working")
    env.herdr.add("free", status="idle")
    ids = {name: env.send(name) for name in ("blocked-one", "busy-one", "missing", "free")}
    second_free = env.send("free", "second")
    conn = env.connector()
    conn.run_once()
    expect = {"blocked-one": "blocked", "busy-one": "busy", "missing": "offline"}
    for name, reason in expect.items():
        rec = local(conn, ids[name])
        assert (rec["state"], rec["hold_reason"]) == (q.AGENT_HELD, reason)
        assert env.events(ids[name]) == [("held", reason)]
    assert local(conn, ids["free"])["state"] == q.SUBMITTED
    assert local(conn, second_free)["hold_reason"] == "busy"  # one prompt per iteration
    conn.run_once()
    assert local(conn, second_free)["state"] == q.SUBMITTED


def test_herdr_and_hook_with_one_name_is_ambiguous(env):
    env.herdr.add("twin", status="idle")
    env.hook_session("s1", "twin")
    mid = env.send("twin")
    conn = env.connector()
    conn.run_once()
    assert (local(conn, mid)["state"], local(conn, mid)["hold_reason"]) == (q.AGENT_HELD, "target_ambiguous")
    assert env.herdr.prompts == []


def test_two_live_hook_sessions_with_one_name_are_ambiguous(env):
    env.hook_session("s1", "notes")
    env.hook_session("s2", "notes", kind="codex")
    mid = env.send("notes")
    conn = env.connector()
    conn.run_once()
    assert local(conn, mid)["hold_reason"] == "target_ambiguous"


# -- §16.7 hook sessions by name -------------------------------------------------------------

def test_hook_session_by_name_gets_a_next_turn_handover_with_the_held_age(env):
    mid = env.send("notes", "please read")
    conn = env.connector()
    conn.run_once()
    assert local(conn, mid)["hold_reason"] == "offline"
    env.now[0] += 2 * 3600
    assert env.hook_session("s1", "notes") is None  # the session starts: nothing handed over yet
    conn.run_once()
    rec = local(conn, mid)
    assert rec["state"] == q.AGENT_HANDED_OVER and rec["handover_key"] == sessions.name_box("notes")
    assert env.events(mid)[-1] == ("held", "next_turn")
    box = Path(sessions.inbox_dir(str(env.state), sessions.name_box("notes")))
    assert box.parent.name == "by-name" and (box / f"{mid}.md").is_file()
    context = env.hook_session("s1", "notes", event="UserPromptSubmit")
    assert body_of(context) == "please read"
    assert "\nTo: notes on bob | held 2 h before this turn\n" in context
    conn.run_once()
    assert local(conn, mid)["state"] == q.SUBMITTED


def test_a_returning_session_with_the_same_name_claims_what_was_held(env):
    env.hook_session("s1", "notes")
    mid = env.send("notes", "for notes")
    conn = env.connector()
    conn.run_once()
    assert local(conn, mid)["state"] == q.AGENT_HANDED_OVER
    env.hook_session("s1", "notes", event="SessionEnd")  # the session leaves before its next turn
    conn.run_once()
    assert (local(conn, mid)["state"], local(conn, mid)["hold_reason"]) == (q.AGENT_HELD, "offline")
    env.hook_session("s2", "notes")  # a new session id, the same name
    conn.run_once()
    assert body_of(env.hook_session("s2", "notes", event="UserPromptSubmit")) == "for notes"
    conn.run_once()
    assert local(conn, mid)["state"] == q.SUBMITTED


def test_offline_hold_expires_after_14_days(env):
    mid = env.send("nobody")
    conn = env.connector()
    conn.run_once()
    env.now[0] += EXPIRE_OFFLINE - 1
    conn.run_once()
    assert local(conn, mid)["state"] == q.AGENT_HELD
    env.now[0] += 2
    conn.run_once()
    assert local(conn, mid)["state"] == q.REJECTED
    assert env.events(mid)[-1] == ("rejected", "expired_offline")


def test_person_sender_and_its_frame(env, fake_api):
    env.herdr.add("reviewer", status="idle")
    mid = fake_api.state.person_message("bob", "carol@example.com", "Carol C", "from a person", agent="reviewer")
    conn = env.connector(trust_mode="team")
    conn.run_once()
    text = env.herdr.prompts[0][1]
    assert "\nFrom: Carol C <carol@example.com> | team alpha\n" in text
    assert local(conn, mid)["sender"] == "@carol@example.com"


def test_a_forged_person_sender_is_skipped(env, fake_api):
    mid = fake_api.state.person_message("bob", "carol@example.com", "Carol", "x", agent="reviewer")
    fake_api.state.messages[mid]["person"] = {"email": "carol@example.com", "display_name": "x"}
    from raincli_agent.connector.runner import message_problem
    good = fake_api.state.render(fake_api.state.messages[mid])
    assert message_problem(good) is None
    assert message_problem({**good, "from": "@mallory@example.com"}) == "bad sender"
    assert message_problem({**good, "to_endpoint": {"machine": "bob", "agent": "bad\nname"}}) == "bad target"


def test_machine_mode_connector_leaves_machine_endpoint_messages_stored(env, fake_api):
    env.herdr.add("reviewer", status="idle")
    to_machine = env.send(None, "to the machine")
    to_agent = env.send("reviewer", "to the agent")
    runtime = env.tmp / "runtime.json"
    runtime.write_text(json.dumps({"machine_config": env.agent_cfg, "state_dir": "machine-state"}))
    cfg = load_connector_config(str(runtime))
    assert cfg.machine and not cfg.has_inbox and cfg.trust_mode == "team"
    conn = Connector(cfg, ApiClient(env.api.url, env.api.bob), env.herdr, Queue(cfg.state_dir),
                     log=lambda line: None, sleep=lambda s: None, sessions_state=str(env.state))
    conn.run_once()
    assert conn.queue.load(to_machine) is None
    assert fake_api.state.messages[to_machine]["acked_at"] is None
    assert local(conn, to_agent)["state"] == q.SUBMITTED


# -- §16.2 reachability -----------------------------------------------------------------------------

def test_reachability_and_ambiguity_in_the_presence_report(env):
    env.herdr.add("reviewer", status="idle")
    env.herdr.add("twin", status="idle")
    env.hook_session("s1", "twin")
    env.hook_session("s2", "notes")
    found = discovery.discover(str(env.state), env.salt, env.herdr, ("herdr", "bob-claude"), now=env.now[0],
                               include_scan=False)
    by = {(a["name"], a["source"]): a for a in found}
    assert by[("bob-claude", "herdr")]["role"] == "inbox" and by[("bob-claude", "herdr")]["reachability"] == "instant"
    assert by[("reviewer", "herdr")]["reachability"] == "instant"
    assert by[("notes", "hook")]["reachability"] == "next-turn"
    for source in ("herdr", "hook"):
        assert by[("twin", source)]["reachability"] == "listed" and by[("twin", source)]["ambiguous"] is True
    assert all("ambiguous" not in a for a in found if a["name"] != "twin")


# -- §16.14 S1: other clients -------------------------------------------------------------------------

def test_cli_inbox_sees_machine_messages_and_agent_with_flag(env, fake_api, as_agent, capsys):
    from raincli_agent import cli
    as_agent(fake_api.bob)
    env.send(None, "to the machine")
    env.send("reviewer", "to the reviewer")
    assert cli.main(["inbox", "--json"]) == 0
    bodies = [json.loads(line)["body"] for line in capsys.readouterr().out.splitlines()]
    assert bodies == ["to the machine"]
    assert fake_api.state.inbox_queries[-1] == ("bob", False)
    assert cli.main(["inbox", "--json", "--agent", "reviewer"]) == 0
    bodies = [json.loads(line)["body"] for line in capsys.readouterr().out.splitlines()]
    assert bodies == ["to the reviewer"]


# -- C1 floors ----------------------------------------------------------------------------------------

def test_routing_capable_raises_every_floor(tmp_path, monkeypatch):
    from raincli_agent.runtime import launcher, pushed
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"connectors": ["c.json"], "state_dir": "state"}))
    (tmp_path / "state").mkdir()
    assert floors.floor_for([str(runtime)]) is None and launcher.rollback_floor(str(runtime)) is None
    floors.record_routing_capable(tmp_path / "state")
    assert floors.floor_for([str(runtime)]) == (0, 5, 0)
    assert launcher.rollback_floor(str(runtime)) == (0, 5, 0)
    driver = pushed.PushedUpdates(root=tmp_path / "m", python=tmp_path / "py")
    driver.state_dir = tmp_path / "state"
    assert driver.floor() == (0, 5, 0)
    driver.managed, driver.mode = (lambda: True), (lambda: "automatic")
    driver.consider({"version": "v0.4.9", "allow_downgrade": True, "set_at": "t"})
    assert driver.data["error"] == "target_below_minimum"
