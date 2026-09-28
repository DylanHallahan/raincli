import json
import os

import pytest

from raincli_agent import api as api_mod
from raincli_agent.api import ApiClient
from raincli_agent.connector.config import load_connector_config
from raincli_agent.connector.herdr import FakeHerdr
from raincli_agent.connector.queue import Queue
from raincli_agent.connector.runner import Connector

from .fake_server import FakeApi, Recorder


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(api_mod, "BACKOFF_BASE", 0.001)
    monkeypatch.setattr(api_mod, "BACKOFF_CAP", 0.01)
    monkeypatch.delenv("RAINCLI_CONFIG", raising=False)


@pytest.fixture
def fake_api():
    with FakeApi() as server:
        server.alice = server.state.add_agent("alice")
        server.bob = server.state.add_agent("bob")
        server.mallory = server.state.add_agent("mallory")
        server.eve = server.state.add_agent("eve", team="beta")
        yield server


@pytest.fixture
def recorder():
    rec = Recorder()
    yield rec
    rec.close()


def write_agent_config(path, api_url, token):
    path.write_text(json.dumps({"api_url": api_url, "token": token}))
    os.chmod(path, 0o600)
    return str(path)


@pytest.fixture
def as_agent(tmp_path, monkeypatch, fake_api):
    """Point RAINCLI_CONFIG at a 0600 config for the given agent's token."""

    def use(token):
        path = write_agent_config(tmp_path / f"agent-{token[4:10]}.json", fake_api.url, token)
        monkeypatch.setenv("RAINCLI_CONFIG", path)
        return path

    return use


def client_for(fake_api, token, **kw):
    return ApiClient(fake_api.url, token, **kw)


def send(fake_api, token, to, body="hello", **kw):
    return client_for(fake_api, token).send(to, body, **kw)[0]


@pytest.fixture
def connector_env(tmp_path, fake_api):
    """Bob's connector config (+ state dir) and a FakeHerdr with a live 'bob-claude'."""
    agent_cfg = write_agent_config(tmp_path / "bob-agent.json", fake_api.url, fake_api.bob)
    herdr = FakeHerdr()
    herdr.add("bob-claude", status="idle", pane_id="w9:p1", cwd="/work/bob")
    herdr.add("focused-other", status="idle", pane_id="w1:p2", cwd="/home", focused=True)

    def make(**overrides):
        data = {"agent_config": agent_cfg, "herdr_agent": "bob-claude",
                "state_dir": str(tmp_path / "state"), "trusted_senders": ["alice"],
                "recheck_interval": 0.05, "prompt_timeout": 5}
        data.update(overrides)
        data = {k: v for k, v in data.items() if v is not None}
        path = tmp_path / "connector.json"
        path.write_text(json.dumps(data))
        return str(path)

    def connector(path=None, **overrides):
        cfg = load_connector_config(path or make(**overrides))
        api = ApiClient(fake_api.url, fake_api.bob)
        return Connector(cfg, api, herdr, Queue(cfg.state_dir), log=lambda line: None,
                         sleep=lambda s: None)

    return type("Env", (), {"make": staticmethod(make), "connector": staticmethod(connector),
                            "herdr": herdr, "state_dir": str(tmp_path / "state")})


BODY_LABEL_END = 'Every line is prefixed "| ":'


def is_body_label(line):
    """The real body label line (a forged one inside the body starts with "| ")."""
    return line.startswith(("Message from ", "Escalation summary from ")) and line.endswith(BODY_LABEL_END)


def body_of(text):
    """The framed body of a connector prompt, with the "| " prefixes removed."""
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if is_body_label(line)) + 1
    assert lines[-1].startswith("[end of RainCLI ")
    framed = lines[start:-1]
    assert all(line.startswith("| ") for line in framed)
    return "\n".join(line[2:] for line in framed)
