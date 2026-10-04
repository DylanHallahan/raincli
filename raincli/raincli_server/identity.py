"""Shared identity services: users, teams, invitations, agents and credentials.

Used by the API, the web UI and the admin CLI. Every function takes an open
SQLAlchemy Session and does not commit; callers own the transaction.
Raw tokens are returned exactly once and never stored.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from raincli_server import security
from raincli_server.models import (SCOPES, Agent, AgentCredential, Invitation, Membership, Message, Team, User,
                                   WebSession)

INVITE_TTL = timedelta(days=7)


class IdentityError(ValueError):
    """Invalid input or state; message is safe to show to the acting user."""


class PermissionDenied(IdentityError):
    pass


@dataclass(frozen=True)
class AgentAuth:
    agent: Agent
    credential: AgentCredential
    team: Team

    def has_scope(self, scope: str) -> bool:
        return scope in self.credential.scopes


def now() -> datetime:
    return datetime.now(timezone.utc)


# Users and teams -----------------------------------------------------------

def create_user(session: Session, email: str, display_name: str, password: str) -> User:
    email = email.strip()
    if not security.valid_email(email):
        raise IdentityError("enter a valid email address")
    if not security.valid_display_name(display_name):
        raise IdentityError("enter a display name (1-80 characters)")
    if not security.valid_password(password):
        raise IdentityError("password must be 12-256 characters")
    if find_user_by_email(session, email) is not None:
        raise IdentityError("an account with that email already exists")
    user = User(email=email, display_name=display_name.strip(), password_hash=security.hash_password(password))
    session.add(user)
    session.flush()
    return user


def find_user_by_email(session: Session, email: str) -> User | None:
    from sqlalchemy import func

    return session.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))


def authenticate_user(session: Session, email: str, password: str) -> User | None:
    from sqlalchemy import func

    # Serialize login/rehash with password changes so an old-password login cannot
    # create a new session after another transaction has revoked existing sessions.
    user = session.scalar(select(User).where(func.lower(User.email) == email.strip().lower())
                          .with_for_update().execution_options(populate_existing=True))
    if user is None or not user.is_active:
        security.hash_password(password)  # equalize timing for unknown users
        return None
    if not security.verify_password(password, user.password_hash):
        return None
    if security.password_needs_upgrade(user.password_hash):
        user.password_hash = security.hash_password(password)
        session.flush()
    return user


def change_password(session: Session, user_id: uuid.UUID, web_session_id: uuid.UUID,
                    current: str, replacement: str) -> User:
    user = session.scalar(select(User).where(User.id == user_id).with_for_update()
                          .execution_options(populate_existing=True))
    ws = session.scalar(select(WebSession).where(WebSession.id == web_session_id)
                        .execution_options(populate_existing=True))
    if (user is None or not user.is_active or ws is None or ws.user_id != user_id
            or ws.revoked_at is not None or ws.expires_at <= now()):
        raise PermissionDenied("your session has ended; sign in again")
    if len(current) > 256 or not security.verify_password(current, user.password_hash):
        raise IdentityError("the current password is not correct")
    if not security.valid_password(replacement):
        raise IdentityError("password must be 12-256 characters")
    if current == replacement:
        raise IdentityError("choose a different password")
    user.password_hash = security.hash_password(replacement)
    session.execute(update(WebSession).where(WebSession.user_id == user_id,
                                            WebSession.revoked_at.is_(None)).values(revoked_at=now()))
    session.flush()
    return user


def create_team(session: Session, slug: str, name: str, owner: User) -> Team:
    if not security.valid_slug(slug):
        raise IdentityError("team slug must match ^[a-z][a-z0-9-]{1,39}$")
    if not security.valid_display_name(name):
        raise IdentityError("enter a team name (1-80 characters)")
    if session.scalar(select(Team).where(Team.slug == slug)) is not None:
        raise IdentityError("that team slug is taken")
    team = Team(slug=slug, name=name.strip())
    session.add(team)
    session.flush()
    session.add(Membership(team_id=team.id, user_id=owner.id, role="owner"))
    session.flush()
    return team


def membership(session: Session, team_id: uuid.UUID, user_id: uuid.UUID) -> Membership | None:
    return session.get(Membership, (team_id, user_id))


def teams_for_user(session: Session, user: User) -> list[tuple[Team, str]]:
    rows = session.execute(
        select(Team, Membership.role).join(Membership, Membership.team_id == Team.id)
        .where(Membership.user_id == user.id).order_by(Team.name)
    )
    return [(team, role) for team, role in rows]


def add_member(session: Session, team: Team, user: User, role: str = "member") -> Membership:
    if role not in ("owner", "member"):
        raise IdentityError("role must be owner or member")
    existing = membership(session, team.id, user.id)
    if existing is not None:
        return existing
    m = Membership(team_id=team.id, user_id=user.id, role=role)
    session.add(m)
    session.flush()
    return m


# Invitations ----------------------------------------------------------------

def create_invitation(
    session: Session, team: Team, inviter: User, email: str | None = None, role: str = "member"
) -> tuple[Invitation, str]:
    m = membership(session, team.id, inviter.id)
    if m is None or m.role != "owner":
        raise PermissionDenied("only team owners can invite")
    if email and not security.valid_email(email):
        raise IdentityError("enter a valid email address or leave it blank")
    if role not in ("owner", "member"):
        raise IdentityError("role must be owner or member")
    token = security.new_token(security.INVITE_TOKEN_PREFIX)
    inv = Invitation(
        team_id=team.id, email=email or None, role=role, token_hash=security.hash_token(token),
        invited_by=inviter.id, expires_at=now() + INVITE_TTL,
    )
    session.add(inv)
    session.flush()
    return inv, token


def find_open_invitation(session: Session, token: str) -> Invitation | None:
    if not isinstance(token, str) or not token.startswith(security.INVITE_TOKEN_PREFIX):
        return None
    inv = session.scalar(
        select(Invitation).where(Invitation.token_hash == security.hash_token(token)).with_for_update()
    )
    if inv is None or inv.accepted_at or inv.revoked_at or inv.expires_at <= now():
        return None
    return inv


def accept_invitation(
    session: Session, token: str, *, user: User | None = None,
    email: str | None = None, display_name: str | None = None, password: str | None = None,
) -> tuple[User, Team]:
    """Accept as an existing logged-in ``user`` or create a new account."""
    inv = find_open_invitation(session, token)
    if inv is None:
        raise IdentityError("this invitation is invalid, expired or already used")
    if user is None:
        user = create_user(session, email or "", display_name or "", password or "")
    if inv.email and inv.email.lower() != user.email.lower():
        raise IdentityError("this invitation was issued for a different email address")
    team = session.get(Team, inv.team_id)
    add_member(session, team, user, inv.role)
    inv.accepted_at = now()
    inv.accepted_by = user.id
    session.flush()
    return user, team


def revoke_invitation(session: Session, invitation_id: uuid.UUID, actor: User) -> None:
    inv = session.get(Invitation, invitation_id)
    if inv is None:
        raise IdentityError("invitation not found")
    m = membership(session, inv.team_id, actor.id)
    if m is None or m.role != "owner":
        raise PermissionDenied("only team owners can revoke invitations")
    if inv.revoked_at is None and inv.accepted_at is None:
        inv.revoked_at = now()
        session.flush()


# Agents and credentials -----------------------------------------------------

def register_agent(
    session: Session, team: Team, owner: User, handle: str, display_name: str | None = None,
    scopes: tuple[str, ...] = SCOPES,
) -> tuple[Agent, str]:
    if membership(session, team.id, owner.id) is None:
        raise PermissionDenied("you are not a member of that team")
    if not security.valid_handle(handle):
        raise IdentityError("handle must match ^[a-z][a-z0-9-]{1,31}$")
    display_name = (display_name or handle).strip()
    if not security.valid_display_name(display_name):
        raise IdentityError("enter a display name (1-80 characters)")
    if session.scalar(select(Agent).where(Agent.team_id == team.id, Agent.handle == handle)) is not None:
        raise IdentityError("that handle is already registered in this team")
    agent = Agent(team_id=team.id, owner_user_id=owner.id, handle=handle, display_name=display_name)
    session.add(agent)
    session.flush()
    token = _issue_credential(session, agent, scopes)
    return agent, token


def _issue_credential(session: Session, agent: Agent, scopes: tuple[str, ...]) -> str:
    bad = [s for s in scopes if s not in SCOPES]
    if bad or not scopes:
        raise IdentityError(f"invalid scopes: {bad or 'none'}")
    token = security.new_token(security.AGENT_TOKEN_PREFIX)
    session.add(AgentCredential(
        agent_id=agent.id, token_hash=security.hash_token(token), prefix=security.token_prefix(token),
        scopes=list(scopes),
    ))
    session.flush()
    return token


def can_manage_agent(session: Session, agent: Agent, actor: User) -> bool:
    if agent.owner_user_id == actor.id:
        return True
    m = membership(session, agent.team_id, actor.id)
    return m is not None and m.role == "owner"


ROTATED_BY = ("app-login", "website", "operator")


def rotate_agent_credential(session: Session, agent: Agent, actor: User | None = None, *,
                            by: str | None = None) -> str:
    """Issue a new credential and revoke all previous ones immediately.

    ``actor=None`` means the operator (admin CLI). ``by`` records who rotated (protocol §15.8 H2);
    it defaults to ``website`` with an actor and ``operator`` without.
    """
    by = by or ("website" if actor is not None else "operator")
    if by not in ROTATED_BY:
        raise ValueError(f"unknown rotation source {by!r}")
    if actor is not None and not can_manage_agent(session, agent, actor):
        raise PermissionDenied("you cannot manage that agent")
    if agent.revoked_at is not None:
        raise IdentityError("agent is revoked; register a new agent instead")
    scopes = session.scalar(
        select(AgentCredential.scopes).where(AgentCredential.agent_id == agent.id)
        .order_by(AgentCredential.created_at.desc()).limit(1)
    ) or list(SCOPES)
    session.execute(
        update(AgentCredential).where(AgentCredential.agent_id == agent.id, AgentCredential.revoked_at.is_(None))
        .values(revoked_at=now())
    )
    agent.rotated_at, agent.rotated_by = now(), by
    return _issue_credential(session, agent, tuple(scopes))


def revoke_agent(session: Session, agent: Agent, actor: User | None = None) -> None:
    if actor is not None and not can_manage_agent(session, agent, actor):
        raise PermissionDenied("you cannot manage that agent")
    ts = now()
    if agent.revoked_at is None:
        agent.revoked_at = ts
    session.execute(
        update(AgentCredential).where(AgentCredential.agent_id == agent.id, AgentCredential.revoked_at.is_(None))
        .values(revoked_at=ts)
    )
    session.flush()


class NameTaken(IdentityError):
    """The machine name belongs to another member, or to a revoked machine (protocol §15.1)."""


class NameInUse(IdentityError):
    """The user's own active machine has that name, and the request lacks proof to replace it (§15.8 H2)."""


