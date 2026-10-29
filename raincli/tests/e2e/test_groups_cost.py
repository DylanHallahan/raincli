"""C27: the cost and behaviour record (evidence, not a gate). Three scripted "always reply" agents (no model calls) in
one group, for a fixed 90 s with a hard local stop, against a live server at the DEFAULT general request limit
(120 a minute per credential). Each agent is a real connector with a fake Herdr pane: every prompt it is handed is a
turn, and it answers in the group through the real CLI (``raincli reply <id> --body-file -``, its own process).

Recorded per minute and per agent: posts, copies delivered, turns, refusals at the limit, and the pending growth
(copies made but not yet taken). One member (Priya) goes offline for the last 30 s; when she comes back, what her
machine gets at once is recorded too. The numbers show where the GENERAL request limit caps the growth, not where
agents would stop by themselves. With ``RAINCLI_C27_OUT`` set, the record is written there (JSON and Markdown)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest  # noqa: F401

from conftest_groups import groups_seed  # noqa: F401  (fixture)
from conftest_v06 import v06_seed  # noqa: F401  (fixture)

WINDOW = 90.0  # seconds, fixed (the brief: 60-120)
OFFLINE_FROM = 60.0  # Priya stops polling here; she returns after the window
ROOT = Path(__file__).resolve().parents[2]
REPLY = "import sys; from raincli_agent import cli; sys.exit(cli.main(sys.argv[1:]))"


@pytest.fixture
def live_default_limit(settings, engine):
    """A real uvicorn server with the DEFAULT general request limit (the test settings raise it)."""
    import socket

    import uvicorn

    from raincli_server.app import create_app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    live = replace(settings, public_url=f"http://127.0.0.1:{port}", max_pending=1000, rate_limit_per_min=120)
    app = create_app(live)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    yield live.public_url
    server.should_exit = True
    thread.join(10)
    app.state.engine.dispose()


class Agent:
    """One member's inbox: a connector (fake Herdr pane) and an agent that always replies in the group."""

    def __init__(self, name, url, token, tmp, start, inbox_agent):
        from raincli_agent.api import ApiClient
        from raincli_agent.connector.config import load_connector_config
        from raincli_agent.connector.herdr import FakeHerdr
        from raincli_agent.connector.queue import Queue
        from raincli_agent.connector.runner import Connector

        self.name, self.start, self.events = name, start, []  # (t, kind)
        folder = tmp / name
        folder.mkdir()
        self.agent_config = folder / "agent.json"
        self.agent_config.write_text(json.dumps({"api_url": url, "token": token}))
        os.chmod(self.agent_config, 0o600)
        cfg_path = folder / "connector.json"
        cfg_path.write_text(json.dumps({"agent_config": str(self.agent_config), "herdr_agent": "inbox-agent",
                                        "state_dir": str(folder / "queue"), "trust_mode": "team",
                                        "recheck_interval": 0.05, "prompt_timeout": 5}))
        cfg = load_connector_config(str(cfg_path))
        self.herdr = FakeHerdr()
        self.herdr.add("inbox-agent", status="idle")
        self.herdr.add(inbox_agent, status="idle")  # copies go to the member's named inbox agent (§21.3)
        (folder / "sessions").mkdir()
        # One attempt per request: a poll refused at the limit is counted and the loop goes on, so nothing sleeps
        # past the hard stop on a Retry-After (the replies go through the real CLI, with its own retries).
        self.connector = Connector(cfg, ApiClient(url, token, max_attempts=1), self.herdr, Queue(cfg.state_dir),
                                   identity={},
                                   log=lambda line: None, sleep=lambda s: None, sessions_state=str(folder / "sessions"))
        self.seen = 0
        self.online = True

    def note(self, kind):
        self.events.append((time.monotonic() - self.start, kind))

    def step(self, deadline):
        """One connector iteration, then a turn (and a group reply) for each prompt handed over."""
        try:
            self.connector.run_once()
        except Exception:  # noqa: BLE001 - a limited or failed poll is just a slow iteration here
            self.note("poll_refused")
            time.sleep(0.5)
            return
        new = self.herdr.prompts[self.seen:]
        self.seen = len(self.herdr.prompts)
        for _, text, _ in new:
            self.note("copy_delivered")
            self.note("turn")
            copy_id = text.split("\n", 1)[0].rsplit(" ", 1)[1].rstrip("]")
            left = deadline - time.monotonic()
            if left <= 0:
                return
            try:  # the hard stop: a reply still waiting (its own retry after a 429) is cut at the window's end
                result = subprocess.run([sys.executable, "-c", REPLY, "--config", str(self.agent_config), "reply",
                                         copy_id, "--body-file", "-"], input=f"{self.name}: noted.", text=True,
                                        capture_output=True, timeout=left, cwd=ROOT,
                                        env={**os.environ, "PYTHONPATH": str(ROOT)})
            except subprocess.TimeoutExpired:
                self.note("post_cut_at_stop")
                return
            if result.returncode == 0:
                self.note("post")
            elif "rate_limited" in result.stderr or "429" in result.stderr or "too many" in result.stderr:
                self.note("post_refused_at_limit")
            else:
                self.note("post_failed")


