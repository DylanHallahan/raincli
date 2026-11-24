"""The runtime's notification pollers for the delegated accounts this app holds (protocol §23 A10, A18.4, A19 E4).

The own session keeps its feed (``person.NotificationFeed``, ``notifications/``, unchanged). Each account in
``accounts.json`` gets its own poller, which long-polls ``/person/inbox?routed=1`` with that account's delegated
session into the account's own feed (``notifications/accounts/<key>/``) and never acks. On every cycle it re-reads the
effective grant's ``toasts`` flag and kind from ``/person/me`` (A19 E4), so the tray follows a grant change at once.
A 401 drops the account (it leaves ``accounts.json``; the window shows a one-line notice). The account's machine-side
facts are never polled (AS13): no inbox-offline check for a delegated account.

``AccountFeeds`` keeps one poller per held account, starting and stopping them as the window and the CLI add and
remove accounts. No token is ever logged.
"""

from __future__ import annotations

import hashlib
import threading
import time

from . import accounts
from .errors import ApiError, ConfigError
from .person import FEED_BACKOFF, FEED_WAIT, NotificationFeed, PersonClient, _queue, append_notifications

RECONCILE_EVERY = 5  # seconds between looks at accounts.json for added and removed accounts


def _client_for(agent_config, account):
    from .api import ApiClient
    from .config import default_config_path, load_config

    config = load_config(agent_config or default_config_path())
    return PersonClient(ApiClient(config.api_url, account.session, max_attempts=1))


class AccountFeed(NotificationFeed):
    """One delegated account's poller. ``email`` names it; its session is re-read from ``accounts.json`` each cycle
    (a re-add replaces it)."""

    def __init__(self, agent_config, email, *, client_factory=None, sleep=None, now=time.time, log=None):
        super().__init__(agent_config, client_factory=client_factory or self._client, sleep=sleep, now=now, log=log)
        self.email = email
        self.folder = accounts.account_notify_dir(agent_config, email)
        self.dropped = False

    def _account(self):
        return accounts.find_account(self.agent_config, self.email)

    def _client(self):
        account = self._account()
        if account is None:
            raise ConfigError("the account is no longer held")
        return _client_for(self.agent_config, account)

    def session_id(self):
        account = self._account()
        return None if account is None else hashlib.sha256(account.session.reveal().encode()).hexdigest()[:16]

    def refresh_grant(self, client):
        """A19 E4: the effective grant's toasts flag and kind, from ``/person/me``, beside the account's feed."""
        me = client.me()
        accounts.save_grant_facts(self.agent_config, self.email, toasts=me.get("toasts") is True,
                                  kind=me.get("grant_kind") if me.get("grant_kind") in ("individual", "all_members")
                                  else None)

    def poll_once(self, wait=FEED_WAIT):
        """One cycle: the grant's facts, then one long-poll into the account's feed. None when it isn't held."""
        session = self.session_id()
        if session is None:
            return None
        queue = _queue(self.agent_config, self.folder)
        cursor = queue["cursor"] if queue["session"] == session else 0
        client = self.client_factory()
        self.refresh_grant(client)
        messages, new_cursor = client.inbox(after=cursor, wait=wait, routed=True)
        self.backoff = getattr(client, "poll_backoff", None)
        to_person = None
        if messages:
            direct, _ = client.inbox(after=cursor, wait=0)
            to_person = {m.get("id") for m in direct if isinstance(m, dict)}
        return append_notifications(self.agent_config, messages, new_cursor, session, self.now(), to_person,
                                    folder=self.folder)

    def drop(self):
        """The server refused the session (401): the account leaves this app, and the window says so once."""
        removed = accounts.remove_account(self.agent_config, self.email)
        if removed is not None:
            accounts.record_dropped(self.agent_config, removed.email, removed.display_name)
        self.dropped = True
        self.stop.set()
        self.log("notification feed: an account's session ended; it was removed from this app")

    def run(self):
        while not self.stop.is_set():
            try:
                added = self.poll_once()
                self.failures = 0
                if added is None:
                    self.stop.set()  # no longer held: AccountFeeds starts a new poller if it comes back
                    return
                if self.backoff:
                    self.sleep(self.backoff)
            except ApiError as exc:
                if exc.status == 401:
                    self.drop()
                    return
                self._failed(exc)
            except (ConfigError, OSError) as exc:
                self._failed(exc)

    def _failed(self, exc):
        self.failures += 1
        self.log(f"notification feed (account): {type(exc).__name__}"
                 + (f" {exc.code}" if isinstance(exc, ApiError) else ""))
        self.sleep(min(FEED_BACKOFF[1], FEED_BACKOFF[0] * 2 ** min(self.failures, 6)))

    def start(self):
        thread = threading.Thread(target=self.run, name="raincli-account-feed", daemon=True)
        thread.start()
        return thread


class AccountFeeds:
    """One ``AccountFeed`` per account in ``accounts.json``, reconciled every ``RECONCILE_EVERY`` seconds."""

    def __init__(self, agent_config, *, feed_factory=None, log=None, sleep=None):
        self.agent_config = agent_config
        self.feed_factory = feed_factory or (lambda email: AccountFeed(agent_config, email, log=self.log))
        self.log = log or (lambda text: None)
        self.feeds: dict[str, AccountFeed] = {}
        self.stop = threading.Event()
        self.sleep = sleep or self.stop.wait

    def held(self) -> list[str]:
        try:
            return [a.email.lower() for a in accounts.load_accounts(self.agent_config)]
        except ConfigError:
            return list(self.feeds)  # a damaged file changes nothing until it is readable again

    def reconcile(self):
        """Start pollers for new accounts, stop those of removed ones (or that ended), then prune their feeds."""
        held = set(self.held())
        for email in list(self.feeds):
            feed = self.feeds[email]
            if email not in held or feed.stop.is_set():
                feed.stop.set()
                del self.feeds[email]
        for email in sorted(held - set(self.feeds)):
            feed = self.feed_factory(email)
            self.feeds[email] = feed
            feed.start()
        accounts.prune_feeds(self.agent_config)
        return sorted(self.feeds)

    def run(self):
        while not self.stop.is_set():
            try:
                self.reconcile()
            except (ConfigError, OSError) as exc:
                self.log(f"account feeds: {type(exc).__name__}")
            self.sleep(RECONCILE_EVERY)
        for feed in self.feeds.values():
            feed.stop.set()

    def start(self):
        thread = threading.Thread(target=self.run, name="raincli-account-feeds", daemon=True)
        thread.start()
        return thread
