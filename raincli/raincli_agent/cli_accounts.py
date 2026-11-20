"""The CLI contract for account switching (protocol §23 A11, A14 Q9/Q10, A19 E4; docs/raincli-cli-json.md).

- ``raincli accounts [--json]``: the own account, the added ones (``accounts.json``) and the granted ones not yet
  added, each with ``kind`` (``own`` or ``granted``), ``grant`` (``individual``, ``all_members`` or null), ``added``,
  ``unread`` (null when not added), ``toasts`` and ``active`` (the app window's choice, read only).
- ``--as <email|name>`` on every ``raincli me`` command: an exact email first (case-insensitive), then an exact
  display name, among the own, added and granted accounts. A granted account not yet added is added on first use
  (its session is minted and stored like the window's). Ambiguous: exit 2, naming the emails. Unknown or ungranted:
  exit 7 (``not_granted``).
- ``raincli me inbox --watch --all-accounts``: one merged stream, a worker per account, each record
  ``{"schema": 1, "account": <email>, "message": {...}}``, deduplicated by (account, id); it never acks.

Every ``raincli me … --json`` object carries ``"schema": 1`` and ``"account"``. Fields are only ever added.
Sessions never reach argv, the environment, a log or the output.
"""

from __future__ import annotations

import queue as queue_mod
import threading
import time

from . import accounts, person
from . import cli as _cli
from .config import default_config_path
from .errors import (EXIT_NOT_GRANTED, EXIT_OK, EXIT_TIMEOUT, ApiError, ConfigError, NotGranted, RainError,
                     UsageError)
from .text import escape_line

SCHEMA = 1


def _cfg(args):
    return args.agent_config or default_config_path()


def _own_client(cfg):
    return person.PersonClient.for_config(cfg)


def _client(cfg, account):
    from .api import ApiClient
    from .config import load_config

    config = load_config(cfg)
    return person.PersonClient(ApiClient(config.api_url, account.session))


class Who:
    """The resolved account: its email, its display name, whether it's the own one, and a person client for it."""

    def __init__(self, email, display_name, client, own):
        self.email, self.display_name, self.client, self.own = email.lower(), display_name, client, own


def _granted(own_client):
    """``GET /person/delegations``: the accounts the own session may add (both grant kinds)."""
    try:
        return own_client.api.request("GET", "/person/delegations")[1].get("accounts") or []
    except ApiError as exc:
        if exc.status == 404:  # a server before §23: nothing is granted
            return []
        raise


class _Named:
    def __init__(self, email, display_name, where, data=None):
        self.email, self.display_name, self.where, self.data = email, display_name, where, data


def add_account(cfg, own_client, email):
    """Mint a delegated session for a granted account (``POST /person/delegations``) and store it."""
    try:
        data = own_client.api.request("POST", "/person/delegations", body={"account": email})[1]
    except ApiError as exc:
        if exc.status == 404:
            raise NotGranted(f"{email} isn't an account this machine may act as") from None
        if exc.code == "too_many_accounts":
            raise RainError("this app already holds 5 accounts; remove one with raincli me sign-out --as") from None
        raise
    acct = data["account"]
    accounts.save_account(cfg, acct["email"], acct.get("display_name") or acct["email"], acct.get("team") or "",
                          data["person_session"])
    return accounts.find_account(cfg, acct["email"])


