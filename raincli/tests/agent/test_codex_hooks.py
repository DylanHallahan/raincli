"""Codex hooks, including native Windows (Phase 2, phase2-codex.md section 3).

Hook input fixtures follow Codex's generated schemas (openai/codex at 7f89227,
codex-rs/hooks/schema/generated/{session-start,user-prompt-submit,stop,
permission-request,session-end}.command.input.schema.json): every required field is
present, with the documented types.
"""
import io
import json
import math
import os
from pathlib import Path
import types

import pytest

from raincli_agent.api import ApiClient
from raincli_agent.connector.config import load_connector_config
from raincli_agent.connector.herdr import FakeHerdr
from raincli_agent.connector.queue import Queue
from raincli_agent.connector.runner import Connector
from raincli_agent.errors import ConfigError
from raincli_agent.runtime import hook, hooks_install, procinfo, sessions

from .conftest import body_of, send, write_agent_config

SESSION = "019a7f2e-4c1d-7b3e-9a21-0f6c5d4e3b2a"
COMMON = {"session_id": SESSION, "transcript_path": None, "cwd": "/home/dev/work/my project",
          "model": "gpt-5.5-codex", "permission_mode": "default"}
FIXTURES = {
    "SessionStart": {**COMMON, "hook_event_name": "SessionStart", "source": "startup"},
    "UserPromptSubmit": {**COMMON, "hook_event_name": "UserPromptSubmit", "turn_id": "t-1", "prompt": "continue"},
    "Stop": {**COMMON, "hook_event_name": "Stop", "turn_id": "t-1", "stop_hook_active": False,
             "last_assistant_message": "done"},
    "PermissionRequest": {**COMMON, "hook_event_name": "PermissionRequest", "turn_id": "t-2",
                          "tool_name": "shell", "tool_input": {"command": ["ls"]}},
    "SessionEnd": {**COMMON, "hook_event_name": "SessionEnd", "reason": "exit"},
}


def approx_tokens(text):
    """Codex's estimate: UTF-8 bytes / 4, rounded up (codex-rs/utils/string approx_token_count)."""
    return math.ceil(len(text.encode("utf-8")) / 4)


@pytest.fixture(autouse=True)
def no_agent_process(monkeypatch):
    monkeypatch.setattr(hook, "agent_pid", lambda agent_type: None)


@pytest.fixture
def env(tmp_path, fake_api):
    state = tmp_path / "runtime-state"
    state.mkdir(mode=0o700)
    salt = sessions.ensure_salt(str(state))
    sessions.sessions_dir(str(state), create=True)
    agent_cfg = write_agent_config(tmp_path / "bob-agent.json", fake_api.url, fake_api.bob)
    now = [1_000_000.0]

    def connector():
        path = tmp_path / "connector.json"
        path.write_text(json.dumps({"agent_config": agent_cfg, "inbox": {"hook": "codex", "name": "my project"},
                                    "state_dir": str(tmp_path / "queue"), "trusted_senders": ["alice"]}))
        cfg = load_connector_config(str(path))
        return Connector(cfg, ApiClient(fake_api.url, fake_api.bob), FakeHerdr(), Queue(cfg.state_dir),
                         log=lambda line: None, sleep=lambda s: None, clock=lambda: now[0],
                         sessions_state=str(state))

    def run(event, payload=None):
        out = io.BytesIO()
        data = payload if payload is not None else FIXTURES[event]
        code = hook.handle("codex", event, None, str(state), io.BytesIO(json.dumps(data).encode()), out, now=now[0])
        return code, (json.loads(out.getvalue()) if out.getvalue() else None)

    return types.SimpleNamespace(connector=connector, run=run, state=state, salt=salt, now=now)


def record(env):
    key = sessions.agent_key(env.salt, f"codex:{SESSION}")
    return sessions.load_record(str(env.state), key)


# -- the hook --------------------------------------------------------------------------------------

