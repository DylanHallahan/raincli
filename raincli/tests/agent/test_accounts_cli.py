"""The account switching CLI contract (protocol §23 A11, A14 Q9/Q10, A19 E4; docs/raincli-cli-json.md) and the
runtime's per-account pollers (A10, A18.4), against the fake server: ``raincli accounts``, ``--as`` on every
``raincli me`` command, the merged ``--all-accounts`` watch, schema 1, the exit codes, and that no session ever
reaches the output. The JSON keys and types here are the contract: change them only additively."""
import json
import threading

import pytest

from raincli_agent import account_feeds, accounts, cli, person
from raincli_agent.errors import EXIT_NOT_GRANTED, EXIT_USAGE

from .test_login import EMAIL
from .test_person import account, agent, home, sign_in  # noqa: F401  (fixtures)

pytestmark = pytest.mark.usefixtures("home")
HELPDESK, RELEASE, BILLING = "helpdesk@example.com", "release@example.com", "billing@example.com"


@pytest.fixture
def granted(account, home):  # noqa: F811
    """A signed-in machine with a person session, and three grants: Help Desk (individual, toasts on), Release (all
    members, toasts off) and Billing (individual, not added by anything)."""
    st = account.state
    sign_in(account, home)
    for email, name in ((HELPDESK, "Help Desk"), (RELEASE, "Release"), (BILLING, "Billing")):
        st.persons[email] = name
    st.grants += [{"grantee": EMAIL, "account": HELPDESK, "team": "alpha", "toasts": True},
                  {"grantee": None, "account": RELEASE, "team": "alpha", "toasts": False},
                  {"grantee": EMAIL, "account": BILLING, "team": "alpha", "toasts": True}]
    st.unread.update({EMAIL: 2, HELPDESK: 3, RELEASE: 1})
    return account