def resolve(cfg, query):
    """``--as``: the own, an added or a granted account (added on first use)."""
    own_client = _own_client(cfg)
    own = own_client.me()["user"]
    candidates = [_Named(own["email"], own.get("display_name") or "", "own")]
    added = accounts.load_accounts(cfg)
    candidates += [_Named(a.email, a.display_name, "added", a) for a in added]
    found = accounts.matches(candidates, query)
    if not found:
        held = {c.email.lower() for c in candidates}
        granted = [_Named(g["email"], g.get("display_name") or "", "granted", g) for g in _granted(own_client)
                   if isinstance(g, dict) and isinstance(g.get("email"), str) and g["email"].lower() not in held]
        found = accounts.matches(candidates + granted, query)
    emails = sorted({c.email.lower() for c in found})
    if len(emails) > 1:
        raise UsageError(f"--as {escape_line(query)} names several accounts: {', '.join(emails)}; use an email")
    if not found:
        raise NotGranted(f"{escape_line(query)} isn't an account this machine may act as")
    chosen = found[0]
    if chosen.where == "own":
        return Who(chosen.email, chosen.display_name, own_client, True)
    account = chosen.data if chosen.where == "added" else add_account(cfg, own_client, chosen.email)
    return Who(account.email, account.display_name, _client(cfg, account), False)


def own_who(cfg):
    client = _own_client(cfg)
    try:
        email = person.owner_email(cfg)
    except ConfigError:
        email = client.me()["user"]["email"]
    return Who(email, "", client, True)


def person_for(args):
    """The person client a ``raincli me`` command uses: the own session, or ``--as``'s account. It also stamps every
    JSON object printed from now on with ``schema`` and ``account``."""
    cfg = _cfg(args)
    query = getattr(args, "as_account", None)
    who = resolve(cfg, query) if query else own_who(cfg)
    _cli.JSON_STAMP[0] = {"schema": SCHEMA, "account": who.email}
    args._who = who
    return who.client


def scope_refusal(exc):
    """A19 E4: a grant's scope refusing a request (``403 delegated_scope``) is exit 7 with the server's sentence."""
    if isinstance(exc, ApiError) and exc.code == "delegated_scope":
        return NotGranted(exc.args[0] if exc.args else "not allowed through this grant", code="delegated_scope")
    return exc


def guarded(func):
    """Wrap a ``raincli me`` command: with ``--json``, a NotGranted is printed as ``{"schema", "error"}``."""
    def run(args):
        try:
            try:
                return func(args)
            except ApiError as exc:
                raise scope_refusal(exc) from None
        except NotGranted as exc:
            if getattr(args, "json", False):
                _cli.JSON_STAMP[0] = None
                _cli.out_json({"schema": SCHEMA, "error": {"code": exc.code, "message": str(exc)}})
                return EXIT_NOT_GRANTED
            raise
        finally:
            _cli.JSON_STAMP[0] = None
    run.__name__ = getattr(func, "__name__", "run")
    return run


# -- raincli accounts --------------------------------------------------------------------------------------------

def _me_facts(client):
    me = client.me()
    return me, (me.get("unread") if isinstance(me.get("unread"), int) else None)


def list_accounts(cfg):
    """The ``raincli accounts`` records (schema 1)."""
    own_client = _own_client(cfg)
    me, unread = _me_facts(own_client)
    teams = me.get("teams") or [{}]
    active = (accounts.load_active(cfg) or "").lower()
    rows = [{"email": me["user"]["email"], "display_name": me["user"].get("display_name"),
             "team": teams[0].get("slug"), "kind": "own", "grant": None, "added": True, "unread": unread,
             "toasts": None, "active": not active}]
    held = set()
    for account in accounts.load_accounts(cfg):
        try:
            facts, count = _me_facts(_client(cfg, account))
        except ApiError as exc:
            if exc.status != 401:
                raise
            accounts.remove_account(cfg, account.email)  # ended on the server: no longer held here
            accounts.record_dropped(cfg, account.email, account.display_name)
            continue
        held.add(account.email.lower())
        rows.append({"email": account.email, "display_name": account.display_name, "team": account.team,
                     "kind": "granted", "grant": facts.get("grant_kind"), "added": True, "unread": count,
                     "toasts": facts.get("toasts"), "active": account.email.lower() == active})
    for g in _granted(own_client):
        if not isinstance(g, dict) or not isinstance(g.get("email"), str) or g["email"].lower() in held:
            continue
        rows.append({"email": g["email"], "display_name": g.get("display_name"), "team": g.get("team"),
                     "kind": "granted", "grant": g.get("kind"), "added": False, "unread": None,
                     "toasts": g.get("toasts"), "active": False})
    return rows


