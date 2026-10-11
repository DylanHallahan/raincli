"""The one listing rule (protocol §20.6 M3): an agent is listed exactly when ``routing.check_agent_route`` would
accept it now, worked out in bulk.

The same §16.2 order, for many machines at once:
1. a machine set to ``inbox-only`` lists no named agents (its inbox is listed by the caller);
2. a name with live entries (reported within ``LIVE_SECONDS``) is listed only when there is exactly one and it is
   deliverable (``instant`` or ``next-turn``, not ambiguous); any other live state (listed-only, ambiguous, two
   entries) is not listed, even if the name is also known;
3. a name with no live entry is listed when ``known_agents`` has it within ``KNOWN_DAYS``, as Offline.

Used by the Team section, the empty-state pre-fill, ``GET /app/recipients`` and the no-JavaScript ``<select>``.
Two queries for any number of machines. Presence words follow §19.8 L3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from raincli_server import identity
from raincli_server.models import Agent, AgentPresence, KnownAgent, MachineAgent
from raincli_server.presence import TTL_SECONDS
from raincli_server.routing import DELIVERABLE, KNOWN_DAYS, LIVE_SECONDS

WORDS = {"idle": "ready", "working": "busy", "blocked": "blocked", "unknown": "unknown"}


@dataclass(frozen=True)
class ListedAgent:
    machine: Agent
    name: str
    reachability: str  # instant or next-turn (a known agent: the reachability it last reported)
    status: str  # ready, busy, blocked, unknown or offline (§19.8 L3)

    @property
    def live(self) -> bool:
        return self.status != "offline"


def listed_agents(db, machines: list[Agent], now=None) -> list[ListedAgent]:
    """The deliverable named agents of ``machines`` (revoked machines are skipped), by machine then name."""
    now = now or identity.now()
    machines = [m for m in machines if m.revoked_at is None]
    routed = {m.id: m for m in machines if m.routing != "inbox-only"}
    if not routed:
        return []
    ids = list(routed)
    live_cut, known_cut = now - timedelta(seconds=LIVE_SECONDS), now - timedelta(days=KNOWN_DAYS)
    live: dict = {}
    for row in db.scalars(select(MachineAgent).where(
            MachineAgent.agent_id.in_(ids), MachineAgent.seen_at > live_cut, ~MachineAgent.name.startswith("-"))):
        live.setdefault((row.agent_id, row.name), []).append(row)
    fresh = {p.agent_id for p in db.scalars(select(AgentPresence).where(
        AgentPresence.agent_id.in_(ids), AgentPresence.seen_at > now - timedelta(seconds=TTL_SECONDS)))}
    out: dict = {}
    for (machine_id, name), entries in live.items():
        if len(entries) == 1 and entries[0].reachability in DELIVERABLE and not entries[0].ambiguous:
            e = entries[0]
            status = WORDS.get(e.status, "offline") if machine_id in fresh else "offline"
            out[(machine_id, name)] = ListedAgent(routed[machine_id], name, e.reachability, status)
    for row in db.scalars(select(KnownAgent).where(
            KnownAgent.agent_id.in_(ids), KnownAgent.last_seen_at > known_cut, ~KnownAgent.name.startswith("-"))):
        key = (row.agent_id, row.name)
        if key not in live:  # a live, undeliverable name is refused even when known (step 2 before step 3)
            out[key] = ListedAgent(routed[row.agent_id], row.name, row.reachability, "offline")
    return sorted(out.values(), key=lambda a: (a.machine.handle, a.name))
