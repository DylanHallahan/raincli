"""Advisory team presence, authenticated to the publishing agent identity."""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from . import identity
from .messaging import MessagingError
from .models import AgentPresence

TTL_SECONDS = 120
STATES = {"ready", "busy", "blocked", "offline", "unknown"}


def publish(session, agent, data):
    if not isinstance(data, dict) or set(data) != {"status"} or not isinstance(data.get("status"), str) or data["status"] not in STATES:
        raise MessagingError(400, "invalid", "presence requires only a valid status")
    now = identity.now()  # the server clock shared with the web view
    values = dict(agent_id=agent.id, status=data["status"], seen_at=now)
    session.execute(insert(AgentPresence).values(**values).on_conflict_do_update(
        index_elements=[AgentPresence.agent_id], set_={"status": data["status"], "seen_at": now}))
    return {"status": data["status"], "seen_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=TTL_SECONDS)).isoformat()}


def directory(session, agents):
    ids = [a.id for a in agents]
    rows = session.scalars(select(AgentPresence).where(AgentPresence.agent_id.in_(ids))) if ids else []
    by_id = {r.agent_id: r for r in rows}
    now = identity.now()  # the server clock shared with the web view
    out = []
    for agent in agents:
        row = by_id.get(agent.id)
        expires = row.seen_at + timedelta(seconds=TTL_SECONDS) if row else None
        status = row.status if row and expires > now else ("offline" if row else "unknown")
        if agent.revoked_at is not None:
            status = "offline"
        out.append({"handle": agent.handle, "display_name": agent.display_name,
                    "active": agent.revoked_at is None,
                    "presence": {"status": status, "seen_at": row.seen_at.isoformat() if row else None,
                                 "expires_at": expires.isoformat() if expires else None}})
    return out
