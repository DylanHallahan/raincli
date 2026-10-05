"""Windows hardening of the Herdr adapter (Phase 2, phase2-herdr.md section 2)."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from raincli_agent.connector import herdr as herdr_mod
from raincli_agent.connector.config import load_connector_config
from raincli_agent.connector.herdr import (CMDLINE_CAP, FakeHerdr, HerdrCli, HerdrError, HerdrRejected,
                                           command_line_length, resolve_herdr_bin)
from raincli_agent.errors import ConfigError

from .conftest import send


def script(tmp_path, body, name="herdr"):
    """A fake herdr executable: ``body`` is Python run with sys.argv."""
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\nimport sys, json\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


# -- (1) UTF-8 output ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX script")
def test_output_is_decoded_as_utf8_whatever_the_locale(tmp_path, monkeypatch):
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("PYTHONIOENCODING", "latin-1")
    body = ('out = json.dumps({"result": {"agents": [{"name": "ünïcødé-✓", "agent": "claude", '
            '"agent_status": "idle", "cwd": "/w/日本"}]}}, ensure_ascii=False)\n'
            'sys.stdout.buffer.write(out.encode("utf-8"))')
    cli = HerdrCli(str(script(tmp_path, body)))
    [agent] = cli.list_agents()
    assert (agent["name"], agent["cwd"]) == ("ünïcødé-✓", "/w/日本")


@pytest.mark.skipif(os.name == "nt", reason="POSIX script")
def test_undecodable_output_is_replaced_not_raised(tmp_path):
    body = ('sys.stderr.buffer.write(b"\\xff\\xfe bad \\x80 bytes")\nsys.exit(1)')
    cli = HerdrCli(str(script(tmp_path, body)))
    with pytest.raises(HerdrError) as info:
        cli.get_agent("x")
    assert "�" in str(info.value)  # replaced, not a UnicodeDecodeError


def test_run_requests_utf8_and_catches_decode_errors(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")
    monkeypatch.setattr(herdr_mod.subprocess, "run", fake_run)
    with pytest.raises(HerdrError, match="could not be read"):
        HerdrCli("herdr", resolve=False).get_agent("x")
    assert (seen["encoding"], seen["errors"], seen["text"]) == ("utf-8", "replace", True)


# -- (2) Windows process flags -------------------------------------------------------------------

def test_windows_calls_have_no_console_and_their_own_process_group(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(herdr_mod.subprocess, "run",
                        lambda argv, **kw: seen.update(kw) or subprocess.CompletedProcess(argv, 0, "{}", ""))
    monkeypatch.setattr(herdr_mod.os, "name", "nt")
    cli = HerdrCli("herdr", resolve=False, own_session=True)
    cli.get_agent("x")
    assert seen["creationflags"] == 0x08000000 | 0x00000200
    assert seen["start_new_session"] is False and seen["shell"] is False


def test_posix_calls_keep_their_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(herdr_mod.subprocess, "run",
                        lambda argv, **kw: seen.update(kw) or subprocess.CompletedProcess(argv, 0, "{}", ""))
    if os.name == "nt":
        pytest.skip("POSIX behaviour")
    HerdrCli("herdr", resolve=False, own_session=True).get_agent("x")
    assert seen["creationflags"] == 0 and seen["start_new_session"] is True


# -- (3) herdr_session ----------------------------------------------------------------------------

def test_session_is_passed_on_every_call(monkeypatch):
    calls = []
    monkeypatch.setattr(herdr_mod.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(
                            argv, 0, json.dumps({"result": {"agents": []}}), ""))
    cli = HerdrCli("herdr", resolve=False, session="work-2")
    cli.get_agent("a")
    cli.list_agents()
    cli.prompt("a", "hello", 5)
    cli.notify("t", "b")
    assert all(argv[:3] == ["herdr", "--session", "work-2"] for argv in calls) and len(calls) == 4
    calls.clear()
    HerdrCli("herdr", resolve=False).get_agent("a")
    assert calls == [["herdr", "agent", "get", "a"]]


@pytest.mark.parametrize("bad", ["-rm", "--session", "a b", "a/b", "x" * 65, "ü", ""])
def test_session_names_are_validated(bad, tmp_path):
    if bad:
        with pytest.raises(ValueError):
            HerdrCli("herdr", resolve=False, session=bad)
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"herdr_agent": "inbox", "herdr_session": bad}))
    if bad:
        with pytest.raises(ConfigError, match="herdr_session"):
            load_connector_config(str(path))
    else:
        assert load_connector_config(str(path)).herdr_session == ""


def test_connector_config_carries_the_session(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"herdr_agent": "inbox", "herdr_session": "raincli.work_1"}))
    assert load_connector_config(str(path)).herdr_session == "raincli.work_1"


def test_machine_mode_discovery_uses_the_runtime_session(tmp_path, fake_api):
    from raincli_agent.config import write_config
    from raincli_agent.runtime import service
    write_config(str(tmp_path / "agent.json"), fake_api.url, fake_api.alice)
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"machine_config": "agent.json", "herdr_session": "work-2",
                                   "herdr_bin": "/opt/herdr/herdr"}))
    path, state, configs = service.load_runtime(runtime)
    supervisor = service.Supervisor(path, state, configs, service.file_sha256(path))
    [worker] = supervisor.workers
    assert (worker.herdr.session, worker.herdr.binary) == ("work-2", "/opt/herdr/herdr")
    for bad in ({"connectors": ["c.json"], "herdr_session": "x"}, {"machine_config": "agent.json", "herdr_session": "-x"}):
        runtime.write_text(json.dumps(bad))
        with pytest.raises(ConfigError):
            service.load_runtime(runtime)


# -- (4) herdr_bin resolution --------------------------------------------------------------------

def test_explicit_absolute_path_wins(tmp_path):
    local = tmp_path / "Local"
    alias = local.joinpath(*herdr_mod.WINDOWS_ALIAS)
    alias.parent.mkdir(parents=True)
    alias.write_text("")
    explicit = str(tmp_path / "mine" / "herdr.exe")
    assert resolve_herdr_bin(explicit, env={"LOCALAPPDATA": str(local)}, windows=True) == explicit


def test_windows_prefers_the_stable_alias_then_path(tmp_path):
    local = tmp_path / "Local"
    alias = local.joinpath(*herdr_mod.WINDOWS_ALIAS)
    on_path = str(tmp_path / ".herdr" / "packages" / "standalone" / "releases" / "0.9.3" / "herdr.exe")
    which = lambda name, path=None: on_path  # noqa: E731
    env = {"LOCALAPPDATA": str(local), "PATH": "x"}
    assert resolve_herdr_bin("herdr", env=env, which=which, windows=True) == on_path  # no alias yet
    alias.parent.mkdir(parents=True)
    alias.write_text("")
    for name in ("herdr", "herdr.exe", "", None):
        assert resolve_herdr_bin(name, env=env, which=which, windows=True) == str(alias)
    assert str(alias).endswith(os.path.join("Programs", "Herdr", "bin", "herdr.exe"))
    # Another command name is looked up on PATH, never swapped for the alias.
    assert resolve_herdr_bin("herdr-dev", env=env, which=lambda n, path=None: "/p/herdr-dev",
                             windows=True) == "/p/herdr-dev"
    assert resolve_herdr_bin("herdr", env=env, which=which, windows=False) == on_path  # POSIX: PATH


def test_unresolved_name_is_left_for_the_os(tmp_path):
    assert resolve_herdr_bin("herdr", env={"PATH": str(tmp_path)}, windows=False) == "herdr"


def test_binary_is_re_resolved_at_each_connector_start(tmp_path, fake_api, monkeypatch):
    from raincli_agent.config import write_config
    from raincli_agent.runtime import service
    write_config(str(tmp_path / "agent.json"), fake_api.url, fake_api.alice)
    (tmp_path / "c.json").write_text(json.dumps({"agent_config": "agent.json", "herdr_agent": "inbox",
                                                 "state_dir": "q", "prompt_timeout": 30}))
    (tmp_path / "runtime.json").write_text(json.dumps({"connectors": ["c.json"]}))
    path, state, configs = service.load_runtime(tmp_path / "runtime.json")
    state.mkdir(mode=0o700)
    found = iter(["/releases/0.9.2/herdr", "/releases/0.9.3/herdr"])
    monkeypatch.setattr(herdr_mod, "resolve_herdr_bin", lambda configured="herdr", **kw: next(found))
    worker = service.Supervisor(path, state, configs, service.file_sha256(path)).workers[0]
    assert worker.herdr.binary == "/releases/0.9.2/herdr"
    monkeypatch.setattr(service.subprocess, "Popen", lambda *a, **k: None)
    worker._spawn()
    assert worker.herdr.binary == "/releases/0.9.3/herdr"


# -- (5) the command-line bound ---------------------------------------------------------------------

def test_command_line_length_counts_quoting_and_utf16():
    assert command_line_length(["a", "b c"]) == len('a "b c"')
    assert command_line_length(["x", 'say "hi"\\']) == len(subprocess.list2cmdline(["x", 'say "hi"\\']))
    assert command_line_length(["😀"]) == 2  # one non-BMP character is two UTF-16 units


def test_oversize_prompt_is_rejected_before_anything_runs(monkeypatch):
    monkeypatch.setattr(herdr_mod.subprocess, "run", lambda *a, **k: pytest.fail("herdr was run"))
    cli = HerdrCli("herdr", resolve=False)
    text = '"' * (CMDLINE_CAP // 2)  # quoting doubles each quote: over the bound
    assert len(text) < CMDLINE_CAP and not cli.command_line_fits("inbox", text)
    with pytest.raises(HerdrRejected) as info:
        cli.prompt("inbox", text, 5)
    assert info.value.reason == "too_large_for_command_line"
    assert cli.command_line_fits("inbox", "x" * (CMDLINE_CAP - 100))


def test_connector_holds_an_oversize_message_without_submitting(fake_api, connector_env):
    msg = send(fake_api, fake_api.alice, "bob", "body")
    conn = connector_env.connector()
    connector_env.herdr.max_prompt_chars = 10  # every framed prompt is "too long"
    conn.run_once()
    conn.run_once()
    record = conn.queue.get(msg["id"])
    assert (record["state"], record["hold_reason"]) == ("held", "too_large_for_command_line")
    assert "submitting" not in [h["state"] for h in record["history"]]
    assert connector_env.herdr.prompts == []
    assert ("held", "too_large_for_command_line") in [(state, detail.split(":")[0])
                                                      for _, state, detail in fake_api.state.events] or \
        fake_api.state.messages[msg["id"]]["delivery_state"] == "held"
    connector_env.herdr.max_prompt_chars = None  # a later, shorter framing is delivered
    conn.run_once()
    assert connector_env.herdr.prompts


# -- (6) docs ----------------------------------------------------------------------------------------

def test_docs_recommend_pane_id_on_windows_and_document_the_session():
    root = Path(__file__).resolve().parents[3]
    protocol = (root / "docs" / "raincli-protocol.md").read_text()
    setup = (root / "SETUP.md").read_text()
    windows = (root / "docs" / "windows-client.md").read_text()
    for text in (protocol, setup):
        assert "herdr_session" in text
    assert "expect_pane_id" in windows and "herdr_session" in windows
    assert "too_large_for_command_line" in protocol


@pytest.mark.skipif(os.name == "nt", reason="POSIX script")
def test_agent_not_ready_is_a_pre_submission_refusal(tmp_path):
    """Herdr refuses before sending input when the pane is not running the named agent."""
    body = ('sys.stderr.write(json.dumps({"error": {"code": "agent_not_ready", '
            '"message": "agent x is not an active named agent"}}))\nsys.exit(1)')
    with pytest.raises(HerdrRejected) as info:
        HerdrCli(str(script(tmp_path, body))).prompt("x", "hello", 5)
    assert info.value.reason == "offline"
