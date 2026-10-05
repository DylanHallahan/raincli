"""Machine agent directory client (protocol 14.3, 14.7): hooks, discovery, hooks install."""
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest

from raincli_agent.connector.herdr import FakeHerdr, HerdrError
from raincli_agent.errors import ConfigError
from raincli_agent.runtime import discovery, hook, hooks_install, sessions

RAINCLI = Path(__file__).resolve().parents[2]
POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX modes and shell hook commands")


@pytest.fixture(autouse=True)
def no_agent_process(monkeypatch):
    """These tests run under a real agent; hook records name no process unless a test says so."""
    monkeypatch.setattr(hook, "agent_pid", lambda agent_type: None)


@pytest.fixture
def state(tmp_path):
    """A runtime state directory as the runtime prepares it: salt and sessions/."""
    directory = tmp_path / "runtime-state"
    directory.mkdir(mode=0o700)
    salt = sessions.ensure_salt(str(directory))
    sessions.sessions_dir(str(directory), create=True)
    return type("State", (), {"dir": str(directory), "salt": salt})


def payload(session_id="sess-1", cwd="/home/me/projects/secret-client/app", **extra):
    return {"session_id": session_id, "transcript_path": "/home/me/.claude/t/transcript.jsonl", "cwd": cwd,
            "prompt": "PRIVATE PROMPT TEXT", **extra}


def fire(state_dir, kind, event, data, name=None, now=None):
    out = io.BytesIO()
    result = hook.handle(kind, event, name, state_dir, io.BytesIO(json.dumps(data).encode()), out, now=now)
    return result, out.getvalue()


def records(state_dir):
    return {r["key"]: r for r in sessions.read_sessions(state_dir, drop=False)}


def run_hook_process(args, stdin=b"", env=None, timeout=10):
    environment = {**os.environ, "PYTHONPATH": str(RAINCLI), **(env or {})}
    return subprocess.run([sys.executable, "-m", "raincli_agent", "hook", *args], input=stdin,
                          capture_output=True, timeout=timeout, env=environment)


# -- keys, salt, names ------------------------------------------------------------

def test_salt_is_private_stable_and_key_is_truncated_hmac(state):
    path = Path(state.dir) / "machine-salt"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(state.salt) == 32 and sessions.ensure_salt(state.dir) == state.salt
    expected = hmac.new(state.salt, b"herdr:inbox", hashlib.sha256).hexdigest()[:32]
    assert sessions.agent_key(state.salt, "herdr:inbox") == expected
    assert sessions.KEY_RE.fullmatch(expected)


@pytest.mark.parametrize("raw, expected", [
    ("app", "app"), ("  spaced   out  ", "spaced out"), ("", "claude"), ("\x1b[31mred\x07", "_[31mred_"),
    ("x" * 90, "x" * 64), ("line\u2028break", "line_break"), ("rtl\u202eoverride", "rtl_override"), (None, "claude"),
])
def test_names_are_normalized_before_sending(raw, expected):
    assert sessions.normalize_name(raw, "claude") == expected


# -- the hook ---------------------------------------------------------------------

def test_hook_status_lifecycle_and_no_paths_or_prompts_stored(state):
    assert fire(state.dir, "claude", "SessionStart", payload(source="startup"))[0] == "recorded"
    [record] = records(state.dir).values()
    assert (record["name"], record["type"], record["status"]) == ("app", "claude", "idle")
    assert record["key"] == sessions.agent_key(state.salt, "claude:sess-1")
    stored = (Path(state.dir) / "sessions" / (record["key"] + ".json")).read_text()
    for private in ("/home/me", "secret-client", "PRIVATE PROMPT", "transcript", "sess-1"):
        assert private not in stored
    fire(state.dir, "claude", "UserPromptSubmit", payload())
    assert records(state.dir)[record["key"]]["status"] == "working"
    fire(state.dir, "claude", "Notification", payload(notification_type="permission_prompt", message="Needs you"))
    assert records(state.dir)[record["key"]]["status"] == "blocked"
    fire(state.dir, "claude", "Notification", payload(notification_type="auth_success", message="x"))
    assert records(state.dir)[record["key"]]["status"] == "blocked"  # unrelated notifications change nothing
    fire(state.dir, "claude", "Stop", payload())
    assert records(state.dir)[record["key"]]["status"] == "idle"
    fire(state.dir, "claude", "SessionEnd", payload(reason="logout"))
    assert records(state.dir) == {}


