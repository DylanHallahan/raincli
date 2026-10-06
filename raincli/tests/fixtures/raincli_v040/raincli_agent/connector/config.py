# Vendored from raincli tag v0.4.0 (commit a214082792ff64731cae1d6f12fc5cc900ae8b11), path raincli/raincli_agent/connector/config.py. Test fixture for protocol 16.12 C1; do not edit.
"""Connector config: one agent identity mapped to one Herdr session target."""

import json
import os
import re
import stat
from dataclasses import dataclass, field

from ..errors import ConfigError
from ..fsutil import is_link

HANDLE_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
# Herdr's agent-name grammar. Pane ids such as "w9:p1" are deliberately not
# accepted: the target is a live agent *name*, never a pane slot.
HERDR_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

KEYS = {"agent_config", "herdr_agent", "expect_pane_id", "expect_cwd", "state_dir",
        "trusted_senders", "poll_wait", "recheck_interval", "prompt_timeout",
        "herdr_bin", "herdr_timeout",
        # protocol section 10 (inbox-agent mode)
        "mode", "trust_mode", "blocked_senders", "shareable_context", "escalation",
        # protocol section 14.4 (next-turn inbox through a Claude Code hook session)
        "inbox"}
INBOX_KEYS = {"hook", "name"}
INBOX_HOOK_TYPES = ("claude",)
ESCALATION_KEYS = {"herdr_agent", "expect_pane_id", "expect_cwd", "notify"}
MODES = ("direct", "inbox")
TRUST_MODES = ("list", "team")


@dataclass(frozen=True)
class EscalationTarget:
    herdr_agent: str
    expect_pane_id: str = ""
    expect_cwd: str = ""
    notify: bool = True


@dataclass(frozen=True)
class ConnectorConfig:
    herdr_agent: str = ""  # "" when the inbox is a hook session (inbox_hook)
    agent_config: str = ""  # path to the agent's own credential config; "" = default
    expect_pane_id: str = ""
    expect_cwd: str = ""
    state_dir: str = ""  # "" = ~/.local/state/raincli/connector/<handle>/
    trusted_senders: tuple = field(default_factory=tuple)
    poll_wait: int = 25
    recheck_interval: float = 5.0
    prompt_timeout: float = 30.0
    herdr_bin: str = "herdr"
    herdr_timeout: float = 10.0
    mode: str = "direct"
    trust_mode: str = "list"
    blocked_senders: tuple = field(default_factory=tuple)
    shareable_context: tuple = field(default_factory=tuple)
    escalation: EscalationTarget = None
    inbox_hook: tuple = None  # (type, session name) for a next-turn inbox, else None
    path: str = ""

    @property
    def target_label(self):
        return self.herdr_agent or "%s hook session %s" % self.inbox_hook


def default_state_dir(handle):
    return os.path.join(os.path.expanduser("~"), ".local", "state", "raincli", "connector", handle)


def _number(data, key, default, lo, hi):
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise ConfigError(f"connector config: {key} must be a number between {lo} and {hi}")
    return value


def _string(data, key):
    value = data.get(key, "")
    if not isinstance(value, str):
        raise ConfigError(f"connector config: {key} must be a string")
    return value


def _handles(data, key):
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(h, str) and HANDLE_RE.match(h) for h in value):
        raise ConfigError(f"connector config: {key} must be a list of agent handles")
    return tuple(value)


def _abs_path(value, what):
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ConfigError(f"connector config: {what} must be an absolute path")
    return value


def _shareable_context(data):
    paths = data.get("shareable_context", [])
    if not isinstance(paths, list):
        raise ConfigError("connector config: shareable_context must be a list of absolute directory paths")
    out = []
    for p in paths:
        _abs_path(p, "each shareable_context entry")
        try:
            st = os.lstat(p)
        except OSError:
            raise ConfigError(f"connector config: shareable_context {p!r} does not exist") from None
        if is_link(st):
            raise ConfigError(f"connector config: shareable_context {p!r} is a symlink")
        if not stat.S_ISDIR(st.st_mode):
            raise ConfigError(f"connector config: shareable_context {p!r} is not a directory")
        if os.name == "nt":
            # realpath also expands legitimate Windows 8.3 names (RUNNER~1).
            # Inspect each ancestor rather than treating any spelling change
            # as a link. Keep the supplied path until all ancestors are checked.
            probe = p
            try:
                while True:
                    if is_link(os.lstat(probe)):
                        raise ConfigError(f"connector config: shareable_context {p!r} goes through a symlink or junction")
                    parent = os.path.dirname(probe)
                    if parent == probe:
                        break
                    probe = parent
            except OSError:
                raise ConfigError(f"connector config: cannot inspect shareable_context {p!r}") from None
            out.append(os.path.realpath(p))
        else:
            if os.path.realpath(p) != os.path.normpath(p):
                raise ConfigError(f"connector config: shareable_context {p!r} goes through a symlink")
            out.append(os.path.normpath(p))
    return tuple(out)


