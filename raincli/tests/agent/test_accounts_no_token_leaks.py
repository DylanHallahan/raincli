"""C22 (client review F1): a delegated session never reaches a log, the environment or any output. One run adds,
switches, drops and watches accounts through the REAL account code paths (the window with the app's real
``Services``, the tray's toasts, the runtime's ``account_feeds`` pollers and ``cli_accounts``), against the fake
server, with every logger captured and the app's and runtime's log files written; then every ``rps_`` token that ever
sat in ``accounts.json`` is looked for everywhere."""
import logging
import os

import pytest

from raincli_agent import account_feeds, accounts, cli, person
from raincli_agent.app.services import Services
from raincli_agent.runtime import winapp

from .test_accounts_cli import BILLING, HELPDESK, RELEASE, granted  # noqa: F401  (fixture)
from .test_app_accounts_window import on_hosted, sentinel, titled
from .test_app_window import FakeServices, fake_tray, make
from .test_person import account, agent, home  # noqa: F401  (fixtures)

pytestmark = pytest.mark.usefixtures("home")

ACCOUNT_METHODS = ("handoff_url", "held_accounts", "active_account", "set_active_account", "add_account",
                   "remove_account", "drop_account", "take_dropped", "account_notifications", "account_toasts",
                   "account_notify_all", "message_summary", "own_account")


class RealAccounts(FakeServices):
    """The window's and tray's fake services, with every account call going to the app's real ``Services``."""

    def __init__(self, real):
        super().__init__()
        for name in ACCOUNT_METHODS:
            setattr(self, name, getattr(real, name))


def _seen_tokens(cfg, seen):
    try:
        for a in accounts.load_accounts(cfg):
            seen.add(a.session.reveal())
    except Exception:  # noqa: BLE001 - a moment with no file
        pass
    return seen


def test_no_delegated_session_reaches_any_log_or_output(granted, home, tmp_path, capsys, caplog,  # noqa: F811
                                                        monkeypatch):
    st = granted.state
    cfg = agent(home)
    root = tmp_path / "approot"
    root.mkdir()
    runtime_log = tmp_path / "runtime-state" / "runtime.log"
    runtime_log.parent.mkdir()

    def runtime(text):  # the runtime's stderr goes to runtime.log
        with open(runtime_log, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    caplog.set_level(logging.DEBUG)  # every logger: raincli.app (window), the tray, account_feeds, cli_accounts
    real = Services(root, "raincli.example", paths=lambda: (cfg, str(tmp_path / "runtime.json")),
                    log=lambda text: winapp.app_log(root, text))
    svc = RealAccounts(real)
    seen = set()

    # The window: add Help Desk and Billing, switch between them and back, remove Billing.
    app, win, _ = make(tmp_path, svc)
    titled(win)
    for email in (HELPDESK, BILLING):
        assert app.before_navigate(sentinel("add-account", on_hosted(app, win), account=email)) is True
        _seen_tokens(cfg, seen)
    for email in (HELPDESK, BILLING, "own"):
        app.before_navigate(sentinel("switch", on_hosted(app, win), account=email if email != "own" else EMAILS[0]))
    st.fail("POST", r"/app/handoff", (500, "unavailable", {}))  # a failed switch is logged (type only)
    app.before_navigate(sentinel("switch", on_hosted(app, win), account=HELPDESK))
    app.before_navigate(sentinel("remove-account", on_hosted(app, win), account=BILLING))
    assert {a["email"] for a in svc.held_accounts()} == {HELPDESK}

    # The CLI adds Release on first use; the runtime polls every account; the tray shows their toasts.
    assert cli.main(["--config", cfg, "me", "inbox", "--as", RELEASE, "--json"]) == 0
    _seen_tokens(cfg, seen)
    st.person_inbox[HELPDESK] = [{"id": "77777777-7777-4777-8777-777777777777", "seq": 1, "kind": "message",
                                  "from_kind": "app"}]
    for email in (HELPDESK, RELEASE):
        assert account_feeds.AccountFeed(cfg, email, log=runtime).poll_once(wait=0) is not None
    st.fail("GET", r"/person/me", (503, "unavailable", {}))  # a failed poller cycle is logged (type only)
    failing = account_feeds.AccountFeed(cfg, RELEASE, log=runtime, sleep=lambda s: failing.stop.set())
    failing.run()
    tray = fake_tray(tmp_path, svc)
    tray.window = None
    tray.toasts()

    # The merged watch over every account.
    assert cli.main(["--config", cfg, "me", "inbox", "--watch", "--all-accounts", "--json", "--timeout", "2"]) in (0, 4)
    assert cli.main(["--config", cfg, "accounts", "--json"]) == 0

    # The server ends Help Desk's grant: the runtime drops it (401) and the window shows its notice.
    st.grants = [g for g in st.grants if g["account"] != HELPDESK]
    feed = account_feeds.AccountFeed(cfg, HELPDESK, log=runtime, sleep=lambda s: None)
    feed.run()
    assert feed.dropped
    app.home()  # the window looks for dropped accounts when it opens a page
    on_hosted(app, win)
    assert any("Help Desk was removed from this app" in sc for sc in win.scripts)

    assert len(seen) == 3  # Help Desk, Billing and Release each had a session in accounts.json
    out = capsys.readouterr()
    places = {
        "stdout and stderr": out.out + out.err,
        "every logger": "\n".join(r.getMessage() + " " + str(r.args) for r in caplog.records),
        "app.log": "".join(p.read_text(errors="replace") for p in (root / "app-lock").glob("app.log*")),
        "runtime.log": runtime_log.read_text() if runtime_log.exists() else "",
        "the environment": "\n".join(f"{k}={v}" for k, v in os.environ.items()),
        "the window's loads and scripts": "\n".join(list(win.loads) + list(win.scripts)),
        "the tray's toasts": "\n".join(str(n) for n in tray.icon.notes),
        "the notification feeds": "".join(p.read_text(errors="replace")
                                          for p in (person.notify_dir(cfg)).rglob("*.json")),
    }
    assert caplog.records and places["runtime.log"]  # the window and the runtime really logged
    leaks = {where: [t[:8] + "…" for t in seen if t in text] for where, text in places.items()}
    assert {k: v for k, v in leaks.items() if v} == {}


EMAILS = ["user@example.com"]  # any address that isn't held switches back to the own account
