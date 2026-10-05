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


def touch(path, executable=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    if executable:
        path.chmod(0o755)
    return path


def test_windows_prefers_the_stable_alias_then_path(tmp_path):
    local = tmp_path / "Local"
    release = touch(tmp_path / "releases" / "0.9.3" / "herdr.exe")
    env = {"LOCALAPPDATA": str(local), "PATH": str(release.parent)}
    assert resolve_herdr_bin("herdr", env=env, windows=True) == str(release)  # no alias yet
    alias = touch(local.joinpath(*herdr_mod.WINDOWS_ALIAS))
    for name in ("herdr", "herdr.exe", "", None):
        assert resolve_herdr_bin(name, env=env, windows=True) == str(alias)
    assert str(alias).endswith(os.path.join("Programs", "Herdr", "bin", "herdr.exe"))
    dev = touch(tmp_path / "dev" / "herdr-dev.exe")
    env["PATH"] = str(dev.parent)
    assert resolve_herdr_bin("herdr-dev", env=env, windows=True) == str(dev)  # another name: PATH only


def test_windows_never_resolves_a_batch_file(tmp_path):
    """H1: herdr.cmd earlier on PATH than herdr.exe: the .exe, never the batch file."""
    early = touch(tmp_path / "early" / "herdr.cmd")
    touch(tmp_path / "early" / "herdr.bat")
    late = touch(tmp_path / "late" / "herdr.exe")
    env = {"PATH": os.pathsep.join([str(early.parent), str(late.parent)]), "PATHEXT": ".COM;.EXE;.BAT;.CMD"}
    assert resolve_herdr_bin("herdr", env=env, windows=True) == str(late)
    env["PATH"] = str(early.parent)
    with pytest.raises(HerdrError, match="not found"):
        resolve_herdr_bin("herdr", env=env, windows=True)
    for explicit in (str(early), "herdr.cmd", "C:/tools/herdr.BAT", str(tmp_path / "herdr")):
        with pytest.raises(HerdrError):
            resolve_herdr_bin(explicit, env=env, windows=True)


@pytest.mark.parametrize("name", ["herdr.cmd", "C:\\tools\\herdr.bat", "/opt/x/HERDR.CMD"])
def test_explicit_batch_herdr_bin_is_a_config_error(tmp_path, name):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"herdr_agent": "inbox", "herdr_bin": name}))
    with pytest.raises(ConfigError, match="not a .bat or .cmd"):
        load_connector_config(str(path))


