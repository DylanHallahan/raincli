"""Who may reach this machine's named agents (protocol §16.12 C5, revised).

The settings live in the machine-mode runtime config (``runtime.json``):

- ``trust_mode``: ``team`` (the default: any member of the team) or ``list`` (only
  ``trusted_senders``; everyone else is held ``approval_required``);
- ``trusted_senders`` and ``blocked_senders``: machine handles and ``@email`` people;
- the owner (``owner_email``) always passes.

The running runtime notices the changed file and restarts its connector with it.
"""
import json
from pathlib import Path

from .connector.config import (EMAIL_RE, HANDLE_RE, TRUST_MODES, load_connector_config, machine_connector,
                               sender_key)
from .errors import ConfigError, UsageError
from .fsutil import atomic_write_json


def _read(runtime_config):
    path = Path(runtime_config)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"this machine is not signed in ({path} does not exist)") from None
    except (OSError, ValueError, UnicodeDecodeError):
        raise ConfigError(f"runtime config {path} is not valid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("machine_config"), str):
        raise ConfigError(f"{path} is not a machine-mode runtime config; trust for a connector is set in "
                          "its connector config (trusted_senders, trust_mode)")
    return path, data


def _write(path, data):
    machine_connector(str(path), data)  # never write a config the runtime would refuse
    atomic_write_json(str(path), data)


def sender(value):
    """A machine handle or a person (``alice@example.com`` or ``@alice@example.com``)."""
    if not isinstance(value, str) or not (HANDLE_RE.match(value) or EMAIL_RE.match(value)):
        raise UsageError("a sender is a machine handle or @email")
    return sender_key(value)


def describe(runtime_config):
    """``{"trust_mode", "trusted_senders", "blocked_senders", "owner_email"}`` as in effect."""
    path, _ = _read(runtime_config)
    cfg = load_connector_config(str(path))
    return {"trust_mode": cfg.trust_mode, "trusted_senders": list(cfg.trusted_senders),
            "blocked_senders": list(cfg.blocked_senders), "owner_email": cfg.owner_email or None}


def set_mode(runtime_config, mode):
    if mode not in TRUST_MODES:
        raise UsageError('the trust mode is "team" or "list"')
    path, data = _read(runtime_config)
    data["trust_mode"] = mode
    _write(path, data)
    return describe(path)


def add(runtime_config, value):
    """Trust a sender (and stop blocking it)."""
    key = sender(value)
    path, data = _read(runtime_config)
    trusted = [sender_key(s) for s in data.get("trusted_senders", [])]
    if key not in trusted:
        trusted.append(key)
    data["trusted_senders"] = trusted
    if data.get("blocked_senders"):
        data["blocked_senders"] = [s for s in data["blocked_senders"] if sender_key(s) != key]
    _write(path, data)
    return describe(path)


def remove(runtime_config, value):
    key = sender(value)
    path, data = _read(runtime_config)
    data["trusted_senders"] = [s for s in data.get("trusted_senders", []) if sender_key(s) != key]
    _write(path, data)
    return describe(path)


def approve(runtime_config, message_id, always=False):
    """``raincli me approve``: deliver one message held ``approval_required`` for a named
    agent; ``always`` also trusts its sender from now on."""
    from .connector import ops, queue as q
    path, _ = _read(runtime_config)
    cfg = load_connector_config(str(path))
    record = ops.approve(q.Queue(cfg.state_dir), message_id)
    if always:
        add(path, record["sender"])
    return record
