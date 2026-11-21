"""The shared account-switching seed (§23), on top of the v0.6 seed (tests/conftest_v06.py). Use the ``accounts_seed``
fixture; it needs the ``session`` fixture (real PostgreSQL).

Maya (the grantee) is signed in on her machine zq-handle-maya in northwind (her own person session). The operator has
granted her two accounts there:
- "Help Desk" (helpdesk@example.test): ADDED. A delegated session acting as Help Desk hangs off Maya's own session.
  Help Desk owns the machine zq-handle-helpdesk (agents zq-agent-helpdesk-inbox, its inbox, and zq-agent-helpdesk-ops),
  which Maya must never see (AS13, C20a).
- "Billing" (billing@example.test): granted, not added; no machine, no inbox.

Help Desk's traffic (northwind):
- ``to_helpdesk``: Jon wrote to Help Desk in the app; delivered to its inbox agent, unread.
- ``held_to_helpdesk``: Priya's agent to Help Desk's inbox, held "busy" (a raw code for the machine's owner).
- ``from_helpdesk_agent``: Help Desk's ops agent to Jon's inbox.
- ``escalation``: Help Desk's ops agent escalating to Help Desk ("From your agents", the self conversation).
- ``own_agents``: Help Desk's legacy "Your agents" history (ops agent to its inbox agent), archived, hidden under a
  grant (A16).
- ``delegated_send``: Help Desk to Jon, written by Maya through the grant (``acting_user_id`` = Maya).
"""

from __future__ import annotations

import uuid

import pytest

from conftest_v06 import PASSWORD, _inbox, _machine, _message, _pair, v06_seed  # noqa: F401  (fixture)

# Help Desk's own machine and agents: never in any response to a delegated session (A5, A14 M1, C20a).
HELPDESK_STRINGS = ("zq-handle-helpdesk", "zq-agent-helpdesk-inbox", "zq-agent-helpdesk-ops")
# The API routes tests/server/test_accounts_privacy_api.py scans with account data (the v0.6 route walk counts them).
ACCOUNT_GETS = {"/api/v1/person/delegations"}
ACCOUNT_DRIVEN = {("POST", "/api/v1/person/delegations")}


def make_grant(session, team, grantee, account, created_by="operator", toasts=True):
    from raincli_server.models import Grant

    grant = Grant(id=uuid.uuid4(), team_id=team.id, grantee_user_id=grantee.id, account_user_id=account.id,
                  created_by=created_by, toasts=toasts)
    session.add(grant)
    session.flush()
    return grant


def own_session(session, token):
    """The person session row of a raw token."""
    from raincli_server import identity

    return identity.authenticate_person(session, token, touch=False).person_session


def _own_agents(session, team, person, machine, *, sender, recipient, body):
    """A person's legacy "Your agents" conversation (migration 0010 makes these: archived, not default)."""
    from raincli_server import identity
    from raincli_server.models import Conversation

    conv = Conversation(id=uuid.uuid4(), team_id=team.id, a_user_id=person.id, b_user_id=person.id,
                        a_key=f"p:{person.id}", b_key=f"self:{person.id}", kind="own_agents", is_default=False)
    session.add(conv)
    session.flush()
    msg = _message(session, team, conv, body=body, route=None, sender_machine=machine, sender_name=sender,
                   recipient_machine=machine, recipient_name=recipient, state="received")
    msg.acked_at = identity.now()
    conv.archived_at, conv.archived_by_key, conv.archived_through_seq = identity.now(), conv.a_key, msg.seq
    session.flush()
    return conv, msg


