"""Agent discovery and connecting agents (protocol §16.19, v0.5.1)."""
import json
import subprocess

import pytest

from raincli_agent.errors import ConfigError
from raincli_agent.runtime import discovery, hooks_install, procinfo, sessions

SALT = b"s" * 32
PREFIX = (["/opt/raincli/bin/raincli"], "test")

# -- item 2 (added): classification by full image path --------------------------------------------

CLAUDE_CODE = [
    r"C:\Users\First Last\.local\bin\claude.exe",  # native installer launcher (code.claude.com/docs/en/setup)
    r"C:\Users\d\.local\share\claude\versions\2.1.291",
    r"C:\Users\d\.local\share\claude\versions\2.1.291\claude.exe",
    r"C:\Users\d\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe",  # npm, linked native
    r"C:\Users\d\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\node_modules"
    r"\@anthropic-ai\claude-code-win32-x64\claude.exe",
]
CODEX = [
    r"C:\Users\d\AppData\Roaming\npm\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64"
    r"\vendor\x86_64-pc-windows-msvc\bin\codex.exe",  # npm (codex-cli/bin/codex.js spawns it)
    r"C:\Users\d\AppData\Local\Microsoft\WinGet\Packages\OpenAI.Codex_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\codex-x86_64-pc-windows-msvc.exe",  # winget portable
    r"C:\Users\d\AppData\Local\Microsoft\WinGet\Links\codex.exe",
    r"D:\Tools\codex-aarch64-pc-windows-msvc.exe",  # the standalone release asset
]
NEVER = [
    r"C:\Users\d\AppData\Local\AnthropicClaude\Claude.exe",  # the Claude desktop app (Electron)
    r"C:\Users\d\AppData\Local\AnthropicClaude\app-0.14.10\Claude.exe",
    r"C:\Users\d\AppData\Local\AnthropicClaude\app-0.14.10\resources\app.asar.unpacked\claude.exe",
    r"C:\Program Files\WindowsApps\Claude_0.14.10.0_x64__pzs8sxrjxfjjc\app\Claude.exe",  # Store build
    r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0.0.0_x64__abc\app\Codex.exe",
    r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0.0.0_x64__abc\app\resources\codex.exe",
    r"D:\Tools\codex-command-runner.exe",  # Codex's helpers
    r"D:\Tools\codex-windows-sandbox-setup.exe",
    r"C:\Program Files\nodejs\node.exe",  # npm-run agents need hooks
    r"D:\somewhere\claude.exe",  # a name alone never counts
    r"D:\somewhere\codex.exe",
    "",
]


@pytest.mark.parametrize("path", CLAUDE_CODE)
def test_claude_code_layouts(path):
    assert procinfo.windows_kind(path) == "claude"
    assert procinfo.windows_kind(path.upper()) == "claude"  # case-insensitive


@pytest.mark.parametrize("path", CODEX)
def test_codex_layouts(path):
    assert procinfo.windows_kind(path) == "codex"


@pytest.mark.parametrize("path", NEVER)
def test_never_classified(path):
    assert procinfo.windows_kind(path) is None


def test_codex_helpers_pass_through_liveness():
    for helper in ("codex-command-runner", "codex-windows-sandbox-setup.exe"):
        assert procinfo.kind_of(helper) is None and procinfo.passes_through(helper)
    assert procinfo.kind_of("codex-x86_64-pc-windows-msvc.exe") == "codex"  # K2 unchanged


# -- item 2: the Toolhelp scan with the token-SID check ------------------------------------------------

def scan(table, paths, mine=None, claimed=()):
    mine = set(table) if mine is None else mine
    return discovery.windows_scan(SALT, set(claimed), table=table, same_user=lambda pid: pid in mine,
                                  image=lambda pid: paths.get(pid))