def test_codex_fixtures_set_status_and_the_project_basename(env):
    assert env.run("SessionStart")[0] == "recorded"
    rec = record(env)
    assert (rec["type"], rec["name"], rec["status"]) == ("codex", "my project", "idle")
    for event, status in (("UserPromptSubmit", "working"), ("PermissionRequest", "blocked"), ("Stop", "idle")):
        env.run(event)
        assert record(env)["status"] == status, event
    assert env.run("SessionEnd")[0] == "ended" and record(env) is None


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact", "fork"])
def test_every_codex_session_source_is_accepted(env, source):
    assert env.run("SessionStart", {**FIXTURES["SessionStart"], "source": source})[0] == "recorded"
    assert not (env.state / "hook.log").exists()


def test_unknown_source_is_logged_by_code_only(env):
    assert env.run("SessionStart", {**FIXTURES["SessionStart"], "source": "secret-path /home/x"})[0] == "recorded"
    log = (env.state / "hook.log").read_text()
    assert "unknown_session_source" in log and "secret" not in log


def test_codex_next_turn_claim_is_additional_context_above_2500_tokens(env, fake_api):
    """A message well over Codex's default 2,500-token spill threshold (and over Claude's
    10,000-character bound) is emitted whole, below the installed 9,000-token limit."""
    env.run("SessionStart")
    conn = env.connector()
    body = ("Ünïcödé ✓ report line with detail. " * 400)[:15000]
    msg = send(fake_api, fake_api.alice, "bob", body)
    conn.run_once()
    assert conn.queue.get(msg["id"])["state"] == "handed_over"
    code, output = env.run("UserPromptSubmit")
    assert code == "claimed"
    assert set(output) == {"hookSpecificOutput"}  # the Codex output schema: no other top-level keys needed
    specific = output["hookSpecificOutput"]
    assert set(specific) == {"hookEventName", "additionalContext"} and specific["hookEventName"] == "UserPromptSubmit"
    context = specific["additionalContext"]
    assert body_of(context) == body
    assert approx_tokens(context) > 2500 and sessions.context_chars(context) > sessions.CLAIM_CAP_CHARS
    assert approx_tokens(context) <= hooks_install.CODEX_CONTEXT_LIMIT
    conn.run_once()
    assert conn.queue.get(msg["id"])["state"] == "submitted"


def test_codex_claim_cap_matches_the_configured_limit(env):
    """The byte bound is the Codex bound: 32 KiB is at most 8,192 tokens, under 9,000."""
    assert math.ceil(sessions.CLAIM_CAP_BYTES / 4) < hooks_install.CODEX_CONTEXT_LIMIT
    text = "x" * (sessions.CLAIM_CAP_CHARS + 5000)
    assert sessions.fits_one_turn(text, "codex") and not sessions.fits_one_turn(text, "claude")
    assert not sessions.fits_one_turn("x" * (sessions.CLAIM_CAP_BYTES + 1), "codex")


def test_codex_session_start_also_claims(env, fake_api):
    env.run("SessionStart")
    conn = env.connector()
    msg = send(fake_api, fake_api.alice, "bob", "hello codex")
    conn.run_once()
    code, output = env.run("SessionStart", {**FIXTURES["SessionStart"], "source": "resume"})
    assert code == "claimed" and output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert body_of(output["hookSpecificOutput"]["additionalContext"]) == "hello codex"


def test_connector_accepts_a_codex_inbox_and_holds_oversize(env, fake_api):
    conn = env.connector()
    assert conn.config.inbox_hook == ("codex", "my project")
    env.run("SessionStart")
    msg = send(fake_api, fake_api.alice, "bob", "日" * 16000)  # 48 KB of UTF-8: over 32 KiB
    conn.run_once()
    rec = conn.queue.get(msg["id"])
    assert (rec["state"], rec["hold_reason"]) == ("held", "too_large_for_hook")
    assert str(sessions.CLAIM_CAP_BYTES) in rec["hold_detail"]


# -- liveness on Windows: codex.exe -> cmd.exe -> raincli.exe (shim) -> raincli.exe ------------------