def test_path_walk_skips_relative_entries_and_the_current_directory(tmp_path, monkeypatch):
    """H2: a herdr in the current directory (a cloned repository) is never run."""
    planted = touch(tmp_path / "repo" / "herdr")
    touch(tmp_path / "repo" / "herdr.exe")
    monkeypatch.chdir(tmp_path / "repo")
    for windows in (False, True):
        env = {"PATH": os.pathsep.join(["", ".", "repo", "./bin"])}
        with pytest.raises(HerdrError, match="not found"):
            resolve_herdr_bin("herdr", env=env, windows=windows)
    good = touch(tmp_path / "bin" / "herdr")
    resolved = resolve_herdr_bin("herdr", env={"PATH": os.pathsep.join([".", "", str(good.parent)])}, windows=False)
    assert resolved == str(good) and os.path.isabs(resolved) and resolved != str(planted)
    with pytest.raises(HerdrError):
        resolve_herdr_bin("bin/herdr", env={"PATH": str(good.parent)}, windows=False)  # relative path


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_posix_requires_an_executable_file(tmp_path):
    touch(tmp_path / "a" / "herdr", executable=False)
    good = touch(tmp_path / "b" / "herdr")
    env = {"PATH": os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")])}
    assert resolve_herdr_bin("herdr", env=env, windows=False) == str(good)


def test_missing_herdr_holds_offline_and_is_found_later(tmp_path, fake_api, connector_env, monkeypatch):
    """No bare-name fallback: until Herdr is found, the message is held offline."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    cli = HerdrCli("herdr")
    assert cli.binary is None
    with pytest.raises(HerdrError, match="not found"):
        cli.get_agent("x")
    msg = send(fake_api, fake_api.alice, "bob")
    conn = connector_env.connector()
    conn.herdr = cli
    conn.run_once()
    record = conn.queue.get(msg["id"])
    assert (record["state"], record["hold_reason"]) == ("held", "offline")
    binary = touch(tmp_path / "found" / "herdr")
    binary.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(0)\n")
    monkeypatch.setenv("PATH", str(binary.parent))
    assert cli.resolve() is None and cli.binary == str(binary)


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
@pytest.mark.parametrize("version,offline", [("0.9.3", True), ("0.10.0", True), ("0.9.2", False), ("", False)])
def test_agent_not_ready_is_offline_only_from_herdr_093(tmp_path, version, offline):
    """H4: a pre-send refusal in Herdr 0.9.3; older or unknown versions stay uncertain."""
    body = (f'if sys.argv[1:] == ["--version"]:\n    print("herdr {version}")\n    sys.exit(0)\n'
            'sys.stderr.write(json.dumps({"error": {"code": "agent_not_ready", '
            '"message": "agent x is not an active named agent"}}))\nsys.exit(1)')
    cli = HerdrCli(str(script(tmp_path, body)))
    if offline:
        with pytest.raises(HerdrRejected) as info:
            cli.prompt("x", "hello", 5)
        assert (info.value.reason, info.value.code) == ("offline", "agent_not_ready")
    else:
        with pytest.raises(HerdrError) as info:
            cli.prompt("x", "hello", 5)
        assert not isinstance(info.value, HerdrRejected)  # -> submission_uncertain


@pytest.mark.skipif(os.name == "nt", reason="POSIX script")
def test_version_is_read_once_per_connector_start(tmp_path):
    count = tmp_path / "count"
    body = (f'open({str(count)!r}, "a").write("v")\nprint("herdr 0.9.3")')
    cli = HerdrCli(str(script(tmp_path, body)))
    assert cli.version() == (0, 9, 3) and cli.version() == (0, 9, 3)
    assert count.read_text() == "v"
    cli.resolve()  # a new connector start reads it again
    cli.version()
    assert count.read_text() == "vv"


def test_repeated_agent_not_ready_backs_off(fake_api, connector_env):
    msg = send(fake_api, fake_api.alice, "bob")
    conn = connector_env.connector()
    now = [1000.0]
    conn._clock = lambda: now[0]
    calls = []

    def prompt(name, text, timeout):
        calls.append(now[0])
        raise HerdrRejected("offline", "agent_not_ready", code="agent_not_ready")
    connector_env.herdr.prompt = prompt
    for _ in range(3):
        conn.run_once()
    assert len(calls) == 3  # three tries, then a backoff
    conn.run_once()
    assert len(calls) == 3
    record = conn.queue.get(msg["id"])
    assert (record["state"], record["hold_reason"]) == ("held", "offline") and "backoff" in record["hold_detail"]
    now[0] += 31
    conn.run_once()
    assert len(calls) == 4
    connector_env.herdr.prompt = lambda name, text, timeout: calls.append(now[0])
    now[0] += 61
    conn.run_once()
    assert conn._not_ready_count == 0 and conn.queue.get(msg["id"])["state"] == "submitted"


def test_oversize_escalation_is_held_before_submitting(fake_api, connector_env):
    """H3: the same command-line check before an escalation is submitted."""
    msg = send(fake_api, fake_api.alice, "bob")
    path = connector_env.make(mode="inbox", escalation={"herdr_agent": "main-session", "notify": False})
    connector_env.herdr.add("main-session")
    conn = connector_env.connector(path=path)
    conn.run_once()
    from raincli_agent.connector import ops
    assert conn.queue.get(msg["id"])["state"] == "submitted"
    esc_id = ops.escalate(conn.queue, conn.config, msg["id"], "please look")[0]["id"]
    connector_env.herdr.max_prompt_chars = 10
    before = len(connector_env.herdr.prompts)
    conn.run_once()
    conn.run_once()
    esc = conn.queue.load_escalation(esc_id)
    assert esc["hold_reason"] == "too_large_for_command_line" and esc.get("attempts", 0) == 0
    assert len(connector_env.herdr.prompts) == before
