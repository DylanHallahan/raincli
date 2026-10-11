"""The app-mode Team section (protocol §19.5): teammates' deliverable agents with a presence word.

An agent is listed while it is deliverable by name (§16.2): in its machine's last snapshot as ``instant`` or
``next-turn`` and not ambiguous, or remembered in ``known_agents`` within the §16.12 C12 window. Presence
follows §19.3: idle is Ready, working or blocked is Busy, anything else is Offline, and every agent is
Offline once its machine hasn't reported for two minutes. Three queries for the whole list, none per agent.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from raincli_server import identity, owners
from raincli_server.models import Agent, AgentPresence, KnownAgent, MachineAgent
from raincli_server.presence import TTL_SECONDS
from raincli_server.routing import KNOWN_DAYS

DELIVERABLE = ("instant", "next-turn")
WORDS = {"idle": "ready", "working": "busy", "blocked": "busy"}


def team_agents(db, viewer) -> list[dict]:
    """``[{person, agent, handle, to, status, team}]`` sorted by person, then agent."""
    team_ids = [team.id for team, _ in viewer.teams]
    if not team_ids:
        return []
    machines = {a.id: a for a in db.scalars(select(Agent).where(
        Agent.team_id.in_(team_ids), Agent.revoked_at.is_(None), Agent.owner_user_id != viewer.user.id))}
    if not machines:
        return []
    ids = list(machines)
    now = identity.now()
    fresh = {p.agent_id for p in db.scalars(select(AgentPresence).where(
        AgentPresence.agent_id.in_(ids), AgentPresence.seen_at > now - timedelta(seconds=TTL_SECONDS)))}
    found: dict = {}
    for row in db.scalars(select(MachineAgent).where(
            MachineAgent.agent_id.in_(ids), MachineAgent.reachability.in_(DELIVERABLE),
            MachineAgent.ambiguous.is_(False), ~MachineAgent.name.startswith("-"))):
        live = row.agent_id in fresh and row.seen_at > now - timedelta(seconds=TTL_SECONDS)
        found[(row.agent_id, row.name)] = WORDS.get(row.status, "offline") if live else "offline"
    for row in db.scalars(select(KnownAgent).where(
            KnownAgent.agent_id.in_(ids), KnownAgent.last_seen_at > now - timedelta(days=KNOWN_DAYS),
            ~KnownAgent.name.startswith("-"))):
        found.setdefault((row.agent_id, row.name), "offline")
    shown = {team_id: owners.displays(db, team_id) for team_id in {m.team_id for m in machines.values()}}
    teams = {team.id: team.name for team, _ in viewer.teams}
    out = []
    for (machine_id, name), status in found.items():
        machine = machines[machine_id]
        person = shown[machine.team_id].get(machine.owner_user_id) or machine.handle
        out.append({"person": person, "agent": name, "handle": machine.handle, "to": f"{machine.handle}/{name}",
                    "status": status, "team": teams.get(machine.team_id, "")})
    out.sort(key=lambda r: (r["person"].casefold(), r["agent"].casefold(), r["handle"]))
    return out