def test_windows_liveness_reaches_codex_through_cmd_and_the_shim(monkeypatch):
    snapshot = {
        4000: ("raincli.exe", 3900),  # versions\0.4.0\raincli.exe running the hook
        3900: ("raincli.exe", 3800),  # bin\raincli.exe, the PATH shim
        3800: ("cmd.exe", 3700),  # %COMSPEC% /C "<command>"
        3700: ("codex.exe", 3600),  # the Codex CLI
        3600: ("node.exe", 1),
    }
    monkeypatch.setattr(procinfo, "sys", types.SimpleNamespace(platform="win32"))
    monkeypatch.setattr(procinfo.os, "name", "nt")
    monkeypatch.setattr(procinfo, "windows_snapshot", lambda: {pid: (procinfo.plain(exe), parent)
                                                                 for pid, (exe, parent) in snapshot.items()})
    assert procinfo.agent_pid("codex", start=3900) == 3700
    assert procinfo.agent_pid("claude", start=3900) is None  # never another agent's process
    snapshot[3800] = ("explorer.exe", 3700)
    assert procinfo.agent_pid("codex", start=3900) is None  # an unrelated process stops the walk


def test_pip_entry_point_launcher_passes_through():
    assert procinfo.passes_through("C:\\\\venv\\\\Scripts\\\\raincli.exe")
    assert procinfo.kind_of("C:\\\\npm\\\\vendor\\\\x86_64-pc-windows-msvc\\\\codex\\\\codex.exe") == "codex"


# -- hooks install --codex --------------------------------------------------------------------------

def probe(version):
    return lambda: (True, f"codex-cli {version}: feature hooks stable true")


SHIM = "C:\\Users\\First Last\\AppData\\Local\\Programs\\RainCLI\\bin\\raincli.exe"
PREFIX = ([SHIM], "app PATH shim")


def test_windows_codex_hooks_need_0_145(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for old in ("0.144.6", "0.132.0", "unknown"):
        with pytest.raises(ConfigError, match="0.145.0 or later"):
            hooks_install.install("codex", "C:\\state", home=home, prefix=PREFIX, probe=probe(old), windows=True)
    assert not (home / ".codex" / "hooks.json").exists()


def test_windows_codex_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", "C:\\WINDOWS")
    home = tmp_path / "home"
    state = "C:\\Users\\First Last\\.config\\raincli\\runtime-state"
    result = hooks_install.install("codex", state, home=home, prefix=PREFIX, probe=probe("0.160.0"), windows=True)
    assert result["status"] == "installed" and "/hooks" in result["note"] and "again" in result["note"]
    data = json.loads((home / ".codex" / "hooks.json").read_text())
    assert set(data) == {"hooks"}  # nothing else: no hook state, no trusted_hash
    assert "trusted_hash" not in json.dumps(data) and "bypass" not in json.dumps(data)
    for event in ("SessionStart", "UserPromptSubmit", "Stop", "PermissionRequest", "SessionEnd"):
        [group] = data["hooks"][event]
        [entry] = group["hooks"]
        assert entry["type"] == "command" and entry["statusMessage"] == "raincli"
        # As real codex 0.160.0 accepts them without warnings: the limit only where context
        # can be emitted, and SessionEnd within its 3 s cap.
        assert entry["timeout"] == (3 if event == "SessionEnd" else 5)
        if event in ("SessionStart", "UserPromptSubmit"):
            assert entry["additionalContextLimit"] == hooks_install.CODEX_CONTEXT_LIMIT
        else:
            assert "additionalContextLimit" not in entry
        expected = (f'C:\\WINDOWS\\System32\\cmd.exe /d /c call "{SHIM}" hook codex {event} --state-dir "{state}"')
        assert entry["commandWindows"] == entry["command"] == expected


@pytest.mark.parametrize("bad", ["C:\\wow!\\state", "C:\\100%\\state", "C:\\a^b", "C:\\a&b", "C:\\a|b", "C:\\a<b", "C:\\a>b",
                                 'C:\\a"b', "C:\\state\\", "C:\\a$b", "C:\\a`b",
                                 "C:\\Users\\O\u2019Brien\\s", "C:\\Users\\\u201cx\u201d\\s",
                                 "C:\\a\u201eb", "C:\\a\u2018b", "C:\\a\u201bb"])
def test_cmd_special_characters_are_refused(tmp_path, monkeypatch, bad):
    with pytest.raises(ConfigError, match="cmd.exe or PowerShell"):
        hooks_install.install("codex", bad, home=tmp_path / "h", prefix=PREFIX, probe=probe("0.160.0"), windows=True)
    if bad.endswith("\\"):
        return  # only a final path argument can end in a backslash
    with pytest.raises(ConfigError, match="cmd.exe"):
        hooks_install.install("codex", "C:\\ok", home=tmp_path / "h", prefix=([bad + "\\raincli.exe"], "x"),
                              probe=probe("0.160.0"), windows=True)


def test_codex_home_is_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "throwaway-codex"))
    assert hooks_install.config_path("codex") == tmp_path / "throwaway-codex" / "hooks.json"
    monkeypatch.delenv("CODEX_HOME")
    assert hooks_install.config_path("codex") == Path.home() / ".codex" / "hooks.json"


