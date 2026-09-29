"""Windows sharing violations while a file is replaced or read (runtime smoke, 3.11).

The race is simulated on any OS: ``RETRY_PERMISSION_ERRORS`` is what Windows sets,
and ``os.replace`` / ``open`` raise PermissionError for the first N attempts.
"""
import builtins
import json

import pytest

from raincli_agent import fsutil
from raincli_agent.errors import ConfigError
from raincli_agent.fsutil import atomic_write_json, read_file_bytes, read_private_file
from raincli_agent.runtime import service, updates
from raincli_agent.runtime.service import Worker

from .test_runtime_handoff import mapping


def failing(real, failures):
    calls = []

    def fake(*args, **kwargs):
        calls.append(args)
        if len(calls) <= failures:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real(*args, **kwargs)
    fake.calls = calls
    return fake


@pytest.fixture
def windows_retry(monkeypatch):
    monkeypatch.setattr(fsutil, "RETRY_PERMISSION_ERRORS", True)
    monkeypatch.setattr(fsutil.time, "sleep", lambda s: None)


def test_replace_is_retried_until_the_reader_lets_go(tmp_path, monkeypatch, windows_retry):
    replace = failing(fsutil.os.replace, 3)
    monkeypatch.setattr(fsutil.os, "replace", replace)
    atomic_write_json(tmp_path / "status.json", {"status": "running"})
    assert len(replace.calls) == 4
    assert json.loads(read_private_file(tmp_path / "status.json")) == {"status": "running"}
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]  # no temp file left


def test_a_persistent_replace_error_is_raised_and_cleaned_up(tmp_path, monkeypatch, windows_retry):
    replace = failing(fsutil.os.replace, 10 ** 6)
    monkeypatch.setattr(fsutil.os, "replace", replace)
    with pytest.raises(PermissionError):
        atomic_write_json(tmp_path / "status.json", {})
    assert len(replace.calls) == fsutil.RETRY_ATTEMPTS
    assert list(tmp_path.iterdir()) == []


def test_reads_are_retried_mid_replace(tmp_path, monkeypatch, windows_retry):
    atomic_write_json(tmp_path / "status.json", {"status": "running"})
    opener = failing(fsutil.open_read_nofollow, 2)
    monkeypatch.setattr(fsutil, "open_read_nofollow", opener)
    assert json.loads(read_private_file(tmp_path / "status.json")) == {"status": "running"}
    assert len(opener.calls) == 3
    plain = failing(builtins.open, 2)
    monkeypatch.setattr(fsutil, "open", plain, raising=False)  # module global shadows the builtin
    assert json.loads(read_file_bytes(tmp_path / "status.json")) == {"status": "running"}
    assert len(plain.calls) == 3


def test_a_persistent_read_error_is_still_reported(tmp_path, monkeypatch, windows_retry):
    atomic_write_json(tmp_path / "status.json", {})
    monkeypatch.setattr(fsutil, "open_read_nofollow", failing(fsutil.open_read_nofollow, 10 ** 6))
    with pytest.raises(ConfigError, match="cannot open"):
        read_private_file(tmp_path / "status.json")


def test_posix_never_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(fsutil, "RETRY_PERMISSION_ERRORS", False)
    replace = failing(fsutil.os.replace, 1)
    monkeypatch.setattr(fsutil.os, "replace", replace)
    with pytest.raises(PermissionError):
        atomic_write_json(tmp_path / "status.json", {})
    assert len(replace.calls) == 1


def test_config_hash_rides_out_a_transient_sharing_violation(tmp_path, monkeypatch, windows_retry):
    """A config replaced by an editor must not look changed and retire a connector."""
    (tmp_path / "c.json").write_text("{}")
    monkeypatch.setattr(fsutil, "open", failing(builtins.open, 2), raising=False)
    assert service.file_sha256(tmp_path / "c.json") is not None