def cmd_accounts(args):
    rows = list_accounts(_cfg(args))
    if args.json:
        _cli.out_json({"schema": SCHEMA, "accounts": rows})
        return EXIT_OK
    for r in rows:
        mark = "*" if r["active"] else " "
        state = "own" if r["kind"] == "own" else ("added" if r["added"] else "not added")
        grant = f", {r['grant'].replace('_', '-')} grant" if r.get("grant") else ""
        unread = f", {r['unread']} unread" if r["unread"] is not None else ""
        _cli.out(f"{mark} {escape_line(str(r['display_name'] or ''))} <{escape_line(r['email'])}> ({state}{grant}"
                 f"{unread})")
    return EXIT_OK


# -- raincli me sign-out --as (A14 Q10) ----------------------------------------------------------------------------

def remove_from_app(args):
    """The CLI's "remove from this app": end that delegated session only; the grant stays."""
    cfg = _cfg(args)
    who = resolve(cfg, args.as_account)
    if who.own:
        raise UsageError("--as names this machine's own account; use raincli me sign-out without --as")
    try:
        who.client.api.request("POST", "/person/sign-out", body={})
        server = "revoked"
    except ApiError as exc:
        if exc.status != 401:
            raise
        server = "already_revoked"
    accounts.remove_account(cfg, who.email)
    _cli.out(f"removed {escape_line(who.email)} from this app"
             + (" (the server had already ended it)" if server == "already_revoked" else "") + "; the grant stays")
    return EXIT_OK


# -- raincli me inbox --watch --all-accounts ------------------------------------------------------------------------

def watch_all(args):
    """One merged stream of new messages for the own and every added account, each tagged with its account. Never
    acks. Exit 4 after ``--timeout`` with nothing; ``--once`` after the first batch."""
    cfg = _cfg(args)
    who = [own_who(cfg)] + [Who(a.email, a.display_name, _client(cfg, a), False) for a in accounts.load_accounts(cfg)]
    out_q: queue_mod.Queue = queue_mod.Queue()
    stop = threading.Event()

    def worker(w):
        after, failures = 0, 0
        while not stop.is_set():
            try:
                page, cursor = w.client.inbox(after=after, wait=25, include_acked=args.all)
                failures = 0
                if page:
                    out_q.put((w.email, page))
                elif w.client.poll_backoff:
                    stop.wait(w.client.poll_backoff)
                after = max(after, int(cursor))
            except ApiError as exc:
                if exc.status == 401:
                    out_q.put((w.email, None))  # ended: this account leaves the stream
                    return
                failures += 1
                stop.wait(min(60, 2 ** min(failures, 6)))
            except (ConfigError, OSError):
                failures += 1
                stop.wait(min(60, 2 ** min(failures, 6)))

    for w in who:
        threading.Thread(target=worker, args=(w,), daemon=True).start()
    deadline = time.monotonic() + args.timeout if args.timeout else None
    seen, received = set(), 0
    try:
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return EXIT_OK if received else EXIT_TIMEOUT
            try:
                email, page = out_q.get(timeout=remaining if remaining is not None else 1.0)
            except queue_mod.Empty:
                continue
            if page is None:
                _cli.out(f"raincli: {escape_line(email)}'s session ended; it is no longer watched") \
                    if not args.json else None
                continue
            for message in page:
                key = (email, message.get("id"))
                if key in seen:
                    continue
                seen.add(key)
                received += 1
                if args.json:
                    _cli.out_json({"schema": SCHEMA, "account": email, "message": message})
                else:
                    _cli.out(f"[{escape_line(email)}] {_cli.format_message(message)}")
            if args.once and received:
                return EXIT_OK
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        stop.set()
