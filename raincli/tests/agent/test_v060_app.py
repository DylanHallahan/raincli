"""The Windows app's v0.6.0 pieces (protocol §21 C9.1, C9.5, C9.6, C13 H3 and L4).

- This computer's inbox picker: ``Services.inbox``/``set_inbox``/``clear_inbox`` over ``/api/v1/me/inbox`` with
  the machine credential, and the js_api actions the page calls (nonce-guarded like Connect/Disconnect).
- PR6 toasts: the routed notification feed (``person.NotificationFeed``), the facts it records, the inbox-offline
  entry, ``policy.toast_wanted`` and the tray, and the "Notify me about every message" setting.
- The People rail and the handoff to ``/app/people``.
"""

from __future__ import annotations

import json
import re
import uuid

import pytest

from raincli_agent import person
from raincli_agent.app import policy
from raincli_agent.app.services import ServiceError, Services
from raincli_agent.app.window import _HANDOFF_NEXT, LOCAL_DIR
from raincli_agent.errors import error_for

from .test_app_window import SERVICE, FakeIcon, FakeServices, fake_tray, make, nonce_of, LOCAL

HANDLE = "alice-laptop"


# -- fakes ------------------------------------------------------------------------------------------------

class FakeClient:
    """The machine-credential ``ApiClient`` as ``Services`` uses it: ``me()`` and ``request``."""

    def __init__(self, inbox=None, candidates=(), waiting=0, put=None, team="Alpha"):
        self.data = {"inbox": inbox, "candidates": list(candidates), "waiting": waiting}
        self.put = put or {"inbox": None, "moved": 0, "stayed": 0, "delivered_waiting": 0}
        self.team = team
        self.calls = []

    def me(self):
        return {"agent": {"handle": HANDLE, "team": {"slug": "alpha", "name": self.team}}}

    def request(self, method, path, *, body=None, query=None, **kw):
        self.calls.append((method, path, body))
        if (method, path) == ("GET", "/me/inbox"):
            return 200, self.data
        if (method, path) == ("PUT", "/me/inbox"):
            if isinstance(self.put, Exception):
                raise self.put
            return 200, self.put
        raise AssertionError(path)


def services(tmp_path, client, teams=1):
    s = Services(None, None, paths=lambda: (str(tmp_path / "agent.json"), str(tmp_path / "runtime.json")))
    s._client = lambda token=None, timeout=15: client
    s._person_teams = lambda: teams
    return s


CANDIDATES = [{"machine": HANDLE, "agent": "api-agent", "live": True, "suggested": True},
              {"machine": HANDLE, "agent": "reviewer", "live": False, "suggested": False},
              {"machine": "jon-laptop", "agent": "rev", "live": True, "suggested": True}]


# -- This computer: the inbox picker (C9.1) ---------------------------------------------------------------

def test_no_inbox_offers_this_computers_agents_and_the_waiting_tick(tmp_path):
    out = services(tmp_path, FakeClient(candidates=CANDIDATES, waiting=3)).inbox()
    assert out["status"] == "No inbox yet" and out["inbox"] is None
    assert out["use_label"] == "Use as my inbox"
    # C13 L4: this machine's credential picks only agents on this machine; jon-laptop's agent is not offered.
    assert [c["agent"] for c in out["candidates"]] == ["api-agent", "reviewer"]
    assert out["candidates"][0] == {"agent": "api-agent", "live": True, "suggested": True, "current": False}
    assert "jon-laptop" not in json.dumps(out)
    assert out["waiting"] == 3 and out["waiting_label"] == "Also deliver the 3 waiting messages"
    assert out["stayed_label"] is None
    one = services(tmp_path, FakeClient(candidates=CANDIDATES, waiting=1)).inbox()
    assert one["waiting_label"] == "Also deliver the waiting message"
    none = services(tmp_path, FakeClient(candidates=CANDIDATES)).inbox()
    assert none["waiting"] == 0 and none["waiting_label"] is None


