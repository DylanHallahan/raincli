"""Stale or foreign machine credentials (protocol §16.17, §16.18): check_credential,
set_aside, migration and the CLI."""
import io
import json
import os
from pathlib import Path
import sys

import pytest

from raincli_agent import cli, config as config_mod, dpapi, login, person, setaside
from raincli_agent.api import ApiClient
from raincli_agent.config import Secret, load_config, write_config
from raincli_agent.connector import queue as q
from raincli_agent.connector.config import load_connector_config
from raincli_agent.connector.herdr import FakeHerdr
from raincli_agent.connector.queue import Queue
from raincli_agent.connector.runner import Connector
from raincli_agent.errors import ConfigError
from raincli_agent.migrate import Migration
from raincli_agent.runtime import sessions

from .test_dpapi_machine import FakeDpapi
from .test_migrate import Registry

PASSWORD = "correct horse battery staple 7f3a"
EMAIL = "alice@example.test"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config" / "raincli").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("RAINCLI_CONFIG", raising=False)
    monkeypatch.setattr(login, "default_machine_name", lambda: "work-pc")
    return home


@pytest.fixture
def account(fake_api):
    fake_api.state.add_user(EMAIL, PASSWORD)
    fake_api.state.add_user("mallory@example.test", "pw-mallory-123456")
    return fake_api


def cfg(home):
    return home / ".config" / "raincli"


def agent(home):
    return cfg(home) / "agent.json"


def sign_in(account, home, name="work-pc", **kw):
    return login.login(EMAIL, Secret(PASSWORD), api_url=account.url, config_path=str(agent(home)),
                       machine_name=name, person_session=True, **kw)


def revoke(account, home):
    """The machine's credential stops answering (revoked on the website)."""
    st = account.state
    token = load_config(str(agent(home))).token.reveal()
    st.agents[st.tokens.pop(token)]["active"] = False


def files(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*")
                  if p.is_file() and p.name not in (".migration.lock", "run.lock"))  # lock files, created by probing


def migration(home, **kw):
    kw.setdefault("registry", Registry())
    kw.setdefault("managed_root", home / "none")
    return Migration(**kw)


# -- §16.17 2, §16.18 V4: check_credential ---------------------------------------------------------------

def test_check_credential_outcomes(account, home):
    sign_in(account, home)
    path = str(agent(home))
    assert setaside.check_credential(path) == "ok"
    assert setaside.check_credential(path, EMAIL.upper()) == "ok"  # case-insensitive
    account.state.owners[account.state.tokens[load_config(path).token.reveal()]] = "mallory@example.test"
    assert setaside.check_credential(path, EMAIL) == "not_owner"
    assert setaside.check_credential(path) == "ok"  # not_owner is only decided with an email
    account.state.me_owner = False  # an older server
    assert setaside.check_credential(path, EMAIL) == "unknown"
    account.state.fail("GET", r"/me$", (503, "unavailable", {}), times=10)
    assert setaside.check_credential(path) == "unknown"
    account.state.faults.clear()
    revoke(account, home)
    assert setaside.check_credential(path, EMAIL) == "invalid"


def test_check_credential_unreachable_is_unknown(home):
    write_config(str(agent(home)), "http://127.0.0.1:9", "rca_" + "A" * 43)
    assert setaside.check_credential(str(agent(home)), EMAIL) == "unknown"


def test_check_credential_unreadable_forms(home, monkeypatch):
    """V4 and test (d): a foreign DPAPI blob, a damaged file, both token forms."""
    path = agent(home)
    dpapi.set_backend(FakeDpapi(owner="olduser@oldpc"))
    monkeypatch.setattr(config_mod, "protects_tokens", lambda: True)
    try:
        write_config(str(path), "https://raincli.example", "rca_" + "A" * 43)
        dpapi.set_backend(FakeDpapi(owner="dylan@newpc"))  # another Windows account
        assert setaside.check_credential(str(path)) == "unreadable"
    finally:
        dpapi.set_backend(None)
    for text in ("{not json", json.dumps({"api_url": "https://raincli.example", "token": "rca_x",
                                          "token_dpapi": "QUJD"})):
        path.write_text(text)
        os.chmod(path, 0o600)
        assert setaside.check_credential(str(path)) == "unreadable"
    with pytest.raises(ConfigError):
        setaside.check_credential(str(cfg(home) / "missing.json"))