def _escalation(data, inbox):
    esc = data.get("escalation")
    if esc is None:
        return None
    if not isinstance(esc, dict):
        raise ConfigError("connector config: escalation must be an object")
    unknown = sorted(set(esc) - ESCALATION_KEYS)
    if unknown:
        raise ConfigError(f"connector config: unknown escalation keys {', '.join(unknown)}")
    name = esc.get("herdr_agent")
    if not isinstance(name, str) or not HERDR_NAME_RE.match(name):
        raise ConfigError("connector config: escalation.herdr_agent must be a Herdr agent name "
                          "(^[a-z][a-z0-9_-]{0,31}$), not a pane id")
    if inbox["herdr_agent"] and name == inbox["herdr_agent"]:
        raise ConfigError("connector config: escalation.herdr_agent must differ from the inbox herdr_agent")
    pane = _string(esc, "expect_pane_id")
    if pane and pane == inbox["expect_pane_id"]:
        raise ConfigError("connector config: escalation.expect_pane_id must differ from the inbox pane")
    cwd = _string(esc, "expect_cwd")
    if cwd:
        _abs_path(cwd, "escalation.expect_cwd")
    notify = esc.get("notify", True)
    if not isinstance(notify, bool):
        raise ConfigError("connector config: escalation.notify must be true or false")
    return EscalationTarget(herdr_agent=name, expect_pane_id=pane, expect_cwd=cwd, notify=notify)


def _inbox_hook(data):
    inbox = data.get("inbox")
    if inbox is None:
        return None
    if not isinstance(inbox, dict) or set(inbox) != INBOX_KEYS:
        raise ConfigError('connector config: inbox must be {"hook": "claude", "name": "<session name>"}')
    if inbox["hook"] not in INBOX_HOOK_TYPES:
        raise ConfigError('connector config: inbox.hook must be "claude"')
    name = inbox["name"]
    from ..runtime.sessions import normalize_name
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or normalize_name(name, "") != name:
        raise ConfigError("connector config: inbox.name must be a session name (1-64 printable characters, "
                          "no leading, trailing or repeated spaces)")
    return (inbox["hook"], name)


def load_connector_config(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"connector config not found: {path}") from None
    except (ValueError, UnicodeDecodeError):
        raise ConfigError(f"connector config {path} is not valid JSON") from None
    if not isinstance(data, dict):
        raise ConfigError("connector config must be a JSON object")
    unknown = sorted(set(data) - KEYS)
    if unknown:
        raise ConfigError(f"connector config: unknown keys {', '.join(unknown)}")
    if "token" in data or "api_url" in data:
        raise ConfigError("connector config: put credentials in the agent config (agent_config)")

    inbox_hook = _inbox_hook(data)
    herdr_agent = data.get("herdr_agent")
    if inbox_hook is not None:
        if herdr_agent is not None:
            raise ConfigError("connector config: inbox and herdr_agent are mutually exclusive")
        if data.get("expect_pane_id") or data.get("expect_cwd"):
            raise ConfigError("connector config: expect_pane_id and expect_cwd apply to herdr_agent only")
        herdr_agent = ""
    elif not isinstance(herdr_agent, str) or not HERDR_NAME_RE.match(herdr_agent):
        raise ConfigError("connector config: herdr_agent must be a Herdr agent name "
                          "(^[a-z][a-z0-9_-]{0,31}$), not a pane id")
    trusted = _handles(data, "trusted_senders")
    mode = data.get("mode", "direct")
    if mode not in MODES:
        raise ConfigError('connector config: mode must be "direct" or "inbox"')
    trust_mode = data.get("trust_mode", "team" if mode == "inbox" else "list")
    if trust_mode not in TRUST_MODES:
        raise ConfigError('connector config: trust_mode must be "list" or "team"')

    base = os.path.dirname(os.path.abspath(path))

    def resolve(p):
        return os.path.join(base, os.path.expanduser(p)) if p else ""

    expect_cwd = _string(data, "expect_cwd")
    escalation = _escalation(data, {"herdr_agent": herdr_agent,
                                    "expect_pane_id": _string(data, "expect_pane_id")})
    if expect_cwd and not os.path.isabs(expect_cwd):
        raise ConfigError("connector config: expect_cwd must be an absolute path")
    return ConnectorConfig(
        herdr_agent=herdr_agent,
        agent_config=resolve(_string(data, "agent_config")),
        expect_pane_id=_string(data, "expect_pane_id"),
        expect_cwd=expect_cwd,
        state_dir=resolve(_string(data, "state_dir")),
        trusted_senders=trusted,
        mode=mode,
        trust_mode=trust_mode,
        blocked_senders=_handles(data, "blocked_senders"),
        shareable_context=_shareable_context(data),
        escalation=escalation,
        inbox_hook=inbox_hook,
        poll_wait=int(_number(data, "poll_wait", 25, 0, 25)),
        recheck_interval=float(_number(data, "recheck_interval", 5, 0.05, 300)),
        prompt_timeout=float(_number(data, "prompt_timeout", 30, 1, 600)),
        herdr_bin=_string(data, "herdr_bin") or "herdr",
        herdr_timeout=float(_number(data, "herdr_timeout", 10, 1, 120)),
        path=os.path.abspath(path),
    )