def run(home, capsys, *argv):
    code = cli.main(["--config", agent(home), *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def tokens_in(text, st):
    return [t for t in st.person_sessions if t in text]


# -- raincli accounts -------------------------------------------------------------------------------------------

def test_accounts_json_is_schema_1_with_every_key(granted, home, capsys):
    code, out, err = run(home, capsys, "me", "inbox", "--as", HELPDESK, "--json")  # adds Help Desk on first use
    assert code == 0, err
    code, out, err = run(home, capsys, "accounts", "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["schema"] == 1 and set(data) == {"schema", "accounts"}
    rows = {r["email"]: r for r in data["accounts"]}
    for r in rows.values():
        assert set(r) == {"email", "display_name", "team", "kind", "grant", "added", "unread", "toasts", "active"}
        assert isinstance(r["added"], bool) and isinstance(r["active"], bool)
        assert r["unread"] is None or isinstance(r["unread"], int)
    assert (rows[EMAIL]["kind"], rows[EMAIL]["grant"], rows[EMAIL]["added"], rows[EMAIL]["unread"],
            rows[EMAIL]["active"]) == ("own", None, True, 2, True)
    assert (rows[HELPDESK]["kind"], rows[HELPDESK]["grant"], rows[HELPDESK]["added"], rows[HELPDESK]["unread"],
            rows[HELPDESK]["toasts"]) == ("granted", "individual", True, 3, True)
    assert (rows[RELEASE]["grant"], rows[RELEASE]["added"], rows[RELEASE]["unread"], rows[RELEASE]["toasts"]) == (
        "all_members", False, None, False)  # available, never added by itself (A18.3)
    assert rows[BILLING]["added"] is False
    assert list(rows)[0] == EMAIL  # the own account first
    assert tokens_in(out + err, granted.state) == []


def test_accounts_reads_the_windows_active_choice(granted, home, capsys):
    run(home, capsys, "me", "inbox", "--as", HELPDESK, "--json")
    accounts.save_active(agent(home), HELPDESK)
    rows = {r["email"]: r for r in json.loads(run(home, capsys, "accounts", "--json")[1])["accounts"]}
    assert rows[HELPDESK]["active"] is True and rows[EMAIL]["active"] is False
    code, out, _ = run(home, capsys, "accounts")
    assert code == 0 and f"* Help Desk <{HELPDESK}> (added, individual grant, 3 unread)" in out


# -- --as ---------------------------------------------------------------------------------------------------------

def test_as_resolves_by_email_then_name_and_adds_on_first_use(granted, home, capsys):
    st = granted.state
    code, out, err = run(home, capsys, "me", "inbox", "--as", "Help Desk", "--json")  # by display name
    assert code == 0, err
    held = accounts.find_account(agent(home), HELPDESK)
    assert held is not None and st.person_sessions[held.session.reveal()]["acting"] == EMAIL
    code, out, _ = run(home, capsys, "me", "inbox", "--as", "HELPDESK@example.com", "--json")  # no second add
    assert code == 0 and len([s for s in st.person_sessions.values() if s.get("acting")]) == 1
    code, out, _ = run(home, capsys, "me", "inbox", "--as", EMAIL, "--json")  # the own account by email
    assert code == 0


def test_every_me_json_object_carries_schema_and_account(granted, home, capsys):
    st = granted.state
    st.person_inbox[HELPDESK] = [{"id": "11111111-1111-4111-8111-111111111111", "seq": 1, "body": "hi",
                                  "from": "@jon@example.com", "kind": "message"}]
    code, out, _ = run(home, capsys, "me", "inbox", "--as", HELPDESK, "--json")
    line = json.loads(out.splitlines()[0])
    assert (line["schema"], line["account"], line["id"]) == (1, HELPDESK, "11111111-1111-4111-8111-111111111111")
    code, out, _ = run(home, capsys, "me", "inbox", "--json")
    assert all(json.loads(x)["account"] == EMAIL for x in out.splitlines())


def test_unknown_ungranted_and_ambiguous(granted, home, capsys):
    st = granted.state
    code, out, err = run(home, capsys, "me", "inbox", "--as", "nobody@example.com", "--json")
    assert code == EXIT_NOT_GRANTED == 7
    assert json.loads(out) == {"schema": 1, "error": {"code": "not_granted",
                                                       "message": "nobody@example.com isn't an account this "
                                                                  "machine may act as"}}
    code, out, err = run(home, capsys, "me", "inbox", "--as", "nobody@example.com")
    assert code == 7 and "isn't an account this machine may act as" in err
    st.persons[RELEASE] = "Help Desk"  # two accounts named alike
    code, out, err = run(home, capsys, "me", "inbox", "--as", "Help Desk")
    assert code == EXIT_USAGE and HELPDESK in err and RELEASE in err


def test_as_on_approve_is_a_usage_error(granted, home, capsys):
    code, _, err = run(home, capsys, "me", "approve", "00000000-0000-4000-8000-000000000000", "--as", HELPDESK)
    assert code == EXIT_USAGE and "approved on the machine that holds it" in err


def test_a_reply_the_grant_refuses_exits_7_with_the_servers_reason(granted, home, capsys, tmp_path, monkeypatch):
    """A19 E4: under an all-members grant, replying to the account's own traffic is refused: exit 7."""
    st = granted.state
    mid = "22222222-2222-4222-8222-222222222222"
    st.self_traffic.add(mid)
    st.messages[mid] = {"id": mid}
    monkeypatch.setattr(st, "render", lambda m, reader_email=None: {
        "id": mid, "seq": 1, "kind": "escalation", "body": "ok?", "from": "@" + RELEASE, "to": "@" + RELEASE,
        "conversation_id": "66666666-6666-4666-8666-666666666666", "created_at": "2026-11-20T10:00:00Z",
        "attachments": []})
    body = tmp_path / "b.txt"
    body.write_text("ok")
    try:
        code, out, err = run(home, capsys, "me", "reply", mid, "--as", RELEASE, "--body-file", str(body), "--json")
    finally:
        st.messages.pop(mid)
    assert code == 7, err
    assert json.loads(out.splitlines()[-1])["error"]["code"] == "delegated_scope"
    assert st.sent_as == []


def test_sign_out_as_removes_only_that_account(granted, home, capsys):
    st = granted.state
    run(home, capsys, "me", "inbox", "--as", HELPDESK, "--json")
    token = accounts.find_account(agent(home), HELPDESK).session.reveal()
    code, out, err = run(home, capsys, "me", "sign-out", "--as", HELPDESK)
    assert code == 0 and "the grant stays" in out
    assert token not in st.person_sessions and accounts.find_account(agent(home), HELPDESK) is None
    assert person.load_session(agent(home)) is not None  # the own session stays
    assert any(g["account"] == HELPDESK for g in st.grants)


# -- --watch --all-accounts ----------------------------------------------------------------------------------------

def test_the_merged_watch_tags_each_message_with_its_account(granted, home, capsys):
    st = granted.state
    run(home, capsys, "me", "inbox", "--as", HELPDESK, "--json")
    same = "33333333-3333-4333-8333-333333333333"  # one message relevant to both accounts keeps both tags
    st.person_inbox[EMAIL] = [{"id": same, "seq": 1, "body": "a"}]
    st.person_inbox[HELPDESK] = [{"id": same, "seq": 1, "body": "a"},
                                 {"id": "44444444-4444-4444-8444-444444444444", "seq": 2, "body": "b"}]
    code, out, err = run(home, capsys, "me", "inbox", "--watch", "--all-accounts", "--json", "--timeout", "3")
    assert code == 0, err
    records = [json.loads(x) for x in out.splitlines()]
    assert all(set(r) == {"schema", "account", "message"} and r["schema"] == 1 for r in records)
    assert sorted((r["account"], r["message"]["id"]) for r in records) == sorted(
        [(EMAIL, same), (HELPDESK, same), (HELPDESK, "44444444-4444-4444-8444-444444444444")])
    assert not [r for r in st.requests if "/ack" in r[1]]  # never acks
    assert tokens_in(out + err, st) == []


def test_all_accounts_needs_watch_and_no_as(granted, home, capsys):
    assert run(home, capsys, "me", "inbox", "--all-accounts")[0] == EXIT_USAGE
    assert run(home, capsys, "me", "inbox", "--watch", "--all-accounts", "--as", HELPDESK)[0] == EXIT_USAGE


# -- the runtime's per-account pollers (A10, A18.4, A19 E4) -----------------------------------------------------------

def test_an_account_poller_feeds_its_own_queue_and_records_the_grant(granted, home, capsys):
    st = granted.state
    run(home, capsys, "me", "inbox", "--as", RELEASE, "--json")
    st.person_inbox[RELEASE] = [{"id": "55555555-5555-4555-8555-555555555555", "seq": 7, "kind": "escalation",
                                 "from_kind": "agent"}]
    feed = account_feeds.AccountFeed(agent(home), RELEASE)
    assert feed.poll_once(wait=0) == 1
    taken = person.take_notifications(agent(home), folder=accounts.account_notify_dir(agent(home), RELEASE))
    assert [(t["id"], t["kind"]) for t in taken] == [("55555555-5555-4555-8555-555555555555", "escalation")]
    assert person.take_notifications(agent(home)) == []  # never in the own feed
    assert accounts.account_toasts(agent(home), RELEASE) is False  # all-members: toasts off (A18.4)
    st.grants.append({"grantee": EMAIL, "account": RELEASE, "team": "alpha", "toasts": True})
    feed.poll_once(wait=0)  # the flag is re-read every cycle (A19 E4)
    assert accounts.account_toasts(agent(home), RELEASE) is True


def test_a_401_drops_the_account_with_a_notice(granted, home, capsys):
    st = granted.state
    run(home, capsys, "me", "inbox", "--as", HELPDESK, "--json")
    st.grants = [g for g in st.grants if g["account"] != HELPDESK]  # the grant is removed on the server
    feed = account_feeds.AccountFeed(agent(home), HELPDESK, sleep=lambda s: None)
    feed.run()
    assert feed.dropped and accounts.find_account(agent(home), HELPDESK) is None
    assert accounts.take_dropped(agent(home)) == [{"email": HELPDESK, "display_name": "Help Desk"}]
    assert accounts.take_dropped(agent(home)) == []


def test_the_feed_set_follows_accounts_json(granted, home, capsys):
    started = []

    class Fake:
        def __init__(self, email):
            self.email, self.stop = email, threading.Event()

        def start(self):
            started.append(self.email)
    feeds = account_feeds.AccountFeeds(agent(home), feed_factory=Fake)
    assert feeds.reconcile() == []
    run(home, capsys, "me", "inbox", "--as", HELPDESK, "--json")
    assert feeds.reconcile() == [HELPDESK] and started == [HELPDESK]
    stopped = feeds.feeds[HELPDESK]
    accounts.remove_account(agent(home), HELPDESK)
    assert feeds.reconcile() == [] and stopped.stop.is_set()