def test_codex_permission_request_blocks_and_codex_never_claims(state):
    fire(state.dir, "codex", "SessionStart", payload(session_id="c1", source="startup"))
    fire(state.dir, "codex", "PermissionRequest", payload(session_id="c1", tool_name="shell"))
    [record] = records(state.dir).values()
    assert (record["type"], record["status"]) == ("codex", "blocked")
    assert record["key"] == sessions.agent_key(state.salt, "codex:c1")


def test_name_defaults_and_overrides(state, monkeypatch):
    fire(state.dir, "claude", "SessionStart", payload(session_id="a", cwd="/w/project-one/"))
    fire(state.dir, "claude", "SessionStart", payload(session_id="b"), name="inbox")
    monkeypatch.setenv("RAINCLI_AGENT_NAME", "from-env")
    fire(state.dir, "claude", "SessionStart", payload(session_id="c"))
    fire(state.dir, "claude", "SessionStart", payload(session_id="d"), name="flag-wins")
    monkeypatch.delenv("RAINCLI_AGENT_NAME")
    fire(state.dir, "claude", "SessionStart", payload(session_id="e", cwd="C:\\Users\\me\\winproj"))
    fire(state.dir, "claude", "SessionStart", {"session_id": "f"})
    names = sorted(r["name"] for r in records(state.dir).values())
    assert names == ["claude", "flag-wins", "from-env", "inbox", "project-one", "winproj"]


def test_hook_without_salt_does_nothing(tmp_path):
    (tmp_path / "s").mkdir()
    assert fire(str(tmp_path / "s"), "claude", "SessionStart", payload()) == ("no_salt", b"")
    assert not (tmp_path / "s" / "sessions").exists()


@pytest.mark.parametrize("args, stdin", [
    (["claude", "SessionStart", "--state-dir", "{state}"], b"not json"),
    (["claude", "SessionStart", "--state-dir", "{state}"], b"[1, 2]"),
    (["claude", "SessionStart", "--state-dir", "{state}"], b"x" * (1024 * 1024 + 10)),
    (["claude", "SessionStart", "--state-dir", "{state}"], json.dumps({"session_id": 5}).encode()),
    (["claude", "NoSuchEvent", "--state-dir", "{state}"], json.dumps(payload()).encode()),
    (["gemini", "SessionStart", "--state-dir", "{state}"], json.dumps(payload()).encode()),
    (["claude", "SessionStart", "--state-dir", "/nonexistent/dir"], json.dumps(payload()).encode()),
    (["claude"], b"{}"),
    (["--bogus-flag", "claude", "SessionStart", "--state-dir"], b"{}"),
    ([], b""),
], ids=["not-json", "not-object", "stdin-too-large", "bad-session-id", "unknown-event", "unknown-type",
        "missing-state-dir", "missing-event", "bogus-flag", "no-args"])
def test_hook_failures_never_break_the_agent(state, args, stdin):
    """Exit 0, nothing on stderr (Claude Code treats exit 2 as blocking), fast."""
    args = [a.replace("{state}", state.dir) for a in args]
    started = time.monotonic()
    result = run_hook_process(args, stdin)
    assert (result.returncode, result.stderr, result.stdout) == (0, b"", b"")
    assert time.monotonic() - started < 5


