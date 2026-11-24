"""The accounts this machine's person acts as, beside the own session (protocol §23 A10, A14 L5/L6 and Q8).

Three private things sit beside the agent config, next to ``person.json`` (which stays the OWN session, unchanged,
so an older client reads only it):

- ``accounts.json``: the delegated sessions this app holds (at most 4: five accounts with the own one),
  ``{"accounts": [{"email", "display_name", "team", "person_session" | "person_session_dpapi"}]}``. Each token is
  stored exactly like ``person.json``'s (DPAPI on Windows, mode 0600 elsewhere, atomic). Every read-modify-write
  happens under ``accounts.lock`` (an exclusive file lock), so the window, the tray, the runtime and the CLI never
  lose each other's changes.
- ``accounts-active.json``: ``{"active": <email or null>}``, the account the app window shows (null: the own one).
  Written only by the window and the tray; the CLI reads it (``raincli accounts`` reports ``active``).
- ``notifications/accounts/<sha256(email)[:16]>/``: each delegated account's notification feed, with its own
  ``queue.json``, ``taken.json`` and ``settings.json``, laid out like the own feed (``notifications/``, unchanged).

No token is ever put in argv, the environment, a log, an error message or a return value other than
``Account.session`` (a ``Secret``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod
from .config import Secret
from .errors import ConfigError
from .filelock import lock, unlock
from .fsutil import atomic_write_json, ensure_private_dir, read_private_file
from .person import PERSON_TOKEN_RE, _dir, notify_dir

ACCOUNTS_FILE = "accounts.json"
ACTIVE_FILE = "accounts-active.json"
LOCK_FILE = "accounts.lock"
NOTIFY_ACCOUNTS = "accounts"
MAX_DELEGATED = 4  # five accounts per app, the own one included (AS2)
_FIELDS = ("email", "display_name", "team")


@dataclass(frozen=True)
class Account:
    email: str
    display_name: str
    team: str
    session: Secret  # the delegated person session (never printed)

    def public(self) -> dict:
        """The account without its session."""
        return {"email": self.email, "display_name": self.display_name, "team": self.team}


# -- paths ---------------------------------------------------------------------------------------------------------

def accounts_path(agent_config=None) -> Path:
    return _dir(agent_config) / ACCOUNTS_FILE


def active_path(agent_config=None) -> Path:
    return _dir(agent_config) / ACTIVE_FILE


def lock_path(agent_config=None) -> Path:
    return _dir(agent_config) / LOCK_FILE


def account_key(email: str) -> str:
    """The account's folder name: the first 16 hex digits of sha256 of its lower-cased email (A14 Q8)."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


def account_notify_dir(agent_config, email: str) -> Path:
    """A delegated account's notification feed (queue.json, taken.json, settings.json)."""
    return notify_dir(agent_config) / NOTIFY_ACCOUNTS / account_key(email)


# -- the lock (A14 L5) --------------------------------------------------------------------------------------------

@contextlib.contextmanager
def locked(agent_config=None):
    """Hold ``accounts.lock`` exclusively for a read-modify-write of ``accounts.json``."""
    path = lock_path(agent_config)
    ensure_private_dir(str(path.parent))
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")  # Windows locks a byte range: the file needs one byte
        lock(fd)
        try:
            yield
        finally:
            unlock(fd)
    finally:
        os.close(fd)


# -- accounts.json ---------------------------------------------------------------------------------------------------

def _same(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def _damaged(path):
    return ConfigError(f"accounts file {path} is damaged; remove the accounts and add them again")


def load_accounts(agent_config=None) -> list[Account]:
    """The delegated accounts this app holds, in the order they were added. A missing file is none; a damaged one
    is a ``ConfigError`` that never quotes the file."""
    path = accounts_path(agent_config)
    if not os.path.lexists(path):
        return []
    raw = read_private_file(str(path), "accounts file")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise _damaged(path) from None
    entries = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(entries, list) or set(data) - {"accounts"}:
        raise _damaged(path)
    out = []
    for entry in entries:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) and entry.get(k) for k in _FIELDS):
            raise _damaged(path)
        secret_keys = {"person_session", "person_session_dpapi"} & set(entry)
        if len(secret_keys) != 1 or set(entry) - set(_FIELDS) - secret_keys:
            raise _damaged(path)
        # As person.load_session (person._read_secret): either form is read on any platform, so a file written
        # where DPAPI wasn't in use still loads; every write uses DPAPI where it is (review note).
        if "person_session_dpapi" in entry:
            from .dpapi import unprotect_token
            token = unprotect_token(entry["person_session_dpapi"])
        else:
            token = entry["person_session"]
        if not isinstance(token, str) or not PERSON_TOKEN_RE.fullmatch(token):
            raise _damaged(path)
        out.append(Account(entry["email"], entry["display_name"], entry["team"], Secret(token)))
    return out