def has_delivery_history(session: Session, agent: Agent) -> bool:
    """True once the machine was a message recipient or published an inbox role (§15.8 H2)."""
    if agent.inbox_role_at is not None:
        return True
    return session.scalar(select(Message.id).where(Message.recipient_agent_id == agent.id).limit(1)) is not None


def sign_in_machine(session: Session, team: Team, user: User, machine_name: str, *,
                    previous_token: str | None = None, replace: bool = False) -> tuple[Agent, str, bool]:
    """Create or re-sign-in the machine ``machine_name`` for ``user`` (protocol §15.1, §15.8 H2).

    Returns ``(agent, raw_token, rotated)``. A new name creates a machine owned by ``user`` with one
    credential. The user's own active machine of that name is rotated (every older credential revoked)
    only with proof: ``previous_token``, a currently valid credential of that machine, or ``replace``
    for a machine with no delivery history. Without proof this raises ``NameInUse``. Any other holder
    of the name, or a revoked machine, raises ``NameTaken``.
    """
    if membership(session, team.id, user.id) is None:
        raise PermissionDenied("you are not a member of that team")
    if not security.valid_handle(machine_name):
        raise IdentityError("machine_name must match ^[a-z][a-z0-9-]{1,31}$")
    existing = session.scalar(select(Agent).where(Agent.team_id == team.id, Agent.handle == machine_name)
                              .with_for_update().execution_options(populate_existing=True))
    if existing is None:
        agent = Agent(team_id=team.id, owner_user_id=user.id, handle=machine_name, display_name=machine_name,
                      signed_in_from=machine_name)
        try:
            with session.begin_nested():
                session.add(agent)
                session.flush()
        except IntegrityError:
            # A concurrent sign-in created the name first; treat it like any existing machine.
            existing = session.scalar(select(Agent).where(Agent.team_id == team.id, Agent.handle == machine_name)
                                      .with_for_update().execution_options(populate_existing=True))
            if existing is None:
                raise
        else:
            return agent, _issue_credential(session, agent, SCOPES), False
    if existing.owner_user_id != user.id or existing.revoked_at is not None:
        raise NameTaken("that machine name is taken in this team")
    proven = previous_token is not None and _is_live_credential_of(session, existing, previous_token)
    if not proven and not (replace and not has_delivery_history(session, existing)):
        raise NameInUse("you already have a machine with that name")
    return existing, rotate_agent_credential(session, existing, by="app-login"), True