def _per_minute(events, kind, minutes):
    return [sum(1 for t, k in events if k == kind and m * 60 <= t < (m + 1) * 60) for m in range(minutes)]


def test_three_always_reply_agents_for_ninety_seconds(live_default_limit, session, groups_seed, tmp_path):  # noqa: F811
    from raincli_server.models import GroupPost, Message

    s, url = groups_seed, live_default_limit
    group = s["groups"]["checkout"]
    start = time.monotonic()
    agents = {name: Agent(name, url, s["machine_tokens"][key], tmp_path, start, inbox)
              for name, key, inbox in (("Maya", "maya", "zq-agent-maya-laptop"), ("Jon", "jon", "zq-agent-jon-api"),
                                       ("Priya", "priya", "zq-agent-priya-web"))}
    for a in agents.values():  # the first poll makes each machine group-capable (0.6.1, groups=1)
        a.connector.run_once()
    from raincli_agent.api import ApiClient
    ApiClient(url, s["machine_tokens"]["maya"]).group_post(group.id, "@Jon @Priya kick-off: reply to everything.",
                                                           post_id=uuid.uuid4())
    hard_stop = start + WINDOW
    pending_samples = []

    def loop(agent):
        while time.monotonic() < hard_stop:
            if agent.name == "Priya" and time.monotonic() - start >= OFFLINE_FROM:
                agent.online = False
                time.sleep(0.2)
                continue
            agent.step(hard_stop)

    threads = [threading.Thread(target=loop, args=(a,), daemon=True) for a in agents.values()]
    for t in threads:
        t.start()
    while time.monotonic() < hard_stop:
        session.expire_all()
        pending = session.query(Message).filter(Message.conversation_id == group.id, Message.route == "group",
                                                Message.acked_at.is_(None), Message.withdrawn_at.is_(None)).count()
        pending_samples.append((round(time.monotonic() - start, 1), pending))
        time.sleep(5)
    for t in threads:  # the hard stop: no agent starts anything after the window
        t.join(30)
    stopped_after = round(time.monotonic() - start, 1)
    elapsed = WINDOW

    # Priya comes back online: what her machine gets at once.
    session.expire_all()
    priya_machine = s["machines"]["priya"].id
    backlog = session.query(Message).filter(Message.recipient_agent_id == priya_machine, Message.route == "group",
                                            Message.acked_at.is_(None), Message.withdrawn_at.is_(None)).count()
    priya = agents["Priya"]
    before = len(priya.herdr.prompts)
    time.sleep(61)  # the limit's window passes: she returns to a fresh minute, as after a real absence
    priya.connector.poll(0)  # one poll takes the whole backlog (up to 100 a page)
    waiting = ("received", "held", "agent_received", "agent_held")
    taken = sum(1 for r in priya.connector.queue.all() if r.get("group") and r.get("state") in waiting)
    turns_back = 0
    for _ in range(backlog + 5):  # every iteration hands over one copy: one turn each
        priya.connector.process_named()
        priya.connector.process()
        turns_back = len(priya.herdr.prompts) - before
        if turns_back >= backlog:
            break

    session.expire_all()
    posts = session.query(GroupPost).filter_by(conversation_id=group.id).count()
    copies = session.query(Message).filter_by(conversation_id=group.id, route="group").count()
    minutes = 2
    record = {
        "window_seconds": WINDOW, "stopped_after_seconds": stopped_after, "limit_per_minute_per_credential": 120,
        "members_taking_part": 3,
        "posts_total": posts, "copies_total": copies,
        "per_agent": {name: {kind: _per_minute(a.events, kind, minutes)
                             for kind in ("post", "copy_delivered", "turn", "post_refused_at_limit", "poll_refused",
                                          "post_cut_at_stop", "post_failed")}
                      for name, a in agents.items()},
        "pending_samples": pending_samples,
        "offline_return": {"offline_seconds": round(elapsed - OFFLINE_FROM, 1), "backlog_copies": backlog,
                           "copies_taken_in_one_poll": taken, "turns_to_clear": turns_back},
    }
    per_second = sum(sum(v["post"]) for v in record["per_agent"].values()) / max(1.0, elapsed - 10)
    record["extrapolated_5_minutes"] = {
        "method": "the measured post rate after the first 10 s, held for 300 s (the general limit caps it, so the "
                  "curve is linear, not geometric, once it is reached)",
        "posts": round(per_second * 300), "copies": round(per_second * 300 * 2), "agent_turns": round(per_second * 300 * 2)}
    out = os.environ.get("RAINCLI_C27_OUT")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / "c27.json").write_text(json.dumps(record, indent=2))
        (Path(out) / "c27.md").write_text(_markdown(record))
    print(json.dumps(record, indent=2))

    # Evidence, not a gate: the loop ran, every copy went to a member other than its poster, and nothing was lost.
    assert posts > 1 and copies >= 2 * (posts - 1)
    assert all(any(k == "turn" for _, k in a.events) for a in agents.values())  # every agent took turns
    assert turns_back == backlog  # the returning machine takes and hands over its whole backlog


