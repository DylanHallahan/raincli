"""The tray's toasts with several accounts (protocol §23 AS5, A16, A18.4): a delegated account's toast is named
first ("Help Desk: new message from Ryan", "Help Desk: escalation from its agent"), never carries a body, never toasts
"held for approval" or "inbox offline", and none at all when its grant has toasts off (A18.4). With a delegated
account showing, the own account's toasts are named first too. Clicking switches to that account and opens the
thread."""

from __future__ import annotations

import types

from raincli_agent import accounts
from raincli_agent.app import policy

from .test_app_window import FakeServices, fake_tray
from .test_v060_app import entry

HD = {"email": "helpdesk@example.test", "display_name": "Help Desk", "team": "northwind"}
BILL = {"email": "billing@example.test", "display_name": "Billing", "team": "northwind"}


def svc_with(own=(), feeds=None, toasts=None, summaries=None):
    svc = FakeServices()
    svc.notifications = lambda: list(own)
    svc.notify_all = lambda: False
    svc.held_accounts = lambda: [HD, BILL]
    svc.account_notifications = lambda email: list((feeds or {}).get(email, []))
    svc.account_toasts = lambda email: (toasts or {}).get(email, True)
    svc.account_notify_all = lambda email: False
    svc.own_account = lambda: {"email": "maya@example.test", "display_name": "Maya"}
    svc.message_summary = lambda mid, account=None: (summaries or {}).get(
        mid, {"kind": "message", "sender": "Ryan", "conversation_id": "c-" + mid[:4]})
    return svc


class Window:
    def __init__(self, active=None):
        self.active_account, self.switched, self.shown = active, [], 0

    def show(self):
        self.shown += 1

    def switch_to(self, email, path="/app/inbox"):
        self.switched.append((email, path))


def test_the_wording():
    assert policy.account_toast_text("Help Desk", "message", "Ryan") == "Help Desk: new message from Ryan"
    assert policy.account_toast_text("Help Desk", "escalation", "zq-agent-ops") == "Help Desk: escalation from its agent"
    assert policy.account_toast_text("", "message", None) == "Another account: new message from a teammate"


def test_a_delegated_accounts_toasts_are_named_first_and_follow_its_rules(tmp_path):
    esc = entry(kind="escalation")
    feeds = {HD["email"]: [entry(from_kind="app"), entry(), entry(hold="approval"), esc,
                           entry(kind="inbox_offline", from_kind=None)]}
    t = fake_tray(tmp_path, svc_with(feeds=feeds, summaries={esc["id"]: {"kind": "escalation",
                                                                         "sender": "zq-agent-helpdesk-ops",
                                                                         "conversation_id": "c-esc"}}))
    t.window = Window()
    t.toasts()
    assert [n[0] for n in t.icon.notes] == ["Help Desk: new message from Ryan", "Help Desk: escalation from its agent"]
    assert all("zq-" not in n[0] for n in t.icon.notes)  # never an agent's name (A16)
    assert t.toast_account == HD["email"] and t.toast_thread == "c-esc"


def test_toasts_off_for_an_account_show_nothing(tmp_path):
    feeds = {HD["email"]: [entry(from_kind="app")], BILL["email"]: [entry(from_kind="app")]}
    t = fake_tray(tmp_path, svc_with(feeds=feeds, toasts={BILL["email"]: False}))
    t.window = Window()
    t.toasts()
    assert [n[0] for n in t.icon.notes] == ["Help Desk: new message from Ryan"]


def test_with_a_delegated_account_showing_the_own_toasts_are_named_first(tmp_path):
    feeds = {HD["email"]: [entry(from_kind="app")]}
    t = fake_tray(tmp_path, svc_with(own=[entry(from_kind="app")], feeds=feeds))
    t.window = Window(active=HD["email"])
    t.toasts()
    assert [n[0] for n in t.icon.notes] == ["Maya: new message from Ryan", "New message from Ryan"]


def test_clicking_switches_to_the_account_and_opens_the_thread(tmp_path):
    t = fake_tray(tmp_path, svc_with(feeds={HD["email"]: [entry(from_kind="app")]}))
    t.window = Window()
    t.post = lambda fn, *a: fn(*a)
    t.toasts()
    t.toast_clicked()
    assert t.window.switched == [(HD["email"], f"/app/conversations/{t.toast_thread}")]
    # An own toast while Help Desk shows: back to the own account.
    t = fake_tray(tmp_path, svc_with(own=[entry(from_kind="app")]))
    t.window = Window(active=HD["email"])
    t.post = lambda fn, *a: fn(*a)
    t.toasts()
    t.toast_clicked()
    assert t.window.switched[-1][0] is None


def test_the_grant_flag_is_recorded_per_account(tmp_path):
    cfg = str(tmp_path / "agent.json")
    assert accounts.account_toasts(cfg, HD["email"]) is True  # not recorded yet: on
    accounts.save_grant_facts(cfg, HD["email"], toasts=False, kind="all_members")
    assert accounts.account_toasts(cfg, HD["email"]) is False and accounts.account_toasts(cfg, BILL["email"]) is True
    accounts.save_grant_facts(cfg, HD["email"], toasts=True, kind="individual")
    assert accounts.account_toasts(cfg, HD["email"]) is True
