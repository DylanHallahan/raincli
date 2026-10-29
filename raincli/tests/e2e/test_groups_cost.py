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
        api = ApiClient(url, token, max_attempts=1)
        real_request = api.request

        def counted(*args, **kwargs):  # every request the connector makes, toward the limit
            self.note("request")
            return real_request(*args, **kwargs)
        api.request = counted
        self.connector = Connector(cfg, api, self.herdr, Queue(cfg.state_dir), identity={},
                                   log=lambda line: None, sleep=lambda s: None, sessions_state=str(folder / "sessions"))
        self.seen = 0
        self.online = True
        self.handed = {}  # copy id -> first hand-over time: a turn per DISTINCT copy (a re-prompt isn't a new turn)
        self.cli_posts = []  # (t, post id) for each CLI success that printed "posted: <id>"
        self.cli_other = []  # (t, first line) for CLI successes that didn't print a new post

    def note(self, kind):
        self.events.append((time.monotonic() - self.start, kind))

    WAITING = ("received", "held", "agent_received", "agent_held", "attachment_pending")

    def local_backlog(self):
        """Group records this connector has taken (stored AND acked) but not yet handed to its agent."""
        return sum(1 for r in self.connector.queue.all()
                   if r.get("group") and r.get("state") in self.WAITING and r.get("acked"))

    def ack_pending(self):
        """Group records stored locally whose ack the limit refused so far: the server still counts them untaken."""
        return sum(1 for r in self.connector.queue.all()
                   if r.get("group") and r.get("state") in self.WAITING and not r.get("acked"))

    def step(self, deadline):
        """One connector iteration, then a turn (and a group reply) for each copy handed to the agent."""
        try:
            self.connector.run_once()
        except Exception:  # noqa: BLE001 - a limited or failed poll is just a slow iteration here
            self.note("poll_refused")
            time.sleep(0.5)
            return
        new = self.herdr.prompts[self.seen:]
        self.seen = len(self.herdr.prompts)
        for _, text, _ in new:
            header = text.split("\n", 1)[0]
            if not header.startswith("[RainCLI group post "):
                self.note("one_to_one_prompt")  # the seed's 1:1 messages to this machine: not part of the loop
                continue
            copy_id = header.rsplit(" ", 1)[1].rstrip("]")
            if copy_id in self.handed:
                self.note("re_prompt")  # the same copy handed over again: not a new turn, no new reply
                continue
            self.handed[copy_id] = time.monotonic() - self.start
            self.note("turn")  # a distinct copy handed to the agent: one agent turn
            left = deadline - time.monotonic()
            if left <= 0:
                return
            self.note("request")  # the CLI's GET /messages/{id} and POST /messages (its own retries uncounted)
            self.note("request")
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
                first = (result.stdout.splitlines() or [""])[0]
                if first.startswith("posted: "):
                    self.cli_posts.append((time.monotonic() - self.start, first.split()[1]))
                else:
                    self.cli_other.append((time.monotonic() - self.start, first[:120]))
            elif "rate_limited" in result.stderr or "429" in result.stderr or "too many" in result.stderr:
                self.note("post_refused_at_limit")
            else:
                self.note("post_failed")


def _per_minute(events, kind, minutes):
    return [sum(1 for t, k in events if k == kind and m * 60 <= t < (m + 1) * 60) for m in range(minutes)]


def _at(samples, t):
    """The last sample at or before ``t`` seconds."""
    return [v for at, v in samples if at <= t][-1] if any(at <= t for at, _ in samples) else 0


