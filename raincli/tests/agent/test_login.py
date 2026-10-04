"""Machine sign-in and sign-out (protocol 15.1, 15.2, 15.8 H2, H3, L1, M2, M3, M9)."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest

from raincli_agent import cli, login
from raincli_agent.config import Secret, load_config, write_config
from raincli_agent.errors import ApiError, ConfigError, UsageError

PASSWORD = "correct horse battery staple 7f3a"
EMAIL = "alice@example.test"

# Shared with the server's slug tests (15.8 L1): computer name -> machine name.
SLUG_VECTORS = [
    ("DESKTOP-4F2K9QH", "desktop-4f2k9qh"),
    ("Dylan's MacBook Pro", "dylan-s-macbook-pro"),
    ("  --Build_Box--  ", "build-box"),
    ("123server", "m-123server"),
    ("9", "m-9"),
    ("", "machine"),
    ("---", "machine"),
    ("é", "machine"),
    ("x", "machine"),
    ("ab", "ab"),
    ("Ünïcode Host", "n-code-host"),
    ("a" * 40, "a" * 32),
    ("1" + "b" * 40, "m-1" + "b" * 29),
    ("a" * 31 + " b", "a" * 31),
    ("WIN_SERVER.corp.example", "win-server-corp-example"),
]


@pytest.mark.parametrize("name,slug", SLUG_VECTORS)
def test_slug_vectors(name, slug):
    assert login.slugify_machine_name(name) == slug
    assert login.HANDLE_RE.fullmatch(slug)


@pytest.fixture
def account(fake_api):
    fake_api.state.add_user(EMAIL, PASSWORD, teams=("alpha",))
    return fake_api


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A private HOME: the default config directory and scans stay in tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def config_path(home):
    return home / ".config" / "raincli" / "agent.json"


def sign_in(fake_api, home, **kw):
    kw.setdefault("machine_name", "work-pc")
    return login.login(EMAIL, Secret(kw.pop("password", PASSWORD)), api_url=fake_api.url,
                       config_path=str(config_path(home)), **kw)


def test_new_machine_writes_config_and_machine_runtime(account, home):
    result = sign_in(account, home)
    assert (result["handle"], result["rotated"], result["team"]["slug"]) == ("work-pc", False, "alpha")
    cfg = load_config(str(config_path(home)))
    assert cfg.api_url == account.url  # the origin signed in to, not the reply's advisory api_url
    assert account.state.tokens[cfg.token.reveal()]
    runtime = json.loads((config_path(home).parent / "runtime.json").read_text())
    assert runtime == {"machine_config": str(config_path(home)), "state_dir": "runtime-state"}
    assert result["runtime_written"] and result["server_api_url"] == "https://raincli.example"
    assert oct(config_path(home).stat().st_mode & 0o777) == "0o600"
    assert PASSWORD not in config_path(home).read_text()


def test_refuses_existing_credential_without_force(account, home):
    sign_in(account, home)
    with pytest.raises(login.AlreadySignedIn):
        login.prepare(str(config_path(home)))
    assert len(account.state.login_bodies) == 1  # refused before any request


def test_force_rotates_own_machine_with_previous_token(account, home):
    sign_in(account, home)
    old = load_config(str(config_path(home))).token.reveal()
    result = sign_in(account, home, force=True)
    assert result["rotated"] is True
    assert account.state.login_bodies[-1]["previous_token"] == old
    assert old not in account.state.tokens  # the old credential is revoked
    assert load_config(str(config_path(home))).token.reveal() in account.state.tokens


def test_name_in_use_needs_replace_confirmation(account, home, tmp_path):
    other = tmp_path / "other" / "agent.json"
    login.login(EMAIL, Secret(PASSWORD), api_url=account.url, config_path=str(other), machine_name="work-pc")
    with pytest.raises(login.NameInUse):
        sign_in(account, home)
    result = sign_in(account, home, replace=True)
    assert result["rotated"] and account.state.login_bodies[-1]["replace"] is True


def test_machine_with_delivery_history_never_rotates_by_replace(account, home, tmp_path):
    other = tmp_path / "other" / "agent.json"
    login.login(EMAIL, Secret(PASSWORD), api_url=account.url, config_path=str(other), machine_name="work-pc")
    account.state.delivered.add(account.state.agent_by_handle("alpha", "work-pc")["id"])
    with pytest.raises(login.NameInUse):
        sign_in(account, home, replace=True)


def test_name_taken_by_another_member(account, home):
    account.state.add_agent("work-pc")  # not owned by this account
    with pytest.raises(login.NameTaken):
        sign_in(account, home)
    assert not config_path(home).exists()


def test_team_choice(fake_api, home):
    fake_api.state.add_user(EMAIL, PASSWORD, teams=("alpha", "beta"))
    with pytest.raises(login.TeamChoiceRequired) as info:
        sign_in(fake_api, home)
    assert [t["slug"] for t in info.value.teams] == ["alpha", "beta"]
    assert sign_in(fake_api, home, team="beta")["team"]["slug"] == "beta"


def test_wrong_password_and_rate_limit(account, home):
    for _ in range(5):
        with pytest.raises(login.InvalidCredentials):
            sign_in(account, home, password="wrong")
    with pytest.raises(login.RateLimited) as info:
        sign_in(account, home)
    assert info.value.retry_after == 900
    assert len(account.state.login_bodies) == 6  # one request per attempt, never retried
    assert not config_path(home).exists()


def test_errors_never_carry_the_password(account, home, monkeypatch):
    account.state.fail("POST", r"/app/login", (400, "invalid " + PASSWORD, {}))
    with pytest.raises(login.InvalidRequest) as info:
        sign_in(account, home)
    assert PASSWORD not in str(info.value) and "***" in str(info.value)


def test_redirect_never_followed(account, home, recorder):
    account.state.redirect_to = recorder.url
    with pytest.raises(ApiError):
        sign_in(account, home)
    assert recorder.requests == []


def test_api_url_must_be_https_unless_loopback(home):
    with pytest.raises(ConfigError):
        login.login(EMAIL, Secret(PASSWORD), api_url="http://raincli.example", config_path=str(config_path(home)),
                    machine_name="work-pc")


def write_connector(home, agent, **extra):
    path = home / ".config" / "raincli" / "connector.json"
    data = {"herdr_agent": "inbox", **extra}
    if agent is not None:
        data["agent_config"] = str(agent)
    path.write_text(json.dumps(data))
    return path


def test_refuses_when_a_connector_uses_the_credential(account, home):
    """15.8 H3: no sign-in reroutes delivery, --force included."""
    write_config(str(config_path(home)), account.url, account.alice)
    write_connector(home, None)  # no agent_config: the default path, which is this one
    with pytest.raises(login.ConnectorMachine):
        sign_in(account, home, force=True, machine_name="other-name")
    account.state.tokens.pop(account.alice)  # a revoked credential cannot prove a rotation
    with pytest.raises(login.ConnectorMachine):
        login.prepare(str(config_path(home)), force=True)
    assert account.state.login_bodies == []


def test_connector_credential_rotates_only_its_own_handle(account, home):
    agent = config_path(home)
    token = account.state.add_agent("conn-box")
    account.state.owners[account.state.tokens[token]] = EMAIL
    write_config(str(agent), account.url, token)
    write_connector(home, agent)
    runtime = agent.parent / "runtime.json"
    runtime.write_text(json.dumps({"connectors": ["connector.json"]}))
    before = runtime.read_bytes()
    plan = login.prepare(str(agent), force=True)
    assert plan["handle"] == "conn-box" and not plan["write_runtime"]
    result = login.login(EMAIL, Secret(PASSWORD), plan=plan, api_url=account.url)
    assert result["handle"] == "conn-box" and result["rotated"]
    assert runtime.read_bytes() == before  # a connector-mode runtime config is never replaced
    assert account.state.login_bodies[-1]["previous_token"] == token


def test_never_replaces_a_connector_runtime_for_a_new_sign_in(account, home):
    runtime = config_path(home).parent / "runtime.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(json.dumps({"connectors": ["elsewhere.json"]}))
    with pytest.raises(login.ConnectorMachine):
        login.prepare(str(config_path(home)))


def test_logout_revokes_and_cleans_up_keeping_queues(account, home, monkeypatch):
    from raincli_agent.runtime import startup
    calls = []
    monkeypatch.setattr(startup, "remove_for", lambda config: calls.append(config) or "disabled")
    sign_in(account, home)
    queue = home / ".local" / "state" / "raincli" / "connector" / "work-pc"
    queue.mkdir(parents=True)
    (queue / "cursor.json").write_text("{}")
    token = load_config(str(config_path(home))).token.reveal()
    result = login.logout(str(config_path(home)))
    assert result["server"] == "signed_out" and token not in account.state.tokens
    assert not config_path(home).exists() and not (config_path(home).parent / "runtime.json").exists()
    assert (queue / "cursor.json").exists()
    assert calls == [str(config_path(home).parent / "runtime.json")]  # logon start disabled (15.8 M9)


def test_logout_failure_keeps_the_credential(account, home, monkeypatch):
    sign_in(account, home)
    account.state.fail("POST", r"/app/sign-out", (503, "unavailable", {}), times=10)
    with pytest.raises(ApiError):
        login.logout(str(config_path(home)))
    assert config_path(home).exists()
    from raincli_agent.runtime import startup
    monkeypatch.setattr(startup, "remove_for", lambda config: "not_enabled")
    assert login.logout(str(config_path(home)), local_only=True)["server"] == "skipped"
    assert not config_path(home).exists()


def test_logout_of_a_revoked_machine_cleans_up(account, home, monkeypatch):
    from raincli_agent.runtime import startup
    monkeypatch.setattr(startup, "remove_for", lambda config: "not_enabled")
    sign_in(account, home)
    account.state.tokens.clear()
    assert login.logout(str(config_path(home)))["server"] == "already_revoked"


def test_logout_asks_for_confirmation_without_a_tty(account, home, capsys):
    sign_in(account, home)
    assert cli.main(["--config", str(config_path(home)), "logout"]) == 2
    assert config_path(home).exists()


def test_cli_login_refuses_without_tty_before_getpass(monkeypatch, home):
    import getpass
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: pytest.fail("getpass called"))
    monkeypatch.setattr(sys, "stdin", open(os.devnull))
    with pytest.raises(UsageError):
        cli._need_tty("raincli login")
    assert cli.main(["login", "--email", EMAIL]) == 2


def test_cli_login_with_mocked_getpass(account, home, monkeypatch, capsys):
    import builtins
    import getpass
    monkeypatch.setattr(cli, "_need_tty", lambda what: None)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": PASSWORD)
    answers = iter([""])  # accept the default machine name
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    monkeypatch.setattr(login, "default_machine_name", lambda: "auto-name")
    code = cli.main(["--config", str(config_path(home)), "login", "--email", EMAIL, "--api-url", account.url])
    output = capsys.readouterr()
    assert code == 0, output.err
    assert "signed in as auto-name in team alpha" in output.out
    assert "raincli runtime startup --config" in output.out
    assert PASSWORD not in output.out + output.err


def run_on_pty(argv, env, inputs, timeout=30):
    """Run ``argv`` on a pseudo-terminal, answering prompts in order. Returns
    (exit status, transcript, /proc snapshots of argv and environment taken
    after the password was typed)."""
    import pty
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # the child execs at once
        pid, fd = pty.fork()
    if pid == 0:
        os.execve(argv[0], argv, env)
    transcript, snapshots, pending = b"", [], list(inputs)
    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                break
            transcript += chunk
            while pending and pending[0][0] in transcript.decode(errors="replace").rsplit("\n", 1)[-1]:
                prompt, answer = pending.pop(0)
                os.write(fd, answer.encode() + b"\n")
                if prompt.startswith("Password"):
                    time.sleep(0.2)
                    for name in ("cmdline", "environ"):
                        try:
                            snapshots.append(Path(f"/proc/{pid}/{name}").read_bytes())
                        except OSError:
                            pass
        done, raw = os.waitpid(pid, os.WNOHANG)
        if done:
            status = os.waitstatus_to_exitcode(raw)
            while select.select([fd], [], [], 0.2)[0]:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                transcript += chunk
            break
    if status is None:
        _, raw = os.waitpid(pid, 0)
        status = os.waitstatus_to_exitcode(raw)
    os.close(fd)
    return status, transcript.decode(errors="replace"), snapshots


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="pty and /proc")
def test_password_on_a_pty_never_reaches_argv_env_logs_or_output(fake_api, home, tmp_path):
    """The real CLI on a pty: no echo, team choice asked on the terminal, and the
    password is absent from the process's argv and environment, its output, the
    config and every file it wrote."""
    fake_api.state.add_user(EMAIL, PASSWORD, teams=("alpha", "beta"))
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).parents[2])}
    argv = [sys.executable, "-m", "raincli_agent", "login", "--email", EMAIL, "--machine-name", "pty-box",
            "--api-url", fake_api.url]
    status, transcript, snapshots = run_on_pty(argv, env, [("Password:", PASSWORD), ("Team", "2")])
    assert status == 0, transcript
    assert "signed in as pty-box in team beta" in transcript
    assert PASSWORD not in transcript  # not echoed
    assert snapshots and all(PASSWORD.encode() not in s for s in snapshots)
    for path in home.rglob("*"):
        if path.is_file():
            assert PASSWORD.encode() not in path.read_bytes(), path
    assert [b["password"] for b in fake_api.state.login_bodies] == [PASSWORD, PASSWORD]
    assert "team" not in fake_api.state.login_bodies[0] and fake_api.state.login_bodies[1]["team"] == "beta"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="pty and /proc")
def test_deliberate_400_leaves_no_password_in_output(fake_api, home):
    """15.8 M3: a refused request shows the server's code, never the request body."""
    fake_api.state.add_user(EMAIL, PASSWORD)
    fake_api.state.fail("POST", r"/app/login", (400, "invalid", {}))
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).parents[2])}
    argv = [sys.executable, "-m", "raincli_agent", "login", "--email", EMAIL, "--machine-name", "pty-box",
            "--api-url", fake_api.url]
    status, transcript, _ = run_on_pty(argv, env, [("Password:", PASSWORD)])
    assert status != 0 and "refused the sign-in request" in transcript
    assert PASSWORD not in transcript


def test_cli_login_no_tty_subprocess(home):
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).parents[2])}
    result = subprocess.run([sys.executable, "-m", "raincli_agent", "login", "--email", EMAIL],
                            input=PASSWORD + "\n", capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 2 and "interactive terminal" in result.stderr
    assert PASSWORD not in result.stdout + result.stderr
