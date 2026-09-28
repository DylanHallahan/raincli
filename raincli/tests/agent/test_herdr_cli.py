"""The real HerdrCli boundary, run against a fake ``herdr`` executable.

Nothing here invokes the real herdr binary or touches a live pane.
"""

import json
import os
import stat
import sys

import pytest

from raincli_agent.connector.herdr import HerdrCli, HerdrError, HerdrRejected, HerdrTimeout

from .conftest import send

FAKE = r'''#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
mode = open(os.path.join(here, "mode")).read().strip()
cmd = sys.argv[1:3]
if cmd == ["agent", "get"]:
    if mode == "missing" or sys.argv[3] != "bob-claude":
        sys.stderr.write(json.dumps({{"error": {{"code": "agent_not_found", "message": "no agent"}}}}))
        sys.exit(1)
    print(json.dumps({{"id": "1", "result": {{"type": "agent", "agent": {{
        "agent": "claude", "agent_status": "idle", "pane_id": "w9:p1", "cwd": "/work/bob",
        "focused": False}}}}}}))
elif cmd == ["agent", "prompt"]:
    if mode == "slow":
        time.sleep(5)
    if mode == "blocked":
        sys.stderr.write(json.dumps({{"error": {{"code": "agent_blocked", "message": "blocked"}}}}))
        sys.exit(1)
    if mode == "broken":
        sys.stderr.write("something odd")
        sys.exit(1)
    print(json.dumps({{"id": "2", "result": {{"type": "agent_prompt"}}}}))
else:
    sys.exit(2)
'''


@pytest.fixture
def fake_bin(tmp_path):
    path = tmp_path / "herdr"
    path.write_text(FAKE.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "mode").write_text("ok")

    class Bin:
        binary = str(path)

        @staticmethod
        def mode(value):
            (tmp_path / "mode").write_text(value)

        @staticmethod
        def calls():
            p = tmp_path / "calls.jsonl"
            return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    return Bin


def test_get_agent_parses_json(fake_bin):
    info = HerdrCli(fake_bin.binary).get_agent("bob-claude")
    assert (info.name, info.status, info.pane_id, info.cwd) == ("bob-claude", "idle", "w9:p1", "/work/bob")
    assert fake_bin.calls() == [["agent", "get", "bob-claude"]]


def test_missing_agent_is_none(fake_bin):
    fake_bin.mode("missing")
    assert HerdrCli(fake_bin.binary).get_agent("bob-claude") is None


def test_prompt_argv_is_literal_no_shell(fake_bin, tmp_path):
    text = "hi $(touch pwned) `id`; rm -rf ~ && echo \"x\" | cat\n--focus"
    HerdrCli(fake_bin.binary).prompt("bob-claude", text, timeout=5)
    assert fake_bin.calls() == [["agent", "prompt", "bob-claude", text]]
    assert not (tmp_path / "pwned").exists() and not os.path.exists("pwned")


def test_prompt_outcomes(fake_bin):
    herdr = HerdrCli(fake_bin.binary)
    fake_bin.mode("blocked")
    with pytest.raises(HerdrRejected) as exc:
        herdr.prompt("bob-claude", "x", timeout=5)
    assert exc.value.reason == "blocked"
    fake_bin.mode("broken")
    with pytest.raises(HerdrError) as exc:
        herdr.prompt("bob-claude", "x", timeout=5)
    assert not isinstance(exc.value, HerdrRejected)
    fake_bin.mode("slow")
    with pytest.raises(HerdrTimeout):
        herdr.prompt("bob-claude", "x", timeout=0.5)


def test_missing_binary_is_herdr_error(tmp_path):
    with pytest.raises(HerdrError):
        HerdrCli(str(tmp_path / "nope")).get_agent("bob-claude")


def test_connector_with_real_boundary(fake_api, connector_env, fake_bin):
    msg = send(fake_api, fake_api.alice, "bob", "via the cli boundary")
    conn = connector_env.connector(expect_pane_id="w9:p1")
    conn.herdr = HerdrCli(fake_bin.binary)
    conn.run_once()
    calls = fake_bin.calls()
    assert calls[0] == ["agent", "get", "bob-claude"]
    assert calls[1][:3] == ["agent", "prompt", "bob-claude"] and len(calls) == 2
    assert "\n| via the cli boundary\n[end of RainCLI message " in calls[1][3]
    assert fake_api.state.messages[msg["id"]]["delivery_state"] == "submitted"


def test_connector_slow_prompt_is_uncertain(fake_api, connector_env, fake_bin):
    fake_bin.mode("slow")
    msg = send(fake_api, fake_api.alice, "bob")
    conn = connector_env.connector(prompt_timeout=1)
    conn.herdr = HerdrCli(fake_bin.binary)
    conn.run_once()
    conn.run_once()
    prompts = [c for c in fake_bin.calls() if c[:2] == ["agent", "prompt"]]
    assert len(prompts) == 1
    assert fake_api.state.messages[msg["id"]]["delivery_state"] == "submission_uncertain"
