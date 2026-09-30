"""Private-file owner and ACL decision, and the launcher's rollback writes (Windows admins)."""
import json
import os
import stat
import sys

import pytest

from raincli_agent.fsutil import ADMINISTRATORS_SID, SYSTEM_SID, windows_acl_problem
from raincli_agent.runtime import launcher

ME = "S-1-5-21-1111111111-2222222222-3333333333-1001"
EVERYONE = "S-1-1-0"
USERS = "S-1-5-32-545"
ALLOW, DENY, AUDIT = 0, 1, 2
INHERIT_ONLY = 8


def private_aces():
    return [(ALLOW, 0, ME), (ALLOW, 0, SYSTEM_SID), (ALLOW, 0, ADMINISTRATORS_SID)]


def test_owned_by_the_user_with_a_private_dacl_is_trusted():
    assert windows_acl_problem(ME, private_aces(), ME) is None
    assert windows_acl_problem(ME, [(ALLOW, 0, ME)], ME) is None


@pytest.mark.parametrize("owner", [ADMINISTRATORS_SID, SYSTEM_SID, "S-1-5-21-9-9-9-1002", None])
def test_any_other_owner_is_refused_even_administrators(owner):
    """An administrator's objects default to BUILTIN\\Administrators ownership;
    RainCLI sets the user as owner explicitly on every write, so anything else is refused."""
    assert "not owned by the current Windows user" in windows_acl_problem(owner, private_aces(), ME)


@pytest.mark.parametrize("extra", [(ALLOW, 0, EVERYONE), (ALLOW, 0, USERS), (AUDIT, 0, ME), (5, 0, None)])
def test_broader_or_unknown_access_is_refused(extra):
    assert "grants access outside" in windows_acl_problem(ME, private_aces() + [extra], ME)


def test_deny_and_inherit_only_entries_grant_nothing():
    aces = private_aces() + [(DENY, 0, EVERYONE), (ALLOW, INHERIT_ONLY, EVERYONE)]
    assert windows_acl_problem(ME, aces, ME) is None


def test_missing_dacl_is_refused():
    assert "unrestricted" in windows_acl_problem(ME, None, ME)


def test_launcher_writes_through_the_client_helper(tmp_path):
    path = tmp_path / "current.json"
    launcher.write_json(path, {"tag": "v0.3.1"}, sys.executable)
    assert json.loads(path.read_text()) == {"tag": "v0.3.1"}
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_launcher_never_writes_an_untrusted_file_on_windows(tmp_path, monkeypatch):
    path = tmp_path / "current.json"
    broken = tmp_path / "no-such-python"
    monkeypatch.setattr(launcher.os, "name", "nt")
    with pytest.raises(OSError, match="private-file helper"):
        launcher.write_json(path, {"tag": "v0.3.1"}, broken)
    assert not path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fallback")
def test_launcher_falls_back_to_a_private_local_write_on_posix(tmp_path):
    path = tmp_path / "current.json"
    launcher.write_json(path, {"tag": "v0.3.1"}, "/bin/true")  # exits 0, writes nothing: verified, then fallback
    assert json.loads(path.read_text()) == {"tag": "v0.3.1"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