def test_the_status_line_names_this_computer_or_the_other_machine(tmp_path):
    here = services(tmp_path, FakeClient(inbox={"machine": HANDLE, "agent": "api-agent", "set_at": "t"},
                                         candidates=CANDIDATES)).inbox()
    assert here["status"] == "Your inbox: api-agent on this computer"
    assert here["inbox"] == {"agent": "api-agent", "here": True}
    assert [c["current"] for c in here["candidates"]] == [True, False]
    there = services(tmp_path, FakeClient(inbox={"machine": "jon-laptop", "agent": "rev", "set_at": "t"},
                                          candidates=CANDIDATES)).inbox()
    assert there["status"] == "Your inbox: rev on jon-laptop" and there["inbox"]["here"] is False
    assert not any(c["current"] for c in there["candidates"])


def test_several_teams_name_this_machines_team(tmp_path):
    """C13 H3: one inbox per (person, team); the machine credential's team is the one."""
    out = services(tmp_path, FakeClient(candidates=CANDIDATES, team="Beta"), teams=2).inbox()
    assert out["use_label"] == "Use as my inbox in Beta" and out["status"] == "No inbox yet in Beta"
    out = services(tmp_path, FakeClient(inbox={"machine": HANDLE, "agent": "api-agent"}, team="Beta"), teams=2).inbox()
    assert out["status"] == "Your inbox in Beta: api-agent on this computer"
    assert services(tmp_path, FakeClient(team="Beta"), teams=1).inbox()["use_label"] == "Use as my inbox"


def test_set_sends_this_machine_and_the_waiting_choice(tmp_path):
    client = FakeClient(put={"inbox": {"machine": HANDLE, "agent": "api-agent"}, "moved": 0, "stayed": 0,
                             "delivered_waiting": 3})
    s = services(tmp_path, client)
    assert s.set_inbox("api-agent") == ("Your inbox is now api-agent on this computer. "
                                        "3 waiting messages were delivered to it.")
    assert client.calls[-1] == ("PUT", "/me/inbox", {"machine": HANDLE, "agent": "api-agent", "deliver_waiting": True})
    s.set_inbox("api-agent", False)
    assert client.calls[-1][2]["deliver_waiting"] is False


def test_a_change_with_stayed_messages_says_so_until_dismissed(tmp_path):
    client = FakeClient(inbox={"machine": HANDLE, "agent": "reviewer"}, candidates=CANDIDATES,
                        put={"inbox": {}, "moved": 4, "stayed": 2, "delivered_waiting": 0})
    s = services(tmp_path, client)
    assert s.set_inbox("reviewer") == "Your inbox is now reviewer on this computer. 4 messages were moved to it."
    assert s.inbox()["stayed_label"] == "2 messages are with your previous inbox"
    assert s.inbox()["stayed_label"] == "2 messages are with your previous inbox"  # until dismissed
    s.dismiss_inbox_notice()
    assert s.inbox()["stayed_label"] is None
    client.put = {"inbox": {}, "moved": 0, "stayed": 1, "delivered_waiting": 0}
    s.set_inbox("reviewer")
    assert s.inbox()["stayed_label"] == "1 message is with your previous inbox"
    client.put = {"inbox": {}, "moved": 0, "stayed": 0, "delivered_waiting": 0}
    s.set_inbox("api-agent")
    assert s.inbox()["stayed_label"] is None


def test_clear_and_refused_names(tmp_path):
    client = FakeClient(put={"inbox": None, "moved": 2, "stayed": 0, "delivered_waiting": 0})
    s = services(tmp_path, client)
    assert s.clear_inbox() == ("You have no inbox now: teammates' messages wait in your app. "
                               "2 messages went back to your app.")
    assert client.calls[-1] == ("PUT", "/me/inbox", {"none": True})
    before = len(client.calls)
    for bad in ("", "-rm", " x", "x" * 65, None, 3):
        with pytest.raises(ServiceError):
            s.set_inbox(bad)
    assert len(client.calls) == before


# -- the js_api the page calls (nonce-guarded, like Connect/Disconnect) -----------------------------------