@POSIX
def test_hook_errors_are_logged_as_codes_only(state):
    Path(state.dir, "sessions").chmod(0o755)  # not private: refused
    result = run_hook_process(["claude", "UserPromptSubmit", "--state-dir", state.dir],
                              json.dumps(payload()).encode())
    assert (result.returncode, result.stderr) == (0, b"")
    log = Path(state.dir, "hook.log").read_text()
    assert "error:ConfigError" in log
    for private in ("PRIVATE PROMPT", "/home/me", "transcript", "secret-client"):
        assert private not in log


@POSIX
def test_hook_deadline_exits_zero_even_if_stdin_never_closes(state):
    process = subprocess.Popen([sys.executable, "-m", "raincli_agent", "hook", "claude", "SessionStart",
                                "--state-dir", state.dir], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env={**os.environ, "PYTHONPATH": str(RAINCLI)})
    try:
        assert process.wait(timeout=10) == 0  # the 2 s deadline, not the agent's timeout
    finally:
        process.stdin.close()
    assert process.stderr.read() == b""


def test_hook_help_is_documented():
    result = subprocess.run([sys.executable, "-m", "raincli_agent", "hook", "--help"], capture_output=True,
                            text=True, env={**os.environ, "PYTHONPATH": str(RAINCLI)}, timeout=20)
    assert result.returncode == 0 and "always exits 0" in result.stdout


# -- discovery ---------------------------------------------------------------------

def test_herdr_discovery_maps_status_marks_inbox_and_keeps_unknown(state):
    herdr = FakeHerdr()
    herdr.add("inbox", status="idle", cwd="/home/me/private/repo")
    herdr.add("worker", status="working", kind="codex")
    herdr.add("finished", status="done")
    herdr.add("stuck", status="blocked", kind="gemini")
    herdr.add("mystery", status="unknown", kind="pi")
    herdr.add("odd", status="thinking", kind="opencode")
    found, ok = discovery.herdr_entries(state.salt, herdr, ("herdr", "inbox"))
    assert ok
    by_name = {a["name"]: a for a in found}
    assert {n: (a["type"], a["status"]) for n, a in by_name.items()} == {
        "inbox": ("claude", "idle"), "worker": ("codex", "working"), "finished": ("claude", "idle"),
        "stuck": ("gemini", "blocked"), "mystery": ("other", "unknown"), "odd": ("opencode", "unknown")}
    assert (by_name["inbox"]["role"], by_name["inbox"]["reachability"]) == ("inbox", "instant")
    assert all(a["role"] is None and a["reachability"] is None for n, a in by_name.items() if n != "inbox")
    assert by_name["inbox"]["key"] == sessions.agent_key(state.salt, "herdr:inbox")
    assert "/home/me" not in json.dumps(found)


def test_unnamed_herdr_agent_uses_folder_basename_and_a_stable_key(state):
    class Unnamed(FakeHerdr):
        def list_agents(self):
            return [{"name": None, "kind": "claude", "status": "idle", "terminal_id": "term-7",
                     "cwd": "/home/me/work/billing"}]
    [agent], _ = discovery.herdr_entries(state.salt, Unnamed(), None)
    assert agent["name"] == "billing" and agent["key"] == sessions.agent_key(state.salt, "herdr:#term-7")


def test_missing_or_unreadable_herdr_inbox_is_still_listed(state):
    herdr = FakeHerdr()
    [inbox], ok = discovery.herdr_entries(state.salt, herdr, ("herdr", "inbox"))
    assert ok and (inbox["status"], inbox["role"]) == ("offline", "inbox")
    herdr.list_error = HerdrError("no herdr")
    [inbox], ok = discovery.herdr_entries(state.salt, herdr, ("herdr", "inbox"))
    assert not ok and inbox["status"] == "unknown"