def test_three_always_reply_agents_for_ninety_seconds(live_default_limit, session, groups_seed, tmp_path):  # noqa: F811
    from datetime import datetime, timezone

    from raincli_server.models import GroupPost, Message

    s, url = groups_seed, live_default_limit
    group = s["groups"]["checkout"]
    start, start_wall = time.monotonic(), datetime.now(timezone.utc)
    agents = {name: Agent(name, url, s["machine_tokens"][key], tmp_path, start, inbox)
              for name, key, inbox in (("Maya", "maya", "zq-agent-maya-laptop"), ("Jon", "jon", "zq-agent-jon-api"),
                                       ("Priya", "priya", "zq-agent-priya-web"))}
    machines = {name: s["machines"][key].id for name, key in (("Maya", "maya"), ("Jon", "jon"), ("Priya", "priya"))}
    for a in agents.values():  # the first poll makes each machine group-capable (0.6.1, groups=1)
        a.connector.run_once()
    from raincli_agent.api import ApiClient
    kickoff = str(uuid.uuid4())
    ApiClient(url, s["machine_tokens"]["maya"]).group_post(group.id, "@Jon @Priya kick-off: reply to everything.",
                                                           post_id=kickoff)
    hard_stop = start + WINDOW
    samples = {"local_backlog": {n: [] for n in agents}, "server_unacked": [], "server_taken_not_handed": []}

    def loop(agent):
        while time.monotonic() < hard_stop:
            if agent.name == "Priya" and time.monotonic() - start >= OFFLINE_FROM:
                agent.online = False
                time.sleep(0.2)
                continue
            agent.step(hard_stop)

    def sample():
        at = round(time.monotonic() - start, 1)
        session.expire_all()
        copies = session.query(Message).filter(Message.conversation_id == group.id, Message.route == "group",
                                               Message.withdrawn_at.is_(None))
        samples["server_unacked"].append((at, copies.filter(Message.acked_at.is_(None),  # the run's own copies
                                                            Message.created_at >= start_wall).count()))
        samples["server_taken_not_handed"].append((at, copies.filter(  # the run's own copies too
            Message.acked_at.is_not(None), Message.delivery_state.in_(("received", "held")),
            Message.created_at >= start_wall).count()))
        for name, a in agents.items():
            samples["local_backlog"][name].append((at, a.local_backlog()))

    threads = [threading.Thread(target=loop, args=(a,), daemon=True) for a in agents.values()]
    for t in threads:
        t.start()
    while time.monotonic() < hard_stop:
        sample()
        time.sleep(5)
    for t in threads:  # the hard stop: no agent starts anything after the window
        t.join(30)
    stopped_after = round(time.monotonic() - start, 1)
    sample()

    session.expire_all()
    minutes = 2
    names_by_machine = {mid: n for n, mid in machines.items()}

    def minute_of(ts):
        m = int((ts - start_wall).total_seconds() // 60)
        return m if 0 <= m < minutes else None

    # From the database, the source of truth: posts by author and minute (the scripted kick-off and the seed's
    # three posts excluded: they aren't agent replies), and the copies each connector took (its ack) by minute.
    run_posts = [p for p in session.query(GroupPost).filter(GroupPost.conversation_id == group.id,
                                                          GroupPost.created_at >= start_wall)
                 if str(p.id) != kickoff]
    posted = {name: [0] * minutes for name in agents}
    for p in run_posts:
        m = minute_of(p.created_at)
        if m is not None and p.author_agent_id in names_by_machine:
            posted[names_by_machine[p.author_agent_id]][m] += 1
    copies_total = session.query(Message).filter(Message.conversation_id == group.id, Message.route == "group",
                                                 Message.created_at >= start_wall).count()
    delivered = {name: [0] * minutes for name in agents}
    acked_since_start = {name: 0 for name in agents}
    for copy in session.query(Message).filter(Message.conversation_id == group.id, Message.route == "group",
                                              Message.acked_at >= start_wall):
        name = names_by_machine.get(copy.recipient_agent_id)
        if name is None:
            continue
        acked_since_start[name] += 1
        m = minute_of(copy.acked_at)
        if m is not None:
            delivered[name][m] += 1
    per_agent = {name: {"copies_delivered_to_connector": delivered[name],
                        "handed_to_agent_turns": _per_minute(a.events, "turn", minutes),  # distinct copies
                        "posts": posted[name],  # from group_posts
                        "refused_at_limit": [p + q for p, q in zip(_per_minute(a.events, "poll_refused", minutes),
                                                                   _per_minute(a.events, "post_refused_at_limit",
                                                                               minutes))],
                        "local_backlog_at_minute_end": [_at(samples["local_backlog"][name], 60),
                                                        _at(samples["local_backlog"][name], WINDOW + 1)],
                        "requests": _per_minute(a.events, "request", minutes),
                        "re_prompts": sum(1 for _, k in a.events if k == "re_prompt"),
                        "one_to_one_prompts_ignored": sum(1 for _, k in a.events if k == "one_to_one_prompt")}
                 for name, a in agents.items()}

    # The identities the record rests on (R1). Copies a connector acked = distinct copies handed to its agent + its
    # local backlog, give or take the few in flight (being handed over) when the samples were taken.
    in_flight = {}
    for name, a in agents.items():
        handed = len(a.handed)
        backlog_now = a.local_backlog()
        in_flight[name] = acked_since_start[name] - handed - backlog_now
        if abs(in_flight[name]) > 3:  # diagnose: which ids disagree, and what they are
            server_ids = {str(c.id): c for c in session.query(Message).filter(
                Message.recipient_agent_id == machines[name], Message.route == "group")}
            local = {r["id"]: r for r in a.connector.queue.all() if r.get("group")}
            backlog_ids = {i for i, r in local.items() if r.get("state") in a.WAITING and r.get("acked")}
            acked_ids = {i for i, c in server_ids.items() if c.acked_at is not None and c.acked_at >= start_wall}
            extra = (set(a.handed) | backlog_ids) - acked_ids
            both = set(a.handed) & backlog_ids
            raise AssertionError((name, acked_since_start[name], handed, backlog_now, len(both), sorted(
                (local[i]["state"] if i in local else "-", str(server_ids[i].acked_at)[:26] if i in server_ids else "?",
                 server_ids[i].created_at >= start_wall if i in server_ids else "?") for i in extra)))
    # Every post the CLI reported is in the database, once; the database has no agent post the CLI didn't report.
    cli_ids = [pid for a in agents.values() for _, pid in a.cli_posts]
    db_ids = {str(p.id) for p in run_posts}
    assert len(cli_ids) == len(set(cli_ids)) and set(cli_ids) <= db_ids, (len(cli_ids), len(db_ids))
    unreported = db_ids - set(cli_ids)  # made by a reply the hard stop cut after the server stored it
    assert sum(sum(v["posts"]) for v in per_agent.values()) == len(db_ids)

    turns_all = sum(len(a.handed) for a in agents.values())
    requests = sum(sum(v["requests"]) for v in per_agent.values())
    # The run's own copies only (the seed's earlier copies, made before the start, are reported apart): turns taken on
    # them, and those owed, copies made in the run that no agent has had yet.
    run_ids = {str(c.id) for c in session.query(Message).filter(Message.conversation_id == group.id,
                                                                Message.route == "group",
                                                                Message.created_at >= start_wall)}
    turns = sum(len(set(a.handed) & run_ids) for a in agents.values())
    owed = copies_total - turns
    record = {
        "window_seconds": WINDOW, "stopped_after_seconds": stopped_after, "limit_per_minute_per_credential": 120,
        "members_taking_part": 3, "agent_posts": len(db_ids), "copies_made": copies_total,
        "turns_taken": turns, "turns_owed_at_stop": owed, "turns_on_seed_copies": turns_all - turns,
        "identities": {"acked_since_start": acked_since_start, "handed_distinct": {n: len(a.handed) for n, a in
                                                                                   agents.items()},
                       "local_backlog_at_stop": {n: a.local_backlog() for n, a in agents.items()},
                       "stored_ack_pending_at_stop": {n: a.ack_pending() for n, a in agents.items()},
                       "in_flight": in_flight, "posts_cli_reported": len(cli_ids), "posts_in_database": len(db_ids),
                       "posts_stored_but_cut_at_stop": len(unreported),
                       "cli_successes_without_a_new_post": sum(len(a.cli_other) for a in agents.values())},
        "requests_per_turn": round(requests / max(1, turns_all), 1),
        "turns_per_agent_minute": round(turns / 3 / (WINDOW / 60), 1),
        "copies_per_agent_minute": round(copies_total / 3 / (WINDOW / 60), 1),
        "per_agent": per_agent, "samples": samples,
    }

    # Priya comes back online: what her machine gets at once.
    priya = agents["Priya"]
    session.expire_all()
    backlog = session.query(Message).filter(Message.recipient_agent_id == machines["Priya"], Message.route == "group",
                                            Message.acked_at.is_(None), Message.withdrawn_at.is_(None)).count()
    local_before = priya.local_backlog()
    before = len(priya.herdr.prompts)
    time.sleep(61)  # the limit's window passes: she returns to a fresh minute, as after a real absence
    priya.connector.poll(0)  # one poll takes the whole backlog (up to 100 a page)
    taken = priya.local_backlog() + priya.ack_pending()
    turns_back = 0

    def distinct_group_prompts():
        return {text.split("\n", 1)[0].rsplit(" ", 1)[1].rstrip("]") for _, text, _ in priya.herdr.prompts[before:]
                if text.startswith("[RainCLI group post ")}
    for _ in range(taken + 5):  # every iteration hands over one copy: one turn each (acks the limit refused retried)
        priya.connector.retry_acks()
        priya.connector.process_named()
        priya.connector.process()
        turns_back = len(distinct_group_prompts())
        if turns_back >= taken:
            break
    record["offline_return"] = {"offline_seconds": round(WINDOW - OFFLINE_FROM, 1), "copies_waiting_on_server": backlog,
                                "already_held_locally": local_before, "local_backlog_after_one_poll": taken,
                                "turns_to_clear": turns_back}
    rate = turns / max(1.0, WINDOW - 10)  # turns a second, at the limit
    record["extrapolated_5_minutes"] = {
        "method": "the measured rates after the first 10 s held for 300 s; the general limit caps them, so the growth "
                  "is linear once it is reached",
        "posts": round(len(db_ids) / max(1.0, WINDOW - 10) * 300),
        "copies_made": round(copies_total / max(1.0, WINDOW - 10) * 300),
        "turns_taken": round(rate * 300),
        "turns_owed": round(owed / max(1.0, WINDOW - 10) * 300)}
    out = os.environ.get("RAINCLI_C27_OUT")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / "c27.json").write_text(json.dumps(record, indent=2, default=str))
        (Path(out) / "c27.md").write_text(_markdown(record))
    print(json.dumps(record, indent=2, default=str))

    # Evidence, not a gate: the loop ran, every copy went to a member other than its poster, and nothing was lost.
    assert len(db_ids) > 1 and copies_total >= 2 * len(db_ids)
    assert all(sum(v["handed_to_agent_turns"]) for v in per_agent.values())  # every agent took turns
    assert turns_back == taken  # the returning machine hands over its whole local backlog, one turn each