@pytest.mark.skipif(os.name == "nt", reason="POSIX form")
def test_posix_codex_entry_sets_the_limit_and_warns_on_old_codex(tmp_path):
    home = tmp_path / "home"
    result = hooks_install.install("codex", str(tmp_path / "s"), home=home, prefix=(["/opt/raincli"], "x"),
                                   probe=probe("0.140.0"))
    assert "0.145.0" in result["warning"]
    entry = json.loads((home / ".codex" / "hooks.json").read_text())["hooks"]["SessionStart"][0]["hooks"][0]
    assert entry["additionalContextLimit"] == hooks_install.CODEX_CONTEXT_LIMIT and "commandWindows" not in entry


def test_installer_never_bypasses_trust():
    source = Path(hooks_install.__file__).read_text()
    assert "dangerously" not in source and "trusted_hash\"" not in source.replace("trusted_hash", "trusted_hash")
    assert '"trusted_hash"' not in source and "'trusted_hash'" not in source


# -- §16.14 K1-K3 -----------------------------------------------------------------------------------

def make_exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    path.chmod(0o755)
    return path


def test_codex_probe_never_runs_one_from_the_current_directory(tmp_path, monkeypatch):
    """K1: absolute PATH entries only, by absolute path; codex.cmd allowed on Windows."""
    planted = make_exe(tmp_path / "repo" / "codex")
    make_exe(tmp_path / "repo" / "codex.cmd")
    monkeypatch.chdir(tmp_path / "repo")
    env = {"PATH": os.pathsep.join(["", ".", "repo", "./x"])}
    assert hooks_install.find_codex(env, windows=False) is None
    assert hooks_install.find_codex(env, windows=True) is None
    good = make_exe(tmp_path / "bin" / "codex")
    found = hooks_install.find_codex({"PATH": os.pathsep.join([".", str(good.parent)])}, windows=False)
    assert found == str(good) and os.path.isabs(found) and found != str(planted)
    npm = make_exe(tmp_path / "npm" / "codex.cmd")
    assert hooks_install.find_codex({"PATH": str(npm.parent)}, windows=True) == str(npm)
    native = make_exe(tmp_path / "npm" / "codex.exe")
    assert hooks_install.find_codex({"PATH": str(npm.parent)}, windows=True) == str(native)  # .exe first
    ran = []
    hooks_install.codex_support(lambda argv, **kw: ran.append(argv) or
                                types.SimpleNamespace(stdout="hooks stable true\n"), lambda: str(good))
    assert all(argv[0] == str(good) for argv in ran)


@pytest.mark.parametrize("exe", ["codex", "codex.exe", "C:\\dl\\codex-x86_64-pc-windows-msvc.exe",
                                 "/opt/codex-aarch64-unknown-linux-musl"])
def test_codex_release_binaries_count_as_codex(exe):
    """K2: liveness treats codex and codex-* executables as Codex."""
    assert procinfo.kind_of(exe) == "codex"
    assert procinfo.kind_of("C:\\x\\codexx.exe") is None


# -- §16.19 item 5: the line runs the same wrapped or not, and never starts with a quote ----------------

