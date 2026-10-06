"""The person session on the client (protocol §16.3, §16.10, §16.11, §16.12 C5, C14, §16.14 S3)."""
import hashlib
import json
import logging
import os
import sys

import pytest

from raincli_agent import cli, config as config_mod, dpapi, login, person, trust
from raincli_agent.config import Secret, load_config
from raincli_agent.errors import ConfigError, UsageError

from .test_dpapi_machine import FakeDpapi
from .test_login import EMAIL, PASSWORD, config_path

pytestmark = pytest.mark.usefixtures("home")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RAINCLI_CONFIG", raising=False)
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


@pytest.fixture
def account(fake_api):
    fake_api.state.add_user(EMAIL, PASSWORD, teams=("alpha",))
    return fake_api


def sign_in(account, home, **kw):
    kw.setdefault("machine_name", "work-pc")
    kw.setdefault("person_session", True)
    return login.login(EMAIL, Secret(PASSWORD), api_url=account.url, config_path=str(config_path(home)), **kw)


def agent(home):
    return str(config_path(home))


def install_token(home):
    return person.app_install_token(agent(home))


# -- sign-in stores a person session ---------------------------------------------------------------

def test_login_sends_person_session_and_stores_it_0600(account, home):
    result = sign_in(account, home)
    assert account.state.login_bodies[-1]["person_session"] is True
    assert result["person_session"] is True
    path = person.person_path(agent(home))
    data = json.loads(path.read_text())
    assert set(data) == {"person_session"} and data["person_session"] in account.state.person_sessions
    if os.name != "nt":
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert person.load_session(agent(home)).reveal() == data["person_session"]
    assert "rps_" not in repr(person.load_session(agent(home)))


def test_login_without_person_session_sends_nothing_and_clears_an_old_one(account, home):
    sign_in(account, home)
    assert person.load_session(agent(home)) is not None
    sign_in(account, home, force=True, person_session=False)
    assert "person_session" not in account.state.login_bodies[-1]
    assert person.load_session(agent(home)) is None  # the rotation ended it on the server (C6)


def test_windows_stores_person_session_dpapi(account, home, monkeypatch):
    dpapi.set_backend(FakeDpapi())
    monkeypatch.setattr(config_mod, "protects_tokens", lambda: True)
    try:
        sign_in(account, home)
        data = json.loads(person.person_path(agent(home)).read_text())
        assert set(data) == {"person_session_dpapi"} and "rps_" not in json.dumps(data)
        assert person.load_session(agent(home)).reveal() in account.state.person_sessions
        install = json.loads(person.install_path(agent(home)).read_text())
        assert set(install) == {"app_install_token_dpapi", "created_under"}
        assert person.INSTALL_TOKEN_RE.fullmatch(install_token(home))
    finally:
        dpapi.set_backend(None)


def test_damaged_person_file_is_a_clear_error_without_content(account, home):
    sign_in(account, home)
    path = person.person_path(agent(home))
    path.write_text(json.dumps({"person_session": "rps_short"}))
    with pytest.raises(ConfigError, match="sign in again") as err:
        person.load_session(agent(home))
    assert "rps_short" not in str(err.value)


def test_login_person_adds_a_session_without_rotation(account, home):
    sign_in(account, home, person_session=False)
    token = load_config(agent(home)).token.reveal()
    person.add_session(agent(home), EMAIL, Secret(PASSWORD))
    body = account.state.login_bodies[-1]
    assert body["person_only"] is True and body["previous_token"] == token and "machine_name" not in body
    assert load_config(agent(home)).token.reveal() == token  # no rotation
    assert person.load_session(agent(home)).reveal() in account.state.person_sessions


def test_login_person_by_someone_else_is_refused(account, home):
    account.state.add_user("mallory@example.test", "pw-mallory-123", teams=("alpha",))
    sign_in(account, home, person_session=False)
    with pytest.raises(login.NotMachineOwner):
        person.add_session(agent(home), "mallory@example.test", Secret("pw-mallory-123"))
    assert person.load_session(agent(home)) is None


def test_cli_login_person(account, home, monkeypatch, capsys):
    import getpass
    sign_in(account, home, person_session=False)
    monkeypatch.setattr(cli, "_need_tty", lambda what: None)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": PASSWORD)
    code = cli.main(["--config", agent(home), "login", "--person", "--email", EMAIL])
    output = capsys.readouterr()
    assert code == 0, output.err
    assert "added a person session" in output.out
    assert PASSWORD not in output.out + output.err
    session = person.load_session(agent(home)).reveal()
    assert session not in output.out + output.err