def test_check_credential_never_logs_the_token(account, home, caplog, capsys):
    sign_in(account, home)
    token = load_config(str(agent(home))).token.reveal()
    revoke(account, home)
    assert setaside.check_credential(str(agent(home))) == "invalid"
    seen = caplog.text + "".join(capsys.readouterr())
    assert token not in seen


def test_stale_messages():
    assert setaside.stale_message("invalid") == "This computer's saved RainCLI setup belongs to a machine that was revoked."
    assert setaside.stale_message("not_machine_owner").endswith(
        "The other machine stays active for its owner until they revoke it.")
    assert "can't be read" in setaside.stale_message("unreadable")
    assert setaside.stale_message("ok") is None


# -- §16.17 5, §16.18 V1: what moves -------------------------------------------------------------------

def old_machine_with_held_messages(account, home):
    """A signed-in machine-mode setup whose queue holds an agent_held record and whose
    sessions/ hold a by-name handover file (V1)."""
    sign_in(account, home)
    runtime = cfg(home) / "runtime.json"
    conn_cfg = load_connector_config(str(runtime))
    state = cfg(home) / "runtime-state"
    state.mkdir(mode=0o700, exist_ok=True)
    sessions.ensure_salt(str(state))
    sessions.sessions_dir(str(state), create=True)
    sender = account.state.add_agent("old-teammate")
    held = ApiClient(account.url, sender).send({"machine": "work-pc", "agent": "reviewer"}, "old secret")[0]["id"]
    conn = Connector(conn_cfg, ApiClient(account.url, load_config(str(agent(home))).token), FakeHerdr(),
                     Queue(conn_cfg.state_dir), log=lambda line: None, sleep=lambda s: None,
                     sessions_state=str(state))
    conn.run_once()
    assert conn.queue.get(held)["state"] == q.AGENT_HELD
    sessions.hand_over(str(state), sessions.name_box("notes"), "0b7f3c1e-2a4d-4c6e-9f10-1a2b3c4d5e6f",
                       "old by-name handover")
    (state / "status.json").write_text(json.dumps({"status": "stopped"}))
    person.NotificationFeed(str(agent(home))).poll_once(wait=0)
    return held, state


def test_set_aside_moves_the_whole_old_setup_including_its_state(account, home):
    held, state = old_machine_with_held_messages(account, home)
    before = files(cfg(home))
    result = login.set_aside(str(agent(home)), migration=migration(home))
    backup = Path(result["backup"])
    assert backup.parent == cfg(home) and backup.name.startswith("replaced-")
    if os.name != "nt":
        assert backup.stat().st_mode & 0o777 == 0o700
    moved = {Path(m["from"]).name for m in result["moved"]}
    assert moved == {"agent.json", "runtime.json", "runtime-state", "person.json", "app-install.json",
                     "notifications"}
    for name in moved:
        assert not (cfg(home) / name).exists() and (backup / name).exists()
    for name in ("machine-salt", "routing-capable.json", "status.json"):
        assert (backup / "runtime-state" / name).is_file(), name
    assert (backup / "runtime-state" / "queue").is_dir() and (backup / "runtime-state" / "sessions").is_dir()
    # Nothing deleted: every old file is in the backup (plus the result record).
    after = files(backup)
    assert set(before) <= set(after) and set(after) - set(before) == {"set-aside.json"}
    assert json.loads((backup / "set-aside.json").read_text())["moved"] == result["moved"]
    assert "rca_" not in json.dumps(result) and "rps_" not in json.dumps(result)