@pytest.fixture
def accounts_seed(session, v06_seed):  # noqa: F811
    from raincli_server import identity
    from raincli_server.models import DeliveryEvent

    s = v06_seed
    people, machines, nw = dict(s["people"]), dict(s["machines"]), s["teams"]["northwind"]
    maya, jon, priya = people["Maya"], people["Jon"], people["Priya"]
    helpdesk = identity.create_user(session, "helpdesk@example.test", "Help Desk", PASSWORD)
    billing = identity.create_user(session, "billing@example.test", "Billing", PASSWORD)
    for person in (helpdesk, billing):
        identity.add_member(session, nw, person)
    people.update({"Help Desk": helpdesk, "Billing": billing})
    machines["helpdesk"], helpdesk_machine_token = _machine(
        session, nw, helpdesk, "zq-handle-helpdesk",
        [("zq-agent-helpdesk-inbox", "instant", "idle"), ("zq-agent-helpdesk-ops", "next-turn", "idle")])
    _inbox(session, helpdesk, nw, machines["helpdesk"], "zq-agent-helpdesk-inbox")
    grants = {"helpdesk": make_grant(session, nw, maya, helpdesk), "billing": make_grant(session, nw, maya, billing)}

    with_jon = _pair(session, nw, helpdesk, jon)
    to_helpdesk = _message(session, nw, with_jon, body="Ryan's laptop won't boot.", route="inbox", sender_user=jon,
                           recipient_machine=machines["helpdesk"], recipient_name="zq-agent-helpdesk-inbox")
    from_agent = _message(session, nw, with_jon, body="Ticket 42 is closed.", route="inbox",
                          sender_machine=machines["helpdesk"], sender_name="zq-agent-helpdesk-ops",
                          recipient_machine=machines["jon"], recipient_name="zq-agent-jon-api")
    with_priya = _pair(session, nw, helpdesk, priya)
    held = _message(session, nw, with_priya, body="VPN request.", route="inbox", sender_machine=machines["priya"],
                    sender_name="zq-agent-priya-web", recipient_machine=machines["helpdesk"],
                    recipient_name="zq-agent-helpdesk-inbox", state="held")
    session.add(DeliveryEvent(message_id=held.id, state="held", detail="busy", reported_by=machines["helpdesk"].id))
    own = _pair(session, nw, helpdesk, helpdesk, kind="self")
    escalation = _message(session, nw, own, body="Should I reset Ryan's password?", route="self", kind="escalation",
                          sender_machine=machines["helpdesk"], sender_name="zq-agent-helpdesk-ops",
                          recipient_user=helpdesk)
    own_agents, own_agents_msg = _own_agents(session, nw, helpdesk, machines["helpdesk"],
                                             sender="zq-agent-helpdesk-ops", recipient="zq-agent-helpdesk-inbox",
                                             body="Sync the ticket queue.")
    delegated_send = _message(session, nw, with_jon, body="On it, Jon.", route="inbox", sender_user=helpdesk,
                              recipient_machine=machines["jon"], recipient_name="zq-agent-jon-api")
    delegated_send.acting_user_id, delegated_send.grant_id = maya.id, grants["helpdesk"].id

    maya_own = own_session(session, s["person_tokens"]["Maya"])
    helpdesk_token, helpdesk_session, _ = identity.issue_delegated_session(session, maya_own, "helpdesk@example.test")
    session.commit()
    return {
        **s, "people": people, "machines": machines,
        "machine_tokens": {**s["machine_tokens"], "helpdesk": helpdesk_machine_token},
        "grants": grants,
        "sessions": {"maya_own": maya_own, "helpdesk": helpdesk_session},
        "person_tokens": {**s["person_tokens"], "Help Desk (delegated)": helpdesk_token},
        "delegated_token": helpdesk_token,
        "conversations": {**s["conversations"], "helpdesk_jon": with_jon, "helpdesk_priya": with_priya,
                          "helpdesk_self": own, "helpdesk_own_agents": own_agents},
        "messages": {**s["messages"], "to_helpdesk": to_helpdesk, "from_helpdesk_agent": from_agent,
                     "held_to_helpdesk": held, "helpdesk_escalation": escalation,
                     "helpdesk_own_agents": own_agents_msg, "delegated_send": delegated_send},
        "helpdesk_strings": HELPDESK_STRINGS,
    }