def test_cli_login_person_refuses_without_tty(home, monkeypatch):
    import getpass
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: pytest.fail("getpass called"))
    monkeypatch.setattr(sys, "stdin", open(os.devnull))
    assert cli.main(["login", "--person", "--email", EMAIL]) == 2


# -- sign-out clears it -------------------------------------------------------------------------------

def test_machine_logout_deletes_the_person_session(account, home, monkeypatch):
    from raincli_agent.runtime import startup
    monkeypatch.setattr(startup, "remove_for", lambda config: "not_enabled")
    sign_in(account, home)
    before = install_token(home)
    result = login.logout(agent(home))
    assert str(person.person_path(agent(home))) in result["removed"]
    assert person.load_session(agent(home)) is None and not account.state.person_sessions
    assert install_token(home) != before  # §16.14 S3: rotated on sign-out


def test_me_sign_out_revokes_only_the_person_session(account, home, capsys):
    sign_in(account, home)
    before = install_token(home)
    assert cli.main(["--config", agent(home), "me", "sign-out"]) == 0
    assert "revoked" in capsys.readouterr().out
    assert not account.state.person_sessions and person.load_session(agent(home)) is None
    assert load_config(agent(home)).token.reveal() in account.state.tokens  # the machine stays
    assert install_token(home) != before
    assert cli.main(["--config", agent(home), "me", "sign-out"]) == 0
    assert "no person session" in capsys.readouterr().out


def test_me_sign_out_of_a_revoked_session_cleans_up(account, home):
    sign_in(account, home)
    account.state.person_sessions.clear()
    assert person.sign_out(agent(home))["server"] == "already_revoked"
    assert person.load_session(agent(home)) is None


# -- the app install token (§16.14 S3) -------------------------------------------------------------------

def test_install_token_is_random_private_and_stable_until_rotated(account, home):
    sign_in(account, home)
    token = install_token(home)
    assert person.INSTALL_TOKEN_RE.fullmatch(token) and install_token(home) == token
    path = person.install_path(agent(home))
    assert path.parent == person.person_path(agent(home)).parent
    if os.name != "nt":
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    got, digest = person.app_install(agent(home))
    assert got == token and digest == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert len(digest) == 64 and digest == digest.lower()


def test_install_token_rotates_on_every_sign_in_and_sign_out(account, home, monkeypatch):
    from raincli_agent.runtime import startup
    monkeypatch.setattr(startup, "remove_for", lambda config: "not_enabled")
    seen = set()
    sign_in(account, home)
    seen.add(install_token(home))
    sign_in(account, home, force=True)  # sign in again
    seen.add(install_token(home))
    person.add_session(agent(home), EMAIL, Secret(PASSWORD))  # a new person session
    seen.add(install_token(home))
    person.sign_out(agent(home))  # person sign-out
    seen.add(install_token(home))
    login.logout(agent(home))  # machine sign-out
    seen.add(install_token(home))
    assert len(seen) == 5


def test_rotate_for_the_installer_prints_no_token(account, home, capsys):
    sign_in(account, home)
    before = install_token(home)
    assert cli.main(["--config", agent(home), "app", "rotate-install-token"]) == 0
    output = capsys.readouterr()
    after = install_token(home)
    assert after != before and before not in output.out + output.err and after not in output.out + output.err
    assert person.rotate_app_install_token(agent(home)) is None  # nothing a caller could log


def test_install_token_never_in_logs_or_errors(account, home, caplog):
    caplog.set_level(logging.DEBUG)
    sign_in(account, home)
    token = install_token(home)
    path = person.install_path(agent(home))
    path.write_text(json.dumps({"app_install_token": token + "!"}))
    with pytest.raises(ConfigError) as err:
        person.app_install_token(agent(home))
    assert token not in str(err.value) and token not in caplog.text
    path.write_text(json.dumps({"app_install_token": token, "extra": 1}))
    with pytest.raises(ConfigError) as err:
        person.app_install_token(agent(home))
    assert token not in str(err.value)


def test_handoff_request_carries_only_the_hash(account, home):
    """The app's request (web-builder's services.handoff_url) as the client supports it."""
    from raincli_agent.api import ApiClient
    sign_in(account, home)
    token, digest = person.app_install(agent(home))
    api = ApiClient(load_config(agent(home)).api_url, person.load_session(agent(home)), max_attempts=1)
    reply = api.request("POST", "/app/handoff", body={"app_install_hash": digest})[1]
    assert reply["url"].startswith("https://")
    sent = [r for r in account.state.requests if r[1].endswith("/app/handoff")]
    assert sent and all(token not in json.dumps(r, default=str) for r in account.state.requests)