def _write_accounts(agent_config, accounts: list[Account]) -> None:
    path = accounts_path(agent_config)
    ensure_private_dir(str(path.parent))
    protect = config_mod.protects_tokens()
    if protect:
        from .dpapi import protect_token
    entries = []
    for a in accounts:
        entry = a.public()
        if protect:
            entry["person_session_dpapi"] = protect_token(a.session.reveal())
        else:
            entry["person_session"] = a.session.reveal()
        entries.append(entry)
    atomic_write_json(str(path), {"accounts": entries}, mode=0o600)


def find_account(agent_config, email: str) -> Account | None:
    return next((a for a in load_accounts(agent_config) if _same(a.email, email)), None)


def save_account(agent_config, email: str, display_name: str, team: str, token) -> Account:
    """Add or replace (by email, case-insensitively) a delegated account, under the lock. A fresh session for an
    account already held replaces it (the server revoked the old one, A14 M5/D2). More than ``MAX_DELEGATED``
    accounts is a ``ConfigError``."""
    value = token.reveal() if isinstance(token, Secret) else token
    if not isinstance(value, str) or not PERSON_TOKEN_RE.fullmatch(value):
        raise ConfigError("the server's person session is malformed")
    account = Account(email, display_name, team, Secret(value))
    with locked(agent_config):
        accounts = load_accounts(agent_config)
        kept = [a for a in accounts if not _same(a.email, email)]
        if len(kept) >= MAX_DELEGATED:
            raise ConfigError(f"this app already holds {MAX_DELEGATED} other accounts; remove one first")
        index = next((i for i, a in enumerate(accounts) if _same(a.email, email)), len(kept))
        kept.insert(index, account)
        _write_accounts(agent_config, kept)
    return account


def remove_account(agent_config, email: str) -> Account | None:
    """Drop a delegated account (its session and its notification feed) under the lock; if the window showed it,
    the own account becomes active. Returns the removed account (its session, so the caller can end it on the
    server), or None."""
    with locked(agent_config):
        accounts = load_accounts(agent_config)
        removed = next((a for a in accounts if _same(a.email, email)), None)
        if removed is None:
            return None
        _write_accounts(agent_config, [a for a in accounts if a is not removed])
        if (load_active(agent_config) or "") and _same(load_active(agent_config), removed.email):
            save_active(agent_config, None)
    shutil.rmtree(account_notify_dir(agent_config, removed.email), ignore_errors=True)
    return removed


def clear_accounts(agent_config=None) -> bool:
    """Every delegated account gone (the own sign-out, A10 and A14 Q10): ``accounts.json``, the active choice and the
    delegated feeds. The server ends the sessions itself (the cascade from the own session). It never parses
    anything, so a damaged file can't stop a sign-out, and it clears even when the lock can't be taken (review S1).
    Returns whether ``accounts.json`` existed."""
    def clear():
        had = os.path.lexists(accounts_path(agent_config))
        for path in (accounts_path(agent_config), active_path(agent_config)):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
        return had

    try:
        with locked(agent_config):
            had = clear()
    except OSError:  # an unreadable or foreign lock file: clear without it
        had = clear()
    shutil.rmtree(notify_dir(agent_config) / NOTIFY_ACCOUNTS, ignore_errors=True)
    return had


