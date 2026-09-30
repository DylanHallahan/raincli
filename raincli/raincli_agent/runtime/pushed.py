"""Pushed updates (protocol 14.5 and 14.7 H2/M9): act on the presence reply's target.

The server names a version only. The source stays the canonical repository's
stable release with that tag, resolved to its commit and verified exactly as a
manual ``runtime update --install``. State survives restarts in the managed
root's ``update-state.json``, which the launcher also writes when it rolls back
a version that fails its first start.
"""
from pathlib import Path
import re
import sys
import threading
import time

from .. import __version__
from ..errors import ConfigError
from . import updates

BACKOFF_START = 300  # 5 min, doubling up to 6 h: network and download failures only
BACKOFF_MAX = 6 * 3600
SWITCH_TIMEOUT = 900  # an installed version the launcher has not switched to within this is retried
ERROR_RE = re.compile(r"^[a-z0-9_.:-]{1,64}$")
STATES = ("current", "updating", "failed", "rolled_back")


def error_code(value):
    """A short, secret-free code for ``client.error`` (14.7 M5), or None."""
    if value is None:
        return None
    code = re.sub(r"[^a-z0-9_.:-]", "_", str(value).lower())[:64]
    return code if ERROR_RE.fullmatch(code) else "error"


def target_key(target):
    """What identifies a target row: a new version, flag or set_at is a new target."""
    return {"version": target["version"], "allow_downgrade": target["allow_downgrade"],
            "set_at": target.get("set_at")}


def valid_target(target):
    return (isinstance(target, dict) and isinstance(target.get("version"), str)
            and updates.TAG_RE.fullmatch(target["version"]) is not None
            and isinstance(target.get("allow_downgrade"), bool))


def managed_root_of(python):
    """The managed root whose ``versions/`` holds this interpreter, if any."""
    for parent in Path(python).absolute().parents:
        if parent.name == "versions" and (parent.parent / "current.json").is_file():
            return parent.parent
    return None


