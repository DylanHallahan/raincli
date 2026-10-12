"""presentation.py (protocol §21 C5, C13 M3/M7): labels, hold wording, errors, the typed-recipient parser,
conversation peers and person presence, on the shared v0.6 seed."""

from __future__ import annotations

import pytest

from conftest_v06 import v06_seed  # noqa: F401  (fixture)
from raincli_server import presentation as pres
from raincli_server.routing import Endpoint


def test_labels_are_people_and_agent_sent_says_agent(session, v06_seed):  # noqa: F811
    m, jon = v06_seed["machines"], v06_seed["people"]["Jon"]
    assert pres.person_label(session, Endpoint("agent", agent=m["jon"], name="zq-agent-jon-api"), sender=True) == "Jon · agent"
    assert pres.person_label(session, Endpoint("machine", agent=m["jon"]), sender=True) == "Jon · agent"
    assert pres.person_label(session, Endpoint("machine", agent=m["jon"])) == "Jon"  # as a recipient: his inbox
    assert pres.person_label(session, Endpoint("person", user=jon), sender=True) == "Jon"


@pytest.mark.parametrize("code, route, text, api", [
    ("no_inbox", None, "waiting for Jon to pick an inbox", "no_inbox"),
    ("busy", None, "Jon's inbox is busy", "inbox_busy"),
    ("blocked", None, "Jon's inbox is busy", "inbox_busy"),
    ("offline", None, "Jon's inbox is offline", "inbox_offline"),
    ("expired_offline", None, "Jon's inbox is offline", "inbox_offline"),
    ("client_update_needed", None, "Jon's RainCLI needs an update", "update_needed"),
    ("approval_required", None, "waiting for Jon's approval", "approval"),
    ("sender_blocked", None, "waiting for Jon's approval", "approval"),
    ("target_ambiguous", None, "Jon's inbox couldn't take it", "inbox_failed"),
    ("too_large_for_hook", None, "Jon's inbox couldn't take it", "inbox_failed"),
    ("anything else", None, "Jon's inbox couldn't take it", "inbox_failed"),
    ("busy", "reply", "Jon's agent is busy", "agent_busy"),
    ("offline", "reply", "Jon's agent is offline", "agent_offline"),
    ("no_inbox", "reply", "waiting for Jon to pick an inbox", "no_inbox"),
])
def test_hold_wording_and_api_codes(code, route, text, api):
    assert pres.hold_text(code, "Jon", route) == text
    assert pres.held_line(code, "Jon", route) == f"held: {text}"
    assert pres.api_hold_code(code, route) == api
    assert "zq-" not in text and "session" not in text


def test_no_hold_is_none():
    assert pres.hold_text(None, "Jon") is None and pres.api_hold_code("") is None


def test_typed_recipients(session, v06_seed):  # noqa: F811
    team, p = v06_seed["teams"]["northwind"].id, v06_seed["people"]
    maya = p["Maya"]
    for text in ("jon@example.test", "@JON@example.test", " Jon ", "jon"):
        r = pres.resolve_person_text(session, team, text, sender=maya)
        assert r.kind == "person" and r.user.id == p["Jon"].id and r.error is None, text
    # A teammate's handle (or agent form) is refused, naming its person: the only place that mapping is shown.
    for text in ("zq-handle-jon", "zq-handle-jon/zq-agent-jon-api"):
        r = pres.resolve_person_text(session, team, text, sender=maya)
        assert r.kind == "address_people" and r.error[1:3] == (
            "address_people", "Address people: send to Jon (jon@example.test)"), text
    # Your own handle, an unknown handle and an unknown name all get the same generic refusal.
    answers = [pres.resolve_person_text(session, team, t, sender=maya).error
               for t in ("zq-handle-maya", "zq-handle-nobody", "zq-handle-nobody/x", "Nobody", "nobody@example.test")]
    assert all(a == (400, "address_people", pres.ADDRESS_PEOPLE, {}) for a in answers), answers
    # A person outside the team is not found either; Lee is in contoso only.
    assert pres.resolve_person_text(session, team, "lee@example.test", sender=maya).kind == "address_people"
    assert pres.resolve_person_text(session, team, "", sender=maya).kind == "invalid"