class InboxServices(FakeServices):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.sets, self.cleared, self.dismissed, self.fail = [], 0, 0, None

    def inbox(self):
        if self.fail:
            raise self.fail
        return {"status": "No inbox yet", "candidates": [{"agent": "api-agent"}]}

    def set_inbox(self, agent, deliver_waiting=True):
        if self.fail:
            raise self.fail
        self.sets.append((agent, deliver_waiting))
        return f"Your inbox is now {agent} on this computer."

    def clear_inbox(self):
        self.cleared += 1
        return "You have no inbox now: teammates' messages wait in your app."

    def dismiss_inbox_notice(self):
        self.dismissed += 1


def test_the_inbox_actions_need_the_nonce_and_report_text(tmp_path):
    svc = InboxServices()
    app, win, _ = make(tmp_path, svc)
    api = app.api
    app.load(LOCAL + "this-computer.html")
    app.on_loaded()
    nonce = nonce_of(win)
    for call in (lambda n: api.inbox(n), lambda n: api.set_inbox(n, "api-agent"), lambda n: api.clear_inbox(n),
                 lambda n: api.dismiss_inbox_notice(n)):
        with pytest.raises(PermissionError):
            call("stale")
    assert svc.sets == [] and svc.cleared == 0 and svc.dismissed == 0
    assert api.inbox(nonce) == {"ok": True, "status": "No inbox yet", "candidates": [{"agent": "api-agent"}]}
    assert api.set_inbox(nonce, "api-agent")["message"] == "Your inbox is now api-agent on this computer."
    api.set_inbox(nonce, "api-agent", False)
    assert svc.sets == [("api-agent", True), ("api-agent", False)]  # ticked by default
    assert api.clear_inbox(nonce)["ok"] and svc.cleared == 1
    assert api.dismiss_inbox_notice(nonce)["ok"] and svc.dismissed == 1
    svc.fail = error_for(400, "inbox_invalid", "That agent on alice-laptop can't be your inbox")
    refused = api.set_inbox(nonce, "x")
    assert refused["ok"] is False and refused["message"].startswith("Not changed: ")
    assert api.inbox(nonce) == {"ok": False, "message": "Couldn't read your inbox. Check the connection and try again."}


def test_this_computer_page_has_the_picker_and_follows_the_page_rules():
    html = (LOCAL_DIR / "this-computer.html").read_text("utf-8")
    js = (LOCAL_DIR / "this-computer.js").read_text("utf-8")
    for el in ("inbox-status", "inbox-change", "inbox-candidates", "inbox-waiting", "inbox-stayed-dismiss",
               "inbox-clear"):
        assert f'id="{el}"' in html, el
    assert re.search(r'<input type="checkbox" id="inbox-waiting" checked>', html)  # ticked by default
    assert ">Change</button>" in html
    for action in ('rc.read("inbox")', 'rc.call("set_inbox"', 'rc.call("clear_inbox")',
                   'rc.call("dismiss_inbox_notice")'):
        assert action in js, action
    assert "innerHTML" not in js  # names are text, never markup
    settings = (LOCAL_DIR / "settings.html").read_text("utf-8")
    assert 'id="notify-all"' in settings and "Notify me about every message" in settings
    assert 'id="notify-all" checked' not in settings  # off by default
    assert 'id="inbox-status"' in settings  # "Your inbox: …" in Settings too (acceptance 8)


# -- toast rules (PR6, C9.5) -------------------------------------------------------------------------------

def entry(**kw):
    base = {"id": str(uuid.uuid4()), "kind": "message", "at": "t", "from_kind": "agent", "hold": None,
            "waiting": False}
    base.update(kw)
    return base


def test_toast_wanted_fires_only_for_what_needs_the_person():
    assert policy.toast_wanted(entry(from_kind="app"))  # a teammate wrote it by hand
    assert policy.toast_wanted(entry(hold="approval"))  # held for the person's approval
    assert policy.toast_wanted(entry(waiting=True))  # waiting: no inbox
    assert policy.toast_wanted(entry(kind="escalation"))
    assert policy.toast_wanted(entry(kind="inbox_offline", from_kind=None))
    assert not policy.toast_wanted(entry())  # agent-sent, received by the inbox: quiet
    assert policy.toast_wanted(entry(), every=True)  # "Notify me about every message"
    assert policy.toast_wanted(entry(from_kind=None))  # a pre-v0.6 entry or server: as before