def test_fresh_sign_in_never_delivers_the_old_state(account, home):
    """V1: an old agent_held record and a by-name handover file are never delivered afterwards."""
    held, _ = old_machine_with_held_messages(account, home)
    revoke(account, home)
    login.set_aside(str(agent(home)), migration=migration(home))
    sign_in(account, home, name="work-pc-2")
    runtime = cfg(home) / "runtime.json"
    conn_cfg = load_connector_config(str(runtime))
    state = cfg(home) / "runtime-state"
    state.mkdir(mode=0o700, exist_ok=True)
    assert not (state / "queue").exists() and not (state / "sessions").exists()  # a fresh state directory
    sessions.ensure_salt(str(state))
    sessions.sessions_dir(str(state), create=True)
    herdr = FakeHerdr()
    herdr.add("reviewer", status="idle")
    conn = Connector(conn_cfg, ApiClient(account.url, load_config(str(agent(home))).token), herdr,
                     Queue(conn_cfg.state_dir), log=lambda line: None, sleep=lambda s: None,
                     sessions_state=str(state))
    conn.run_once()
    conn.run_once()
    assert herdr.prompts == [] and conn.queue.load(held) is None
    assert sessions.claim(str(state), sessions.name_box("notes")) == ([], [])  # no old handover box
    backup = next(cfg(home).glob("replaced-*"))
    assert (backup / "runtime-state" / "queue").is_dir()  # the old state is only in the backup


def test_connector_setup_moves_configs_and_runtime_and_keeps_own_queues(account, home, tmp_path):
    sign_in(account, home)
    (cfg(home) / "runtime.json").unlink()
    queue_dir = tmp_path / "my-queue"
    queue_dir.mkdir()
    (queue_dir / "cursor.json").write_text("{}")
    connector = cfg(home) / "connector.json"
    connector.write_text(json.dumps({"agent_config": str(agent(home)), "herdr_agent": "inbox",
                                     "state_dir": str(queue_dir)}))
    (cfg(home) / "runtime.json").write_text(json.dumps({"connectors": [str(connector)], "state_dir": "rs"}))
    (cfg(home) / "rs").mkdir()
    (cfg(home) / "rs" / "status.json").write_text("{}")
    result = login.set_aside(str(agent(home)), migration=migration(home))
    moved = {Path(m["from"]).name for m in result["moved"]}
    assert {"agent.json", "connector.json", "runtime.json", "rs"} <= moved
    assert (queue_dir / "cursor.json").is_file() and str(queue_dir) in result["kept_queues"]


def test_runtime_with_other_credentials_is_rewritten_not_moved(account, home, tmp_path):
    """V2: the other credential keeps delivering; the original runtime config is kept in the backup."""
    sign_in(account, home)
    (cfg(home) / "runtime.json").unlink()
    other = tmp_path / "other" / "agent.json"
    other.parent.mkdir()
    write_config(str(other), account.url, account.state.add_agent("other-box"))
    mine, theirs = cfg(home) / "mine.json", cfg(home) / "theirs.json"
    mine.write_text(json.dumps({"agent_config": str(agent(home)), "herdr_agent": "a"}))
    theirs.write_text(json.dumps({"agent_config": str(other), "herdr_agent": "b"}))
    runtime = cfg(home) / "runtime.json"
    original = {"connectors": ["mine.json", "theirs.json"], "state_dir": "rs"}
    runtime.write_text(json.dumps(original))
    result = login.set_aside(str(agent(home)), migration=migration(home))
    assert json.loads(runtime.read_text()) == {"connectors": ["theirs.json"], "state_dir": "rs"}
    [rewrite] = result["rewritten"]
    assert rewrite["path"] == str(runtime) and json.loads(Path(rewrite["original"]).read_text()) == original
    assert not mine.exists() and theirs.exists() and other.exists()