def test_hook_sessions_are_listed_and_single_live_one_is_next_turn_inbox(state):
    fire(state.dir, "claude", "SessionStart", payload(session_id="a"), name="inbox")
    fire(state.dir, "codex", "SessionStart", payload(session_id="b"))
    found, _ = discovery.hook_entries(state.salt, state.dir, ("hook", "claude", "inbox"))
    by_name = {a["name"]: a for a in found}
    assert (by_name["inbox"]["role"], by_name["inbox"]["reachability"], by_name["inbox"]["source"]) == (
        "inbox", "next-turn", "hook")
    assert by_name["app"]["role"] is None
    # A second live session with the inbox name: ambiguous, so none is marked.
    fire(state.dir, "claude", "SessionStart", payload(session_id="c"), name="inbox")
    found, _ = discovery.hook_entries(state.salt, state.dir, ("hook", "claude", "inbox"))
    assert not [a for a in found if a["role"]]


def test_stale_records_go_offline_then_are_dropped(state):
    now = time.time()
    fire(state.dir, "claude", "SessionStart", payload(session_id="a"), now=now - 700)
    fire(state.dir, "claude", "SessionStart", payload(session_id="b"), now=now - 4000)
    found = sessions.read_sessions(state.dir, now)
    assert [r["status"] for r in found] == ["offline"]
    assert len(list(Path(state.dir, "sessions").glob("*.json"))) == 1  # the hour-old record was dropped


def test_scan_reports_unclaimed_agents_by_type_and_basename_only(state):
    processes = {
        10: ("systemd", 1, None),
        20: ("bash", 10, None),
        21: ("claude.exe", 20, None),     # plain terminal Claude Code: scanned
        22: ("claude.exe", 21, None),     # its own child: part of 21
        30: ("herdr", 10, None),
        31: ("bash", 30, None),
        32: ("codex", 31, None),          # inside a Herdr pane: Herdr reports it
        40: ("bash", 10, None),
        41: ("claude", 40, None),         # a hook session claims this pid
        50: ("python3", 10, None),        # not an agent
        60: ("opencode", 10, None),
    }
    cwds = {21: "alpha", 60: ""}
    found = discovery.linux_scan(state.salt, {41}, herdr_ok=True, processes=processes,
                                 cwd_name=lambda pid: cwds.get(pid, "nope"))
    assert [(a["name"], a["type"], a["status"], a["source"]) for a in found] == [
        ("alpha", "claude", "unknown", "scan"), ("opencode", "opencode", "unknown", "scan")]
    assert found[0]["key"] == sessions.agent_key(state.salt, "scan:claude:21")
    # Herdr unreadable: the pane's agent is no longer covered, so it is scanned.
    found = discovery.linux_scan(state.salt, {41}, herdr_ok=False, processes=processes, cwd_name=lambda pid: "x")
    assert sorted(a["type"] for a in found) == ["claude", "codex", "opencode"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc")
def test_real_proc_scan_reads_only_this_user(state):
    processes = discovery.linux_processes()
    assert os.getpid() in processes
    for pid in processes:
        assert os.stat(f"/proc/{pid}").st_uid == os.getuid()


def test_windows_scan_is_type_only(monkeypatch):
    monkeypatch.setenv("USERNAME", "me")
    csv_out = '"claude.exe","4242","Console","1","100 K"\n"notepad.exe","7","Console","1","1 K"\n' \
              '"codex.exe","99","Console","1","1 K"\n'
    run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, csv_out, "")
    found = discovery.windows_scan(b"s" * 32, {99}, run=run)
    assert [(a["name"], a["type"], a["status"], a["source"]) for a in found] == [
        ("claude", "claude", "unknown", "scan")]
    monkeypatch.delenv("USERNAME")
    assert discovery.windows_scan(b"s" * 32, set(), run=run) == []  # never other users' processes