def tray_with(tmp_path, entries, every=False, summaries=None):
    svc = FakeServices()
    svc.notifications = lambda: entries
    svc.notify_all = lambda: every
    svc.message_summary = lambda mid: (summaries or {}).get(
        mid, {"kind": "message", "sender": "Jon", "conversation_id": "c-" + mid[:4]})
    return fake_tray(tmp_path, svc)


def test_the_tray_toasts_each_pr6_case_and_stays_quiet_otherwise(tmp_path):
    quiet = entry()
    cases = [entry(from_kind="app"), entry(hold="approval"), entry(waiting=True), entry(kind="escalation")]
    summaries = {cases[3]["id"]: {"kind": "escalation", "sender": "Jon", "conversation_id": "c9"}}
    t = tray_with(tmp_path, [quiet, *cases], summaries=summaries)
    t.toasts()
    assert [n[0] for n in t.icon.notes] == ["New message from Jon",
                                            "A message from Jon is waiting for your approval",
                                            "New message from Jon", "Escalation from Jon"]
    assert t.toast_thread == "c9"


def test_the_setting_makes_every_message_toast_and_is_off_by_default(tmp_path):
    t = tray_with(tmp_path, [entry(), entry()], every=True)
    t.toasts()
    assert len(t.icon.notes) == 2
    t = tray_with(tmp_path, [entry(), entry()])
    t.toasts()
    assert t.icon.notes == []
    svc = FakeServices()  # no notify_all at all: the default
    svc.notifications = lambda: [entry()]
    t = fake_tray(tmp_path, svc)
    t.toasts()
    assert t.icon.notes == []


def test_the_inbox_offline_toast_opens_this_computer(tmp_path):
    t = tray_with(tmp_path, [entry(kind="inbox_offline", from_kind=None)])
    t.toasts()
    assert t.icon.notes == [(policy.INBOX_OFFLINE_TOAST, "RainCLI")] and t.toast_opens == "this-computer"
    posted = []
    t.post = lambda fn, *a: posted.append(fn.__name__)
    t.toast_clicked()
    assert posted == ["open_this_computer"]


def test_the_setting_is_stored_with_the_apps_local_settings(tmp_path):
    s = Services(None, None, paths=lambda: (str(tmp_path / "agent.json"), str(tmp_path / "runtime.json")))
    s._client = lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))
    assert s.notify_all() is False and s.settings()["notify_all"] is False
    assert "every message" in s.save_settings({"notify_all": True})
    assert s.notify_all() is True and s.settings()["notify_all"] is True
    stored = json.loads((person.notify_dir(str(tmp_path / "agent.json")) / "settings.json").read_text())
    assert stored == {"notify_all": True}
    with pytest.raises(ServiceError):
        s.save_settings({"notify_all": "yes"})
    s.save_settings({"notify_all": False})
    assert s.notify_all() is False


def test_toast_sender_is_the_person_label_never_a_machine():
    assert policy.toast_sender({"from_endpoint": {"person": "jon@x.example", "display_name": "Jon"},
                                "from": "@jon@x.example", "body": "secret"}) == "Jon"
    assert policy.toast_sender({"from_endpoint": {"person": "jon@x.example", "display_name": None}}) == "jon@x.example"
    assert policy.toast_sender({"from_endpoint": {"person": "jon@x.example", "display_name": " "}}) == "jon@x.example"
    for machine_form in ({"from_endpoint": {"machine": "jon-laptop", "agent": "rev"}, "from": "jon-laptop/rev"},
                         {"from_endpoint": {"machine": "jon-laptop"}, "from": "jon-laptop"},
                         {"from": "jon-laptop"}, {}, None):
        assert policy.toast_sender(machine_form) == "a teammate", machine_form
    assert policy.toast_sender({"from_endpoint": {"machine": "jon-laptop"}, "from_owner": "Jon"}) == "Jon"
    assert policy.toast_sender({"from_endpoint": {"machine": "jon-laptop"}, "from": "@jon@x.example"}) == "jon@x.example"