def test_finds_connectors_the_way_migration_does(account, home, tmp_path):
    """V3: a connector outside the scan directories, named by the old Run value's runtime config,
    by app.json's runtime config, or given explicitly."""
    from raincli_agent.runtime import winapp
    sign_in(account, home)
    (cfg(home) / "runtime.json").unlink()
    far = tmp_path / "far"
    far.mkdir()
    via_run, via_app, explicit = far / "run.json", far / "app.json", far / "explicit.json"
    for path in (via_run, via_app, explicit):
        path.write_text(json.dumps({"agent_config": str(agent(home)), "herdr_agent": "x"}))
    (far / "runtime-run.json").write_text(json.dumps({"connectors": ["run.json"], "state_dir": "s1"}))
    (far / "runtime-app.json").write_text(json.dumps({"connectors": ["app.json"], "state_dir": "s2"}))
    registry = Registry()
    registry[winapp.RUN_VALUE] = f'"python" -m raincli_agent runtime run --config "{far / "runtime-run.json"}"'
    root = tmp_path / "app"
    root.mkdir()
    winapp.write_settings(root, {"agent_config": str(agent(home)), "runtime_config": str(far / "runtime-app.json")})
    result = login.set_aside(str(agent(home)), migration=migration(home, registry=registry, app_root=root,
                                                                   connector_configs=[str(explicit)]))
    moved = {m["from"] for m in result["moved"]}
    assert {str(via_run), str(via_app), str(explicit), str(far / "runtime-run.json"),
            str(far / "runtime-app.json")} <= moved
    assert winapp.read_settings(root) == {} and result["app_settings"] == "cleared"
    assert result["run_value"] == "app" and registry[winapp.RUN_VALUE] == winapp.run_value(root)  # V5


def test_old_run_value_is_removed_by_the_cli(account, home):
    from raincli_agent.runtime import winapp
    sign_in(account, home)
    registry = Registry()
    registry[winapp.RUN_VALUE] = f'"raincli" runtime run --config "{cfg(home) / "runtime.json"}"'
    result = login.set_aside(str(agent(home)), migration=migration(home, registry=registry))
    assert result["run_value"] == "removed" and winapp.RUN_VALUE not in registry
    log = (cfg(home) / "runtime-state" / "migration.log").read_text()
    assert "old_run_value_disabled" in log


def test_refuses_while_another_process_holds_a_queue(account, home):
    """V3: nothing changes while an old window's connector holds a queue run lock."""
    sign_in(account, home)
    state = cfg(home) / "runtime-state"
    state.mkdir(mode=0o700, exist_ok=True)
    holder = Queue(str(state))
    holder.acquire_run_lock()
    before = files(cfg(home))
    try:
        with pytest.raises(setaside.SetAsideRefused, match="Close the old RainCLI window"):
            login.set_aside(str(agent(home)), migration=migration(home), wait=0)
    finally:
        holder.release_run_lock()
    assert files(cfg(home)) == before and not list(cfg(home).glob("replaced-*"))


def test_stops_the_apps_own_runtime_first(account, home):
    """V6: the window's host.pause() runs before anything is checked or moved."""
    sign_in(account, home)
    state = cfg(home) / "runtime-state"
    state.mkdir(mode=0o700, exist_ok=True)
    runtime_lock = Queue(str(state))
    runtime_lock.acquire_run_lock()  # the app's own runtime
    calls = []
    result = login.set_aside(str(agent(home)), migration=migration(home, own_runtime=str(cfg(home) / "runtime.json")),
                             stop_own=lambda: calls.append("pause") or runtime_lock.release_run_lock())
    assert calls == ["pause"] and Path(result["backup"]).is_dir()


def test_a_failed_move_moves_everything_back(account, home):
    sign_in(account, home)
    before = files(cfg(home))
    calls = []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 3:
            raise OSError("disk full")
        os.replace(src, dst)
    with pytest.raises(OSError, match="disk full"):
        login.set_aside(str(agent(home)), migration=migration(home), _replace=flaky)
    assert files(cfg(home)) == before and not list(cfg(home).glob("replaced-*"))


def test_holds_the_migration_lock(account, home):
    sign_in(account, home)
    fd = setaside.lock_migration(cfg(home))
    try:
        with pytest.raises(setaside.SetAsideRefused, match="running"):
            login.set_aside(str(agent(home)), migration=migration(home))
    finally:
        setaside.unlock_migration(fd)


