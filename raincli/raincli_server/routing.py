"""Endpoints and send-time routing (protocol §16.1, §16.2, §16.5, §16.12 C7, C8, C12).

An endpoint is a machine (its inbox), a named agent on a machine, or a person. The server is the only
router: it resolves an endpoint in the sender's team, applies the machine's routing policy and the
§16.2 order for agent endpoints, and derives the holds a sender sees before the recipient acks.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from raincli_server import identity, security
from raincli_server.messaging_errors import MessagingError
from raincli_server.models import Agent, KnownAgent, MachineAgent, Membership, User

LIVE_SECONDS = 120  # the directory's liveness window (§14.2)
KNOWN_DAYS = 14  # §16.12 C12: step 4 accepts names seen within 14 days
KNOWN_RETENTION_DAYS = 30  # known_agents rows are pruned after 30 days (§16.2)
DELIVERABLE = ("instant", "next-turn")


def invalid(message: str) -> MessagingError:
    return MessagingError(400, "invalid", message)


BAD_RECIPIENT = "recipient must be another active agent, agent name or member of your team"


@dataclass(frozen=True)
class Endpoint:
    """One side of a message: ``machine`` (agent set), ``agent`` (agent and name set) or ``person``."""

    kind: str
    agent: Agent | None = None
    name: str | None = None
    user: User | None = None

    @property
    def key(self) -> str:
        if self.kind == "person":
            return f"p:{self.user.id}"
        if self.kind == "agent":
            return f"a:{self.agent.id}:{self.name}"
        return f"m:{self.agent.id}"

    @property
    def machine_id(self) -> uuid.UUID | None:
        return self.agent.id if self.agent is not None else None

    def json(self) -> dict:
        if self.kind == "person":
            return {"person": self.user.email, "display_name": self.user.display_name}
        if self.kind == "agent":
            return {"machine": self.agent.handle, "agent": self.name}
        return {"machine": self.agent.handle}

    def label(self) -> str:
        """The §16.1 CLI form: ``handle``, ``handle/name`` or ``@email``."""
        if self.kind == "person":
            return "@" + self.user.email
        if self.kind == "agent":
            return f"{self.agent.handle}/{self.name}"
        return self.agent.handle


@dataclass(frozen=True)
class EndpointRef:
    """A parsed, not yet resolved, endpoint from a request."""

    kind: str
    handle: str | None = None
    name: str | None = None
    email: str | None = None


def parse_endpoint(value: object) -> EndpointRef:
    """§16.1 API forms: ``"handle"``, ``{"machine"}``, ``{"machine", "agent"}`` or ``{"person"}``."""
    from raincli_server.presence import valid_agent_name

    if isinstance(value, str):
        value = {"machine": value}
    if not isinstance(value, dict) or not value:
        raise invalid('to must be a handle or {"machine": ...}, {"machine": ..., "agent": ...} or {"person": ...}')
    keys = set(value)
    if keys == {"person"}:
        email = value["person"]
        if not isinstance(email, str) or not security.valid_email(email.strip()):
            raise invalid(BAD_RECIPIENT)
        return EndpointRef("person", email=email.strip())
    if keys in ({"machine"}, {"machine", "agent"}):
        handle = value["machine"]
        if not security.valid_handle(handle):
            raise invalid(BAD_RECIPIENT)
        if "agent" not in value:
            return EndpointRef("machine", handle=handle)
        name = value["agent"]
        if not valid_agent_name(name) or name != name.strip():
            raise invalid("agent must be a directory name: 1-64 characters, no control or format characters")
        return EndpointRef("agent", handle=handle, name=name)
    raise invalid('to must be a handle or {"machine": ...}, {"machine": ..., "agent": ...} or {"person": ...}')


def member(session: Session, team_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return session.get(Membership, (team_id, user_id)) is not None


def resolve(session: Session, team_id: uuid.UUID, ref: EndpointRef, *, lock: bool = False) -> Endpoint:
    """The endpoint in this team. Unknown, foreign, revoked and inactive cases share one ``400 invalid``."""
    if ref.kind == "person":
        stmt = select(User).where(func.lower(User.email) == ref.email.lower())
        if lock:
            stmt = stmt.with_for_update(key_share=True).execution_options(populate_existing=True)
        user = session.scalar(stmt)
        if user is None or not user.is_active or not member(session, team_id, user.id):
            raise invalid(BAD_RECIPIENT)
        return Endpoint("person", user=user)
    stmt = select(Agent).where(Agent.team_id == team_id, Agent.handle == ref.handle)
    if lock:
        # Serializes sends to one machine (capacity and cursor order), as before (§2).
        stmt = stmt.with_for_update(key_share=True).execution_options(populate_existing=True)
    agent = session.scalar(stmt)
    if agent is None or agent.revoked_at is not None:
        raise invalid(BAD_RECIPIENT)
    if ref.kind == "agent":
        return Endpoint("agent", agent=agent, name=ref.name)
    return Endpoint("machine", agent=agent)


def live_entries(session: Session, agent_id: uuid.UUID, name: str) -> list[MachineAgent]:
    cutoff = identity.now() - timedelta(seconds=LIVE_SECONDS)
    return list(session.scalars(select(MachineAgent).where(
        MachineAgent.agent_id == agent_id, MachineAgent.name == name, MachineAgent.seen_at > cutoff)))


def known_agent(session: Session, agent_id: uuid.UUID, name: str, days: int = KNOWN_DAYS) -> KnownAgent | None:
    cutoff = identity.now() - timedelta(days=days)
    return session.scalar(select(KnownAgent).where(
        KnownAgent.agent_id == agent_id, KnownAgent.name == name, KnownAgent.last_seen_at > cutoff))


def deliverable(entry: MachineAgent) -> bool:
    return entry.reachability in DELIVERABLE and not entry.ambiguous


def check_agent_route(session: Session, endpoint: Endpoint) -> None:
    """The §16.2 order for an agent endpoint. Returns when accepted; raises the refusal otherwise."""
    machine = endpoint.agent
    if machine.routing == "inbox-only":
        raise MessagingError(400, "routing_inbox_only",
                             f"{machine.handle} accepts messages only at its inbox; send to {machine.handle}")
    live = live_entries(session, machine.id, endpoint.name)
    if len(live) == 1 and deliverable(live[0]):
        return
    if live:
        reason = "ambiguous" if len(live) > 1 or any(e.ambiguous for e in live) else "listed_only"
        error = MessagingError(400, "not_deliverable",
                               f"{endpoint.label()} can't receive messages ({reason.replace('_', ' ')})")
        error.extra = {"reason": reason}
        raise error
    if known_agent(session, machine.id, endpoint.name) is not None:
        return
    raise MessagingError(400, "unknown_agent", f"{endpoint.label()} is not a known agent on {machine.handle}")


def derived_hold(session: Session, recipient: Agent | None, agent_name: str | None,
                 sender_is_person: bool) -> str | None:
    """§16.12 C8: the hold a sender sees for a ``stored`` message before the recipient acks, or None."""
    if recipient is None:
        return None
    if recipient.routing_capable_at is None and (agent_name is not None or sender_is_person):
        return "client_update_needed"
    if agent_name is not None and not any(deliverable(e) for e in live_entries(session, recipient.id, agent_name)):
        return "offline"
    return None
