"""The app-mode Team section (protocol §19.5): teammates' deliverable agents with a presence word.

Which agents: the one listing rule (§20.6 M3, ``web/listing.py``), so an agent is listed exactly when it would be
accepted as a recipient now. Presence follows §19.8 L3: ``ready``, ``busy`` (working), ``blocked`` (both shown as
Busy, with their own tooltips), ``unknown`` (no presence word) and ``offline``.
"""

from __future__ import annotations

from sqlalchemy import select

from raincli_server import owners
from raincli_server.models import Agent
from raincli_server.web.listing import listed_agents


def team_agents(db, viewer, now=None) -> list[dict]:
    """``[{person, agent, handle, to, status, team}]`` sorted by person, then agent. ``now``: the clock
    (§19.8 L6), the server's by default."""
    team_ids = [team.id for team, _ in viewer.teams]
    if not team_ids:
        return []
    machines = list(db.scalars(select(Agent).where(
        Agent.team_id.in_(team_ids), Agent.revoked_at.is_(None), Agent.owner_user_id != viewer.user.id)))
    if not machines:
        return []
    shown = {team_id: owners.displays(db, team_id) for team_id in {m.team_id for m in machines}}
    teams = {team.id: team.name for team, _ in viewer.teams}
    out = []
    for a in listed_agents(db, machines, now=now):
        person = shown[a.machine.team_id].get(a.machine.owner_user_id) or a.machine.handle
        out.append({"person": person, "agent": a.name, "handle": a.machine.handle, "to": f"{a.machine.handle}/{a.name}",
                    "status": a.status, "team": teams.get(a.machine.team_id, "")})
    out.sort(key=lambda r: (r["person"].casefold(), r["agent"].casefold(), r["handle"]))
    return out
