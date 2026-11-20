"""The app window's account switching (protocol §23 A10; A14 L2): the switcher's sentinels act only with the hosted
page's per-load nonce (one action per load); switching hands off with that account's session and sets the active
account and the native title; Ctrl+Shift+A cycles; adding asks the server from the own session, then switches;
removing ends that session and falls back to the own account; a refused account is dropped; a restart opens on the
active account."""

from __future__ import annotations

from urllib.parse import quote

from .test_app_window import FakeServices, SERVICE, make


class AccountServices(FakeServices):
    def __init__(self, held=(), active=None, **kw):
        super().__init__(**kw)
        self.held = [dict(a) for a in held]
        self.active = active
        self.handoff_for = []
        self.added, self.removed, self.dropped = [], [], []
        self.refuse = set()

    def handoff_url(self, path=None, account=None):
        self.handoff_for.append(account)
        if account in self.refuse:
            err = RuntimeError("refused")
            err.status = 401
            raise err
        return super().handoff_url(path)

    def held_accounts(self):
        return list(self.held)

    def active_account(self):
        return self.active

    def set_active_account(self, email):
        self.active = email

    def add_account(self, email):
        acc = {"email": email, "display_name": email.split("@")[0].title(), "team": "northwind"}
        self.held.append(acc)
        self.added.append(email)
        return acc

    def remove_account(self, email):
        self.removed.append(email)
        found = next((a for a in self.held if a["email"].lower() == email.lower()), None)
        self.held = [a for a in self.held if a is not found]
        return found

    def drop_account(self, email):
        self.dropped.append(email)
        self.held = [a for a in self.held if a["email"] != email]


HD = {"email": "helpdesk@example.test", "display_name": "Help Desk", "team": "northwind"}
BILL = {"email": "billing@example.test", "display_name": "Billing", "team": "northwind"}


def titled(win):
    win.titles = []
    win.set_title = win.titles.append
    return win


def on_hosted(app, win, path="/app/inbox"):
    app.load(SERVICE + path)
    app.on_loaded()
    return win.scripts[-1].split('"')[1]  # the hosted nonce


def sentinel(name, nonce=None, **params):
    q = "&".join(f"{k}={quote(v)}" for k, v in params.items())
    return f"{SERVICE}/app/local/{name}?{q}" + (f"&nonce={nonce}" if nonce else "")


def test_a_sentinel_without_the_pages_nonce_does_nothing(tmp_path):
    svc = AccountServices(held=[HD])
    app, win, _ = make(tmp_path, svc)
    titled(win)
    good = on_hosted(app, win)
    loads = list(win.loads)
    for url in (sentinel("switch", account=HD["email"]), sentinel("switch", "forged", account=HD["email"]),
                sentinel("add-account", None, account="billing@example.test"),
                sentinel("remove-account", "x" + good, account=HD["email"])):
        assert app.before_navigate(url) is True  # cancelled, never shown
    assert win.loads == loads and svc.handoff_for == [] and svc.added == [] and svc.removed == []
    assert svc.active is None


def test_switching_hands_off_with_that_accounts_session(tmp_path):
    svc = AccountServices(held=[HD])
    app, win, _ = make(tmp_path, svc)
    titled(win)
    nonce = on_hosted(app, win)
    assert app.before_navigate(sentinel("switch", nonce, account="HelpDesk@Example.test")) is True
    assert svc.handoff_for[-1] == HD["email"] and svc.active == HD["email"] and app.active_account == HD["email"]
    assert win.loads[-1].startswith(SERVICE + "/app/handoff") and win.titles[-1] == "RainCLI · Help Desk"
    # One action per page load: the same nonce again does nothing.
    before = len(svc.handoff_for)
    assert app.before_navigate(sentinel("switch", nonce, account="")) is True and len(svc.handoff_for) == before
    # Back to the own account (any email that isn't held), on the next page's nonce.
    nonce = on_hosted(app, win)
    app.before_navigate(sentinel("switch", nonce, account="maya@example.test"))
    assert svc.handoff_for[-1] is None and svc.active is None and win.titles[-1] == "RainCLI"


def test_ctrl_shift_a_cycles_through_the_accounts(tmp_path):
    svc = AccountServices(held=[HD, BILL])
    app, win, _ = make(tmp_path, svc)
    titled(win)
    seen = []
    for _ in range(4):
        app.before_navigate(sentinel("switch", on_hosted(app, win), next="1"))
        seen.append(svc.active)
    assert seen == [HD["email"], BILL["email"], None, HD["email"]]


def test_adding_asks_the_server_then_switches(tmp_path):
    svc = AccountServices()
    app, win, _ = make(tmp_path, svc)
    titled(win)
    app.before_navigate(sentinel("add-account", on_hosted(app, win), account=BILL["email"]))
    assert svc.added == [BILL["email"]] and svc.active == BILL["email"] and svc.handoff_for[-1] == BILL["email"]


def test_removing_ends_that_session_and_falls_back_to_the_own_account(tmp_path):
    svc = AccountServices(held=[HD, BILL], active=HD["email"])
    app, win, _ = make(tmp_path, svc)
    titled(win)
    app.home()
    assert app.active_account == HD["email"] and win.titles[-1] == "RainCLI · Help Desk"  # a restart opens on active
    app.before_navigate(sentinel("remove-account", on_hosted(app, win), account=BILL["email"]))
    assert svc.removed == [BILL["email"]] and svc.active == HD["email"]  # removing another account: stay
    app.before_navigate(sentinel("remove-account", on_hosted(app, win), account=HD["email"]))
    assert svc.removed[-1] == HD["email"] and svc.active is None and svc.handoff_for[-1] is None
    assert win.titles[-1] == "RainCLI"


def test_a_refused_account_is_dropped_and_the_own_account_shown(tmp_path):
    svc = AccountServices(held=[HD], active=HD["email"])
    svc.refuse.add(HD["email"])
    app, win, _ = make(tmp_path, svc)
    titled(win)
    app.home()
    assert svc.dropped == [HD["email"]] and app.active_account is None and svc.handoff_for[-1] is None
    assert win.loads[-1].startswith(SERVICE + "/app/handoff")


def test_the_local_pages_say_whose_computer_it_is(tmp_path):
    svc = AccountServices(held=[HD])
    svc.own_account = lambda: {"email": "maya@example.test", "display_name": "Maya"}
    app, win, _ = make(tmp_path, svc)
    titled(win)
    assert app.account_note() == {"owner": None, "active": None}  # the own account: no line
    app.before_navigate(sentinel("switch", on_hosted(app, win), account=HD["email"]))
    assert app.account_note() == {"owner": "Maya", "active": "Help Desk"}
    assert "rps_" not in str(app.account_note())


def test_the_local_pages_ask_for_the_line_through_the_nonce_gate(tmp_path):
    from pathlib import Path

    from raincli_agent.app import window as window_mod

    local = Path(window_mod.__file__).parent / "local"
    for page in ("this-computer.html", "settings.html"):
        assert '<p class="belongs" id="belongs" hidden></p>' in (local / page).read_text()
    assert 'rc.read("account_note")' in (local / "local.js").read_text()
    assert hasattr(window_mod.Api, "account_note")
