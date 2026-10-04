"""Advisory team presence and the machine agent directory (protocol §13, §14).

Everything here is authenticated to the publishing handle (the machine credential):
the client never names an agent, team or timestamp. A report may carry a snapshot of
the machine's coding-agent sessions and the client's version; neither says anything
about message delivery.
"""
import re
import unicodedata
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from . import identity, security
from .messaging import MessagingError
from .models import AgentPresence, ClientTarget, MachineAgent

TTL_SECONDS = 120
STATES = {"ready", "busy", "blocked", "offline", "unknown"}

MAX_AGENTS = 100
AGENT_NAME_MAX = 64
AGENT_KEY_RE = re.compile(r"^[a-z0-9]{8,64}$")
AGENT_TYPES = ("claude", "codex", "gemini", "cursor", "opencode", "other")
AGENT_STATES = ("working", "idle", "blocked", "offline", "unknown")
AGENT_SOURCES = ("herdr", "hook", "scan")
REACHABILITY = ("instant", "next-turn")
AGENT_FIELDS = {"key", "name", "type", "status", "role", "reachability", "source"}
AGENT_REQUIRED = {"key", "name", "type", "status", "source"}

# ASCII digits without leading zeros (protocol §14.7, §14.9); \d would also match non-ASCII digits.
_PART = r"(0|[1-9][0-9]{0,3})"
CLIENT_VERSION_RE = re.compile(rf"^{_PART}\.{_PART}\.{_PART}$")
TARGET_VERSION_RE = re.compile(rf"^v{_PART}\.{_PART}\.{_PART}$")
MIN_TARGET = (0, 3, 0)  # the first target-aware client (protocol §14.7)
UPDATE_MODES = ("automatic", "manual")
UPDATE_STATES = ("current", "updating", "failed", "rolled_back")
CLIENT_FIELDS = {"version", "update_mode", "update_state", "error"}
CLIENT_REQUIRED = {"version", "update_mode", "update_state"}
CLIENT_ERROR_RE = re.compile(r"^[a-z0-9_.:-]{1,64}$")  # a code or exception class name only
# Names are ordinary display names: slashes, dots and emoji are fine. Only a RainCLI
# token-shaped string is treated as a leak (review 1, finding 1).
TOKEN_RE = re.compile(r"rc[ai]_[A-Za-z0-9_-]{20,}")
# Control, format (bidi and zero-width), line/paragraph separator and surrogate code points.
# The client normalizer replaces these and more (unassigned and private use), so a
# normalized name always passes; unassigned code points are not rejected here, because
# the server's Unicode version may be older than the client's.
_NAME_FORBIDDEN_CATEGORIES = {"Cc", "Cf", "Zl", "Zp", "Cs"}


def _invalid(message: str) -> MessagingError:
    return MessagingError(400, "invalid", message)


def _is_str(value, allowed=None) -> bool:
    return isinstance(value, str) and (allowed is None or value in allowed)


