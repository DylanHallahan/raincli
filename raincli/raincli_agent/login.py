"""Machine sign-in and sign-out (protocol 15.1, 15.2), shared by the CLI and the tray app.

The password is held only in a ``Secret`` for the duration of the request. It
is never written, logged, put in argv or the environment, or included in an
error message. One request is one attempt: sign-in is never retried
automatically, so a retry can never spend the shared login limiter's budget.
"""
import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request

from . import __version__
from .api import _build_opener, encode_body
from .config import (Secret, default_config_path, load_config, validate_api_url, validate_token,
                     write_config)
from .errors import ApiError, ConfigError, RainError, Unreachable, error_for
from .fsutil import atomic_write_json
from .text import escape_text

DEFAULT_API_URL = "https://raincli.com"
TRUST_KEYS = {"trust_mode", "trusted_senders", "blocked_senders"}
MACHINE_KEYS = {"machine_config", "state_dir", "herdr_bin", "herdr_session", "owner_email"} | TRUST_KEYS
HANDLE_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
TIMEOUT = 30.0


# -- errors -----------------------------------------------------------------------

class LoginError(RainError):
    """A sign-in reply the user must act on. ``code`` is the server's error code."""

    code = "error"

    def __init__(self, message, *, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class InvalidCredentials(LoginError):
    code = "invalid_credentials"


class RateLimited(LoginError):
    code = "rate_limited"


class TeamChoiceRequired(LoginError):
    code = "team_choice_required"

    def __init__(self, message, teams):
        super().__init__(message)
        self.teams = teams  # [{"slug", "name"}]


class NameTaken(LoginError):
    code = "name_taken"


class NameInUse(LoginError):
    """The user's own active machine already has this name and the request did
    not prove it may be replaced (15.8 H2). ``replace=True`` may be sent after
    the user confirms, for a machine without delivery history."""
    code = "name_in_use"


class MfaRequired(LoginError):
    code = "mfa_required"


class InvalidRequest(LoginError):
    code = "invalid"


class AlreadySignedIn(LoginError):
    code = "already_signed_in"


class StaleMachineCredential(LoginError):
    """§16.17: ``person_only`` refused this computer's machine credential (revoked, rotated or
    unknown). The fix is a new machine (``raincli login --new-machine``)."""
    code = "machine_credential_invalid"


class NotMachineOwner(LoginError):
    """§16.17: this computer's machine belongs to another account."""
    code = "not_machine_owner"


class ConnectorMachine(LoginError):
    """The credential feeds connector delivery (a migrated install): a new
    sign-in would replace it with a machine that has no delivery (15.6)."""
    code = "connector_machine"


# -- machine names ------------------------------------------------------------------

def slugify_machine_name(name):
    """The computer name in the handle grammar (15.8 L1): lowercase; each run of
    characters outside ``[a-z0-9]`` becomes ``-``; ``-`` stripped from both ends;
    ``m-`` in front unless it starts with a letter; at most 32 characters, with
    ``-`` stripped again; ``machine`` when shorter than 2 characters."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if not ("a" <= slug[:1] <= "z"):
        slug = "m-" + slug
    slug = slug[:32].strip("-")
    return slug if len(slug) >= 2 else "machine"


def computer_name():
    """This computer's name: %COMPUTERNAME% on Windows, else the host name's first label."""
    if os.name == "nt" and os.environ.get("COMPUTERNAME"):
        return os.environ["COMPUTERNAME"]
    return socket.gethostname().split(".")[0]


def default_machine_name():
    return slugify_machine_name(computer_name())


def suggest_new_machine_name(old_handle=None):
    """A machine name for "Set up this computer as a new machine" (§16.17 4): the computer name
    in handle form, never the old handle. When the old handle is unknown (its credential no
    longer answers), the computer name gets a ``-2`` suffix, as the old machine most likely
    carried it."""
    base = default_machine_name()
    if old_handle is not None and base != old_handle:
        return base
    for n in range(2, 100):
        candidate = f"{base[:32 - len(str(n)) - 1].rstrip('-')}-{n}"
        if candidate != old_handle:
            return candidate
    return "machine-new"


def known_handle(agent_config):
    """The old machine's handle when its credential still answers ``/me`` (``not_owner``), else None."""
    return existing_identity(agent_config)[1] if os.path.lexists(agent_config) else None


from .setaside import (OFFER, SetAsideRefused, check_credential, set_aside,  # noqa: E402,F401 - §16.17 API
                       stale_message)


def check_machine_name(name):
    if not isinstance(name, str) or not HANDLE_RE.fullmatch(name):
        raise InvalidRequest("machine name must be 2-32 characters: a lowercase letter, then lowercase "
                             "letters, digits or '-'")
    return name


# -- local paths ----------------------------------------------------------------------

def runtime_config_path(agent_config):
    """The runtime config beside an agent config (``runtime.json``)."""
    return str(Path(agent_config).absolute().parent / "runtime.json")


def machine_runtime(agent_config, owner_email=None, keep=None):
    """The machine-mode runtime config (15.4) for an agent config. ``keep`` is the
    previous machine-mode config, whose trust settings (§16.12 C5) survive a sign-in."""
    data = {k: v for k, v in (keep or {}).items() if k in TRUST_KEYS}
    data.update(machine_config=str(Path(agent_config).absolute()), state_dir="runtime-state")
    if owner_email:
        data["owner_email"] = owner_email.strip().lower()
    return data


def record_owner(agent_config, email):
    """Remember the owner's email in this machine's machine-mode runtime config, for
    ``"escalation": {"to": "owner"}`` (§16.8) and owner trust (§16.12 C5)."""
    runtime_path = runtime_config_path(agent_config)
    data = read_runtime_json(runtime_path)
    if runtime_mode(runtime_path) != "machine" or not _same(
            Path(runtime_path).parent / Path(data["machine_config"]).expanduser(), agent_config):
        return False
    data["owner_email"] = email.strip().lower()
    atomic_write_json(runtime_path, data)
    return True


def read_runtime_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def connector_agent_config(path, data):
    """The agent config a connector config's mapping uses (15.8 M8: missing means
    the default path), or None when ``data`` is not a connector config."""
    if not isinstance(data, dict) or not ({"herdr_agent", "inbox"} & set(data)) or "connectors" in data:
        return None
    value = data.get("agent_config")
    if isinstance(value, str) and value:
        return str(Path(path).absolute().parent / Path(value).expanduser())
    return default_config_path()


def scan_directories(agent_config=None):
    """Where connector and runtime configs live (15.6): ~/.config/raincli, the
    directory of $RAINCLI_CONFIG and the directory of ``agent_config``."""
    from .config import standard_config_path
    dirs = [Path(standard_config_path()).parent, Path(default_config_path()).absolute().parent]
    if agent_config:
        dirs.append(Path(agent_config).absolute().parent)
    unique = []
    for d in dirs:
        if all(not _same(d, u) for u in unique):
            unique.append(d)
    return unique


def json_files(directories):
    for directory in directories:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix.lower() != ".json" or not entry.is_file() or entry.is_symlink():
                continue
            try:
                if entry.stat().st_size > 1024 * 1024:
                    continue
                data = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            yield entry, data


def connector_references(agent_config, directories=None):
    """Connector configs that deliver through ``agent_config``, from the config
    directories and from every connector-mode runtime config there (15.8 H3)."""
    agent_config = str(Path(agent_config).absolute())
    found = []

    def add(path):
        if all(not _same(path, f) for f in found):
            found.append(str(path))
    for path, data in json_files(directories or scan_directories(agent_config)):
        target = connector_agent_config(path, data)
        if target is not None and _same(target, agent_config):
            add(path)
        elif isinstance(data, dict) and isinstance(data.get("connectors"), list):
            for entry in data["connectors"]:
                if not isinstance(entry, str) or not entry:
                    continue
                connector = path.parent / Path(entry).expanduser()
                try:
                    cfg = json.loads(connector.read_text(encoding="utf-8"))
                except (OSError, ValueError, UnicodeDecodeError):
                    continue
                target = connector_agent_config(connector, cfg)
                if target is not None and _same(target, agent_config):
                    add(connector)
    return found


def runtime_mode(runtime_path):
    """"machine", "connector", None (absent) or "other" for a runtime config file."""
    data = read_runtime_json(runtime_path)
    if data is None:
        return None
    if set(data) <= MACHINE_KEYS and isinstance(data.get("machine_config"), str):
        return "machine"
    if isinstance(data.get("connectors"), list) and data["connectors"]:
        return "connector"
    return "other"


# -- the endpoints ----------------------------------------------------------------------

def _redact(text, *secrets):
    text = escape_text(str(text), allow_newlines=False)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text[:300]


def _post(api_url, path, body, *, token=None, secrets=(), timeout=TIMEOUT):
    """One POST, no redirects, no retries. Returns ``(status, payload)``; an error
    reply raises ``ApiError`` with ``payload`` attached. Nothing echoes the body."""
    api_url = validate_api_url(api_url)
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "User-Agent": f"raincli-agent/{__version__}"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token.reveal()
    request = urllib.request.Request(api_url + "/api/v1" + path, data=encode_body(body), method="POST",
                                     headers=headers)
    hidden = [s for s in secrets] + ([token.reveal()] if token is not None else [])
    try:
        with _build_opener(api_url).open(request, timeout=timeout) as response:
            status, raw = response.status, response.read(1024 * 1024)
            retry_after = None
    except urllib.error.HTTPError as exc:
        status, retry_after = exc.code, exc.headers.get("Retry-After")
        try:
            raw = exc.read(1024 * 1024)
        except (OSError, http.client.HTTPException):
            raw = b""
        finally:
            exc.close()
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        reason = getattr(exc, "reason", None) or type(exc).__name__
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise Unreachable(f"TLS certificate verification failed for {api_url}") from None
        raise Unreachable(f"cannot reach {api_url}: {_redact(reason, *hidden)}") from None
    try:
        payload = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        payload = None
    if 300 <= status < 400:
        raise ApiError(f"server answered {status}; redirects are never followed", code="redirect_refused",
                       status=status)
    if status >= 400:
        code, message = None, f"HTTP {status}"
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            raw_code, raw_message = payload["error"].get("code"), payload["error"].get("message")
            code = _redact(raw_code, *hidden) if isinstance(raw_code, str) else None
            message = _redact(raw_message, *hidden) if isinstance(raw_message, str) and raw_message else message
        try:
            seconds = max(0, int(float(retry_after))) if retry_after else None
        except ValueError:
            seconds = None
        error = error_for(status, code, message, retry_after=seconds)
        error.payload = payload if isinstance(payload, dict) else {}
        raise error
    if not isinstance(payload, dict):
        raise ApiError("server returned a non-JSON response", code="bad_response", status=status)
    return status, payload


def _teams(payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    teams = (error or {}).get("teams", payload.get("teams") if isinstance(payload, dict) else None)
    out = []
    for team in teams if isinstance(teams, list) else []:
        if isinstance(team, dict) and isinstance(team.get("slug"), str):
            out.append({"slug": escape_text(team["slug"], allow_newlines=False)[:64],
                        "name": escape_text(str(team.get("name") or team["slug"]), allow_newlines=False)[:120]})
    return out


def request_login(api_url, email, password, machine_name, team=None, *, previous_token=None, replace=False,
                  person_session=False):
    """``POST /api/v1/app/login``. Returns the validated reply; raises a ``LoginError``
    for the codes a user acts on (15.1), or the transport's ``ApiError``."""
    if not isinstance(password, Secret):
        raise TypeError("password must be a Secret")
    check_machine_name(machine_name)
    body = {"email": email, "password": password.reveal(), "machine_name": machine_name}
    if team:
        body["team"] = team
    if previous_token is not None:
        body["previous_token"] = previous_token.reveal()
    if replace:
        body["replace"] = True
    if person_session:
        body["person_session"] = True  # §16.12 C15: only when asked
    secrets = [password.reveal()] + ([previous_token.reveal()] if previous_token is not None else [])
    try:
        status, reply = _post(api_url, "/app/login", body, secrets=secrets)
    except ApiError as exc:
        _login_error(exc, machine_name)
        raise
    finally:
        body.clear()
    try:
        token = validate_token(reply.get("token"))
        handle = reply.get("handle")
        team_info = reply.get("team") or {}
        if not isinstance(handle, str) or not HANDLE_RE.fullmatch(handle):
            raise ConfigError("bad handle")
        if not isinstance(team_info.get("slug"), str):
            raise ConfigError("bad team")
        reply_url = validate_api_url(reply["api_url"]) if reply.get("api_url") else None
    except (ConfigError, KeyError, AttributeError):
        raise ApiError("the server's sign-in reply is malformed", code="bad_response", status=status) from None
    person = None
    if person_session:
        from .person import validate_session
        try:
            person = validate_session(reply.get("person_session"))
        except ConfigError:
            raise ApiError("the server's sign-in reply is malformed", code="bad_response", status=status) from None
    return {"token": token, "handle": handle, "rotated": bool(reply.get("rotated")),
            "team": {"slug": team_info["slug"], "name": str(team_info.get("name") or team_info["slug"])},
            "api_url": reply_url, "status": status, "person_session": person}


def _login_error(exc, machine_name=None):
    """Raise the ``LoginError`` a sign-in error reply maps to (15.1); return otherwise."""
    payload, code = getattr(exc, "payload", {}), exc.code
    if code == "invalid_credentials" or exc.status == 401:
        raise InvalidCredentials("the email or password is not correct") from None
    if code == "rate_limited" or exc.status == 429:
        wait = f"; try again in {exc.retry_after} s" if exc.retry_after else "; try again later"
        raise RateLimited("too many sign-in attempts" + wait, retry_after=exc.retry_after) from None
    if code == "team_choice_required":
        raise TeamChoiceRequired("you are a member of several teams; choose one", _teams(payload)) from None
    if code == "name_in_use":
        raise NameInUse(f"you already have a machine named {machine_name}. Replace it (its old credential "
                        "is revoked), or choose another name. A machine that has received messages can only "
                        "be replaced from that machine, or revoked on the website") from None
    if code == "name_taken":
        raise NameTaken(f"the machine name {machine_name} is taken in this team (another member's "
                        "machine, or a revoked one); choose another name") from None
    if code == "mfa_required":
        raise MfaRequired("this account needs multi-factor sign-in, which this client does not support "
                          "yet; sign in on the website") from None
    if code == "invalid" or exc.status == 400:
        raise InvalidRequest(f"the server refused the sign-in request: {exc}") from None


def request_person_only(api_url, email, password, machine_token):
    """``POST /api/v1/app/login`` with ``person_only`` (§16.3): a person session for the
    owner of the machine whose current credential is ``machine_token``. It rotates
    nothing. Returns the session as a ``Secret``."""
    if not isinstance(password, Secret):
        raise TypeError("password must be a Secret")
    body = {"email": email, "password": password.reveal(), "previous_token": machine_token.reveal(),
            "person_only": True}
    try:
        status, reply = _post(api_url, "/app/login", body, secrets=[password.reveal(), machine_token.reveal()])
    except ApiError as exc:
        from .setaside import stale_message
        if exc.code == "machine_credential_invalid":
            raise StaleMachineCredential(stale_message("invalid")) from None
        if exc.code == "not_machine_owner":
            raise NotMachineOwner(stale_message("not_owner")) from None
        if exc.code == "invalid" or exc.status == 400:
            raise InvalidRequest("the server refused a person session for this machine: sign in with the "
                                 "email and password of the machine's owner, or sign in again") from None
        _login_error(exc)
        raise
    finally:
        body.clear()
    from .person import validate_session
    try:
        return validate_session(reply.get("person_session"))
    except ConfigError:
        raise ApiError("the server's sign-in reply is malformed", code="bad_response", status=status) from None


def request_sign_out(config):
    """``POST /api/v1/app/sign-out`` with the machine's own credential."""
    return _post(config.api_url, "/app/sign-out", {}, token=config.token)[1]


# -- the operations ----------------------------------------------------------------------

def existing_identity(config_path):
    """``(token, handle)`` of a currently valid credential at ``config_path``, else
    ``(None, None)``. It becomes ``previous_token``, the proof for a rotation (15.8 H2a)."""
    try:
        config = load_config(config_path)
    except ConfigError:
        return None, None
    try:
        from .api import ApiClient
        me = ApiClient.from_config(config, timeout=15, max_attempts=1).me()
        return config.token, me["agent"]["handle"]
    except (ApiError, KeyError, TypeError):
        return None, None


def prepare(config_path=None, force=False, *, identity=existing_identity):
    """Everything decided before a password is asked for (15.2, 15.8 H3):

    - no credential: a new sign-in;
    - a credential needs ``--force``;
    - a credential that connector delivery uses may only be rotated for its own
      handle, with its own token as proof; its runtime config is never replaced;
    - otherwise only a machine-mode (or absent) runtime config is replaced.

    Returns a plan for ``login``."""
    config_path = str(Path(config_path or default_config_path()).absolute())
    runtime_path = runtime_config_path(config_path)
    plan = {"config": config_path, "runtime_config": runtime_path, "previous_token": None,
            "handle": None, "connectors": [], "write_runtime": True}
    mode = runtime_mode(runtime_path)
    connectors = connector_references(config_path)
    if not os.path.lexists(config_path):
        if connectors:
            # A new handle in a file that connector configs still name would reroute
            # their delivery (15.8 H3, review 1a F3).
            raise ConnectorMachine(
                f"connector configs still name {config_path} ({', '.join(connectors)}); signing in would give "
                "them a new machine's credential. Remove or repoint those connector configs first")
        if mode not in (None, "machine"):
            raise ConnectorMachine(f"{runtime_path} belongs to another setup and is never replaced by a sign-in")
        return plan
    if not force:
        raise AlreadySignedIn(f"this machine is already signed in ({config_path}); run raincli logout first, "
                              "or use --force to sign in again")
    token, handle = identity(config_path)
    plan.update(previous_token=token, handle=handle, connectors=connectors)
    if connectors or mode == "connector":
        if token is None:
            raise ConnectorMachine(
                f"{config_path} is the credential of this machine's connector delivery, and it is not currently "
                "valid, so it cannot prove a rotation. Signing in would route nothing to this machine. Revoke "
                "the machine on the website and set the connector up again, or keep it")
        plan["write_runtime"] = False
        return plan
    if mode not in (None, "machine"):
        raise ConnectorMachine(f"{runtime_path} belongs to another setup and is never replaced by a sign-in")
    return plan


def login(email, password, *, plan=None, machine_name=None, team=None, replace=False, api_url=DEFAULT_API_URL,
          force=False, config_path=None, person_session=False):
    """Sign this machine in; write its config and, in machine mode, its runtime config.

    Returns ``{"handle", "team", "rotated", "config", "runtime_config", "api_url", ...}``.
    Raises ``TeamChoiceRequired`` (with ``teams``), ``NameInUse``, ``NameTaken``,
    ``RateLimited``, ``InvalidCredentials`` and the other ``LoginError``s, which the
    caller handles (asking again; the tray asks for the password again)."""
    plan = plan or prepare(config_path, force)
    api_url = validate_api_url(api_url)
    machine_name = check_machine_name(machine_name or plan["handle"] or default_machine_name())
    if plan["connectors"] or not plan["write_runtime"]:
        if machine_name != plan["handle"]:
            raise ConnectorMachine(f"this machine's connector delivery uses the handle {plan['handle']}; "
                                   "signing in again may only rotate that same machine")
    previous = plan["previous_token"] if machine_name == plan["handle"] else None
    reply = request_login(api_url, email, password, machine_name, team, previous_token=previous, replace=replace,
                          person_session=person_session)
    if not plan["write_runtime"] and reply["handle"] != plan["handle"]:
        raise ConnectorMachine("the server did not rotate this machine; the connector credential was left unchanged")
    config_path, runtime_path = plan["config"], plan["runtime_config"]
    # The credential is used with the origin it was obtained from; the reply's
    # api_url is advisory (a different public URL is reported, never followed).
    write_config(config_path, api_url, reply["token"], force=True)
    if plan["write_runtime"]:
        keep = read_runtime_json(runtime_path) if runtime_mode(runtime_path) == "machine" else None
        atomic_write_json(runtime_path, machine_runtime(config_path, email, keep))
    # A new machine credential ends this machine's person sessions on the server (§16.12 C6);
    # either way the install token is rotated (§16.14 S3: sign-in again).
    from . import person
    if reply["person_session"] is not None:
        person.save_session(config_path, reply["person_session"])
    else:
        person.clear_session(config_path)
    result = {"handle": reply["handle"], "team": reply["team"], "rotated": reply["rotated"],
              "config": config_path, "runtime_config": runtime_path, "runtime_written": plan["write_runtime"],
              "api_url": api_url, "server_api_url": reply["api_url"],
              "person_session": reply["person_session"] is not None}
    from .runtime import winapp
    root = winapp.app_root()
    if root is not None:
        winapp.write_settings(root, {"agent_config": config_path, "runtime_config": runtime_path})
        result["startup"] = winapp.enable_logon_start(root)
    return result


def logon_start_hint(runtime_path):
    from .runtime import winapp
    if winapp.app_root() is not None:
        return "The RainCLI app starts this machine's runtime at logon."
    return ("Start it now with: raincli runtime run --config " + runtime_path
            + "\nStart it at logon with: raincli runtime startup --config " + runtime_path)


def describe(config_path=None):
    """``(config path, handle or None)`` for the sign-out confirmation (15.8 M9)."""
    config_path = str(Path(config_path or default_config_path()).absolute())
    if not os.path.lexists(config_path):
        raise ConfigError(f"this machine is not signed in ({config_path} does not exist)")
    return config_path, existing_identity(config_path)[1]


def logout(config_path=None, *, local_only=False):
    """Sign this machine out (15.2, 15.8 M9): revoke it on the server, stop and
    disable this machine's runtime at logon, then delete the local credential and
    its runtime config. Connector configs and queues stay.

    If the server call fails the credential is kept and the error raised. A
    credential the server already rejects (revoked) is cleaned up. ``local_only``
    skips the server; the machine then stays listed until revoked on the website."""
    from .runtime.service import request_stop
    from .runtime import startup, winapp
    config_path = str(Path(config_path or default_config_path()).absolute())
    runtime_path = runtime_config_path(config_path)
    if not os.path.lexists(config_path):
        raise ConfigError(f"this machine is not signed in ({config_path} does not exist)")
    server = "skipped"
    if not local_only:
        config = load_config(config_path)
        try:
            request_sign_out(config)
            server = "signed_out"
        except ApiError as exc:
            if exc.status != 401:
                raise
            server = "already_revoked"
    mode = runtime_mode(runtime_path)
    uses = mode == "machine" and _same(Path(runtime_path).parent / Path(
        read_runtime_json(runtime_path)["machine_config"]).expanduser(), config_path)
    uses = uses or bool(connector_references(config_path)) and mode == "connector"
    result = {"server": server, "removed": [], "runtime": None, "startup": None,
              "connectors_left": connector_references(config_path)}
    if uses:
        try:
            result["runtime"] = request_stop(runtime_path)["status"]
        except (ConfigError, OSError):
            pass
    root = winapp.app_root()
    try:
        if root is not None:
            result["startup"] = winapp.disable_logon_start(root)
            winapp.write_settings(root, {})
        elif uses:
            result["startup"] = startup.remove_for(runtime_path)
    except (ConfigError, OSError) as exc:
        result["startup"] = "error:" + type(exc).__name__
    os.unlink(config_path)
    result["removed"].append(config_path)
    from . import person
    if person.clear_session(config_path):  # revoked with the machine (§16.3); install token rotated (§16.14 S3)
        result["removed"].append(str(person.person_path(config_path)))
    if uses:
        os.unlink(runtime_path)
        result["removed"].append(runtime_path)
    return result
