# Vendored from raincli tag v0.4.0 (commit a214082792ff64731cae1d6f12fc5cc900ae8b11), path raincli/raincli_agent/config.py. Test fixture for protocol 16.12 C1; do not edit.
"""Agent config: ``{"api_url": ..., "token": ...}`` in a 0600 JSON file (protocol section 4).

On Windows the token is stored DPAPI-protected as ``token_dpapi`` (protocol 15.3);
``load_config`` reads either form and every Windows write produces ``token_dpapi``."""

import ipaddress
import json
import os
import re
import urllib.parse
from dataclasses import dataclass

from .errors import ConfigError
from .fsutil import atomic_write_json, ensure_private_dir, read_private_file

TOKEN_RE = re.compile(r"^rca_[A-Za-z0-9_-]{43}$")


class Secret:
    """Holds a credential. ``repr``/``str`` never reveal it."""

    __slots__ = ("_value",)

    def __init__(self, value):
        self._value = value

    def reveal(self):
        return self._value

    def __repr__(self):
        return "Secret('***')"

    __str__ = __repr__

    def __eq__(self, other):
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self):
        return hash(self._value)

    def __reduce__(self):
        raise TypeError("Secret cannot be pickled")


def standard_config_path():
    """The default location, ignoring $RAINCLI_CONFIG."""
    return os.path.join(os.path.expanduser("~"), ".config", "raincli", "agent.json")


def default_config_path(env=None):
    env = os.environ if env is None else env
    if env.get("RAINCLI_CONFIG"):
        return env["RAINCLI_CONFIG"]
    return standard_config_path()


def is_loopback_host(host):
    if not host:
        return False
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_api_url(url):
    """Return the normalized API base URL, or raise ConfigError."""
    if not isinstance(url, str) or not url:
        raise ConfigError("api_url must be a non-empty string")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise ConfigError("api_url must not contain whitespace or control characters")
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise ConfigError("api_url must use https://")
    if "?" in url or parts.query:
        raise ConfigError("api_url must not contain a query")
    if "#" in url or parts.fragment:
        raise ConfigError("api_url must not contain a fragment")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise ConfigError("api_url must not contain userinfo")
    try:
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        raise ConfigError("api_url has an invalid port") from None
    if not host:
        raise ConfigError("api_url must include a host")
    if scheme == "http" and not is_loopback_host(host):
        raise ConfigError("api_url must use https:// (http:// is allowed only for loopback hosts)")
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit((scheme, parts.netloc, path, "", ""))


def validate_token(token):
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        raise ConfigError("token must look like rca_ followed by 43 URL-safe characters")
    return Secret(token)


@dataclass(frozen=True)
class AgentConfig:
    api_url: str
    token: Secret
    path: str = ""


def load_config(path=None):
    path = path or default_config_path()
    raw = read_private_file(path, "config file")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ConfigError(f"config file {path} is not valid JSON") from None
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must contain a JSON object")
    return AgentConfig(api_url=validate_api_url(data.get("api_url")), token=token_from(data), path=path)


def protects_tokens():
    """Whether writes store ``token_dpapi`` (Windows) rather than a plain ``token``."""
    return os.name == "nt"


def token_from(data):
    """The token of a parsed agent config, in either stored form (15.3)."""
    if "token_dpapi" in data:
        if "token" in data:
            raise ConfigError("config file holds both token and token_dpapi; sign in again with raincli login --force")
        from .dpapi import unprotect_token
        return validate_token(unprotect_token(data["token_dpapi"]))
    return validate_token(data.get("token"))


def stored_form(api_url, token):
    """The JSON object written for a credential on this platform."""
    if protects_tokens():
        from .dpapi import protect_token
        return {"api_url": api_url, "token_dpapi": protect_token(token.reveal())}
    return {"api_url": api_url, "token": token.reveal()}


def read_token_source(source, stdin, api_url=None):
    """Read a token from a file path or ``-`` (stdin). Never from argv.

    The source may hold the bare token, or the website's downloaded agent
    config JSON (``raincli-<handle>.json``: ``{"api_url": ..., "token": ...}``).
    For JSON, its api_url must equal ``api_url`` after normalisation. No error
    message ever includes the token or the file's content."""
    if TOKEN_RE.match(source.strip()):
        raise ConfigError("refusing a token on the command line; pass a file path or - for stdin")
    if source == "-":
        text = stdin.read()
    else:
        try:
            with open(source, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise ConfigError(f"cannot read token file: {exc.strerror}") from None
        except UnicodeDecodeError:
            raise ConfigError("token file is not UTF-8 text") from None
    if text.lstrip().startswith("{"):
        return _token_from_agent_json(text, api_url)
    return validate_token(text.strip())


def _token_from_agent_json(text, api_url):
    try:
        data = json.loads(text)
    except ValueError:
        raise ConfigError("token file looks like JSON but is not valid JSON "
                          "(expected the downloaded agent config {\"api_url\": ..., \"token\": ...})") from None
    if not isinstance(data, dict) or not isinstance(data.get("token"), str):
        raise ConfigError('agent config JSON must be an object with a "token" string')
    token = validate_token(data["token"])
    if api_url is not None:
        if not isinstance(data.get("api_url"), str):
            raise ConfigError('agent config JSON has no "api_url"; refusing to guess the server')
        try:
            file_url = validate_api_url(data["api_url"])
        except ConfigError as exc:
            raise ConfigError(f"agent config JSON has a bad api_url: {exc}") from None
        wanted = validate_api_url(api_url)
        if file_url != wanted:
            raise ConfigError(f"agent config JSON is for {file_url}, but --api-url is {wanted}; "
                              "refusing to write a config for a different server")
    return token


def write_config(path, api_url, token, force=False):
    api_url = validate_api_url(api_url)
    if not isinstance(token, Secret):
        token = validate_token(token)
    if os.path.lexists(path) and not force:
        raise ConfigError(f"config file {path} already exists (use --force to replace it)")
    directory = os.path.dirname(os.path.abspath(path))
    ensure_private_dir(directory)
    atomic_write_json(path, stored_form(api_url, token), mode=0o600)
    return AgentConfig(api_url=api_url, token=token, path=path)
