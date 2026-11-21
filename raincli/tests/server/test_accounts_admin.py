"""The grant operator commands (protocol §23 A2, C1, C11): raincli-admin grant-add, grant-remove, grant-list and
delegated-actions. People only; never machines, agents or bodies; a refusal is a clear message and exit code 2.
Real PostgreSQL."""

from __future__ import annotations

import pytest

from conftest_accounts import HELPDESK_STRINGS, accounts_seed  # noqa: F401  (fixture)
from conftest_v06 import TEAMMATE_STRINGS, v06_seed  # noqa: F401  (fixture)
from raincli_server import identity
from raincli_server.models import Grant, PersonSession
from test_api_admin import run

MACHINE_WORDS = TEAMMATE_STRINGS + HELPDESK_STRINGS + ("zq-handle-maya", "zq-agent")


def _clean(*texts):
    for text in texts:
        assert not [s for s in MACHINE_WORDS if s in text], text


def test_add_list_and_remove_a_grant(database_url, session, v06_seed):  # noqa: F811
    p, nw = v06_seed["people"], v06_seed["teams"]["northwind"]
    code, out, err = run(database_url, "grant-add", "northwind", "maya@example.test", "JON@example.test")
    assert code == 0, err
    assert out == "maya@example.test may now act as JON@example.test in northwind (toasts on)\n"
    grant = session.query(Grant).one()
    assert (grant.team_id, grant.grantee_user_id, grant.account_user_id, grant.created_by, grant.toasts) == (
        nw.id, p["Maya"].id, p["Jon"].id, "operator", True)
    code, out, _ = run(database_url, "grant-list", "northwind")
    assert code == 0 and out.startswith("Maya <maya@example.test>\tas Jon <jon@example.test>\ttoasts on\tgranted ")
    assert out.rstrip().endswith("\tlive")
    code, out, err = run(database_url, "grant-remove", "northwind", "maya@example.test", "jon@example.test")
    assert code == 0 and out == "maya@example.test may no longer act as jon@example.test in northwind; 0 sessions ended\n"
    code, out, _ = run(database_url, "grant-list", "northwind")
    assert "\trevoked " in out  # kept for the audit
    _clean(out, err)
    assert run(database_url, "grant-list", "contoso")[1] == "contoso has no grants\n"


@pytest.mark.parametrize("argv, message", [
    (("grant-add", "northwind", "maya@example.test", "lee@example.test"),
     "lee@example.test (account) is not an active member of northwind"),
    (("grant-add", "contoso", "lee@example.test", "jon@example.test"),
     "jon@example.test (account) is not an active member of contoso"),  # a grant across teams (C19)
    (("grant-add", "northwind", "nobody@example.test", "jon@example.test"),
     "nobody@example.test (grantee) is not an active member of northwind"),
    (("grant-add", "northwind", "maya@example.test", "maya@example.test"), "can't be granted their own account"),
    (("grant-remove", "northwind", "maya@example.test", "priya@example.test"),
     "maya@example.test has no grant to act as priya@example.test in northwind"),
])
def test_refusals_exit_2_with_a_clear_message(database_url, v06_seed, argv, message):  # noqa: F811
    code, out, err = run(database_url, *argv)
    assert (code, out) == (2, "") and message in err, err


def test_a_duplicate_live_grant_and_a_disabled_person_are_refused(database_url, session, v06_seed):  # noqa: F811
    assert run(database_url, "grant-add", "northwind", "maya@example.test", "jon@example.test")[0] == 0
    code, _, err = run(database_url, "grant-add", "northwind", "maya@example.test", "jon@example.test")
    assert code == 2 and "may already act as jon@example.test" in err
    identity.set_user_active(session, v06_seed["people"]["Priya"], False)
    session.commit()
    code, _, err = run(database_url, "grant-add", "northwind", "priya@example.test", "jon@example.test")
    assert code == 2 and "priya@example.test (grantee) is not an active member" in err


def test_grant_remove_ends_every_session_issued_through_it(database_url, session, accounts_seed):  # noqa: F811
    s = accounts_seed
    code, out, err = run(database_url, "grant-remove", "northwind", "maya@example.test", "helpdesk@example.test")
    assert code == 0 and out.endswith("; 1 sessions ended\n"), err
    session.expire_all()
    assert session.get(PersonSession, s["sessions"]["helpdesk"].id).revoked_at is not None
    assert identity.authenticate_person(session, s["delegated_token"], touch=False) is None
    assert identity.authenticate_person(session, s["person_tokens"]["Maya"], touch=False) is not None  # hers stays
    # The grant stays revoked, and the other grant (Billing) is untouched.
    assert session.get(Grant, s["grants"]["billing"].id).revoked_at is None


def test_delegated_actions_lists_sends_only_and_never_a_body(database_url, accounts_seed):  # noqa: F811
    s = accounts_seed
    code, out, err = run(database_url, "delegated-actions", "northwind")
    assert code == 0, err
    lines = out.splitlines()
    assert len(lines) == 1
    at, actor, account, where, message, via = lines[0].split("\t")
    assert via == "via individual grant"
    assert actor == "Maya <maya@example.test>" and account == "as Help Desk <helpdesk@example.test>"
    assert where == f"sent in conversation {s['conversations']['helpdesk_jon'].id} with Jon"
    assert message == f"message {s['messages']['delegated_send'].id}"
    assert "On it, Jon." not in out
    _clean(out, err)
    assert run(database_url, "delegated-actions", "northwind", "--since", "2999-01-01")[1] == \
        "no sends through grants in northwind\n"
    code, _, err = run(database_url, "delegated-actions", "northwind", "--since", "yesterday")
    assert code == 2 and "--since must be a date" in err


def test_a_member_with_a_revoked_grant_can_be_removed_and_re_added(database_url, session, accounts_seed):  # noqa: F811
    """A14 M2: grants key on users and teams, so removal (which deletes the membership) works, and the history stays."""
    assert run(database_url, "grant-remove", "northwind", "maya@example.test", "helpdesk@example.test")[0] == 0
    code, _, err = run(database_url, "remove-member", "--team", "northwind", "--email", "helpdesk@example.test")
    assert code == 0, err
    assert run(database_url, "add-member", "--team", "northwind", "--email", "helpdesk@example.test")[0] == 0
    code, out, _ = run(database_url, "grant-list", "northwind")
    assert code == 0 and out.count("as Help Desk") == 1 and "revoked" in out
    assert run(database_url, "grant-add", "northwind", "maya@example.test", "helpdesk@example.test")[0] == 0
