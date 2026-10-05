"""Version floors for update targets and rollbacks (protocol §15.8 H8, §16.12 C1).

- Machine mode needs v0.4.0 or later.
- A runtime whose connector has polled with ``routing=1`` (it may hold messages
  for named agents, in ``agent_*`` states a v0.4 client would not deliver) needs
  v0.5.0 or later. It records that as ``routing-capable.json`` in its state
  directory.
"""
import json
import os
from pathlib import Path
import time

from ..fsutil import atomic_write_json

MACHINE_FLOOR = (0, 4, 0)
ROUTING_FLOOR = (0, 5, 0)
ROUTING_FILE = "routing-capable.json"


def record_routing_capable(state_dir):
    path = Path(state_dir) / ROUTING_FILE
    if not path.exists():
        atomic_write_json(path, {"routing_capable": True, "at": time.time()})


def routing_capable(state_dir):
    try:
        data = json.loads((Path(state_dir) / ROUTING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("routing_capable") is True


def runtime_facts(runtime_config):
    """``(machine_mode, state_dir)`` of a runtime config, read leniently (None when unreadable)."""
    try:
        path = Path(runtime_config)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False, None
    if not isinstance(data, dict):
        return False, None
    state = data.get("state_dir", "runtime-state")
    state_dir = (path.parent / Path(state).expanduser()) if isinstance(state, str) and state else None
    return "machine_config" in data, state_dir


def floor_for(runtime_configs):
    """The highest floor any of these runtime configs needs, or None."""
    floor = None
    for config in runtime_configs:
        if not config:
            continue
        machine, state_dir = runtime_facts(config)
        if state_dir is not None and routing_capable(state_dir):
            return ROUTING_FLOOR
        if machine:
            floor = MACHINE_FLOOR
    return floor