def cmd_c(line):
    """What ``cmd /C <rest>`` executes, by the rule ``cmd /?`` documents: if the text after /C
    starts with a quote and it is not the "exactly two quotes" case, cmd removes the first
    quote and the last quote."""
    rest = line.lstrip()
    if rest.startswith('"') and not (rest.count('"') == 2 and not any(c in rest for c in '&<>()@^|')
                                     and " " in rest.split('"')[1]):
        last = rest.rfind('"')
        rest = rest[1:last] + rest[last + 1:]
    return rest


def test_cmd_quote_rule_wrapped_and_unwrapped():
    argv = [SHIM, "hook", "codex", "SessionStart", "--state-dir", "C:\\Users\\First Last\\state"]
    line = hooks_install.windows_command_line(argv, cmd="C:\\Windows\\System32\\cmd.exe")
    assert not line.startswith('"')
    # Codex's cmd path wraps it (raw_arg "\"<line>\""); a plain cmd /C does not: both run the same.
    assert cmd_c(f'"{line}"') == cmd_c(line) == line
    # The inner cmd /d /c sees text starting with "call", so it never strips a quote either.
    inner = line.split(" /d /c ", 1)[1]
    assert inner.startswith("call ") and cmd_c(inner) == inner
    assert f'"{SHIM}"' in inner and '"C:\\Users\\First Last\\state"' in inner
    # The old form survived only when wrapped: unwrapped, cmd broke both paths.
    old = f'"{SHIM}" hook codex SessionStart --state-dir "C:\\Users\\First Last\\state"'
    assert cmd_c(f'"{old}"') == old and cmd_c(old) != old


def test_cmd_path_is_absolute_and_checked(monkeypatch):
    monkeypatch.setenv("SystemRoot", "D:\\Win")
    assert hooks_install.system_cmd() == "D:\\Win\\System32\\cmd.exe"
    with pytest.raises(ConfigError, match="cmd.exe's path"):
        hooks_install.windows_command_line(["x"], cmd="C:\\Program Files\\cmd.exe")


# -- §16.19 item 6: repair on update ------------------------------------------------------------------

def old_windows_codex(home, state):
    """A v0.5.0 entry (a line starting with a quoted path) next to someone else's hook."""
    path = home / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    old = f'"{SHIM}" hook codex SessionStart --state-dir "{state}"'
    data = {"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": old, "commandWindows": old, "timeout": 5,
                    "statusMessage": "raincli", "additionalContextLimit": hooks_install.CODEX_CONTEXT_LIMIT}]},
        {"hooks": [{"type": "command", "command": "echo mine", "statusMessage": "someone else"}]}]},
        "other": {"keep": True}}
    path.write_text(json.dumps(data))
    return path, old


def test_repair_regenerates_an_old_codex_entry_with_a_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", "C:\\WINDOWS")
    home, state = tmp_path / "home", tmp_path / "state"
    path, old = old_windows_codex(home, "C:\\Users\\First Last\\state")
    lines = []
    changed = hooks_install.repair(str(state), home=home, prefix=PREFIX, windows=True, log=lines.append, now=50.0)
    assert changed == {"codex": ["SessionStart"]}
    data = json.loads(path.read_text())
    [ours, theirs] = data["hooks"]["SessionStart"]
    assert ours["hooks"][0]["commandWindows"] == (
        f'C:\\WINDOWS\\System32\\cmd.exe /d /c call "{SHIM}" hook codex SessionStart '
        '--state-dir "C:\\Users\\First Last\\state"')  # its own state dir kept
    assert theirs == {"hooks": [{"type": "command", "command": "echo mine", "statusMessage": "someone else"}]}
    assert data["other"] == {"keep": True}
    [backup] = list(path.parent.glob("hooks.json.raincli-backup-*"))
    assert json.loads(backup.read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"] == old
    assert any(old in line and "cmd.exe /d /c call" in line for line in lines)
    notice = json.loads((state / hooks_install.NOTICE_FILE).read_text())
    assert notice["message"] == "RainCLI updated its Codex hooks; open /hooks in Codex and trust them again"
    assert json.loads((state / hooks_install.CONNECTED_FILE).read_text())["codex"] == 50.0  # needs_approval


def test_repair_writes_nothing_when_current_and_adds_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", "C:\\WINDOWS")
    home, state = tmp_path / "home", tmp_path / "state"
    hooks_install.install("codex", "C:\\state", home=home, prefix=PREFIX, probe=probe("0.160.0"), windows=True)
    path = home / ".codex" / "hooks.json"
    before = path.read_bytes()
    assert hooks_install.repair(str(state), home=home, prefix=PREFIX, windows=True) == {}
    assert path.read_bytes() == before and not list(path.parent.glob("*.raincli-backup-*"))
    assert not (home / ".claude" / "settings.json").exists()  # never installs for an agent without our hooks
    assert not (state / hooks_install.NOTICE_FILE).exists()


def test_claude_repair_raises_no_notice(tmp_path):
    home, state = tmp_path / "home", tmp_path / "state"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    old = {"type": "command", "command": "/old/raincli hook claude SessionStart --state-dir /s 2>/dev/null || true",
           "timeout": 5, "statusMessage": "raincli"}
    settings.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [old]}]}, "theme": "dark"}))
    changed = hooks_install.repair(str(state), home=home, prefix=(["/new/raincli"], "x"), windows=False)
    assert changed == {"claude": ["SessionStart"]}
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark" and "/new/raincli" in data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "--state-dir /s" in data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert not (state / hooks_install.NOTICE_FILE).exists()