def _parse_agents(value) -> list[dict]:
    if not isinstance(value, list):
        raise _invalid("agents must be a list")
    if len(value) > MAX_AGENTS:
        raise _invalid(f"at most {MAX_AGENTS} agents per report")
    out, keys, inboxes = [], set(), 0
    for i, item in enumerate(value):
        where = f"agents[{i}]"
        if not isinstance(item, dict) or not AGENT_REQUIRED <= set(item) or set(item) - AGENT_FIELDS:
            raise _invalid(f"{where} must have exactly key, name, type, status, source and optionally role, reachability")
        key, name = item["key"], item["name"]
        if not _is_str(key) or not AGENT_KEY_RE.fullmatch(key):
            raise _invalid(f"{where}.key must match [a-z0-9]{{8,64}}")
        if key in keys:
            raise _invalid(f"{where}.key is repeated; keys are unique within a report")
        keys.add(key)
        if not valid_agent_name(name):
            raise _invalid(f"{where}.name must be a 1-{AGENT_NAME_MAX} character display name "
                           "without control, bidi or zero-width characters or tokens")
        if not _is_str(item["type"], AGENT_TYPES):
            raise _invalid(f"{where}.type must be one of {', '.join(AGENT_TYPES)}")
        if not _is_str(item["status"], AGENT_STATES):
            raise _invalid(f"{where}.status must be one of {', '.join(AGENT_STATES)}")
        if not _is_str(item["source"], AGENT_SOURCES):
            raise _invalid(f"{where}.source must be one of {', '.join(AGENT_SOURCES)}")
        role, reach = item.get("role"), item.get("reachability")
        if role is not None and role != "inbox":
            raise _invalid(f'{where}.role must be "inbox" or null')
        if role == "inbox":
            inboxes += 1
            if not _is_str(reach, REACHABILITY):
                raise _invalid(f'{where}.reachability must be "instant" or "next-turn" for the inbox')
        elif reach is not None:
            raise _invalid(f"{where}.reachability is only reported for the inbox agent")
        if item["source"] == "scan" and item["status"] != "unknown":
            raise _invalid(f'{where}: a scanned agent must report status "unknown"')
        out.append({"key": key, "name": name.strip(), "type": item["type"], "status": item["status"],
                    "role": role, "reachability": reach, "source": item["source"]})
    if inboxes > 1:
        raise _invalid("at most one agent may be the inbox")
    return out


def valid_agent_name(name) -> bool:
    """A display-name-valid name of at most 64 code points (protocol §14.1, §14.7 H4)."""
    return (security.valid_display_name(name) and len(name) <= AGENT_NAME_MAX
            and not any(unicodedata.category(ch) in _NAME_FORBIDDEN_CATEGORIES for ch in name)
            and not TOKEN_RE.search(name))


def _parse_client(value) -> dict:
    if not isinstance(value, dict) or not CLIENT_REQUIRED <= set(value) or set(value) - CLIENT_FIELDS:
        raise _invalid("client must have exactly version, update_mode, update_state and optionally error")
    if not _is_str(value["version"]) or not CLIENT_VERSION_RE.fullmatch(value["version"]):
        raise _invalid("client.version must look like 1.2.3, without leading zeros")
    if not _is_str(value["update_mode"], UPDATE_MODES):
        raise _invalid("client.update_mode must be automatic or manual")
    if not _is_str(value["update_state"], UPDATE_STATES):
        raise _invalid(f"client.update_state must be one of {', '.join(UPDATE_STATES)}")
    error = value.get("error")
    if error is not None and not (_is_str(error) and CLIENT_ERROR_RE.fullmatch(error)):
        raise _invalid("client.error must be a short code matching [a-z0-9_.:-]{1,64}, or null")
    return {"client_version": value["version"], "update_mode": value["update_mode"],
            "update_state": value["update_state"], "update_error": error}


def parse_report(data) -> tuple[str, list[dict] | None, dict | None]:
    """Validate a §14.1 body; returns (status, agents or None if absent, client or None if absent)."""
    if not isinstance(data, dict) or "status" not in data or set(data) - {"status", "agents", "client"}:
        raise _invalid("presence takes status, and optionally agents and client")
    if not _is_str(data["status"], STATES):
        raise _invalid("presence requires a valid status")
    agents = _parse_agents(data["agents"]) if "agents" in data else None
    client = _parse_client(data["client"]) if "client" in data else None
    return data["status"], agents, client