class PushedUpdates:
    def __init__(self, root=None, python=None, clock=time.time, resolve=None, install=None, log=None):
        self.python = Path(python or sys.executable).absolute()
        self.root = Path(root or managed_root_of(self.python) or updates.default_root()).expanduser().resolve()
        self.clock = clock
        self.resolve = resolve or updates.resolve
        self.install = install or (lambda release: updates.install(self.root, release))
        self.log = log or (lambda text: print("raincli runtime: " + text, file=sys.stderr, flush=True))
        self.thread = None
        self.launcher_checked = False
        self.lock = threading.Lock()
        self.data = {"state": "current", "error": None}
        if self.managed():
            saved = updates.read_update_state(self.root)
            if saved.get("state") in STATES:
                self.data = saved
            if self.data.get("state") == "updating":
                target = self.data.get("target") or {}
                if target.get("version") and updates.version_key(target["version"]) != updates.version_key(__version__):
                    # An install that died before switching versions: retry after a backoff.
                    self._fail("interrupted", network=True)

    # -- facts ----------------------------------------------------------------

    def managed(self):
        """A managed install whose launcher runs this very interpreter."""
        return ((self.root / "current.json").is_file() and (self.root / "launch.py").is_file()
                and self.python.parent.resolve().is_relative_to(self.root / "versions"))

    def mode(self):
        return updates.update_mode(self.root) if self.managed() else "manual"

    def client(self):
        with self.lock:
            state, error = self.data.get("state", "current"), error_code(self.data.get("error"))
        return {"version": __version__, "update_mode": self.mode(), "update_state": state, "error": error}

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    # -- state ------------------------------------------------------------------

    def _save(self, **changes):
        with self.lock:
            self.data = {**self.data, **changes}
            data = dict(self.data)
        if self.managed():
            try:
                updates.write_update_state(self.root, data)
            except OSError:
                pass  # advisory: reported state still comes from memory

    def _fail(self, code, network=False, target=None):
        failures = int(self.data.get("failures", 0)) + 1 if network else 0
        changes = {"state": "failed", "error": code, "failures": failures}
        if target is not None:
            changes["target"] = target
        if network:
            changes["next_try_at"] = self.clock() + min(BACKOFF_MAX, BACKOFF_START * 2 ** (failures - 1))
        else:
            changes["blocked"] = changes.get("target", self.data.get("target"))  # until the target row changes
        self._save(**changes)

    def started(self):
        """A supervision tick of this version completed: an update to it succeeded,
        and this version's launcher may now replace the one that supervised it."""
        target = self.data.get("target") or {}
        waiting = self.data.get("state") == "updating" or (self.data.get("state") == "failed"
                                                           and self.data.get("error") == "switch_timeout")
        if (waiting and target.get("version")
                and updates.version_key(target["version"]) == updates.version_key(__version__)):
            self._save(state="current", error=None, failures=0, blocked=None)
        if not self.launcher_checked and self.managed() and self.data.get("state") == "current":
            self.launcher_checked = True
            try:
                result = updates.adopt_launcher(self.root, self.python)
            except Exception as exc:  # noqa: BLE001 - advisory; the old launcher keeps working
                result = "error:" + type(exc).__name__
            if result == "adopted":
                self.log("adopted this version's launcher")
            elif result == "candidate_failed":
                self.log("this version's launcher failed its check; keeping the current launcher")

    # -- the decision -------------------------------------------------------------

    def consider(self, target):
        """Called with each presence reply's ``target`` (or None). Never blocks."""
        if self.busy():
            return
        if target is None:
            if self.data.get("state") != "current" or self.data.get("target"):
                self._save(state="current", error=None, target=None, blocked=None, failures=0)
            return
        if not valid_target(target):
            self._save(state="failed", error="bad_target")
            return
        key = target_key(target)
        wanted, installed = updates.version_key(key["version"]), updates.version_key(__version__)
        if wanted == installed:
            if self.data.get("state") != "current" or self.data.get("target") != key:
                self._save(state="current", error=None, target=key, blocked=None, failures=0)
            return
        if not self.managed() or self.mode() != "automatic":
            return  # reported as update_mode manual; a manual `runtime update --install` still works
        if self.data.get("blocked") == key:
            return  # failed verification or rolled back: wait for a new set_at or version
        if self.data.get("state") == "updating" and self.data.get("target") == key:
            # Installed and waiting for the launcher's switch: never install again,
            # which could report current and skip the new version's probation.
            if self.clock() - self.data.get("updating_since", self.clock()) < SWITCH_TIMEOUT:
                return
            self._fail("switch_timeout", network=True, target=key)
            return
        if wanted < updates.MIN_TARGET:
            self._save(state="failed", error="target_below_minimum", target=key, blocked=key)
            return
        if wanted < installed and not key["allow_downgrade"]:
            self._save(state="failed", error="downgrade_not_allowed", target=key, blocked=key)
            return
        if self.data.get("target") == key and self.clock() < self.data.get("next_try_at", 0):
            return  # backing off after a network failure for this same target
        if self.data.get("target") != key:
            self._save(failures=0, next_try_at=0)  # a changed target is tried at once
        self._save(state="updating", error=None, target=key, blocked=None, updating_since=self.clock(),
                   **{"from": __version__})
        self.log(f"installing pushed client version {key['version']}")
        self.thread = threading.Thread(target=self._run, args=(key,), daemon=True)
        self.thread.start()

    def _run(self, key):
        """Install in the background. Only a verification failure (or a rollback,
        recorded by the launcher) blocks a target; anything transient backs off
        (14.7 M9, review 1 finding 6)."""
        try:
            release = self.resolve(key["version"])
            result = self.install(release)
        except updates.VerificationError:
            self._fail("verification_failed", target=key)
        except updates.ReleaseNotFound:
            self._fail("release_not_found", network=True, target=key)
        except updates.NetworkError:
            self._fail("network", network=True, target=key)
        except Exception as exc:  # noqa: BLE001 - busy lock, disk full, timeouts, bad JSON: transient
            self._fail("install_failed:" + type(exc).__name__, network=True, target=key)
        else:
            if result.get("status") == "installed" or (
                    result.get("status") == "current"
                    and updates.version_key(key["version"]) != updates.version_key(__version__)):
                # The pointer names the target; the launcher switches to it and the
                # new version reports current after its first tick (on probation).
                self.log(f"installed {key['version']}; the launcher switches to it")
            else:
                self._save(state="current", error=None, failures=0)