def test_status_write_failures_never_stop_the_supervisor(tmp_path, monkeypatch):
    mapping(tmp_path)
    config = tmp_path / "runtime.json"
    atomic_write_json(config, {"connectors": ["connector.json"], "state_dir": "state"})
    monkeypatch.setattr(Worker, "tick", lambda self, now: {"connector": self.path, "status": "offline"})
    monkeypatch.setattr(Worker, "stop", lambda self: None)
    real = service.atomic_write_json
    writes = []

    def flaky(path, obj, *a, **kw):
        writes.append(obj.get("status"))
        if len(writes) <= 2:  # "starting" and "running" both fail persistently
            raise PermissionError(13, "denied")
        return real(path, obj, *a, **kw)
    monkeypatch.setattr(service, "atomic_write_json", flaky)
    service.run(config, once=True)  # does not raise
    assert writes == ["starting", "running", "stopped"]
    assert service.status(config)["status"] == "stopped"


def test_pointer_write_uses_the_shared_retry(tmp_path, monkeypatch, windows_retry):
    replace = failing(fsutil.os.replace, 3)
    monkeypatch.setattr(fsutil.os, "replace", replace)
    updates.write_pointer(tmp_path, {"tag": "v1.0.0"})
    assert updates.read_pointer(tmp_path) == {"tag": "v1.0.0"} and len(replace.calls) == 4


def test_queue_reads_share_delete_and_ride_out_a_replace(tmp_path, monkeypatch, windows_retry):
    """R4-M1 hazard 1: `connector status` reading records while the connector saves."""
    from raincli_agent.connector.queue import Queue
    queue = Queue(str(tmp_path / "q"))
    queue.save({"id": "00000000-0000-4000-8000-000000000001", "seq": 1, "state": "submitted"})
    opener = failing(fsutil.open_read_nofollow, 2)  # the share-DELETE reader on Windows
    monkeypatch.setattr(fsutil, "open_read_nofollow", opener)
    assert [r["state"] for r in queue.all()] == ["submitted"]
    assert len(opener.calls) == 3


def test_connector_status_reads_under_the_queue_lock(connector_env, monkeypatch):
    from raincli_agent.cli import main
    from raincli_agent.connector.queue import Queue
    held = []
    real_lock = Queue.lock

    def lock(self):
        held.append(True)
        return real_lock(self)
    monkeypatch.setattr(Queue, "lock", lock)
    real_all = Queue.all
    monkeypatch.setattr(Queue, "all", lambda self: (held or pytest.fail("read without the lock")) and real_all(self))
    assert main(["connector", "status", "--config", connector_env.make()]) == 0


def test_log_rotation_and_open_are_best_effort(tmp_path, monkeypatch):
    """R4-M1 hazard 5: a reader holding the connector log must not block a start."""
    from .test_runtime import FakeProcess
    cfg, identity, binding = mapping(tmp_path)
    worker = Worker(str(tmp_path / "connector.json"), cfg, identity, tmp_path, binding)
    worker.log_path.write_bytes(b"x" * (service.LOG_LIMIT + 1))
    monkeypatch.setattr(service.os, "replace", failing(service.os.replace, 10 ** 6))
    real_open = service.os.open
    monkeypatch.setattr(service.os, "open", lambda path, *a: (_ for _ in ()).throw(PermissionError(13, "in use"))
                        if str(path) == str(worker.log_path) else real_open(path, *a))
    monkeypatch.setattr(service.subprocess, "Popen", FakeProcess)
    assert worker._spawn().pid  # started, output discarded this time


@pytest.mark.skipif(__import__("os").name != "nt", reason="native Windows file sharing")
def test_native_replace_while_a_reader_holds_the_file(tmp_path):
    import threading
    import time
    atomic_write_json(tmp_path / "status.json", {"n": 0})
    for hold in (lambda p: open(p, "rb"), lambda p: fsutil.open_read_nofollow(p)):
        handle = hold(tmp_path / "status.json")
        closer = threading.Timer(0.3, lambda: handle.close() if hasattr(handle, "close") else __import__("os").close(handle))
        closer.start()
        started = time.monotonic()
        atomic_write_json(tmp_path / "status.json", {"n": 1})
        closer.join()
        assert time.monotonic() - started < fsutil.RETRY_ATTEMPTS * 0.1 + 1
        assert json.loads(read_private_file(tmp_path / "status.json")) == {"n": 1}
