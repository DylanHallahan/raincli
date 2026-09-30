"""Operator CLI: ``raincli-admin``. For the server operator only; never exposed over HTTP.

The database URL comes from ``RAINCLI_DATABASE_URL``. Passwords are read from
stdin or a TTY prompt, never from argv. Agent tokens are written to a new
0600 config file; they are printed (to stdout, with a warning on stderr) only
when ``--out`` is omitted from ``register-agent``.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Callable, TextIO

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from raincli_server import identity, presence
from raincli_server.db import make_engine, make_sessionmaker, session_scope
from raincli_server.models import Agent, AgentCredential, AgentPresence, ClientTarget, Team, User


class AdminError(Exception):
    pass


def _env_url(env) -> str:
    url = env.get("RAINCLI_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        raise AdminError("set RAINCLI_DATABASE_URL to a postgresql:// URL")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _base_url(env) -> str:
    public = env.get("RAINCLI_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")
    return public + env.get("RAINCLI_ROOT_PATH", "").rstrip("/")


def read_password(stdin: TextIO, prompt: Callable[[str], str] = getpass.getpass) -> str:
    if stdin.isatty():
        first = prompt("Password: ")
        if prompt("Repeat password: ") != first:
            raise AdminError("passwords do not match")
        return first
    line = stdin.readline()
    return line[:-1] if line.endswith("\n") else line


def write_agent_config(path: str, api_url: str, token: str) -> None:
    """Create ``path`` with mode 0600; refuse to overwrite an existing file."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise AdminError(f"{path} already exists; refusing to overwrite") from None
    with os.fdopen(fd, "w") as f:
        json.dump({"api_url": api_url, "token": token}, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(path, 0o600)


def _user(session: Session, email: str) -> User:
    user = identity.find_user_by_email(session, email)
    if user is None:
        raise AdminError(f"no user with email {email}")
    return user


def _team(session: Session, slug: str) -> Team:
    team = session.scalar(select(Team).where(Team.slug == slug))
    if team is None:
        raise AdminError(f"no team {slug}")
    return team


def _agent(session: Session, team: Team, handle: str) -> Agent:
    agent = identity.find_agent(session, team, handle)
    if agent is None:
        raise AdminError(f"no agent {handle} in team {team.slug}")
    return agent


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="raincli-admin", description="RainCLI operator CLI")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("create-user", help="create a user; the password is read from stdin or a prompt")
    c.add_argument("--email", required=True)
    c.add_argument("--name", required=True)

    c = sub.add_parser("create-team", help="create a team owned by an existing user")
    c.add_argument("--slug", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--owner", required=True, metavar="EMAIL")

    c = sub.add_parser("add-member", help="add an existing user to a team")
    c.add_argument("--team", required=True, metavar="SLUG")
    c.add_argument("--email", required=True)
    c.add_argument("--role", choices=("member", "owner"), default="member")

    c = sub.add_parser("remove-member", help="remove a user from a team; revokes their agents there and web sessions")
    c.add_argument("--team", required=True, metavar="SLUG")
    c.add_argument("--email", required=True)

    c = sub.add_parser("disable-user", help="disable a user; revokes their web sessions and all their agents")
    c.add_argument("--email", required=True)

    c = sub.add_parser("enable-user", help="re-enable a disabled user (revoked agents stay revoked)")
    c.add_argument("--email", required=True)

    c = sub.add_parser("invite", help="create an invitation and print its URL")
    c.add_argument("--team", required=True, metavar="SLUG")
    c.add_argument("--by", required=True, metavar="EMAIL", help="an owner of the team")
    c.add_argument("--email", help="bind the invitation to this email")

    c = sub.add_parser("register-agent", help="register an agent and write its config file")
    c.add_argument("--team", required=True, metavar="SLUG")
    c.add_argument("--owner", required=True, metavar="EMAIL")
    c.add_argument("--handle", required=True)
    c.add_argument("--display-name")
    c.add_argument("--out", metavar="CONFIG.json", help="new file to write (mode 0600)")

    c = sub.add_parser("rotate-agent", help="issue a new credential and revoke the old ones")
    c.add_argument("--team", required=True, metavar="SLUG")
    c.add_argument("--handle", required=True)
    c.add_argument("--out", required=True, metavar="FILE", help="new file to write (mode 0600)")

    c = sub.add_parser("revoke-agent", help="revoke an agent and all its credentials")
    c.add_argument("--team", required=True, metavar="SLUG")
    c.add_argument("--handle", required=True)

    c = sub.add_parser("list-agents", help="list agents (never shows tokens)")
    c.add_argument("--team", metavar="SLUG")

    c = sub.add_parser("set-client-version",
                       help="push a client version to every managed machine in a team (protocol §14.5)")
    c.add_argument("--team", required=True, metavar="SLUG")
    target = c.add_mutually_exclusive_group(required=True)
    target.add_argument("version", nargs="?", metavar="vX.Y.Z", help="release tag, v0.3.0 or later")
    target.add_argument("--clear", action="store_true", help="remove the team's target")
    c.add_argument("--allow-downgrade", action="store_true",
                   help="let machines on a newer version install this older one")

    c = sub.add_parser("client-status", help="list each machine's client version and update state")
    c.add_argument("--team", required=True, metavar="SLUG")

    sub.add_parser("migrate", help="upgrade the database schema to head")
    return p


def main(argv: list[str] | None = None, *, env=None, stdin: TextIO | None = None,
         stdout: TextIO | None = None, stderr: TextIO | None = None,
         prompt: Callable[[str], str] = getpass.getpass) -> int:
    env = os.environ if env is None else env
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        url = _env_url(env)
        if args.command == "migrate":
            from raincli_server.migrate import upgrade

            upgrade(url)
            print("migrated to head", file=stdout)
            return 0
        engine = make_engine(url)
        try:
            with session_scope(make_sessionmaker(engine)) as session:
                _dispatch(args, session, env, stdin, stdout, stderr, prompt)
        finally:
            engine.dispose()
    except (AdminError, identity.IdentityError) as exc:
        print(f"raincli-admin: {exc}", file=stderr)
        return 1
    return 0


def _dispatch(args, session: Session, env, stdin, stdout, stderr, prompt) -> None:
    cmd = args.command
    if cmd == "create-user":
        password = read_password(stdin, prompt)
        user = identity.create_user(session, args.email, args.name, password)
        print(f"created user {user.email}", file=stdout)
    elif cmd == "create-team":
        team = identity.create_team(session, args.slug, args.name, _user(session, args.owner))
        print(f"created team {team.slug} (owner {args.owner})", file=stdout)
    elif cmd == "add-member":
        m = identity.add_member(session, _team(session, args.team), _user(session, args.email), args.role)
        print(f"{args.email} is {m.role} of {args.team}", file=stdout)
    elif cmd == "remove-member":
        identity.remove_member(session, _team(session, args.team), _user(session, args.email))
        print(f"removed {args.email} from {args.team}; their agents there and web sessions are revoked",
              file=stdout)
    elif cmd in ("disable-user", "enable-user"):
        active = cmd == "enable-user"
        identity.set_user_active(session, _user(session, args.email), active)
        print(f"{args.email} is {'enabled' if active else 'disabled; web sessions and agents revoked'}",
              file=stdout)
    elif cmd == "invite":
        _, token = identity.create_invitation(
            session, _team(session, args.team), _user(session, args.by), args.email)
        print(f"{_base_url(env)}/invite/{token}", file=stdout)
    elif cmd == "register-agent":
        if args.out and os.path.lexists(args.out):
            raise AdminError(f"{args.out} already exists; refusing to overwrite")
        agent, token = identity.register_agent(
            session, _team(session, args.team), _user(session, args.owner), args.handle, args.display_name)
        session.flush()
        _emit_token(args.out, token, env, stdout, stderr, f"registered agent {agent.handle}")
    elif cmd == "rotate-agent":
        if os.path.lexists(args.out):
            raise AdminError(f"{args.out} already exists; refusing to overwrite")
        agent = _agent(session, _team(session, args.team), args.handle)
        token = identity.rotate_agent_credential(session, agent)
        _emit_token(args.out, token, env, stdout, stderr, f"rotated agent {agent.handle}; old credentials revoked")
    elif cmd == "revoke-agent":
        agent = _agent(session, _team(session, args.team), args.handle)
        identity.revoke_agent(session, agent)
        print(f"revoked agent {agent.handle} and all its credentials", file=stdout)
    elif cmd == "list-agents":
        stmt = (select(Agent, Team, User).join(Team, Team.id == Agent.team_id)
                .join(User, User.id == Agent.owner_user_id).order_by(Team.slug, Agent.handle))
        if args.team:
            stmt = stmt.where(Team.slug == args.team)
        for agent, team, owner in session.execute(stmt):
            creds = session.scalars(select(AgentCredential).where(
                AgentCredential.agent_id == agent.id, AgentCredential.revoked_at.is_(None))).all()
            last = max((c.last_used_at for c in creds if c.last_used_at), default=None)
            print("\t".join([
                team.slug, agent.handle, owner.email, "active" if agent.revoked_at is None else "revoked",
                ",".join(c.prefix + "…" for c in creds) or "-", last.isoformat() if last else "never",
            ]), file=stdout)
    elif cmd == "set-client-version":
        _set_client_version(session, _team(session, args.team), args, stdout, stderr)
    elif cmd == "client-status":
        _client_status(session, _team(session, args.team), stdout)


def _set_client_version(session: Session, team: Team, args, stdout, stderr) -> None:
    if args.clear:
        if args.allow_downgrade:
            raise AdminError("--allow-downgrade cannot be combined with --clear")
        target = session.get(ClientTarget, team.id)
        if target is None:
            print(f"team {team.slug} has no client target", file=stdout)
            return
        session.delete(target)
        print(f"cleared the client target for {team.slug}; machines keep their installed version", file=stdout)
        return
    version = args.version if args.version.startswith("v") else "v" + args.version
    try:
        numeric = presence.version_tuple(version)
    except ValueError:
        raise AdminError(f"{args.version} is not a release version like v0.3.0") from None
    if numeric < presence.MIN_TARGET:
        raise AdminError(f"{version} predates pushed updates; the target must be v0.3.0 or later")
    if args.allow_downgrade:
        print(f"warning: --allow-downgrade lets machines in {team.slug} newer than {version} install it; "
              "it applies only while this target is set", file=stderr)
    # set_at changes on every upsert, so a runtime may retry a target it rolled back (protocol §14.7).
    values = {"version": version, "allow_downgrade": args.allow_downgrade, "set_at": func.now()}
    session.execute(insert(ClientTarget).values(team_id=team.id, **values)
                    .on_conflict_do_update(index_elements=[ClientTarget.team_id], set_=values))
    print(f"client target for {team.slug} is {version}"
          f"{' (downgrade allowed)' if args.allow_downgrade else ''}", file=stdout)


def _client_status(session: Session, team: Team, stdout) -> None:
    """One tab-separated line per active handle; the target first. Never shows keys or paths."""
    target = session.get(ClientTarget, team.id)
    print("target\t" + (f"{target.version}{' allow-downgrade' if target.allow_downgrade else ''}"
                        if target else "none"), file=stdout)
    print("\t".join(["handle", "version", "update_mode", "update_state", "error", "seen_at"]), file=stdout)
    rows = session.execute(
        select(Agent, AgentPresence).outerjoin(AgentPresence, AgentPresence.agent_id == Agent.id)
        .where(Agent.team_id == team.id, Agent.revoked_at.is_(None)).order_by(Agent.handle))
    for agent, row in rows:
        if row is None or row.client_version is None:
            fields = ["-", "-", "-", "-"]
        else:
            fields = [row.client_version, row.update_mode, row.update_state, row.update_error or "-"]
        seen = row.seen_at.isoformat() if row is not None else "never"
        print("\t".join([agent.handle, *fields, seen]), file=stdout)


def _emit_token(out: str | None, token: str, env, stdout, stderr, summary: str) -> None:
    """Write the config file, then let the caller's transaction commit. Never log the token."""
    api_url = _base_url(env)
    if out:
        write_agent_config(out, api_url, token)
        print(f"{summary}; config written to {out} (mode 0600)", file=stdout)
    else:
        print("warning: the agent token follows on stdout and is shown only once; store it in a "
              "0600 config file and do not paste it into shell history or logs", file=stderr)
        print(f"{summary}", file=stderr)
        print(token, file=stdout)


if __name__ == "__main__":
    sys.exit(main())