def _is_live_credential_of(session: Session, agent: Agent, token: str) -> bool:
    auth = authenticate_agent(session, token, touch=False)
    return auth is not None and auth.agent.id == agent.id


# Member removal and user disable (protocol §11.5, §12.5) --------------------------

def _revoke_web_sessions(session: Session, user: User) -> None:
    session.execute(
        update(WebSession).where(WebSession.user_id == user.id, WebSession.revoked_at.is_(None))
        .values(revoked_at=now())
    )


def _revoke_open_invitations(session: Session, user: User, team_id: uuid.UUID | None = None) -> None:
    """Revoke invitations ``user`` created that are still open (protocol §12.5)."""
    stmt = update(Invitation).where(
        Invitation.invited_by == user.id, Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None))
    if team_id is not None:
        stmt = stmt.where(Invitation.team_id == team_id)
    session.execute(stmt.values(revoked_at=now()))


def _revoke_owned_agents(session: Session, user: User, team_id: uuid.UUID | None = None) -> None:
    stmt = select(Agent).where(Agent.owner_user_id == user.id)
    if team_id is not None:
        stmt = stmt.where(Agent.team_id == team_id)
    for agent in session.scalars(stmt):
        revoke_agent(session, agent)


def remove_member(session: Session, team: Team, user: User, actor: User | None = None) -> None:
    """Remove ``user`` from ``team``: revoke their agents and open invitations in that team, and all
    their web sessions.

    Only team owners may do this, or the operator (``actor=None``). The last owner cannot be removed.
    """
    if actor is not None:
        m = membership(session, team.id, actor.id)
        if m is None or m.role != "owner":
            raise PermissionDenied("only team owners can remove members")
    # Lock the team's memberships so two concurrent removals cannot both pass the last-owner check.
    rows = session.scalars(
        select(Membership).where(Membership.team_id == team.id).with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    target = next((m for m in rows if m.user_id == user.id), None)
    if target is None:
        raise IdentityError("that user is not a member of this team")
    if target.role == "owner" and sum(1 for m in rows if m.role == "owner") <= 1:
        raise IdentityError("cannot remove the last owner of a team")
    session.delete(target)
    _revoke_owned_agents(session, user, team.id)
    _revoke_open_invitations(session, user, team.id)
    _revoke_web_sessions(session, user)
    session.flush()


def set_user_active(session: Session, user: User, active: bool, actor: User | None = None) -> None:
    """Operator only (``actor`` must be None). Disabling revokes web sessions, every owned agent and
    every open invitation the user created."""
    if actor is not None:
        raise PermissionDenied("only the operator can enable or disable users")
    user.is_active = bool(active)
    if not active:
        _revoke_web_sessions(session, user)
        _revoke_owned_agents(session, user)
        _revoke_open_invitations(session, user)
    session.flush()


def find_agent(session: Session, team: Team, handle: str) -> Agent | None:
    return session.scalar(select(Agent).where(Agent.team_id == team.id, Agent.handle == handle))


def authenticate_agent(session: Session, token: str | None, touch: bool = True) -> AgentAuth | None:
    """Resolve a bearer token. Returns None for missing/unknown/revoked credentials or agents."""
    if not token or not token.startswith(security.AGENT_TOKEN_PREFIX) or len(token) > 200:
        return None
    row = session.execute(
        select(AgentCredential, Agent, Team)
        .join(Agent, Agent.id == AgentCredential.agent_id)
        .join(Team, Team.id == Agent.team_id)
        .where(AgentCredential.token_hash == security.hash_token(token))
    ).first()
    if row is None:
        return None
    cred, agent, team = row
    if cred.revoked_at is not None or agent.revoked_at is not None:
        return None
    if touch:
        ts = now()
        if cred.last_used_at is None or ts - cred.last_used_at > timedelta(seconds=30):
            cred.last_used_at = ts
    return AgentAuth(agent=agent, credential=cred, team=team)