def test_normalize_dedupes_keys_keeps_one_inbox_first_and_caps_at_100():
    def item(i, role=None):
        return {"key": f"{i:032x}", "name": f"a{i}", "type": "claude", "status": "idle", "role": role,
                "reachability": "instant" if role else None, "source": "herdr"}
    entries = [item(i) for i in range(150)] + [item(3)] + [item(500, "inbox"), item(501, "inbox")]
    out = discovery.normalize(entries)
    assert len(out) == 100 and out[0]["key"] == f"{500:032x}" and out[0]["role"] == "inbox"
    assert len({a["key"] for a in out}) == 100
    assert sum(1 for a in out if a["role"]) == 1


def test_discover_combines_sources_and_validates_against_the_contract(state):
    from .fake_server import presence_problem
    herdr = FakeHerdr()
    herdr.add("inbox")
    herdr.add("helper", status="working")
    fire(state.dir, "claude", "SessionStart", payload(session_id="h1", cwd="/x/\x1bevil\u202e"))
    found = discovery.discover(state.dir, state.salt, herdr, ("herdr", "inbox"), include_scan=False)
    assert found[0]["name"] == "inbox" and found[0]["role"] == "inbox"
    assert presence_problem({"status": "ready", "agents": found}) is None


# -- hooks install --------------------------------------------------------------------

PREFIX = ([sys.executable, "-m", "raincli_agent"], "test interpreter")


def settings(home):
    return json.loads((home / ".claude/settings.json").read_text())


@POSIX
def test_claude_hooks_install_is_idempotent_backed_up_and_removable(tmp_path, state):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    user_hook = {"type": "command", "command": "echo mine"}
    original = {"theme": "dark", "hooks": {"Stop": [{"hooks": [user_hook]}],
                                           "PreToolUse": [{"matcher": "Bash", "hooks": [user_hook]}]}}
    path = home / ".claude/settings.json"
    path.write_text(json.dumps(original))
    path.chmod(0o644)
    result = hooks_install.install("claude", state.dir, home=home, prefix=PREFIX)
    assert result["status"] == "installed" and stat.S_IMODE(os.stat(result["backup"]).st_mode) == 0o600
    assert json.loads(Path(result["backup"]).read_text()) == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o644  # the file's mode is kept
    data = settings(home)
    assert data["theme"] == "dark" and data["hooks"]["PreToolUse"] == original["hooks"]["PreToolUse"]
    assert set(data["hooks"]) == {"Stop", "PreToolUse", "SessionStart", "UserPromptSubmit", "Notification", "SessionEnd"}
    ours = [h for groups in data["hooks"].values() for g in groups for h in g["hooks"] if h.get("statusMessage") == "raincli"]
    assert len(ours) == 5
    for h in ours:
        assert h["timeout"] <= 5 and "--state-dir" in h["command"] and state.dir in h["command"]
        assert h["command"].endswith("2>/dev/null || true")
    assert data["hooks"]["Stop"][0] == {"hooks": [user_hook]}

    assert hooks_install.install("claude", state.dir, home=home, prefix=PREFIX)["status"] == "unchanged"
    assert settings(home) == data
    assert hooks_install.install("claude", state.dir, home=home, remove=True)["status"] == "removed"
    assert settings(home) == original