def test_an_ambiguous_name_lists_the_people(session, v06_seed):  # noqa: F811
    from raincli_server import identity

    nw = v06_seed["teams"]["northwind"]
    other = identity.create_user(session, "jon.two@example.test", "jon", "correct horse battery")
    identity.add_member(session, nw, other)
    session.commit()
    r = pres.resolve_person_text(session, nw.id, "Jon", sender=v06_seed["people"]["Maya"])
    status, code, _, extra = r.error
    assert (status, code) == (400, "ambiguous_person")
    assert {x["email"] for x in extra["people"]} == {"jon@example.test", "jon.two@example.test"}


def test_conversation_peers(session, v06_seed):  # noqa: F811
    convs, p = v06_seed["conversations"], v06_seed["people"]
    assert pres.conversation_peer(session, p["Maya"], convs["jon"]).label == "Jon"
    assert pres.conversation_peer(session, p["Jon"], convs["jon"]).label == "Maya"
    own = pres.conversation_peer(session, p["Maya"], convs["self"])
    assert (own.user, own.label, own.kind) == (None, "From your agents", "self")


def test_person_presence_from_the_inbox_only(session, v06_seed):  # noqa: F811
    nw, p = v06_seed["teams"]["northwind"].id, v06_seed["people"]
    shown = {name: pres.person_presence(session, nw, p[name]).text for name in ("Jon", "Priya", "Sam", "Ryan", "Maya")}
    assert shown == {"Jon": "Ready · Instant", "Priya": "Busy · Next turn", "Sam": "No inbox yet",
                     "Ryan": "Offline", "Maya": "No inbox yet"}
    ct = v06_seed["teams"]["contoso"].id
    assert pres.person_presence(session, ct, p["Lee"]).text == "Ready · Instant"
    assert pres.person_presence(session, ct, p["Jon"]).presence == "no_inbox"  # Jon's inbox is in northwind only


def test_look_alike_names_are_told_apart(session, v06_seed):  # noqa: F811
    """The person is the address: two members whose names read alike never show as the same person."""
    from raincli_server import identity

    nw = v06_seed["teams"]["northwind"]
    twin = identity.create_user(session, "robert@example.test", "Jоn", "correct horse battery")  # Cyrillic о
    identity.add_member(session, nw, twin)
    session.commit()
    jon = v06_seed["people"]["Jon"]
    a, b = pres.name_in_team(session, nw.id, jon), pres.name_in_team(session, nw.id, twin)
    assert a != b and a.startswith("Jon (") and "robert" in b
    assert pres.conversation_peer(session, v06_seed["people"]["Maya"], v06_seed["conversations"]["jon"]).label == a
    names = {p["email"]: p["display_name"] for p in pres.people_json(session, nw.id)}
    assert names["jon@example.test"] != names["robert@example.test"]


def test_every_code_a_v05_or_v06_connector_reports_is_accepted():
    """The connectors report these (runner.report_code, ops.reject, the hold reasons); refusing one would leave the
    sender without the state."""
    reported = {"delivered", "submission_uncertain", "rejected", "declined", "target_mismatch", "agent_not_ready",
                "herdr_timeout", "herdr_failed", "handover_failed", "handover_missing", "claimed_without_receipt",
                "next_turn_emitted", "busy", "blocked", "offline", "next_turn", "target_ambiguous", "expired_offline",
                "too_large_for_hook", "too_large_for_command_line", "owner_unreachable", "approval_required",
                "sender_blocked"}
    assert reported <= pres.EVENT_CODES
    for code in reported:  # and every one reads as a person's inbox state, never an agent or session word
        text = pres.hold_text(code, "Jon")
        assert text.startswith(("Jon", "waiting for")) and "agent " not in text