# -- the routed feed (C9.5) ---------------------------------------------------------------------------------

ME, JON = "alice@x.example", "jon@x.example"


def msg(seq, *, from_kind="agent", kind="message", sender=JON, hold=None):
    return {"id": str(uuid.uuid4()), "seq": seq, "kind": kind, "from_kind": from_kind, "body": f"secret {seq}",
            "from_endpoint": {"person": sender, "display_name": "Jon Secretname"},
            "to_endpoint": {"person": ME, "display_name": "Alice"}, "hold_reason": hold,
            "delivery_state": "held" if hold else "stored"}


class FeedClient:
    """``PersonClient.inbox``: ``routed`` returns everything; without it, only what is addressed to the person."""

    def __init__(self, routed, direct=()):
        self.routed, self.direct, self.calls = routed, list(direct), []
        self.poll_backoff = None

    def inbox(self, *, after=0, wait=0, routed=False, **kw):
        self.calls.append({"after": after, "wait": wait, "routed": routed})
        page = [m for m in (self.routed if routed else self.direct) if m["seq"] > after]
        return page, max([after, *(m["seq"] for m in page)])


def feed_for(tmp_path, client, **kw):
    feed = person.NotificationFeed(str(tmp_path / "agent.json"), client_factory=lambda: client,
                                   now=kw.pop("now", lambda: 1_000_000.0), **kw)
    feed.session_id = lambda: "s1"
    return feed


def test_the_feed_polls_routed_and_records_the_toast_facts(tmp_path):
    by_hand = msg(1, from_kind="app")
    into_inbox = msg(2)  # an agent's message, routed to the person's inbox: quiet
    approval = msg(3, hold="approval_required")
    waiting = msg(4)  # addressed to the person, who has no inbox
    escalation = msg(5, kind="escalation", sender=ME)  # from their own agent
    own = msg(6, sender=ME)  # their own agent's plain message to them: not "waiting" (C13 H2)
    client = FeedClient([by_hand, into_inbox, approval, waiting, escalation, own], direct=[waiting, escalation, own])
    feed = feed_for(tmp_path, client)
    assert feed.poll_once(wait=25) == 6
    assert client.calls == [{"after": 0, "wait": 25, "routed": True}, {"after": 0, "wait": 0, "routed": False}]
    taken = person.take_notifications(str(tmp_path / "agent.json"), now=1_000_000.0)
    facts = [(e["kind"], e["from_kind"], e["hold"], e["waiting"]) for e in taken]
    assert facts == [("message", "app", None, False), ("message", "agent", None, False),
                     ("message", "agent", "approval", False), ("message", "agent", None, True),
                     ("escalation", "agent", None, False), ("message", "agent", None, False)]
    assert [policy.toast_wanted(e) for e in taken] == [True, False, True, True, True, False]
    raw = (person.notify_dir(str(tmp_path / "agent.json")) / "queue.json").read_text()
    assert "secret" not in raw and "Secretname" not in raw and JON not in raw  # no body, no name (C14)
    client.calls.clear()
    assert feed.poll_once(wait=25) == 0
    assert client.calls == [{"after": 6, "wait": 25, "routed": True}]  # nothing new: no second read


def test_a_server_hold_of_no_inbox_counts_as_waiting_and_redelivery_toasts_once(tmp_path):
    first = msg(1, hold="no_inbox")
    client = FeedClient([first])
    feed = feed_for(tmp_path, client)
    feed.poll_once(wait=0)
    [e] = person.take_notifications(str(tmp_path / "agent.json"), now=1_000_000.0)
    assert e["waiting"] is True
    client.inbox = lambda **kw: ([dict(first)], 1)  # a re-target redelivers it below the cursor (C13 H1)
    assert feed.poll_once(wait=0) == 0  # once per arrival


def test_the_person_client_sends_routed():
    seen = []

    class Api:
        timeout, retry_after = 15, None

        def request(self, method, path, *, query=None, timeout=None, **kw):
            seen.append(query)
            return 200, {"messages": [], "cursor": 0}
    client = person.PersonClient(Api())
    client.inbox(after=3, wait=0, routed=True)
    client.inbox(after=3, wait=0)
    assert seen[0]["routed"] == 1 and "routed" not in seen[1]


