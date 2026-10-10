"""The test suite never runs a real Codex or Claude Code (see tests/conftest.py)."""
import os
import shutil
import subprocess

import pytest

from raincli_agent.runtime import hooks_install


@pytest.mark.skipif(os.name == "nt", reason="the shims are POSIX scripts")
def test_a_subprocess_finds_only_the_shim_and_is_caught(tmp_path_factory):
    path = shutil.which("codex")
    assert path and path.endswith(os.sep + "codex") and "agent-shims" in path
    assert subprocess.run(["codex", "--version"], capture_output=True, timeout=10).returncode == 1
    calls = os.path.join(os.path.dirname(path), "calls.log")
    assert open(calls).read() == "codex --version\n"  # the guard would fail this test at teardown
    os.unlink(calls)


def test_in_process_probes_find_nothing():
    assert hooks_install.find_codex() is None and hooks_install.find_claude() is None
    assert hooks_install.codex_support() == (False, "codex not found on PATH")
    # A test's own fakes still work.
    fake = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "codex-cli 0.160.0\nhooks stable true\n", "")
    assert hooks_install.codex_support(fake, lambda: "/x/codex")[0] is True


def test_the_sign_in_hint_probe_is_bounded(monkeypatch):
    """The hint after a sign-in probes Codex with a short timeout; hooks install keeps the long one."""
    from raincli_agent import cli
    timeouts = []
    monkeypatch.setattr(hooks_install, "codex_support",
                        lambda timeout=hooks_install.PROBE_TIMEOUT: timeouts.append(timeout) or (False, "slow"))
    monkeypatch.setattr(hooks_install, "status", lambda kind, rt, probe=None, **kw: probe() and "too_old")
    cli.print_hooks_hint("/x/runtime.json")
    assert timeouts == [hooks_install.HINT_TIMEOUT] * 2 and hooks_install.HINT_TIMEOUT <= 5


def test_a_probe_that_times_out_counts_as_not_supported():
    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])
    supported, evidence = hooks_install.codex_support(slow, lambda: "/x/codex", timeout=5)
    assert supported is False and "TimeoutExpired" in evidence