def prune_feeds(agent_config=None) -> list[str]:
    """For the runtime, after it stops a removed account's poller: delete every delegated feed folder whose account
    is no longer held (a poller still running when ``remove_account`` deleted its feed may have recreated it).
    Returns the folder names removed. A damaged ``accounts.json`` prunes nothing."""
    root = notify_dir(agent_config) / NOTIFY_ACCOUNTS
    try:
        held = {account_key(a.email) for a in load_accounts(agent_config)}
    except ConfigError:
        return []
    removed = []
    for folder in (root.iterdir() if root.is_dir() else ()):
        if folder.is_dir() and folder.name not in held:
            shutil.rmtree(folder, ignore_errors=True)
            removed.append(folder.name)
    return removed


# -- accounts-active.json (A14 L6) ----------------------------------------------------------------------------------

def load_active(agent_config=None) -> str | None:
    """The email the app window shows, or None for the own account (also when the file is missing or damaged)."""
    path = active_path(agent_config)
    if not os.path.lexists(path):
        return None
    try:
        data = json.loads(read_private_file(str(path), "active account file"))
    except (ConfigError, ValueError, UnicodeDecodeError):
        return None
    value = data.get("active") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def save_active(agent_config, email: str | None) -> None:
    """The window and the tray only: which account the window shows (None: the own one)."""
    path = active_path(agent_config)
    ensure_private_dir(str(path.parent))
    atomic_write_json(str(path), {"active": email}, mode=0o600)


# -- --as resolution (A14 Q9) -----------------------------------------------------------------------------------------

def matches(candidates, query: str) -> list:
    """Accounts (anything with ``email`` and ``display_name``) that ``query`` names: an exact email
    (case-insensitive) wins; otherwise every exact display name. Empty when none; several when ambiguous."""
    by_email = [c for c in candidates if _same(c.email, query)]
    if by_email:
        return by_email[:1]
    return [c for c in candidates if c.display_name == query.strip()]


# -- the effective grant's facts, per account (A18.4, A19 E4) ------------------------------------------------------

GRANT_FILE = "grant.json"


def save_grant_facts(agent_config, email: str, *, toasts: bool, kind: str | None = None) -> None:
    """The runtime's poller, on every cycle and after a 401 (A19 E4): the account's effective grant as
    ``/person/me`` gives it: ``toasts`` (show toasts for this account) and its ``kind`` (``individual`` or
    ``all_members``). Written beside that account's queue."""
    folder = account_notify_dir(agent_config, email)
    ensure_private_dir(str(folder))
    atomic_write_json(str(folder / GRANT_FILE), {"toasts": toasts is True, "kind": kind}, mode=0o600)


def account_toasts(agent_config, email: str) -> bool:
    """Whether the tray shows this account's toasts (A18.4). Off only when the poller recorded the flag off;
    its unread count still shows in the switcher."""
    from .person import _read_json

    return _read_json(account_notify_dir(agent_config, email) / GRANT_FILE, {}).get("toasts") is not False


# -- accounts dropped by the runtime (A10: "the window shows a one-line notice") -------------------------------------

DROPPED_FILE = "accounts-dropped.json"


def record_dropped(agent_config, email: str, display_name: str) -> None:
    """The runtime, when a delegated session answers 401 (revoked, the grant gone, or the account or grantee no longer
    a member): remember the account for the window's one-line notice. People only, never a token."""
    path = _dir(agent_config) / DROPPED_FILE
    with locked(agent_config):
        try:
            data = json.loads(read_private_file(str(path), "dropped accounts file")) if os.path.lexists(path) else {}
        except (ConfigError, ValueError, UnicodeDecodeError):
            data = {}
        rows = [r for r in (data.get("dropped") or []) if isinstance(r, dict) and not _same(str(r.get("email")), email)]
        rows.append({"email": email, "display_name": display_name})
        atomic_write_json(str(path), {"dropped": rows[-MAX_DELEGATED:]}, mode=0o600)


def take_dropped(agent_config) -> list[dict]:
    """The window's side: the accounts dropped since it last asked (``{email, display_name}``), each once."""
    path = _dir(agent_config) / DROPPED_FILE
    with locked(agent_config):
        if not os.path.lexists(path):
            return []
        try:
            data = json.loads(read_private_file(str(path), "dropped accounts file"))
        except (ConfigError, ValueError, UnicodeDecodeError):
            data = {}
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
    return [{"email": r["email"], "display_name": str(r.get("display_name") or r["email"])}
            for r in (data.get("dropped") or []) if isinstance(r, dict) and isinstance(r.get("email"), str)]
