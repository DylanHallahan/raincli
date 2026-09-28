import io
import json
from pathlib import Path
import zipfile

import pytest

from raincli_agent.config import write_config
from raincli_agent.connector.config import ConnectorConfig
from raincli_agent.connector.herdr import FakeHerdr
from raincli_agent.errors import ConfigError
from raincli_agent.fsutil import atomic_write_json
from raincli_agent.runtime.service import availability, load_runtime, Worker
from raincli_agent.runtime.startup import systemd_quote
from raincli_agent.runtime import updates


def test_presence_respects_explicit_pins_and_readiness():
    herdr = FakeHerdr()
    herdr.add("inbox", pane_id="w1:p2", cwd="/work")
    cfg = ConnectorConfig(herdr_agent="inbox", expect_pane_id="w1:p2", expect_cwd="/work")
    assert availability(cfg, herdr, False) == "offline"
    assert availability(cfg, herdr, True) == "ready"
    herdr.set_status("inbox", "working")
    assert availability(cfg, herdr, True) == "busy"
    herdr.set_status("inbox", "blocked")
    assert availability(cfg, herdr, True) == "blocked"
    herdr.add("inbox", pane_id="w1:p3", cwd="/work")
    assert availability(cfg, herdr, True) == "blocked"
    herdr.remove("inbox")
    assert availability(cfg, herdr, True) == "offline"


def test_worker_waits_for_authenticated_queue_owner(tmp_path):
    identity = write_config(tmp_path / "agent.json", "http://127.0.0.1:1", "rca_" + "a" * 43)
    worker = Worker("connector.json", ConnectorConfig(herdr_agent="inbox"), identity, tmp_path)
    worker.herdr = FakeHerdr()
    worker.herdr.add("inbox")
    class Process:
        pid = 123
        def poll(self): return None
    class Api:
        def publish_presence(self, state):
            return {"expires_at": "soon"}
    worker.process, worker.api = Process(), Api()
    assert worker.tick(1)["status"] == "offline"
    atomic_write_json(worker.ready_path, {"pid": 456})
    assert worker.tick(2)["status"] == "offline"
    atomic_write_json(worker.ready_path, {"pid": 123})
    assert worker.tick(3)["status"] == "ready"


def test_duplicate_runtime_credentials_are_rejected(tmp_path):
    write_config(tmp_path / "agent.json", "http://127.0.0.1:1", "rca_" + "a" * 43)
    for name in ("a", "b"):
        atomic_write_json(tmp_path / (name + ".json"), {"agent_config": "agent.json", "herdr_agent": name})
    config = tmp_path / "runtime.json"
    atomic_write_json(config, {"connectors": ["a.json", "b.json"]})
    with pytest.raises(ConfigError, match="distinct credentials"):
        load_runtime(config)


@pytest.mark.parametrize("name", ["../escape", "/escape", "root/../../escape", "root/C:escape", "root\\escape", "root/escape. "])
def test_update_archive_rejects_unsafe_names(tmp_path, name):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(name, "bad")
    with pytest.raises(ConfigError):
        updates.unpack(buf.getvalue(), tmp_path)


def test_update_archive_rejects_symlinks(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        link = zipfile.ZipInfo("root/link")
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "../../escape")
    with pytest.raises(ConfigError):
        updates.unpack(buf.getvalue(), tmp_path)


def test_failed_update_never_changes_pointer(tmp_path, monkeypatch):
    old = {"tag": "v0.1.0", "commit": "a" * 40, "python": "/old/python", "automatic": False}
    atomic_write_json(tmp_path / "current.json", old)
    monkeypatch.setattr(updates, "fetch", lambda *a: b"invalid zip")
    with pytest.raises(zipfile.BadZipFile):
        updates.install(tmp_path, {"tag": "v0.2.0", "commit": "b" * 40})
    assert updates.read_pointer(tmp_path) == old
    assert not list((tmp_path / "versions").iterdir())


def test_update_rejects_unexpected_release(monkeypatch):
    monkeypatch.setattr(updates, "fetch", lambda *a: json.dumps({"tag_name": "main", "prerelease": False}).encode())
    with pytest.raises(ConfigError):
        updates.latest()


def test_systemd_escaping():
    assert systemd_quote('/a %h $HOME "x"') == '"/a %%h $$HOME \\"x\\""'
    with pytest.raises(ConfigError):
        systemd_quote("bad\npath")
