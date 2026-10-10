"""v0.5.2 in the app: the last runtime report reaches This computer as a UTC instant for localtime.js (§17.1)."""

from __future__ import annotations

import types

from raincli_agent.app.services import Services
from raincli_agent.runtime import service as runtime_service


def test_status_carries_the_last_report_as_utc(monkeypatch, tmp_path):
    host = types.SimpleNamespace(paused=False)
    svc = Services(None, host, paths=lambda: (str(tmp_path / "agent.json"), str(tmp_path / "runtime.json")))
    monkeypatch.setattr(runtime_service, "status", lambda config: {"status": "ok", "updated_at": 1791633600.75})
    assert svc.status()["last_report"] == "2026-10-10T12:00:00Z"
    for bad in (None, "soon", True):
        monkeypatch.setattr(runtime_service, "status", lambda config, bad=bad: {"updated_at": bad})
        assert "last_report" not in svc.status()