def test_repair_runs_once_per_client_version(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", "C:\\WINDOWS")
    home, state = tmp_path / "home", tmp_path / "state"
    path, _ = old_windows_codex(home, "C:\\s")
    calls = []
    real = hooks_install.repair
    monkeypatch.setattr(hooks_install, "repair", lambda *a, **k: calls.append(1) or real(*a, **k))
    for _ in range(2):
        hooks_install.repair_on_version_change(str(state), "0.5.1", home=home, prefix=PREFIX, windows=True)
    assert len(calls) == 1
    hooks_install.repair_on_version_change(str(state), "0.5.2", home=home, prefix=PREFIX, windows=True)
    assert len(calls) == 2


def test_runtime_status_shows_the_notice_until_dismissed(tmp_path):
    from raincli_agent.runtime import service
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"machine_config": "agent.json", "state_dir": "st"}))
    (tmp_path / "st").mkdir()
    (tmp_path / "st" / "status.json").write_text(json.dumps({"status": "running", "updated_at": 1.0}))
    os.chmod(tmp_path / "st" / "status.json", 0o600)
    hooks_install.raise_notice(str(tmp_path / "st"), now=1.0)
    assert service.status(str(runtime))["notice"] == hooks_install.CODEX_NOTICE
    assert hooks_install.pending_notice(str(runtime)) == hooks_install.CODEX_NOTICE
    hooks_install.dismiss_notice(str(runtime))
    assert "notice" not in service.status(str(runtime)) and hooks_install.pending_notice(str(runtime)) is None


def test_r6_an_incomplete_repair_is_tried_again(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", "C:\\WINDOWS")
    home, state = tmp_path / "home", tmp_path / "state"
    path, _ = old_windows_codex(home, "C:\\s")
    lines = []

    def no_command():
        raise ConfigError("no stable raincli command")
    monkeypatch.setattr(hooks_install, "launcher_prefix", no_command)
    hooks_install.repair_on_version_change(str(state), "0.5.1", home=home, windows=True, log=lines.append)
    assert not (state / hooks_install.REPAIRED_FILE).exists() and "runs again" in lines[-1]
    path.write_text("{ not json")  # an unreadable config is incomplete too
    monkeypatch.setattr(hooks_install, "launcher_prefix", lambda: (list(PREFIX[0]), PREFIX[1]))
    hooks_install.repair_on_version_change(str(state), "0.5.1", home=home, windows=True, log=lines.append)
    assert not (state / hooks_install.REPAIRED_FILE).exists()
    path.unlink()
    old_windows_codex(tmp_path / "home2", "C:\\s")
    hooks_install.repair_on_version_change(str(state), "0.5.1", home=tmp_path / "home2", windows=True)
    assert json.loads((state / hooks_install.REPAIRED_FILE).read_text()) == {"version": "0.5.1"}