# -- the inbox offline for more than an hour (PR6) -----------------------------------------------------

def test_inbox_offline_toasts_once_after_an_hour_and_rearms(tmp_path):
    cfg = str(tmp_path / "agent.json")
    t0 = 1_000_000.0
    assert person.record_inbox_presence(cfg, "offline", t0) is False
    assert person.record_inbox_presence(cfg, "offline", t0 + 3000) is False
    assert person.record_inbox_presence(cfg, "offline", t0 + 3601) is True
    assert person.record_inbox_presence(cfg, "offline", t0 + 9000) is False  # one toast
    assert person.record_inbox_presence(cfg, None, t0 + 9100) is False  # not known: no change
    assert [e["kind"] for e in person.take_notifications(cfg, now=t0 + 9100)] == ["inbox_offline"]
    assert person.record_inbox_presence(cfg, "ready", t0 + 9200) is False  # back: re-armed
    assert person.record_inbox_presence(cfg, "offline", t0 + 9300) is False
    assert person.record_inbox_presence(cfg, "offline", t0 + 9300 + 3601) is True
    person.record_inbox_presence(cfg, "no_inbox", t0 + 20000)  # no inbox is not "offline": re-armed too
    assert person.record_inbox_presence(cfg, "offline", t0 + 20001) is False


class PeopleClient:
    def __init__(self, presence):
        self.presence, self.calls = presence, 0

    def request(self, method, path, **kw):
        assert (method, path) == ("GET", "/people")
        self.calls += 1
        return 200, {"people": [{"email": JON, "presence": "ready"},
                                {"email": "Alice@X.example", "presence": self.presence}]}


def test_the_feed_reads_the_persons_own_presence_from_people_every_minute(tmp_path):
    clock = [1_000_000.0]
    machine = PeopleClient("offline")
    feed = feed_for(tmp_path, FeedClient([]), machine_factory=lambda: machine, now=lambda: clock[0])
    feed._email = ME
    assert feed.check_inbox() is False and machine.calls == 1
    clock[0] += 30
    assert feed.check_inbox() is None and machine.calls == 1  # not yet a minute
    clock[0] += 3600
    assert feed.check_inbox() is True
    assert [e["kind"] for e in person.take_notifications(str(tmp_path / "agent.json"), now=clock[0])] == \
        ["inbox_offline"]


def test_an_old_server_without_people_only_logs_once(tmp_path):
    logs = []

    class Old:
        def request(self, method, path, **kw):
            raise error_for(404, "not_found", "not found")
    clock = [0.0]
    feed = feed_for(tmp_path, FeedClient([]), machine_factory=Old, now=lambda: clock[0], log=logs.append)
    feed._email = ME
    for _ in range(3):
        clock[0] += 61
        feed._check_inbox()
    assert len(logs) == 1 and logs[0].startswith("notification feed: inbox check ") and "not_found" in logs[0]


# -- the People rail and handoff (C9.6) ------------------------------------------------------------------

def test_the_rail_says_people():
    for page in ("this-computer.html", "settings.html"):
        html = (LOCAL_DIR / page).read_text("utf-8")
        assert 'data-open="people"' in html and "</svg>People</button>" in html, page
        assert 'data-open="agents"' not in html and ">Agents</button>" not in html, page


def test_people_opens_hosted_people_and_agents_still_does(tmp_path):
    assert _HANDOFF_NEXT.fullmatch("/app/people") and _HANDOFF_NEXT.fullmatch("/app/agents")
    app, win, _ = make(tmp_path)
    app.open_section("people")
    assert win.loads[-1] == f"{SERVICE}/app/handoff?code=c1&next=/app/people"
    win.url = SERVICE + "/app/inbox"  # a server that ignores next: the pending path follows
    app.on_loaded()
    assert win.loads[-1] == SERVICE + "/app/people"
    app.open_section("agents")  # an older page's button
    assert win.loads[-1] == SERVICE + "/app/people"