def test_backup_directory_is_exclusive(account, home, monkeypatch):
    sign_in(account, home)
    monkeypatch.setattr(setaside, "_stamp", lambda: "20261006T120000Z")
    (cfg(home) / "replaced-20261006T120000Z").mkdir()
    result = login.set_aside(str(agent(home)), migration=migration(home))
    assert Path(result["backup"]).name == "replaced-20261006T120000Z-2"


# -- §16.17 3: migration ----------------------------------------------------------------------------------

def pip_client(home, token, name="agent.json"):
    path = cfg(home) / name
    write_config(str(path), "https://raincli.example", token)
    return path


def test_migration_sets_aside_an_invalid_credential(home, tmp_path):
    pip_client(home, "rca_" + "B" * 43)
    root = tmp_path / "app"
    root.mkdir()
    (root / "RainCLI.exe").write_text("")
    m = migration(home, app_root=root, check=lambda agent: "invalid")
    result = m.run()
    assert result["status"] == "fresh_sign_in_needed"
    [aside] = result["set_aside"]
    assert not agent(home).exists() and (Path(aside["backup"]) / "agent.json").exists()
    log = (cfg(home) / "runtime-state" / "migration.log").read_text()
    assert '"stale_credential_set_aside"' in log and aside["backup"] in log


def test_migration_with_one_stale_credential_of_several(home, tmp_path):
    """V2: a valid credential remains, so it migrates and says what was set aside."""
    good = pip_client(home, "rca_" + "C" * 43)
    stale = pip_client(home, "rca_" + "D" * 43, name="old.json")
    for name, target in (("good-conn.json", good), ("old-conn.json", stale)):
        (cfg(home) / name).write_text(json.dumps({"agent_config": str(target), "herdr_agent": name[:3],
                                                  "state_dir": str(tmp_path / name)}))
    (cfg(home) / "runtime.json").write_text(json.dumps({"connectors": ["good-conn.json", "old-conn.json"],
                                                        "state_dir": "rs"}))
    m = migration(home, check=lambda a: "invalid" if a.endswith("old.json") else "ok")
    result = m.run()
    assert result["status"] == "migrated_with_stale_set_aside"
    assert result["agent_configs"] == [str(good)] and not stale.exists()
    assert json.loads((cfg(home) / "runtime.json").read_text())["connectors"] == ["good-conn.json"]


def test_install_while_offline_adopts_then_the_sign_in_check_catches_it(account, home, monkeypatch, capsys):
    """Test (e): offline, the credential is adopted as unknown; once the server answers, the
    sign-in check finds it stale and points to --new-machine."""
    import getpass
    sign_in(account, home)
    (cfg(home) / "runtime.json").unlink()
    account.state.fail("GET", r"/me$", (503, "unavailable", {}), times=10)
    result = migration(home).run()
    assert result["status"] == "migrated" and agent(home).exists()
    account.state.faults.clear()
    revoke(account, home)
    monkeypatch.setattr(cli, "_need_tty", lambda what: None)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": pytest.fail("no password before the check"))
    assert cli.main(["login", "--person", "--email", EMAIL]) == 1
    err = capsys.readouterr().err
    assert "belongs to a machine that was revoked" in err and "raincli login --new-machine" in err


# -- §16.17 4: the CLI ---------------------------------------------------------------------------------------

def test_login_person_on_another_accounts_machine(account, home, monkeypatch, capsys):
    import getpass
    sign_in(account, home)
    st = account.state
    st.owners[st.tokens[load_config(str(agent(home))).token.reveal()]] = "mallory@example.test"
    monkeypatch.setattr(cli, "_need_tty", lambda what: None)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": PASSWORD)
    assert cli.main(["login", "--person", "--email", EMAIL]) == 1
    err = capsys.readouterr().err
    assert "owned by another account" in err and "stays active for its owner" in err
    assert "raincli login --new-machine" in err