def test_scan_lists_this_users_real_installs_only():
    table = {1: ("explorer.exe", 0), 10: ("claude.exe", 1), 11: ("claude.exe", 1), 20: ("node.exe", 1),
             21: ("codex.exe", 20), 22: ("codex-command-runner.exe", 21), 30: ("claude.exe", 1),
             40: ("codex-x86_64-pc-windows-msvc.exe", 1)}
    paths = {10: CLAUDE_CODE[0], 11: NEVER[1], 21: CODEX[0], 22: NEVER[6], 30: CLAUDE_CODE[0], 40: CODEX[3]}
    found = scan(table, paths, mine={10, 11, 20, 21, 22, 40})  # 30 is another user's
    assert sorted((a["type"], a["source"], a["status"]) for a in found) == [
        ("claude", "scan", "unknown"), ("codex", "scan", "unknown"), ("codex", "scan", "unknown")]
    assert all(a["name"] in ("claude", "codex") for a in found)  # type-only: no paths, no pids


def test_scan_counts_a_child_of_the_same_kind_once_and_skips_claimed():
    table = {5: ("claude.exe", 1), 6: ("claude.exe", 5), 7: ("claude.exe", 1)}
    paths = {5: CLAUDE_CODE[0], 6: CLAUDE_CODE[2], 7: CLAUDE_CODE[0]}
    assert len(scan(table, paths)) == 2
    assert len(scan(table, paths, claimed={7})) == 1


def test_unreadable_image_path_is_not_classified():
    assert scan({5: ("claude.exe", 1)}, {}) == []


def test_without_the_table_tasklist_is_the_fallback(monkeypatch):
    def broken():
        raise OSError("snapshot failed")
    monkeypatch.setattr(procinfo, "process_table", broken)
    monkeypatch.setenv("USERNAME", "me")
    out = b'"codex-x86_64-pc-windows-msvc.exe","8","Console","1","1 K"\n"\xff\xfe\x81 bad","x"\n"claude.exe","9","C","1","1"'
    run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, out, b"")
    found = discovery.windows_scan(SALT, set(), run=run)
    assert [a["type"] for a in found] == ["codex"]


def test_tasklist_contents_never_raise(monkeypatch):
    monkeypatch.setenv("USERNAME", "me")
    for out in (b"\x00\x00\"unterminated", "text, not bytes", None, b'"a","1"\r\n' * 3):
        run = lambda argv, _o=out, **kw: subprocess.CompletedProcess(argv, 0, _o, b"")
        assert discovery.tasklist_scan(SALT, set(), run=run) == []


# -- item 1: one failing source never hides the others ---------------------------------------------------

class Herdr:
    def __init__(self, fail=False):
        self.fail = fail

    def list_agents(self):
        if self.fail:
            raise RuntimeError("herdr broke")
        return []


