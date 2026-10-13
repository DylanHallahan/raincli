"""A hook session's next turn is a waiting state, not a failure (protocol §21.6): every event code maps to its
intended hold group, and a ``next_turn`` hold reads as waiting on the page and in the API, on both routes.
Real PostgreSQL."""

from __future__ import annotations

import re

import pytest

from api_helpers import auth
from conftest_v06 import v06_seed  # noqa: F401  (fixture)
from raincli_server import presentation as pres
from test_v06_routing import _ask, machine_send, person_send, stored
from test_v06_web import conv, maya, teammate_free, text  # noqa: F401  (fixture)

# Every allowlisted code and the group a teammate sees it in. "failed" is meant only for a hold the recipient has to
# fix, or a code that is never a hold's detail; a new code must be placed here before it is accepted.
INTENDED = {
    # waiting states
    "no_inbox": "no_inbox",
    "busy": "busy", "blocked": "busy",
    "next_turn": "next_turn",
    "offline": "offline", "expired_offline": "offline", "agent_not_ready": "offline",
    "client_update_needed": "update",
    "approval_required": "approval", "sender_blocked": "approval",
    # holds the recipient has to fix
    "target_mismatch": "failed", "target_ambiguous": "failed",
    "too_large_for_hook": "failed", "too_large_for_command_line": "failed",
    # never a message hold's detail: an escalation's local hold, and the outcomes reported with submitted,
    # submission_uncertain or rejected
    "owner_unreachable": "failed",
    "delivered": "failed", "next_turn_emitted": "failed", "submission_uncertain": "failed", "rejected": "failed",
    "declined": "failed", "herdr_timeout": "failed", "herdr_failed": "failed", "handover_failed": "failed",
    "handover_missing": "failed", "claimed_without_receipt": "failed",
}


def test_every_allowlisted_code_has_an_intended_group():
    assert set(INTENDED) == set(pres.EVENT_CODES)


@pytest.mark.parametrize("code", sorted(INTENDED))
def test_each_code_maps_to_its_intended_group(code):
    assert pres._group(code) == INTENDED[code]
    if INTENDED[code] != "failed":
        assert "couldn't take it" not in pres.hold_text(code, "Jon")
        assert pres.api_hold_code(code) != "inbox_failed"


@pytest.mark.parametrize("route, text, api", [
    (None, "waiting for Jon's inbox's next turn", "inbox_next_turn"),
    ("inbox", "waiting for Jon's inbox's next turn", "inbox_next_turn"),
    ("reply", "waiting for Jon's agent's next turn", "agent_next_turn"),
])
def test_next_turn_wording(route, text, api):
    assert pres.held_line("next_turn", "Jon", route) == f"held: {text}"
    assert pres.api_hold_code("next_turn", route) == api


def _hold_next_turn(client, token, message_id):
    assert client.post(f"/api/v1/messages/{message_id}/ack", headers=auth(token)).status_code == 200
    r = client.post(f"/api/v1/messages/{message_id}/events", headers=auth(token),
                    json={"state": "held", "detail": "next_turn"})
    assert r.status_code == 200, r.text


def _trail(page, message_id):
    m = re.search(rf'<div class="rc-trail([^"]*)" data-trail="{message_id}" data-state="([^"]+)"', page)
    assert m, f"no trail for {message_id}"
    return m.group(1).strip(), m.group(2)


def test_a_message_to_a_next_turn_inbox_is_waiting_on_the_page_and_in_the_api(client, session, maya, v06_seed):  # noqa: F811
    jon = v06_seed["machine_tokens"]["jon"]
    sent = machine_send(client, v06_seed["machine_tokens"]["maya"], "jon@example.test", "for your inbox")
    assert sent.status_code == 201, sent.text
    mid = sent.json()["message"]["id"]
    assert stored(session, mid).route == "inbox"
    _hold_next_turn(client, jon, mid)

    mine = client.get(f"/api/v1/messages/{mid}", headers=auth(v06_seed["machine_tokens"]["maya"])).json()["message"]
    owner = client.get(f"/api/v1/messages/{mid}", headers=auth(jon)).json()["message"]
    assert (mine["delivery_state"], mine["hold_reason"]) == ("held", "inbox_next_turn")
    assert owner["hold_reason"] == "next_turn"  # the recipient machine's owner sees the raw code

    page = maya.get(f"/app/conversations/{conv(v06_seed, 'jon')}").text
    words = text(page)
    assert "held: waiting for Jon's inbox's next turn" in words
    assert "couldn't take it" not in words and "session" not in words.lower()
    assert _trail(page, mid) == ("rc-trail-warn", "held")  # the waiting tone, not the danger one
    teammate_free(page)


def test_a_reply_to_a_next_turn_agent_is_waiting_on_the_page_and_in_the_api(client, session, maya, v06_seed):  # noqa: F811
    jon = v06_seed["machine_tokens"]["jon"]
    asked = machine_send(client, jon, "maya@example.test", "a question", from_agent="zq-agent-jon-api")
    assert asked.status_code == 201, asked.text
    r = person_send(client, v06_seed["person_tokens"]["Maya"], None, "the answer",
                    in_reply_to=asked.json()["message"]["id"])
    assert r.status_code == 201, r.text
    mid = r.json()["message"]["id"]
    assert stored(session, mid).route == "reply"
    _hold_next_turn(client, jon, mid)

    mine = client.get(f"/api/v1/messages/{mid}", headers=auth(v06_seed["machine_tokens"]["maya"])).json()["message"]
    assert (mine["delivery_state"], mine["hold_reason"]) == ("held", "agent_next_turn")

    page = maya.get(f"/app/conversations/{conv(v06_seed, 'jon')}").text
    words = text(page)
    assert "held: waiting for Jon's agent's next turn" in words
    assert "couldn't take it" not in words and "session" not in words.lower()
    assert _trail(page, mid) == ("rc-trail-warn", "held")
    teammate_free(page)


def test_an_inbox_that_has_to_be_fixed_still_reads_as_failed(client, session, v06_seed):  # noqa: F811
    mid = _ask(client, v06_seed)
    jon = v06_seed["machine_tokens"]["jon"]
    client.post(f"/api/v1/messages/{mid}/ack", headers=auth(jon))
    client.post(f"/api/v1/messages/{mid}/events", headers=auth(jon), json={"state": "held", "detail": "target_mismatch"})
    mine = client.get(f"/api/v1/messages/{mid}", headers=auth(v06_seed["machine_tokens"]["maya"])).json()["message"]
    assert mine["hold_reason"] == "inbox_failed"