def test_login_person_explains_the_409s(account, home, monkeypatch, capsys):
    """The check can't tell (an older /me), and person_only answers 409."""
    import getpass
    sign_in(account, home)
    account.state.me_owner = False
    st = account.state
    st.owners[st.tokens[load_config(str(agent(home))).token.reveal()]] = "mallory@example.test"
    monkeypatch.setattr(cli, "_need_tty", lambda what: None)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": PASSWORD)
    assert cli.main(["login", "--person", "--email", EMAIL]) == 1
    assert "owned by another account" in capsys.readouterr().err


def test_login_new_machine(account, home, monkeypatch, capsys):
    """Set aside, then a normal fresh sign-in with a new password prompt (V7); the suggested
    name is never the old handle."""
    import builtins
    import getpass
    held, _ = old_machine_with_held_messages(account, home)
    revoke(account, home)
    prompts = []
    monkeypatch.setattr(cli, "_need_tty", lambda what: None)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": prompts.append(prompt) or PASSWORD)
    answers = iter([""])  # accept the suggested machine name
    asked = []
    monkeypatch.setattr(builtins, "input", lambda prompt="": asked.append(prompt) or next(answers))
    code = cli.main(["login", "--new-machine", "--email", EMAIL, "--api-url", account.url])
    output = capsys.readouterr()
    assert code == 0, output.err
    assert asked == ["Machine name [work-pc-2]: "] and prompts == ["Password: "]
    assert "signed in as work-pc-2" in output.out and "moved this computer's old RainCLI setup" in output.out
    [backup] = cfg(home).glob("replaced-*")
    assert (backup / "agent.json").exists() and (backup / "runtime-state" / "queue").is_dir()
    assert setaside.check_credential(str(agent(home)), EMAIL) == "ok"
    assert person.load_session(str(agent(home))) is not None
    assert PASSWORD not in output.out + output.err


def test_login_new_machine_needs_an_old_setup(home, monkeypatch):
    monkeypatch.setattr(cli, "_need_tty", lambda what: None)
    assert cli.main(["login", "--new-machine", "--email", EMAIL]) == 2


def test_suggested_name_is_never_the_old_handle(monkeypatch):
    monkeypatch.setattr(login, "default_machine_name", lambda: "dylan-win")
    assert login.suggest_new_machine_name("dylan-win") == "dylan-win-2"
    assert login.suggest_new_machine_name(None) == "dylan-win-2"
    assert login.suggest_new_machine_name("someone-else") == "dylan-win"
    monkeypatch.setattr(login, "default_machine_name", lambda: "a" * 32)
    assert login.suggest_new_machine_name(None) == "a" * 30 + "-2"


# -- review 1 ----------------------------------------------------------------------------------------

def shared_runtime(account, home, tmp_path, inbox_name=None):
    """A connector runtime serving this credential and another one (V2)."""
    sign_in(account, home)
    (cfg(home) / "runtime.json").unlink()
    other = tmp_path / "other" / "agent.json"
    other.parent.mkdir()
    write_config(str(other), account.url, account.state.add_agent("other-box"))
    mine = {"agent_config": str(agent(home)), "state_dir": str(tmp_path / "my-queue")}
    mine.update({"inbox": {"hook": "claude", "name": inbox_name}} if inbox_name else {"herdr_agent": "a"})
    (cfg(home) / "mine.json").write_text(json.dumps(mine))
    (cfg(home) / "theirs.json").write_text(json.dumps({"agent_config": str(other), "herdr_agent": "b",
                                                        "state_dir": str(tmp_path / "their-queue")}))
    runtime = cfg(home) / "runtime.json"
    runtime.write_text(json.dumps({"connectors": ["mine.json", "theirs.json"], "state_dir": "rs"}))
    (cfg(home) / "rs").mkdir(mode=0o700)
    return runtime


