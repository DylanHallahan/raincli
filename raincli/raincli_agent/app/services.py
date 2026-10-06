"""What the app window asks of the client core (protocol §16.10). One thin layer, so the window has no
business logic of its own and the CLI and the app share every behaviour.

Nothing here returns a credential: results are status, names and messages for the page.
The person session (``person.json``), the app install token and the notification queue are the
client's (``raincli_agent.person``; §16.3, §16.10, §16.14 S3, §16.15).
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import __version__
from ..config import Secret, default_config_path
from ..errors import RainError


class ServiceError(RainError):
    """A failure the page shows as text."""


STALE_SENTENCES = {  # §16.17 item 4, §16.18 V4 and V7
    "invalid": "This computer's saved RainCLI setup belongs to a machine that was revoked.",
    "not_owner": "This computer's saved RainCLI setup belongs to a machine owned by another account. "
                 "The other machine stays active for its owner until they revoke it.",
    "unreadable": "This computer's saved RainCLI setup can't be read by this Windows account.",
}
STALE_CODES = {"machine_credential_invalid": "invalid", "not_machine_owner": "not_owner"}  # person_only's 409s


class StaleCredential(ServiceError):
    """§16.17: the saved machine credential is revoked (``invalid``), another account's (``not_owner``) or
    can't be read by this Windows account (``unreadable``, §16.18 V4).
    The page offers "Set up this computer as a new machine"; nothing happens without that choice."""

    def __init__(self, reason, old_handle=None):
        super().__init__(STALE_SENTENCES[reason])
        self.reason = reason
        self.old_handle = old_handle


def _person():
    from .. import person  # the client's person-session store, install token and API
    return person


class Services:
    def __init__(self, root, host, *, paths, open_file=None):
        self.root = root
        self.host = host
        self._paths = paths  # () -> (agent_config, runtime_config), from app.json or the defaults
        self._open_file = open_file or getattr(os, "startfile", None)

    # -- configuration ------------------------------------------------------------------------------

    @property
    def agent_config(self):
        return self._paths()[0]

    @property
    def runtime_config(self):
        return self._paths()[1]

    def signed_in(self):
        return os.path.exists(self.agent_config) and os.path.exists(self.runtime_config)

    def has_person_session(self):
        try:
            return _person().load_session(self.agent_config) is not None
        except Exception:  # noqa: BLE001 - no session, or not readable here: sign in again
            return False

    def _config(self):
        from ..config import load_config
        return load_config(self.agent_config)

    def _client(self, token=None, timeout=15):
        from ..api import ApiClient
        config = self._config()
        return ApiClient(config.api_url, token or config.token, timeout=timeout, max_attempts=1)

    # -- sign-in and sign-out ----------------------------------------------------------------------------

    def sign_in_defaults(self):
        return {"machine_name": self.suggested_machine_name()}

    def suggested_machine_name(self, avoid=None):
        """The computer's name in handle form, never ``avoid`` (the old machine's handle, §16.17 item 4)."""
        from .. import login
        name = login.default_machine_name()
        if avoid and name == avoid:
            name = (name[:28].rstrip("-") or "pc") + "-new"
        return name

    def fresh_api_url(self):
        """The service a fresh sign-in goes to: the saved setup's, else the newest ``replaced-*`` backup's
        (§16.17 item 5), else the default. Read as plain JSON; nothing in it is decrypted."""
        import json as _json
        from .. import login
        from ..config import validate_api_url
        config = Path(self.agent_config or default_config_path())
        candidates = [config] + sorted(config.parent.glob("replaced-*/" + config.name), reverse=True)
        for path in candidates:
            try:
                return validate_api_url(_json.loads(path.read_text(encoding="utf-8"))["api_url"])
            except (OSError, ValueError, KeyError, TypeError, RainError):
                continue
        return login.DEFAULT_API_URL

    def sign_in(self, email, password, *, machine_name, team=None, replace=False, again=False, new_machine=False):
        """``login.login`` with a person session (§15.2, §16.3). ``password`` is a ``Secret``; it is never
        stored, logged or returned. With a credential already here and no ``again``, only a person session
        is added (``person_only``), never a new machine, after ``check_credential`` (§16.17 item 4): a
        revoked or foreign credential raises ``StaleCredential``. ``new_machine`` (the user's explicit
        choice) moves the old setup aside, then signs in fresh."""
        from .. import login
        if not isinstance(password, Secret):
            raise TypeError("the password must be a Secret")
        if new_machine:
            return self._sign_in_new_machine(email, password, machine_name=machine_name, team=team)
        if self.signed_in() and not again:
            state = login.check_credential(self.agent_config, email)
            if state in STALE_SENTENCES:
                raise StaleCredential(state, self.machine_handle() if state == "not_owner" else None)
            try:
                _person().add_session(self.agent_config, email, password)  # person_only: no rotation (§16.3)
            except login.LoginError as exc:
                reason = STALE_CODES.get(getattr(exc, "code", None))
                if reason is None:
                    raise
                raise StaleCredential(reason, self.machine_handle() if reason == "not_owner" else None) from None
            return {"handle": login.describe(self.agent_config)[1]}
        if again:
            self.host.pause()  # the runtime would otherwise publish with a credential being replaced
        config_path = self.agent_config or default_config_path()
        plan = login.prepare(config_path, force=again)
        return login.login(email, password, plan=plan, machine_name=machine_name, team=team or None,
                           replace=replace, person_session=True, api_url=self.fresh_api_url())

    def _sign_in_new_machine(self, email, password, *, machine_name, team=None):
        """§16.17 items 4 and 5: set the old setup aside (``login.set_aside``), then a normal fresh sign-in
        with ``person_session``, under a name that is never the old handle."""
        from .. import login
        api_url = self.fresh_api_url()
        old_handle = self.machine_handle() if os.path.exists(self.agent_config or "") else None
        if old_handle and machine_name == old_handle:
            machine_name = self.suggested_machine_name(avoid=old_handle)
        self.host.pause()  # §16.18 V6: stop the app's own runtime first; the fresh sign-in starts the new one
        if os.path.exists(self.agent_config or ""):
            login.set_aside(self.agent_config)
        config_path = str(default_config_path())  # app.json's entries were cleared: the default paths
        plan = login.prepare(config_path)
        return login.login(email, password, plan=plan, machine_name=machine_name, team=team or None,
                           person_session=True, api_url=api_url)

    def sign_out(self):
        """Sign-out revokes the machine and its person sessions (§16.3) and deletes them here."""
        from .. import login
        self.host.stop()
        return login.logout(self.agent_config)

    def sign_out_local(self):
        from .. import login
        self.host.stop()
        return login.logout(self.agent_config, local_only=True)

    def machine_handle(self):
        from .. import login
        try:
            return login.describe(self.agent_config)[1]
        except Exception:  # noqa: BLE001
            return None

    # -- the hosted site -------------------------------------------------------------------------------

    def app_install_token(self):
        """This install's ``app_install_token`` (§16.14 S3), kept by the client beside ``person.json``, or
        None. The window sends it only in its User-Agent; it is never logged or put in a URL."""
        try:
            return _person().app_install_token(self.agent_config)
        except Exception:  # noqa: BLE001 - not signed in yet
            return None

    def handoff_url(self, path=None):
        """A single-use handoff URL for this person session (§16.10), bound to this app install by
        ``sha256(app_install_token)`` (§16.14 S3)."""
        person = _person()
        if person.load_session(self.agent_config) is None:
            raise ServiceError("this computer has no person session; sign in again")
        _token, install_hash = person.app_install(self.agent_config)
        client = person.PersonClient.for_config(self.agent_config, timeout=15, max_attempts=1)
        return client.api.request("POST", "/app/handoff", body={"app_install_hash": install_hash})[1]["url"]

    def service_url(self):
        return self._config().api_url

    def message_summary(self, message_id):
        """``{kind, sender, conversation_id}`` for a toast, from the person API. Never the body (§16.12 C14)."""
        from .policy import toast_sender
        message = _person().PersonClient.for_config(self.agent_config, timeout=10, max_attempts=1).message(message_id)
        return {"kind": message.get("kind") or "message", "sender": toast_sender(message),
                "conversation_id": message.get("conversation_id")}

    def notifications(self):
        """New entries ``{id, kind, at}`` from the runtime's notification queue (§16.10), oldest first."""
        return _person().take_notifications(self.agent_config)

    # -- status and settings ---------------------------------------------------------------------------------

    def status(self):
        """This computer: connection, machine, version, updates, routing and agents. No credentials."""
        from ..runtime import winapp
        from ..runtime.service import status as runtime_status
        from . import status as model
        try:
            raw = runtime_status(self.runtime_config)
        except Exception:  # noqa: BLE001 - not signed in, or no runtime yet
            raw = {"status": "not_observed"}
        mode = winapp.update_mode(self.root) if self.root else None
        update = ((raw.get("client") or {}).get("update_state")) if isinstance(raw, dict) else None
        out = {"connection": "paused" if self.host.paused else model.icon_state(raw), "version": __version__,
               "updates": " · ".join(x for x in (mode, update) if x) or None, "paused": self.host.paused,
               "machine": self.machine_handle(), "team": None, "routing": None, "agents": []}
        if self.signed_in():
            try:
                client = self._client(timeout=8)
                me = client.me()["agent"]
                out["machine"], out["team"] = me["handle"], me["team"]["name"]
                mine = next((m for m in client.agents() if m.get("handle") == me["handle"]), {})
                out["routing"] = mine.get("routing")
                out["agents"] = [{k: a.get(k) for k in ("name", "type", "status", "reachability")}
                                 for a in mine.get("agents") or []]
            except Exception:  # noqa: BLE001 - offline: the local facts still show
                out["connection"] = "offline"
        return out

    def settings(self):
        from ..runtime import winapp
        trust = _trust_settings(self.runtime_config)
        routing = None
        try:
            routing = self._client(timeout=8).request("GET", "/routing")[1]["routing"]
        except Exception:  # noqa: BLE001
            pass
        return {"routing": routing, "update_mode": winapp.update_mode(self.root) if self.root else None, **trust}

    def save_settings(self, change):
        """One change at a time: routing (§16.5), trust (§16.12 C5) or update mode (§14)."""
        from ..runtime import winapp
        if not isinstance(change, dict) or len(change) != 1:
            raise ServiceError("one setting at a time")
        (key, value), = change.items()
        if key == "routing":
            if value not in ("all", "inbox-only"):
                raise ServiceError("routing is all or inbox-only")
            self._client().request("PUT", "/routing", body={"routing": value})
            return "Saved: " + ("any named agent here can be messaged." if value == "all"
                                else "only this computer's inbox can be messaged.")
        if key == "update_mode":
            if value not in ("automatic", "manual"):
                raise ServiceError("updates are automatic or manual")
            winapp.configure(self.root, mode=value)
            return f"Saved: updates are {value}."
        if key in ("trust_mode", "trust_add", "trust_remove"):
            from .. import trust
            if key == "trust_mode":
                trust.set_mode(self.runtime_config, value)
            elif key == "trust_add":
                trust.add(self.runtime_config, value)
            else:
                trust.remove(self.runtime_config, value)
            return "Saved."
        raise ServiceError("unknown setting")

    def toggle_pause(self):
        if self.host.paused:
            self.host.resume()
        else:
            self.host.pause()
        return self.host.paused

    def open_log(self):
        log = Path(self.runtime_config).parent / "runtime-state" / "runtime.log"
        app_log = Path(self.root) / "state" / "runtime.log" if self.root else None
        for path in (app_log, log):
            if path is not None and path.exists() and self._open_file is not None:
                self._open_file(str(path))  # the user's own private log
                return True
        return False


def _trust_settings(runtime_config):
    try:
        from .. import trust
        return trust.describe(runtime_config)
    except Exception:  # noqa: BLE001 - not signed in yet
        return {"trust_mode": None, "trusted_senders": []}