def publish(session, agent, data):
    """Store one report; returns the §14.1 reply (presence plus the team's client target)."""
    status, agents, client = parse_report(data)
    now = identity.now()  # the server clock shared with the web view
    # An omitted client block leaves the stored client columns unchanged (protocol §14.7).
    fields = {"status": status, "seen_at": now, **(client or {})}
    # The upsert locks this handle's presence row, so concurrent snapshots of one machine serialize.
    session.execute(insert(AgentPresence).values(agent_id=agent.id, **fields).on_conflict_do_update(
        index_elements=[AgentPresence.agent_id], set_=fields))
    if agents is not None:
        session.execute(delete(MachineAgent).where(MachineAgent.agent_id == agent.id))
        if agents:
            session.execute(insert(MachineAgent), [{"agent_id": agent.id, "seen_at": now, **a} for a in agents])
        if agent.inbox_role_at is None and any(a.get("role") == "inbox" for a in agents):
            agent.inbox_role_at = now  # delivery history: app sign-in may no longer replace it (§15.8 H2)
    return {"presence": {"status": status, "seen_at": now.isoformat(),
                         "expires_at": (now + timedelta(seconds=TTL_SECONDS)).isoformat()},
            "target": target_json(session.get(ClientTarget, agent.team_id))}


def version_tuple(version: str) -> tuple[int, int, int]:
    """Numeric comparison key for ``1.2.3`` or ``v1.2.3``; raises ValueError otherwise."""
    if not isinstance(version, str) or not TARGET_VERSION_RE.fullmatch("v" + version.removeprefix("v")):
        raise ValueError(f"not a version: {version!r}")
    major, minor, patch = (int(p) for p in version.removeprefix("v").split("."))
    return major, minor, patch


def target_json(target: ClientTarget | None) -> dict | None:
    """The team's client target: a version only, never a URL, repository or host.

    ``set_at`` changes on every operator upsert, so re-setting the same version re-arms a
    target the client blocked after a rollback (protocol §14.8)."""
    if target is None:
        return None
    return {"version": target.version, "allow_downgrade": bool(target.allow_downgrade),
            "set_at": target.set_at.isoformat()}


def live_machine_agents(session, agent_ids) -> dict:
    """Directory rows younger than the TTL, grouped by handle id: inbox first, then by name."""
    ids = list(agent_ids)
    if not ids:
        return {}
    cutoff = identity.now() - timedelta(seconds=TTL_SECONDS)
    rows = session.scalars(
        select(MachineAgent).where(MachineAgent.agent_id.in_(ids), MachineAgent.seen_at > cutoff)
        .order_by(MachineAgent.agent_id, MachineAgent.role.is_(None), MachineAgent.name, MachineAgent.key))
    out: dict = {}
    for row in rows:
        out.setdefault(row.agent_id, []).append(row)
    return out


def machine_json(row: AgentPresence | None) -> dict | None:
    if row is None or row.client_version is None:
        return None
    return {"client_version": row.client_version, "update_mode": row.update_mode,
            "update_state": row.update_state, "error": row.update_error, "seen_at": row.seen_at.isoformat()}


def directory(session, agents):
    ids = [a.id for a in agents]
    rows = session.scalars(select(AgentPresence).where(AgentPresence.agent_id.in_(ids))) if ids else []
    by_id = {r.agent_id: r for r in rows}
    live = live_machine_agents(session, [a.id for a in agents if a.revoked_at is None])
    now = identity.now()  # the server clock shared with the web view
    out = []
    for agent in agents:
        row = by_id.get(agent.id)
        expires = row.seen_at + timedelta(seconds=TTL_SECONDS) if row else None
        status = row.status if row and expires > now else ("offline" if row else "unknown")
        revoked = agent.revoked_at is not None
        if revoked:
            status = "offline"
        out.append({"handle": agent.handle, "display_name": agent.display_name,
                    "active": not revoked,
                    "presence": {"status": status, "seen_at": row.seen_at.isoformat() if row else None,
                                 "expires_at": expires.isoformat() if expires else None},
                    "machine": None if revoked else machine_json(row),
                    "agents": [{"name": m.name, "type": m.type, "status": m.status, "role": m.role,
                                "reachability": m.reachability, "source": m.source}
                               for m in live.get(agent.id, [])]})
    return out