def test_r2_a_running_shared_runtime_is_started_again(account, home, tmp_path):
    runtime = shared_runtime(account, home, tmp_path)
    state = Queue(str(cfg(home) / "rs"))
    state.acquire_run_lock()  # the app's runtime runs it
    calls = []
    result = login.set_aside(str(agent(home)), migration=migration(home, own_runtime=str(runtime)),
                             stop_own=lambda: calls.append("pause") or state.release_run_lock(),
                             restart_own=lambda: calls.append("resume"))
    assert calls == ["pause", "resume"] and result["restarted"] == {str(runtime): "restarted"}
    assert json.loads(runtime.read_text())["connectors"] == ["theirs.json"]


def test_r2_without_a_way_to_restart_the_command_is_returned(account, home, tmp_path):
    runtime = shared_runtime(account, home, tmp_path)
    state = Queue(str(cfg(home) / "rs"))
    state.acquire_run_lock()
    m = migration(home, own_runtime=str(runtime))
    result = login.set_aside(str(agent(home)), migration=m, stop_own=state.release_run_lock)
    assert result["restarted"] == {str(runtime): f"raincli runtime run --config {runtime}"}


def test_r3_a_refusal_stops_nothing(account, home, tmp_path):
    """A queue held by something that couldn't be started again (a foreground window): refuse
    before stopping anything."""
    runtime = shared_runtime(account, home, tmp_path)
    window = Queue(str(tmp_path / "my-queue"))  # creates the queue directory
    window.acquire_run_lock()
    calls = []
    try:
        with pytest.raises(setaside.SetAsideRefused, match="Close the old RainCLI window"):
            login.set_aside(str(agent(home)), migration=migration(home, own_runtime=str(runtime)),
                            stop_own=lambda: calls.append("pause"), restart_own=lambda: calls.append("resume"),
                            wait=0)
    finally:
        window.release_run_lock()
    assert calls == [] and agent(home).exists()


def test_r3_a_timeout_starts_again_what_was_stopped(account, home, tmp_path):
    runtime = shared_runtime(account, home, tmp_path)
    state = Queue(str(cfg(home) / "rs"))
    state.acquire_run_lock()
    calls = []
    try:
        with pytest.raises(setaside.SetAsideRefused):
            login.set_aside(str(agent(home)), migration=migration(home, own_runtime=str(runtime)),
                            stop_own=lambda: calls.append("pause"),  # but the runtime never lets go
                            restart_own=lambda: calls.append("resume"), wait=0)
    finally:
        state.release_run_lock()
    assert calls == ["pause", "resume"] and agent(home).exists()


def test_r5_a_state_dir_on_another_volume_is_renamed_in_place(account, home):
    import errno
    sign_in(account, home)
    state = cfg(home) / "runtime-state"
    state.mkdir(mode=0o700, exist_ok=True)
    (state / "machine-salt").write_text("salt")

    def cross_volume(src, dst):
        if Path(src) == state and Path(dst).parent.name.startswith("replaced-"):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        os.replace(src, dst)
    result = login.set_aside(str(agent(home)), migration=migration(home), _replace=cross_volume)
    [renamed] = result["renamed_in_place"]
    assert renamed["from"] == str(state) and Path(renamed["to"]).name.startswith("runtime-state.replaced-")
    assert (Path(renamed["to"]) / "machine-salt").read_text() == "salt" and not state.exists()
    assert json.loads((Path(result["backup"]) / "set-aside.json").read_text())["renamed_in_place"] == [renamed]


def test_r5_another_move_error_still_rolls_back(account, home):
    sign_in(account, home)
    before = files(cfg(home))

    def broken(src, dst):
        if Path(src).name == "runtime.json":
            raise OSError(5, "I/O error")
        os.replace(src, dst)
    with pytest.raises(OSError, match="I/O error"):
        login.set_aside(str(agent(home)), migration=migration(home), _replace=broken)
    assert files(cfg(home)) == before