def test_a_failing_source_is_logged_once_and_the_others_are_reported(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    sessions.ensure_salt(str(state))
    sessions.sessions_dir(str(state), create=True)
    monkeypatch.setattr(discovery, "_LOGGED", set())
    lines = []
    monkeypatch.setattr(discovery, "hook_entries", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad record")))
    monkeypatch.setattr(discovery, "herdr_entries", lambda *a, **k: (
        [discovery.entry(SALT, "herdr:x", "reviewer", "claude", "idle", "herdr")], True))
    monkeypatch.setattr(discovery, "scan", lambda *a, **k: [discovery.entry(SALT, "scan:codex:1", "codex", "codex",
                                                                         "unknown", "scan")])
    for _ in range(3):
        found = discovery.discover(str(state), SALT, Herdr(), None, log=lines.append)
        assert sorted(a["name"] for a in found) == ["codex", "reviewer"]
    assert lines == ["agent discovery: the hooks source failed (ValueError); the other sources are still reported"]


def test_every_source_failing_reports_an_empty_directory_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "_LOGGED", set())
    boom = lambda *a, **k: (_ for _ in ()).throw(OSError("x"))
    monkeypatch.setattr(discovery, "hook_entries", boom)
    monkeypatch.setattr(discovery, "herdr_entries", boom)
    monkeypatch.setattr(discovery, "scan", boom)
    monkeypatch.setattr(discovery, "linux_processes", boom)
    lines = []
    assert discovery.discover(str(tmp_path), SALT, Herdr(), None, log=lines.append) == []
    assert any("every source failed" in line and "empty directory" in line for line in lines)
    assert all("/" not in line.split("(")[0] for line in lines)  # no process data


# -- item 3: hooks status and connect -------------------------------------------------------------------

def runtime(tmp_path):
    path = tmp_path / "cfg" / "runtime.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"machine_config": "agent.json", "state_dir": "runtime-state"}))
    (path.parent / "runtime-state").mkdir(mode=0o700)
    return path


def probe(version, enabled=True):
    return lambda: (enabled, f"codex-cli {version}: feature hooks stable {'true' if enabled else 'false'}")


def test_codex_status_through_connect(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home, rt = tmp_path / "home", runtime(tmp_path)
    state = rt.parent / "runtime-state"
    st = lambda **kw: hooks_install.status("codex", str(rt), home=home, find=kw.pop("find", lambda: "/x/codex"),
                                           probe=kw.pop("probe", probe("0.160.0")), **kw)
    assert st(find=lambda: None) == "not_installed_agent"
    assert st(probe=probe("0.120.0", enabled=False)) == "too_old"
    assert st(probe=probe("0.140.0"), windows=True) == "too_old"  # below 0.145.0 on Windows
    assert st() == "not_connected"
    result = hooks_install.connect("codex", str(rt), home=home, prefix=PREFIX, probe=probe("0.160.0"),
                                   windows=False, now=1000.0)
    assert result["status"] == "installed" and "trust them once" in result["note"]
    assert st(now=1001.0) == "needs_approval"  # installed, no hook event since
    sessions.ensure_salt(str(state))
    sessions.sessions_dir(str(state), create=True)
    import io
    from raincli_agent.runtime import hook
    hook.handle("codex", "SessionStart", "proj", str(state), io.BytesIO(json.dumps(
        {"session_id": "s", "cwd": "/w/proj", "hook_event_name": "SessionStart"}).encode()), io.BytesIO(), now=1002.0)
    assert st(now=1003.0) == "connected"
    hooks_install.disconnect("codex", str(rt), home=home, windows=False)
    assert st() == "not_connected"


def test_connect_codex_too_old_on_windows_raises_and_writes_nothing(tmp_path):
    home, rt = tmp_path / "home", runtime(tmp_path)
    with pytest.raises(ConfigError, match="0.145.0"):
        hooks_install.connect("codex", str(rt), home=home, prefix=PREFIX, probe=probe("0.140.0"), windows=True)
    assert not (home / ".codex" / "hooks.json").exists()


def test_claude_status_and_connect(tmp_path):
    home, rt = tmp_path / "home", runtime(tmp_path)
    st = lambda find=lambda: "/x/claude": hooks_install.status("claude", str(rt), home=home, find=find)
    assert st(find=lambda: None) == "not_installed_agent"
    assert st() == "not_connected"
    hooks_install.connect("claude", str(rt), home=home, prefix=PREFIX, windows=False)
    assert st() == "connected"  # Claude Code needs no approval
    other = tmp_path / "other"
    other.mkdir()
    (other / "runtime.json").write_text(json.dumps({"machine_config": "a.json", "state_dir": "elsewhere"}))
    assert hooks_install.status("claude", str(other / "runtime.json"), home=home,
                                find=lambda: "/x/claude") == "not_connected"  # hooks for another state dir


def test_find_claude_native_layout(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    exe = home / ".local" / "bin" / "claude.exe"
    exe.write_text("")
    assert hooks_install.find_claude(env={"PATH": ""}, windows=True, home=home) == str(exe)
    assert hooks_install.find_claude(env={"PATH": "."}, windows=True, home=tmp_path / "none") is None


def test_cli_hint_after_login(monkeypatch, capsys, tmp_path):
    from raincli_agent import cli
    calls = []
    monkeypatch.setattr(hooks_install, "status", lambda kind, rt, **kw: calls.append(kind) or
                        {"codex": "not_connected", "claude": "connected"}[kind])
    cli.print_hooks_hint(str(tmp_path / "runtime.json"))
    out = capsys.readouterr().out
    assert "raincli hooks install --codex --config" in out and "/hooks" in out and "--claude" not in out
    assert calls == ["claude", "codex"]