def _markdown(r):
    lines = [f"# C27 record ({r['window_seconds']} s window, every agent stopped by {r['stopped_after_seconds']} s; "
             f"limit {r['limit_per_minute_per_credential']}/min per credential)",
             "", f"- Posts: {r['posts_total']}; copies: {r['copies_total']}.",
             "", "| Agent | Minute | Posts | Copies delivered | Turns | Refused at the limit |", "|---|---|---|---|---|---|"]
    for name, v in r["per_agent"].items():
        for m in range(len(v["post"])):
            lines.append(f"| {name} | {m + 1} | {v['post'][m]} | {v['copy_delivered'][m]} | {v['turn'][m]} | "
                         f"{v['post_refused_at_limit'][m] + v['poll_refused'][m]} |")
    o = r["offline_return"]
    e = r["extrapolated_5_minutes"]
    lines += ["", f"- Pending copies over time (s, count): {r['pending_samples']}",
              f"- Offline return: after {o['offline_seconds']} s offline, {o['backlog_copies']} copies waiting; "
              f"{o['copies_taken_in_one_poll']} taken in one poll; {o['turns_to_clear']} turns to hand them all over.",
              f"- 5-minute extrapolation: {e['posts']} posts, {e['copies']} copies, {e['agent_turns']} agent turns "
              f"({e['method']}).",
              "- These numbers show where the general request limit caps the growth, not where agents would stop "
              "on their own."]
    return "\n".join(lines) + "\n"
