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
        expected = (f'"{SHIM}" hook codex {event} --state-dir "{state}"')
        assert entry["commandWindows"] == entry["command"] == expected


@pytest.mark.parametrize("bad", ["C:\\100%\\state", "C:\\a^b", "C:\\a&b", "C:\\a|b", "C:\\a<b", "C:\\a>b",
                                 'C:\\a"b', "C:\\state\\"])
def test_cmd_special_characters_are_refused(tmp_path, monkeypatch, bad):
    with pytest.raises(ConfigError, match="cmd.exe"):
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