# -- trust (§16.12 C5) --------------------------------------------------------------------------------------

def runtime(home):
    return str(config_path(home).parent / "runtime.json")


def test_trust_defaults_to_team_and_edits_the_runtime_config(account, home, capsys):
    sign_in(account, home)
    assert trust.describe(runtime(home)) == {"trust_mode": "team", "trusted_senders": [], "blocked_senders": [],
                                            "owner_email": EMAIL}
    trust.set_mode(runtime(home), "list")
    trust.add(runtime(home), "bob-desktop")
    trust.add(runtime(home), "Carol@Example.test")
    trust.add(runtime(home), "@carol@example.test")  # the same person
    assert trust.describe(runtime(home))["trusted_senders"] == ["bob-desktop", "@carol@example.test"]
    trust.remove(runtime(home), "bob-desktop")
    data = json.loads(open(runtime(home)).read())
    assert data["trust_mode"] == "list" and data["trusted_senders"] == ["@carol@example.test"]
    assert cli.main(["--config", agent(home), "trust", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["trust_mode"] == "list"
    assert cli.main(["--config", agent(home), "trust", "--mode", "team"]) == 0
    assert cli.main(["--config", agent(home), "trust", "add", "dave-pc"]) == 0
    assert trust.describe(runtime(home))["trusted_senders"] == ["@carol@example.test", "dave-pc"]
    for bad in (["trust", "add"], ["trust", "add", "x y"], ["trust", "--mode", "list", "add", "dave-pc"]):
        assert cli.main(["--config", agent(home)] + bad) == 2


def test_trust_settings_survive_signing_in_again(account, home):
    sign_in(account, home)
    trust.set_mode(runtime(home), "list")
    trust.add(runtime(home), "bob-desktop")
    sign_in(account, home, force=True)
    assert trust.describe(runtime(home))["trusted_senders"] == ["bob-desktop"]
    assert trust.describe(runtime(home))["trust_mode"] == "list"


def test_trust_refuses_a_connector_runtime(home, tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"connectors": ["c.json"]}))
    with pytest.raises(ConfigError, match="connector config"):
        trust.set_mode(str(path), "list")
    with pytest.raises(UsageError):
        trust.sender("not a sender")


# -- endpoints ---------------------------------------------------------------------------------------------

def test_endpoint_forms():
    assert person.parse_endpoint("bob-pc") == "bob-pc"
    assert person.parse_endpoint("bob-pc/reviewer") == {"machine": "bob-pc", "agent": "reviewer"}
    assert person.parse_endpoint("@bob@example.test") == {"person": "bob@example.test"}
    for bad in ("", " bob", "bob/", "/x", "@nobody", "@a@b@c"):
        with pytest.raises(UsageError):
            person.parse_endpoint(bad)
    for form in ("bob-pc", "bob-pc/reviewer", "@bob@example.test"):
        assert person.endpoint_label(person.parse_endpoint(form)) == form