def _identities(i):
    return "; ".join(f"{n}: {i['acked_since_start'][n]} = {i['handed_distinct'][n]} + {i['local_backlog_at_stop'][n]}"
                     + (f" {'+' if i['in_flight'][n] > 0 else '-'} {abs(i['in_flight'][n])} in flight"
                        if i['in_flight'][n] else "") for n in i["acked_since_start"])


def _markdown(r):
    lines = [f"# C27 record ({r['window_seconds']} s window, every agent stopped by {r['stopped_after_seconds']} s; "
             f"limit {r['limit_per_minute_per_credential']}/min per credential)", "",
             f"- Agent posts (from the database, the scripted kick-off excluded): {r['agent_posts']}; copies made: "
             f"{r['copies_made']}; turns taken (distinct copies handed to an agent): {r['turns_taken']}; "
             f"**turns owed at the stop: {r['turns_owed_at_stop']}** (copies made in the run that no agent has had yet: "
             f"in the connectors' local queues or still on the server). Counts are of the run's own copies; the "
             f"seed's earlier copies took {r['turns_on_seed_copies']} more turns at the start.",
             f"- The identities hold: for each agent, copies acked = distinct copies handed + local backlog, give "
             f"or take the few in flight ({_identities(r['identities'])}); every post the CLI reported is in the "
             f"database once ({r['identities']['posts_cli_reported']} reported, {r['identities']['posts_in_database']} "
             f"stored; {r['identities']['posts_stored_but_cut_at_stop']} stored by a reply the hard stop cut before it "
             "could print).",
             f"- Measured: about **{r['requests_per_turn']} requests per turn** (the connector's poll, ack and events, "
             "and the CLI's two for the reply).", "",
             "| Agent | Minute | Copies delivered to the connector | Handed to the agent (turns) | Posts | "
             "Refused at the limit | Local backlog at the minute's end |", "|---|---|---|---|---|---|---|"]
    for name, v in r["per_agent"].items():
        for m in range(len(v["posts"])):
            lines.append(f"| {name} | {m + 1} | {v['copies_delivered_to_connector'][m]} | "
                         f"{v['handed_to_agent_turns'][m]} | {v['posts'][m]} | {v['refused_at_limit'][m]} | "
                         f"{v['local_backlog_at_minute_end'][m]} |")
    smp, o, e = r["samples"], r["offline_return"], r["extrapolated_5_minutes"]
    lines += ["", f"- Local backlog over time (s, records), per agent: "
                  f"{ {n: v for n, v in smp['local_backlog'].items()} }",
              f"- On the server, copies not yet taken (unacked): {smp['server_unacked']}",
              f"- On the server, copies taken but not yet handed over (received or held): {smp['server_taken_not_handed']}",
              f"- Offline return: after {o['offline_seconds']} s offline, {o['copies_waiting_on_server']} copies waited on "
              f"the server (and {o['already_held_locally']} were already held locally); one poll took them all "
              f"({o['local_backlog_after_one_poll']} records waiting locally), and handing them over took "
              f"{o['turns_to_clear']} turns.",
              f"- 5-minute extrapolation: {e['posts']} posts, {e['copies_made']} copies made, {e['turns_taken']} turns "
              f"taken and {e['turns_owed']} more owed ({e['method']}).",
              f"- In this run each agent took about {r['turns_per_agent_minute']} turns a minute at the limit while "
              f"about {r['copies_per_agent_minute']} copies a minute arrived for it, so copies queued faster than agents "
              "could read them: the backlog grew, and every queued copy is a future turn. The limit counts per minute, "
              "so the agents worked in bursts and then waited (the flat stretches in the samples). With real agents, "
              "whose turns take 10-60 s, the backlog grows faster.",
              f"- The general request limit caps the growth (about {r['requests_per_turn']} requests per turn, so "
              f"{r['limit_per_minute_per_credential']} a minute is about "
              f"{round(r['limit_per_minute_per_credential'] / max(1, r['requests_per_turn']))} turns a minute per "
              "agent). The numbers show where that limit caps it, not where agents would stop on their own."]
    return "\n".join(lines) + "\n"