def test_hooks_install_refuses_unparseable_config(tmp_path, state):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude/settings.json").write_text("{ // comment\n}")
    with pytest.raises(ConfigError, match="does not parse"):
        hooks_install.install("claude", state.dir, home=home, prefix=PREFIX)
    assert (home / ".claude/settings.json").read_text() == "{ // comment\n}"
    assert not list((home / ".claude").glob("*backup*"))


@POSIX
def test_codex_hooks_install_only_when_supported(tmp_path, state):
    home = tmp_path / "home"
    result = hooks_install.install("codex", state.dir, home=home, prefix=PREFIX,
                                   probe=lambda: (False, "codex-cli 0.1.0: no hooks feature"))
    assert result["status"] == "unsupported" and "scan-only" in result["note"]
    assert not (home / ".codex/hooks.json").exists()
    result = hooks_install.install("codex", state.dir, home=home, prefix=PREFIX,
                                   probe=lambda: (True, "codex-cli 0.159.2: feature hooks stable true"))
    assert result["status"] == "installed" and "0.159.2" in result["codex_hooks"]
    data = json.loads((home / ".codex/hooks.json").read_text())
    assert set(data["hooks"]) == {"SessionStart", "UserPromptSubmit", "Stop", "PermissionRequest", "SessionEnd"}
    assert stat.S_IMODE((home / ".codex/hooks.json").stat().st_mode) == 0o600


def test_codex_probe_parses_features_list(monkeypatch):
    monkeypatch.setattr(hooks_install.shutil, "which", lambda name: "/bin/codex")
    def run(argv, **kw):
        out = "codex-cli 0.159.2\n" if argv[-1] == "--version" else "apps  stable  true\nhooks   stable   true\n"
        return subprocess.CompletedProcess(argv, 0, out, "")
    assert hooks_install.codex_support(run) == (True, "codex-cli 0.159.2: feature hooks stable true")
    off = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "hooks  experimental  false\n", "")
    assert hooks_install.codex_support(off)[0] is False


@POSIX
def test_installed_command_runs_and_never_fails_even_without_interpreter(tmp_path, state):
    home = tmp_path / "home"
    hooks_install.install("claude", state.dir, home=home, prefix=PREFIX)
    [group] = settings(home)["hooks"]["SessionStart"]
    command = group["hooks"][0]["command"]
    env = {**os.environ, "PYTHONPATH": str(RAINCLI)}
    result = subprocess.run(["/bin/sh", "-c", command], input=json.dumps(payload()).encode(),
                            capture_output=True, env=env, timeout=20)
    assert (result.returncode, result.stderr) == (0, b"")
    assert [r["status"] for r in records(state.dir).values()] == ["idle"]
    missing = hooks_install.handler("claude", "SessionStart", ["/nonexistent/python3", "/x/launch.py"], state.dir)
    result = subprocess.run(["/bin/sh", "-c", missing["command"]], input=b"{}", capture_output=True, timeout=20)
    assert (result.returncode, result.stderr, result.stdout) == (0, b"", b"")


def test_windows_claude_hooks_use_exec_form(monkeypatch, state):
    monkeypatch.setattr(hooks_install.os, "name", "nt")
    entry = hooks_install.handler("claude", "Stop", ["C:\\Py\\python.exe", "C:\\u\\launch.py"], "C:\\state dir")
    assert entry["command"] == "C:\\Py\\python.exe"
    assert entry["args"] == ["C:\\u\\launch.py", "hook", "claude", "Stop", "--state-dir", "C:\\state dir"]
    # Codex on Windows (Phase 2): a quoted cmd.exe command line, also as commandWindows.
    codex = hooks_install.handler("codex", "Stop", ["C:\\Py\\python.exe", "C:\\u\\launch.py"], "C:\\state dir")
    assert codex["commandWindows"] == codex["command"] == (
        '"C:\\Py\\python.exe" "C:\\u\\launch.py" hook codex Stop --state-dir "C:\\state dir"')


def test_hook_command_prefers_the_stable_launcher(tmp_path, monkeypatch):
    from raincli_agent.runtime import updates
    monkeypatch.setattr(updates, "default_root", lambda: tmp_path / "client")
    monkeypatch.setattr(hooks_install.shutil, "which", lambda name: None)
    with pytest.raises(ConfigError, match="no stable raincli command"):
        hooks_install.launcher_prefix()  # never a versioned interpreter (14.7 M8)
    (tmp_path / "client").mkdir()
    (tmp_path / "client/launch.py").write_text("")
    prefix, how = hooks_install.launcher_prefix()
    assert prefix[1] == str(tmp_path / "client/launch.py") and how == "managed launcher"
    assert "versions" not in prefix[0]