def test_routing_command(account, home, capsys):
    sign_in(account, home)
    assert cli.main(["--config", agent(home), "routing", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"routing": "all"}
    assert cli.main(["--config", agent(home), "routing", "--inbox-only"]) == 0
    assert "only this machine's inbox" in capsys.readouterr().out
    assert list(account.state.routing.values()) == ["inbox-only"]


# -- the notification feed (§16.10, C14) -------------------------------------------------------------------

def person_message(seq, kind="message"):
    import uuid
    return {"id": str(uuid.uuid4()), "seq": seq, "kind": kind, "body": f"secret body {seq}"}


def test_feed_appends_id_kind_at_and_the_app_takes_each_once(account, home):
    sign_in(account, home)
    account.state.person_inbox[EMAIL] = [person_message(1), person_message(2, "escalation")]
    feed = person.NotificationFeed(agent(home), now=lambda: 1_000_000.0)
    assert feed.poll_once(wait=0) == 2
    assert feed.poll_once(wait=0) == 0  # the cursor moved on
    raw = (person.notify_dir(agent(home)) / "queue.json").read_text()
    assert "secret body" not in raw  # no body is ever written (C14)
    taken = person.take_notifications(agent(home), now=1_000_100.0)
    assert [(e["kind"], e["at"]) for e in taken] == [("message", "1970-01-12T13:46:40Z"),
                                                     ("escalation", "1970-01-12T13:46:40Z")]
    assert set(taken[0]) == {"id", "kind", "at"}
    assert person.take_notifications(agent(home), now=1_000_100.0) == []
    account.state.person_inbox[EMAIL].append(person_message(3))
    feed.poll_once(wait=0)
    assert len(person.take_notifications(agent(home), now=1_000_100.0)) == 1


def test_feed_keeps_seven_days_and_500_entries(account, home):
    sign_in(account, home)
    t = [0.0]
    feed = person.NotificationFeed(agent(home), now=lambda: t[0])
    account.state.person_inbox[EMAIL] = [person_message(n) for n in range(1, 601)]
    feed.poll_once(wait=0)
    queue = json.loads((person.notify_dir(agent(home)) / "queue.json").read_text())
    assert len(queue["entries"]) == 500 and queue["entries"][0]["seq"] == 101
    t[0] = 8 * 86400.0
    account.state.person_inbox[EMAIL].append(person_message(601))
    feed.poll_once(wait=0)
    queue = json.loads((person.notify_dir(agent(home)) / "queue.json").read_text())
    assert len(queue["entries"]) == 1
    assert len(person.take_notifications(agent(home), now=t[0])) == 1


def test_feed_idles_without_a_session_and_restarts_for_a_new_one(account, home):
    sign_in(account, home, person_session=False)
    feed = person.NotificationFeed(agent(home))
    assert feed.poll_once(wait=0) is None
    person.add_session(agent(home), EMAIL, Secret(PASSWORD))
    account.state.person_inbox[EMAIL] = [person_message(1)]
    assert feed.poll_once(wait=0) == 1


def test_sign_out_clears_the_feed(account, home):
    sign_in(account, home)
    account.state.person_inbox[EMAIL] = [person_message(1)]
    person.NotificationFeed(agent(home)).poll_once(wait=0)
    person.sign_out(agent(home))
    assert person.take_notifications(agent(home)) == []


def test_feed_failures_log_codes_only(account, home):
    sign_in(account, home)
    logs = []
    feed = person.NotificationFeed(agent(home), log=logs.append, sleep=lambda s: feed.stop.set())
    account.state.person_sessions.clear()
    feed.run()
    assert logs == ["notification feed: Unauthorized unauthorized"]
    session = person.load_session(agent(home)).reveal()
    assert all(session not in line for line in logs)


def test_runtime_starts_the_feed_only_in_machine_mode(account, home, monkeypatch):
    from raincli_agent.runtime import service
    started = []
    monkeypatch.setattr(person.NotificationFeed, "start", lambda self: started.append(self.agent_config))
    sign_in(account, home)
    _, _, configs = service.load_runtime(runtime(home))
    assert service.notification_feed(configs) is not None and started == [agent(home)]
    assert service.notification_feed([("c.json", None)]) is None


def test_install_token_rotates_when_the_app_install_changes(account, home, tmp_path, monkeypatch):
    """§16.15: rotated when install.json's (current, install_stamp) differs from what the token
    was created under: a full install (new stamp) or any update (new current)."""
    from raincli_agent.runtime import winapp
    root = tmp_path / "app"
    root.mkdir()
    (root / "install.json").write_text(json.dumps({"current": "0.5.0", "previous": None, "probation": None,
                                                   "install_stamp": "20261005T120000Z-ab12", "stub": 2}))
    sign_in(account, home)
    monkeypatch.setattr(winapp, "app_root", lambda *a, **k: root)
    first = install_token(home)  # created under this install (rotated once from the pre-app token)
    assert install_token(home) == first  # unchanged install: stable
    winapp.write_install(root, "0.5.1", "0.5.0")  # a pushed update
    data = json.loads((root / "install.json").read_text())
    assert data["install_stamp"] == "20261005T120000Z-ab12" and data["stub"] == 2  # the installer's keys kept
    second = install_token(home)
    assert second != first and install_token(home) == second
    data["install_stamp"] = "20261006T090000Z-cd34"  # a reinstall of the same version
    (root / "install.json").write_text(json.dumps(data))
    third = install_token(home)
    assert third not in (first, second) and install_token(home) == third
    stored = json.loads(person.install_path(agent(home)).read_text())
    assert stored["created_under"] == ["0.5.1", "20261006T090000Z-cd34"]


def test_install_token_outside_the_app_is_not_rotated_by_reading(account, home):
    sign_in(account, home)
    token = install_token(home)
    assert json.loads(person.install_path(agent(home)).read_text())["created_under"] is None
    assert install_token(home) == token