def test_r7_a_rewrite_moves_the_removed_connectors_handover_boxes(account, home, tmp_path):
    runtime = shared_runtime(account, home, tmp_path, inbox_name="old project")
    state = cfg(home) / "rs"
    sessions.ensure_salt(str(state))
    sessions.sessions_dir(str(state), create=True)
    key = sessions.agent_key(sessions.ensure_salt(str(state)), "claude:s1")
    sessions.hand_over(str(state), key, "0b7f3c1e-2a4d-4c6e-9f10-1a2b3c4d5e6f", "old account's message")
    sessions.hand_over(str(state), sessions.name_box("old project"), "1b7f3c1e-2a4d-4c6e-9f10-1a2b3c4d5e6f", "by name")
    queue_dir = tmp_path / "my-queue" / "messages"
    queue_dir.mkdir(parents=True)
    (queue_dir / "0b7f3c1e-2a4d-4c6e-9f10-1a2b3c4d5e6f.json").write_text(json.dumps(
        {"id": "0b7f3c1e-2a4d-4c6e-9f10-1a2b3c4d5e6f", "state": "handed_over", "handover_key": key}))
    result = login.set_aside(str(agent(home)), migration=migration(home))
    assert sessions.claim(str(state), key) == ([], [])
    assert sessions.claim(str(state), sessions.name_box("old project")) == ([], [])
    moved = {Path(m["from"]).name for m in result["moved"]}
    assert key + ".inbox" in moved
    assert json.loads(runtime.read_text())["connectors"] == ["theirs.json"]


# -- §16.20 F1: which server a fresh sign-in uses ---------------------------------------------------

def interactive(monkeypatch, answers=("",)):
    import builtins
    import getpass
    monkeypatch.setattr(cli, "_need_tty", lambda what: None)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": PASSWORD)
    feed = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(feed))


def test_f1_first_sign_in_uses_the_default_never_a_backup(account, home, monkeypatch, capsys):
    monkeypatch.setattr(login, "DEFAULT_API_URL", account.url)
    backup = cfg(home) / "replaced-20261001T000000Z"
    backup.mkdir()
    write_config(str(backup / "agent.json"), "https://old-service.example", "rca_" + "Z" * 43)
    interactive(monkeypatch)
    assert cli.main(["login", "--email", EMAIL]) == 0
    output = capsys.readouterr()
    assert "signed in as work-pc" in output.out and "old-service.example" not in output.out + output.err
    assert load_config(str(agent(home))).api_url == account.url
    assert login.setup_host(str(backup / "agent.json")) is None  # a backup is never a source


def test_f1_new_machine_uses_the_default_and_hints_the_old_host(account, home, monkeypatch, capsys):
    monkeypatch.setattr(login, "DEFAULT_API_URL", account.url)
    write_config(str(agent(home)), "https://old-service.example:8443", "rca_" + "Y" * 43)
    interactive(monkeypatch)
    assert cli.main(["login", "--new-machine", "--email", EMAIL]) == 0
    output = capsys.readouterr()
    assert ("note: this computer's current setup uses https://old-service.example:8443; this sign-in goes to "
            f"{login.host_of(account.url)}") in output.out
    assert "--api-url https://old-service.example:8443" in output.out
    assert load_config(str(agent(home))).api_url == account.url  # the default, not the old host


def test_f1_an_explicit_api_url_wins_and_prints_no_hint(account, home, monkeypatch, capsys):
    monkeypatch.setattr(login, "DEFAULT_API_URL", "https://raincli.example")
    write_config(str(agent(home)), "https://old-service.example", "rca_" + "Y" * 43)
    interactive(monkeypatch)
    assert cli.main(["login", "--new-machine", "--email", EMAIL, "--api-url", account.url]) == 0
    assert "note: this computer's current setup" not in capsys.readouterr().out
    assert load_config(str(agent(home))).api_url == account.url


def test_f1_same_host_gives_no_hint(home):
    write_config(str(agent(home)), "https://raincli.com", "rca_" + "Y" * 43)
    assert login.setup_host(str(agent(home)), "https://raincli.com") is None
    assert login.setup_host(str(cfg(home) / "missing.json")) is None
