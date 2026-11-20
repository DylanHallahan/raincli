"""Shared identity services: users, teams, invitations, agents and credentials.

Used by the API, the web UI and the admin CLI. Every function takes an open
SQLAlchemy Session and does not commit; callers own the transaction.
Raw tokens are returned exactly once and never stored.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from raincli_server import security
from raincli_server.models import (PERSON_SCOPES, SCOPES, Agent, AgentCredential, Grant, Invitation, Membership,
                                   Message, PersonSession, Team, User, WebSession)

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
    if not security.valid_person_name(display_name):
        raise IdentityError("enter a display name (1-80 characters, without ( ) @ or invisible or direction "
                            "characters)")
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


def bump_credential_epoch(user: User) -> None:
    """Every credential change (a password change, a reset, a disable) calls this: recognised-device tokens
    name the epoch they were issued under, so all of them stop counting."""
    user.credential_epoch = (user.credential_epoch or 0) + 1


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
    bump_credential_epoch(user)  # §18.1: ends every recognised device
    # §23 (A14 M4, AS11): the account's own password isn't involved in a grant, so the windows and sessions delegates
    # hold as this user survive it; the user's own sessions end, and with them any delegations they made.
    delegated = select(PersonSession.id).where(PersonSession.acting_user_id.is_not(None))
    session.execute(update(WebSession).where(
        WebSession.user_id == user_id, WebSession.revoked_at.is_(None),
        or_(WebSession.person_session_id.is_(None), WebSession.person_session_id.not_in(delegated)))
        .values(revoked_at=now()))
    revoke_person_sessions(session, user_id=user_id, include_delegated=False)  # §16.3, §16.12 C6
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
    # The membership row lock serializes this with the user's app sign-ins, so the cap holds (§15.9, §18.1).
    member = session.scalar(select(Membership).where(Membership.team_id == team.id, Membership.user_id == owner.id)
                            .with_for_update())
    if member is None:
        raise PermissionDenied("you are not a member of that team")
    if not security.valid_handle(handle):
        raise IdentityError("handle must match ^[a-z][a-z0-9-]{1,31}$")
    _check_machine_limit(session, team, owner)
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
    revoke_person_sessions(session, machine_id=agent.id)  # §16.12 C6: any rotation of the machine's credential
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
    revoke_person_sessions(session, machine_id=agent.id)  # §16.3: revoking or signing out the machine
    from raincli_server import messaging  # §21 C13 M5d: nobody's inbox any more; their messages wait again
    messaging.clear_inboxes_on(session, agent)
    session.flush()


class NameTaken(IdentityError):
    """The machine name belongs to another member, or to a revoked machine (protocol §15.1)."""


class MachineLimit(IdentityError):
    """The user already has MACHINE_LIMIT active machines in the team (protocol §15.9)."""


MACHINE_LIMIT = 20


def _check_machine_limit(session: Session, team: Team, user: User) -> None:
    """Every way of adding a machine (the app or CLI sign-in, the website, the operator) is capped."""
    active = session.scalar(select(func.count()).select_from(Agent).where(
        Agent.team_id == team.id, Agent.owner_user_id == user.id, Agent.revoked_at.is_(None)))
    if active >= MACHINE_LIMIT:
        raise MachineLimit(f"you already have {MACHINE_LIMIT} active machines in this team")


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
    # Locking the membership row serializes this user's sign-ins in this team, so the cap holds.
    member = session.scalar(select(Membership).where(Membership.team_id == team.id, Membership.user_id == user.id)
                            .with_for_update())
    if member is None:
        raise PermissionDenied("you are not a member of that team")
    if not security.valid_handle(machine_name):
        raise IdentityError("machine_name must match ^[a-z][a-z0-9-]{1,31}$")
    existing = session.scalar(select(Agent).where(Agent.team_id == team.id, Agent.handle == machine_name)
                              .with_for_update().execution_options(populate_existing=True))
    if existing is None:
        _check_machine_limit(session, team, user)
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


# Person sessions (protocol §16.3, §16.12 C6, C15) --------------------------------------

PERSON_IDLE = timedelta(days=30)
PERSON_ABSOLUTE = timedelta(days=180)


@dataclass(frozen=True)
class PersonAuth:
    """A person session. ``team_id``: the team of the machine it was issued to, the only team it covers
    (§21.13: an app sign-in covers the team of the machine it's signed in on)."""

    person_session: PersonSession
    user: User
    team_id: uuid.UUID
    acting_user: User | None = None  # §23: the grantee, when the session is delegated (``user`` is the account)

    @property
    def delegated(self) -> bool:
        return self.acting_user is not None

    def has_scope(self, scope: str) -> bool:
        return scope in self.person_session.scopes


def purge_handoff_codes(session: Session) -> int:
    """Delete handoff codes that were used or have expired (§18.1 item 5); returns how many."""
    from raincli_server.models import HandoffCode

    result = session.execute(delete(HandoffCode).where(
        (HandoffCode.used_at.is_not(None)) | (HandoffCode.expires_at <= now())))
    return result.rowcount or 0


def person_session_expires_at(ps: PersonSession) -> datetime:
    return min(ps.last_used_at + PERSON_IDLE, ps.created_at + PERSON_ABSOLUTE)


def issue_person_session(session: Session, user: User, machine: Agent) -> str:
    """A new person session for ``user``, bound to the issuing ``machine``. The raw token is returned once."""
    if machine.owner_user_id != user.id or machine.revoked_at is not None:
        raise PermissionDenied("a person session is issued only to the owner of an active machine")
    # §23 (A14 M5): a new own sign-in on the machine ends the delegated sessions of the previous ones; the app adds
    # its accounts again under the new session.
    for child_id in session.scalars(select(PersonSession.id).where(
            PersonSession.machine_agent_id == machine.id, PersonSession.acting_user_id.is_not(None),
            PersonSession.revoked_at.is_(None))).all():
        revoke_person_sessions(session, session_id=child_id)
    token = security.new_token(security.PERSON_TOKEN_PREFIX)
    session.add(PersonSession(user_id=user.id, machine_agent_id=machine.id, token_hash=security.hash_token(token),
                              prefix=security.token_prefix(token), scopes=list(PERSON_SCOPES)))
    session.flush()
    return token


def authenticate_person(session: Session, token: str | None, touch: bool = True) -> PersonAuth | None:
    """Resolve an ``rps_`` bearer token. None for missing, unknown, revoked, expired or disabled."""
    if not token or not token.startswith(security.PERSON_TOKEN_PREFIX) or len(token) > 200:
        return None
    row = session.execute(
        select(PersonSession, User).join(User, User.id == PersonSession.user_id)
        .where(PersonSession.token_hash == security.hash_token(token))
    ).first()
    if row is None:
        return None
    ps, user = row
    machine = session.get(Agent, ps.machine_agent_id)
    if ps.revoked_at is not None or not user.is_active or machine is None or machine.revoked_at is not None:
        return None
    ts = now()
    if ts >= person_session_expires_at(ps) or not delegated_ok(session, ps, ts):
        return None
    acting = session.get(User, ps.acting_user_id) if ps.delegated else None
    if touch:
        # §23 (A14 Q5): using a delegated session also keeps its parent (the grantee's own session) alive.
        parent = session.get(PersonSession, ps.parent_session_id) if ps.delegated else None
        for row_ in (ps, parent):
            if row_ is not None and ts - row_.last_used_at > timedelta(seconds=30):
                row_.last_used_at = ts
    return PersonAuth(person_session=ps, user=user, team_id=machine.team_id, acting_user=acting)


# Delegated sessions (protocol §23) -------------------------------------------------------

MAX_DELEGATED = 4  # §23 (AS2): at most 5 accounts in one app, its own included


class DelegationNotFound(IdentityError):
    """No live grant for that account in this session's team (or no such person): one answer for all (no oracle)."""


class TooManyAccounts(IdentityError):
    pass


def owner_view(viewer_id: uuid.UUID | None, owner_id: uuid.UUID | None, delegated: bool) -> bool:
    """§23 (A14 M1): the viewer sees a machine as its owner: they own it AND aren't acting through a grant. Under a
    grant the delegate sees the account's machines as a teammate would (no handles, agent names or raw codes)."""
    return viewer_id is not None and owner_id == viewer_id and not delegated


def session_expires_at(session: Session, ps: PersonSession) -> datetime:
    """When ``ps`` ends: its own expiry and, for a delegated session, never later than its parent's (A3, A14 Q5)."""
    ends = person_session_expires_at(ps)
    if ps.delegated:
        parent = session.get(PersonSession, ps.parent_session_id)
        if parent is not None:
            ends = min(ends, person_session_expires_at(parent))
    return ends


def delegated_ok(session: Session, ps: PersonSession, at: datetime | None = None) -> bool:
    """The belt for a delegated session (A4, A14 L1), true for an own session: its parent is a live, unexpired own
    session of the grantee on the same machine; its grant is live and names these two people in the machine's team;
    and both are active members of that team."""
    if not ps.delegated:
        return True
    ts = at or now()
    parent = session.get(PersonSession, ps.parent_session_id)
    machine = session.get(Agent, ps.machine_agent_id)
    if (parent is None or parent.delegated or parent.revoked_at is not None or ts >= person_session_expires_at(parent)
            or parent.machine_agent_id != ps.machine_agent_id or parent.user_id != ps.acting_user_id):
        return False
    if machine is None or machine.revoked_at is not None or effective_grant(session, ps) is None:  # A18.2
        return False
    return all(_active_member(session, machine.team_id, uid) for uid in (ps.user_id, ps.acting_user_id))


def effective_grant(session: Session, ps: PersonSession) -> Grant | None:
    """A18.2: what lets a delegated session act, decided at each use: the live per-user grant for (grantee, account)
    in the machine's team, else the live team-wide grant for the account if it covers the grantee (A18.1), else
    none. It governs the belt, the toasts flag (A18.4) and replies to the account's own traffic (A18.5). ``grant_id``
    records it for the audit, re-pointed when a grant is revoked."""
    if not ps.delegated:
        return None
    machine = session.get(Agent, ps.machine_agent_id)
    if machine is None or not all(_active_member(session, machine.team_id, uid)
                                  for uid in (ps.user_id, ps.acting_user_id)):
        return None  # A19 E1.3: both people active members, whichever kind
    return usable_grant(session, machine.team_id, ps.acting_user_id, ps.user_id)


def grant_kind(grant: Grant | None) -> str | None:
    """A19 D1: ``individual`` or ``all_members`` (None for no grant)."""
    return None if grant is None else ("all_members" if grant.all_members else "individual")


def _active_member(session: Session, team_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    user = session.get(User, user_id)
    return user is not None and user.is_active and membership(session, team_id, user_id) is not None


def _session_live(session: Session, ps: PersonSession, at: datetime) -> bool:
    machine = session.get(Agent, ps.machine_agent_id)
    user = session.get(User, ps.user_id)
    return (ps.revoked_at is None and machine is not None and machine.revoked_at is None and user is not None
            and user.is_active and at < session_expires_at(session, ps) and delegated_ok(session, ps, at))


def linked_sessions(session: Session, ps: PersonSession) -> list[PersonSession]:
    """THE definition of linked person sessions (§23, A8, A9): the own session at the root of ``ps``'s family (``ps``
    itself, or its parent) and its live delegated children, all on one machine; the root first, then the children
    oldest first. Empty when the root isn't live. Two sessions are linked iff both are in one such list."""
    ts = now()
    root = session.get(PersonSession, ps.parent_session_id) if ps.delegated else ps
    if root is None or root.delegated or not _session_live(session, root, ts):
        return []
    children = session.scalars(
        select(PersonSession).where(PersonSession.parent_session_id == root.id, PersonSession.revoked_at.is_(None))
        .order_by(PersonSession.created_at, PersonSession.id)).all()
    return [root] + [c for c in children
                     if c.machine_agent_id == root.machine_agent_id and _session_live(session, c, ts)]


def linked(session: Session, x: PersonSession, y: PersonSession) -> bool:
    """Both live and in one family (``linked_sessions``)."""
    ids = {s.id for s in linked_sessions(session, x)}
    return x.id in ids and y.id in ids


def is_shared_account(session: Session, user_id: uuid.UUID, team_id: uuid.UUID) -> bool:
    """A18.1 (A19 E1.2): the person is the ACCOUNT of a live grant (individual or all-members) in that team."""
    return session.scalar(select(Grant.id).where(Grant.account_user_id == user_id, Grant.team_id == team_id,
                                                 Grant.revoked_at.is_(None)).limit(1)) is not None


def _grant_covers(session: Session, grant: Grant, grantee_id: uuid.UUID | None) -> bool:
    """A per-user grant covers its grantee; a team-wide one (A17) every person but the account, and never a shared
    account (A18.1). Membership is checked by the caller."""
    if grantee_id is None or grantee_id == grant.account_user_id:
        return False
    if grant.grantee_user_id is not None:
        return grant.grantee_user_id == grantee_id
    return grant.all_members and not is_shared_account(session, grantee_id, grant.team_id)


def live_grant(session: Session, team_id: uuid.UUID, grantee_id: uuid.UUID, account_id: uuid.UUID) -> Grant | None:
    """The live PER-USER grant of (team, grantee, account)."""
    return session.scalar(select(Grant).where(
        Grant.team_id == team_id, Grant.grantee_user_id == grantee_id, Grant.account_user_id == account_id,
        Grant.revoked_at.is_(None)))


def live_team_grant(session: Session, team_id: uuid.UUID, account_id: uuid.UUID) -> Grant | None:
    """The live team-wide grant (A17) of (team, account)."""
    return session.scalar(select(Grant).where(
        Grant.team_id == team_id, Grant.all_members.is_(True), Grant.account_user_id == account_id,
        Grant.revoked_at.is_(None)))


def usable_grant(session: Session, team_id: uuid.UUID, grantee_id: uuid.UUID, account_id: uuid.UUID) -> Grant | None:
    """What lets ``grantee_id`` act as ``account_id`` in the team (A17): their per-user grant first, then the team-wide
    one, which covers everyone but the account. Membership is checked by the caller."""
    if grantee_id == account_id:
        return None
    personal = live_grant(session, team_id, grantee_id, account_id)
    if personal is not None:
        return personal
    team_wide = live_team_grant(session, team_id, account_id)
    return team_wide if team_wide is not None and _grant_covers(session, team_wide, grantee_id) else None


def grants_of(session: Session, grantee_id: uuid.UUID, team_id: uuid.UUID) -> list[tuple[Grant, User]]:
    """The accounts ``grantee_id`` may act as in the team (per-user and team-wide grants, the per-user one first for
    an account with both) whose account is an active member, by the account's email."""
    rows = session.execute(
        select(Grant, User).join(User, User.id == Grant.account_user_id)
        .where(Grant.team_id == team_id, Grant.revoked_at.is_(None), Grant.account_user_id != grantee_id,
               or_(Grant.grantee_user_id == grantee_id, Grant.all_members.is_(True)))
        .order_by(User.email, Grant.all_members)).all()
    out, seen = [], set()
    for g, u in rows:
        if u.id not in seen and _grant_covers(session, g, grantee_id) and _active_member(session, team_id, u.id):
            seen.add(u.id)
            out.append((g, u))
    return out


def issue_delegated_session(session: Session, parent: PersonSession, account_email: object
                            ) -> tuple[str, PersonSession, User]:
    """§23 A3: a delegated session acting as ``account_email`` for the grantee of the own session ``parent``.

    The ONLY place §16.3's owner rule is relaxed. Needs a live own ``parent`` on an active machine, a live grant
    (grantee = the parent's person, the account, the machine's team) and both people active members. One live child
    per (parent, grant) (A14 M5): an existing one is revoked, with its web sessions, and a fresh one issued. At most
    ``MAX_DELEGATED`` accounts per parent. The raw token is returned once."""
    parent = session.scalar(select(PersonSession).where(PersonSession.id == parent.id).with_for_update()
                            .execution_options(populate_existing=True))  # serializes adds under one parent
    ts = now()
    if parent is None or parent.delegated or not _session_live(session, parent, ts):
        raise PermissionDenied("a delegated session is issued only from a live own session")
    machine = session.get(Agent, parent.machine_agent_id)
    account = find_user_by_email(session, account_email) if isinstance(account_email, str) else None
    grant = usable_grant(session, machine.team_id, parent.user_id, account.id) if account is not None else None
    if (grant is None or not _active_member(session, machine.team_id, account.id)
            or not _active_member(session, machine.team_id, parent.user_id)):
        raise DelegationNotFound("no such account to act as")
    # M5: one live child per account under this parent: re-adding replaces it (whichever grant it came through).
    for child_id in session.scalars(select(PersonSession.id).where(
            PersonSession.parent_session_id == parent.id, PersonSession.user_id == account.id,
            PersonSession.revoked_at.is_(None))).all():
        revoke_person_sessions(session, session_id=child_id)
    live = [c for c in session.scalars(select(PersonSession).where(
        PersonSession.parent_session_id == parent.id, PersonSession.revoked_at.is_(None)))
        if _session_live(session, c, ts)]
    if len(live) >= MAX_DELEGATED:
        raise TooManyAccounts("at most 5 accounts in one app")
    token = security.new_token(security.PERSON_TOKEN_PREFIX)
    child = PersonSession(user_id=account.id, machine_agent_id=parent.machine_agent_id,
                          token_hash=security.hash_token(token), prefix=security.token_prefix(token),
                          scopes=list(parent.scopes), acting_user_id=parent.user_id, grant_id=grant.id,
                          parent_session_id=parent.id)
    session.add(child)
    session.flush()
    return token, child, account


def revoke_person_sessions(session: Session, *, session_id: uuid.UUID | None = None,
                           machine_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None,
                           parent_id: uuid.UUID | None = None, grant_id: uuid.UUID | None = None,
                           include_delegated: bool = True) -> int:
    """Revoke matching person sessions and every app-mode web session and handoff code made from them (§16.12 C2,
    C6), in the caller's transaction. §23 A7: the delegated sessions under a revoked session go with it.
    ``include_delegated=False`` leaves delegated sessions matched directly alone (the account's own password change,
    A14 M4); their parents' revocation still ends them."""
    if all(v is None for v in (session_id, machine_id, user_id, parent_id, grant_id)):
        raise ValueError("revoke_person_sessions needs a filter")
    stmt = update(PersonSession).where(PersonSession.revoked_at.is_(None))
    if session_id is not None:
        stmt = stmt.where(PersonSession.id == session_id)
    if machine_id is not None:
        stmt = stmt.where(PersonSession.machine_agent_id == machine_id)
    if user_id is not None:
        stmt = stmt.where(PersonSession.user_id == user_id)
    if parent_id is not None:
        stmt = stmt.where(PersonSession.parent_session_id == parent_id)
    if grant_id is not None:
        stmt = stmt.where(PersonSession.grant_id == grant_id)
    if not include_delegated:
        stmt = stmt.where(PersonSession.acting_user_id.is_(None))
    ts = now()
    ids = list(session.scalars(stmt.values(revoked_at=ts).returning(PersonSession.id)))
    if ids:
        ids += list(session.scalars(
            update(PersonSession).where(PersonSession.parent_session_id.in_(ids), PersonSession.revoked_at.is_(None))
            .values(revoked_at=ts).returning(PersonSession.id)))
        session.execute(update(WebSession).where(WebSession.person_session_id.in_(ids),
                                                 WebSession.revoked_at.is_(None)).values(revoked_at=ts))
        from raincli_server.models import HandoffCode

        session.execute(delete(HandoffCode).where(HandoffCode.person_session_id.in_(ids)))
    session.flush()
    return len(ids)


def person_sessions_for(session: Session, user: User) -> list[tuple[PersonSession, Agent]]:
    """The user's live own person sessions with their issuing machines, newest first (Signed-in apps). §23 (A14 M3):
    never a delegated session acting as the user: it's on the grantee's machine, which the account must not see, and
    only the grantee or the operator ends it."""
    rows = session.execute(
        select(PersonSession, Agent).join(Agent, Agent.id == PersonSession.machine_agent_id)
        .where(PersonSession.user_id == user.id, PersonSession.revoked_at.is_(None),
               PersonSession.acting_user_id.is_(None))
        .order_by(PersonSession.created_at.desc())
    ).all()
    ts = now()
    return [(ps, agent) for ps, agent in rows if agent.revoked_at is None and ts < person_session_expires_at(ps)]


# Member removal and user disable (protocol §11.5, §12.5) --------------------------

def repoint_or_revoke(session: Session, grant_id: uuid.UUID) -> int:
    """A18.2: after ``grant_id`` is revoked, each live session issued through it is re-pointed to its new effective
    grant when one still covers it (so the audit names the grant in use), and revoked otherwise (with its web
    sessions and handoff codes). Returns how many were revoked."""
    ended = 0
    for ps in session.scalars(select(PersonSession).where(PersonSession.grant_id == grant_id,
                                                          PersonSession.revoked_at.is_(None))).all():
        grant = effective_grant(session, ps)
        if grant is not None and delegated_ok(session, ps):
            ps.grant_id = grant.id
        else:
            ended += revoke_person_sessions(session, session_id=ps.id)
    session.flush()
    return ended


def revoke_grants(session: Session, user_id: uuid.UUID, team_id: uuid.UUID | None = None, *,
                  grant_id: uuid.UUID | None = None) -> int:
    """§23 A7: revoke live grants naming ``user_id`` as grantee or account (in ``team_id``, or every team), or the one
    ``grant_id``, and every session issued through them, in the caller's transaction. Rows stay for the audit.

    A17: a team-wide grant names no grantee, so a member leaving doesn't revoke it; their own delegated sessions in
    that team (as the acting person, through any grant) end instead. The account leaving revokes it."""
    match = [Grant.revoked_at.is_(None)]
    if grant_id is not None:
        match.append(Grant.id == grant_id)
    else:
        match.append(or_(Grant.grantee_user_id == user_id, Grant.account_user_id == user_id))
    if team_id is not None:
        match.append(Grant.team_id == team_id)
    # A19 E2: every live grant of each (team, account) concerned is locked FOR UPDATE, in id order, before anything is
    # revoked or re-pointed, so two revocations of grants that cover the same sessions can't each re-point to the other.
    pairs = session.execute(select(Grant.team_id, Grant.account_user_id).where(*match)).all()
    if pairs:
        session.scalars(select(Grant.id).where(
            Grant.revoked_at.is_(None),
            or_(*[and_(Grant.team_id == t, Grant.account_user_id == a) for t, a in set(pairs)]))
            .order_by(Grant.id).with_for_update()).all()
    stmt = update(Grant).where(*match)
    ids = list(session.scalars(stmt.values(revoked_at=now()).returning(Grant.id)))
    session.flush()
    for gid in ids:
        repoint_or_revoke(session, gid)
    if grant_id is None:
        acting = select(PersonSession.id).join(Grant, Grant.id == PersonSession.grant_id).where(
            PersonSession.acting_user_id == user_id, PersonSession.revoked_at.is_(None))
        if team_id is not None:
            acting = acting.where(Grant.team_id == team_id)
        for sid in session.scalars(acting).all():
            revoke_person_sessions(session, session_id=sid)
    session.flush()
    return len(ids)


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
    # §22 M2: the person's group rows first (by id), then the memberships.
    from raincli_server import groups
    groups.lock_groups_of(session, user.id, team.id)
    # Lock the team's memberships so two concurrent removals cannot both pass the last-owner check, by user id
    # (§22.6: a post holds its recipients' rows in the same order, so the two never wait on each other in a cycle).
    rows = session.scalars(
        select(Membership).where(Membership.team_id == team.id).order_by(Membership.user_id).with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    target = next((m for m in rows if m.user_id == user.id), None)
    if target is None:
        raise IdentityError("that user is not a member of this team")
    if target.role == "owner" and sum(1 for m in rows if m.role == "owner") <= 1:
        raise IdentityError("cannot remove the last owner of a team")
    groups.leave_all(session, user.id, team.id)  # §22 G4: out of the team's groups, copies withdrawn
    revoke_grants(session, user.id, team.id)  # §23 A7: as grantee or account, with their delegated sessions
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
        from raincli_server import groups
        for team_id in session.scalars(select(Membership.team_id).where(Membership.user_id == user.id)):
            groups.lock_groups_of(session, user.id, team_id)  # §22 M2: group rows first
            groups.leave_all(session, user.id, team_id)  # out of every group, copies withdrawn
        bump_credential_epoch(user)  # §18.1: a disable ends every recognised device too
        _revoke_web_sessions(session, user)
        revoke_grants(session, user.id)  # §23 A7: in every team, as grantee or account
        revoke_person_sessions(session, user_id=user.id)  # §16.12 C6 (delegated sessions acting as them too)
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
