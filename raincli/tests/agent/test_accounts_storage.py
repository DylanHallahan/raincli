"""The delegated accounts' local storage (protocol §23 A10; A14 L5, L6, Q8): ``accounts.json`` beside ``person.json``
(DPAPI on Windows, 0600 elsewhere, atomic), every read-modify-write under ``accounts.lock`` (two writers lose
nothing), ``accounts-active.json`` apart, the per-account notification folders, removal, the own sign-out clearing
them all, the cap, and errors that never quote a token."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import sys
import threading

import pytest

from raincli_agent import accounts, config as config_mod, dpapi, person
from raincli_agent.config import Secret
from raincli_agent.errors import ConfigError

from .test_dpapi_machine import FakeDpapi


def tok(n):
    return "rps_" + (f"{n:02d}" * 30)[:43]


@pytest.fixture
def cfg(tmp_path):
    folder = tmp_path / "raincli"
    folder.mkdir()
    return str(folder / "agent.json")


def test_round_trip_beside_person_json(cfg):
    assert accounts.load_accounts(cfg) == []
    accounts.save_account(cfg, "helpdesk@example.test", "Help Desk", "northwind", tok(1))
    accounts.save_account(cfg, "billing@example.test", "Billing", "northwind", Secret(tok(2)))
    got = accounts.load_accounts(cfg)
    assert [(a.email, a.display_name, a.team, a.session.reveal()) for a in got] == [
        ("helpdesk@example.test", "Help Desk", "northwind", tok(1)), ("billing@example.test", "Billing", "northwind", tok(2))]
    path = accounts.accounts_path(cfg)
    assert path.parent == person.person_path(cfg).parent and path.name == "accounts.json"
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not person.person_path(cfg).exists()  # person.json untouched: older clients read only it
    assert all(tok(1) not in repr(a) and tok(1) not in str(a.public()) for a in got)


def test_replacing_keeps_the_place_and_emails_match_case_insensitively(cfg):
    accounts.save_account(cfg, "helpdesk@example.test", "Help Desk", "northwind", tok(1))
    accounts.save_account(cfg, "billing@example.test", "Billing", "northwind", tok(2))
    accounts.save_account(cfg, "HelpDesk@Example.test", "Help Desk", "northwind", tok(3))  # a fresh session (M5/D2)
    got = accounts.load_accounts(cfg)
    assert [(a.email, a.session.reveal()) for a in got] == [("HelpDesk@Example.test", tok(3)), ("billing@example.test", tok(2))]
    assert accounts.find_account(cfg, "HELPDESK@example.test").session.reveal() == tok(3)


def test_at_most_four_delegated_accounts(cfg):
    for i in range(4):
        accounts.save_account(cfg, f"a{i}@example.test", f"A{i}", "northwind", tok(i))
    with pytest.raises(ConfigError, match="already holds 4"):
        accounts.save_account(cfg, "a9@example.test", "A9", "northwind", tok(9))
    accounts.save_account(cfg, "a0@example.test", "A0", "northwind", tok(8))  # replacing one is fine
    assert len(accounts.load_accounts(cfg)) == 4


def test_windows_stores_each_session_with_dpapi(cfg, monkeypatch):
    dpapi.set_backend(FakeDpapi())
    monkeypatch.setattr(config_mod, "protects_tokens", lambda: True)
    try:
        accounts.save_account(cfg, "helpdesk@example.test", "Help Desk", "northwind", tok(1))
        raw = accounts.accounts_path(cfg).read_text()
        data = json.loads(raw)
        assert set(data["accounts"][0]) == {"email", "display_name", "team", "person_session_dpapi"}
        assert "rps_" not in raw
        assert accounts.load_accounts(cfg)[0].session.reveal() == tok(1)
    finally:
        dpapi.set_backend(None)


@pytest.mark.parametrize("bad", [
    "not json", {"accounts": "x"}, {"accounts": [], "active": "x"},
    {"accounts": [{"email": "a@x", "display_name": "A", "team": "t", "person_session": "rps_short"}]},
    {"accounts": [{"email": "a@x", "display_name": "A", "team": "t"}]},
    {"accounts": [{"email": "a@x", "display_name": "A", "team": "t", "person_session": "x", "person_session_dpapi": "y"}]},
])
def test_a_damaged_file_is_a_clear_error_without_content(cfg, bad):
    path = accounts.accounts_path(cfg)
    path.write_text(bad if isinstance(bad, str) else json.dumps(bad))
    os.chmod(path, 0o600)
    with pytest.raises(ConfigError, match="damaged") as err:
        accounts.load_accounts(cfg)
    assert "rps_short" not in str(err.value) and "person_session" not in str(err.value)
    with pytest.raises(ConfigError):
        accounts.save_account(cfg, "b@x", "B", "t", tok(1))  # never silently overwritten


def test_a_malformed_token_is_refused_before_anything_is_written(cfg):
    with pytest.raises(ConfigError, match="malformed") as err:
        accounts.save_account(cfg, "a@x", "A", "t", "rps_tooshort")
    assert "rps_tooshort" not in str(err.value) and not accounts.accounts_path(cfg).exists()


def test_the_active_account_is_its_own_file(cfg):
    assert accounts.load_active(cfg) is None
    accounts.save_active(cfg, "helpdesk@example.test")
    assert accounts.load_active(cfg) == "helpdesk@example.test"
    assert json.loads(accounts.active_path(cfg).read_text()) == {"active": "helpdesk@example.test"}
    assert not accounts.accounts_path(cfg).exists()  # the window's choice never rewrites accounts.json (L6)
    accounts.active_path(cfg).write_text("{damaged")
    assert accounts.load_active(cfg) is None  # a damaged choice falls back to the own account
    accounts.save_active(cfg, None)
    assert accounts.load_active(cfg) is None


def test_each_account_has_its_own_feed_folder(cfg):
    d = accounts.account_notify_dir(cfg, "HelpDesk@example.test")
    assert d == person.notify_dir(cfg) / "accounts" / accounts.account_key("helpdesk@example.test")
    assert len(d.name) == 16 and all(c in "0123456789abcdef" for c in d.name)
    assert d != accounts.account_notify_dir(cfg, "billing@example.test")


def test_removing_drops_the_session_its_feed_and_an_active_choice(cfg):
    accounts.save_account(cfg, "helpdesk@example.test", "Help Desk", "northwind", tok(1))
    accounts.save_account(cfg, "billing@example.test", "Billing", "northwind", tok(2))
    feed = accounts.account_notify_dir(cfg, "helpdesk@example.test")
    feed.mkdir(parents=True)
    (feed / "queue.json").write_text("{}")
    accounts.save_active(cfg, "helpdesk@example.test")
    removed = accounts.remove_account(cfg, "HELPDESK@example.test")
    assert removed.session.reveal() == tok(1)  # for the caller to end it on the server
    assert [a.email for a in accounts.load_accounts(cfg)] == ["billing@example.test"]
    assert not feed.exists() and accounts.load_active(cfg) is None
    assert accounts.remove_account(cfg, "nobody@example.test") is None


def test_the_own_sign_out_clears_every_delegated_account(cfg):
    accounts.save_account(cfg, "helpdesk@example.test", "Help Desk", "northwind", tok(1))
    accounts.save_active(cfg, "helpdesk@example.test")
    feed = accounts.account_notify_dir(cfg, "helpdesk@example.test")
    feed.mkdir(parents=True)
    person.clear_session(cfg)
    assert not accounts.accounts_path(cfg).exists() and not accounts.active_path(cfg).exists()
    assert not (person.notify_dir(cfg) / "accounts").exists()


def _writer(cfg, start, n, barrier):
    barrier.wait()
    for i in range(start, start + n):
        accounts.save_account(cfg, f"w{i}@example.test", f"W{i}", "northwind", tok(i))
        accounts.remove_account(cfg, f"w{i}@example.test")
        accounts.save_account(cfg, f"w{i}@example.test", f"W{i}", "northwind", tok(i))


def test_two_writers_under_the_lock_lose_nothing(cfg):
    """Two threads add, remove and re-add their own accounts at once (A14 L5): each write is a read-modify-write
    under accounts.lock, so both sets survive."""
    barrier = threading.Barrier(2)
    threads = [threading.Thread(target=_writer, args=(cfg, 10, 2, barrier)),
               threading.Thread(target=_writer, args=(cfg, 20, 2, barrier))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(a.email for a in accounts.load_accounts(cfg)) == [
        "w10@example.test", "w11@example.test", "w20@example.test", "w21@example.test"]


@pytest.mark.skipif(sys.platform == "win32", reason="fork-based process test")
def test_two_processes_under_the_lock_lose_nothing(cfg):
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    procs = [ctx.Process(target=_writer, args=(cfg, 30, 2, barrier)), ctx.Process(target=_writer, args=(cfg, 40, 2, barrier))]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    assert all(p.exitcode == 0 for p in procs)
    assert sorted(a.email for a in accounts.load_accounts(cfg)) == [
        "w30@example.test", "w31@example.test", "w40@example.test", "w41@example.test"]


def test_as_resolution_prefers_an_exact_email_then_names():
    class C:
        def __init__(self, email, name):
            self.email, self.display_name = email, name
    cs = [C("helpdesk@example.test", "Help Desk"), C("hd2@example.test", "Help Desk"), C("billing@example.test", "Billing")]
    assert [c.email for c in accounts.matches(cs, "HelpDesk@example.test")] == ["helpdesk@example.test"]
    assert [c.email for c in accounts.matches(cs, "Billing")] == ["billing@example.test"]
    assert len(accounts.matches(cs, "Help Desk")) == 2  # ambiguous: the caller lists the emails (exit 2)
    assert accounts.matches(cs, "nobody") == []
