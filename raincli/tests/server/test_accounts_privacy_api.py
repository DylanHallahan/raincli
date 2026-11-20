"""The privacy scan, team-iso probe and PR5 diff for account switching (protocol §23, C19, C20, C20a, A14 M1, A16), and
an old client against the new server (C21). A delegated session sees the account's conversations as a teammate sees
machines: never a handle or agent name (the account's own included); the grantee's name never reaches a recipient;
other teams stay unknown; a delegated read changes nothing a sender can fetch; a v0.6.1 client works as one person.
Real PostgreSQL."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from api_helpers import auth
from conftest_accounts import HELPDESK_STRINGS, accounts_seed  # noqa: F401  (fixture)
from conftest_v06 import OWN_STRINGS, TEAMMATE_STRINGS, _message, _pair, v06_seed  # noqa: F401  (fixture)
from raincli_server.models import Message
from test_app_mode import NAV, WEBVIEW, handoff_code
from test_v060_cli import live  # noqa: F401  (fixture: a real uvicorn server)
from test_v06_old_clients import run_old
from test_v06_privacy_api import _sender_snapshot

# Every machine handle and agent name in the seed: a delegate acting as Help Desk sees none, Help Desk's own included.
EVERY_MACHINE = HELPDESK_STRINGS + TEAMMATE_STRINGS + OWN_STRINGS


def _leaks(text):
    return [s for s in EVERY_MACHINE if s in text]


def _fetch_all(client, s, token):
    """Everything a person session can fetch over the person API, and drive, as (label, text) pairs."""
    h = auth(token)
    out = []

    def record(label, r):
        out.append((label, f"{r.status_code} {json.dumps(dict(r.headers))} {r.text}"))
        return r
    for path in ("/api/v1/person/me", "/api/v1/person/delegations", "/api/v1/person/me/inbox",
                 "/api/v1/person/inbox?include_acked=true", "/api/v1/person/inbox?include_acked=true&routed=1",
                 "/api/v1/person/conversations?archived=include"):
        record(path, client.get(path, headers=h))
    for conv in s["conversations"].values():
        record(f"conversation {conv.id}", client.get(f"/api/v1/person/conversations/{conv.id}", headers=h))
    for msg in s["messages"].values():
        record(f"message {msg.id}", client.get(f"/api/v1/person/messages/{msg.id}", headers=h))
    esc = s["messages"]["helpdesk_escalation"]
    record("reply to escalation", client.post("/api/v1/person/send", headers=h, json={
        "id": str(uuid.uuid4()), "in_reply_to": str(esc.id), "body": "ok"}))
    record("send", client.post("/api/v1/person/send", headers=h, json={
        "id": str(uuid.uuid4()), "to": "priya@example.test", "body": "hello"}))
    record("ack", client.post(f"/api/v1/person/messages/{s['messages']['to_helpdesk'].id}/ack", headers=h))
    conv = s["conversations"]["helpdesk_jon"].id
    record("archive", client.post(f"/api/v1/person/conversations/{conv}/archive", headers=h))
    record("unarchive", client.post(f"/api/v1/person/conversations/{conv}/unarchive", headers=h))
    record("put inbox", client.put("/api/v1/person/me/inbox", headers=h, json={"none": True}))
    record("delegate", client.post("/api/v1/person/delegations", headers=h, json={"account": "billing@example.test"}))
    return out


def test_a_delegate_never_sees_a_machine_or_agent_over_the_api(client, accounts_seed):  # noqa: F811
    leaks = [(label, _leaks(text)) for label, text in _fetch_all(client, accounts_seed,
                                                                  accounts_seed["delegated_token"])]
    assert [leak for leak in leaks if leak[1]] == []


def test_a_delegate_never_sees_a_machine_or_agent_in_the_window(app, client, accounts_seed):  # noqa: F811
    s = accounts_seed
    with TestClient(app, headers=WEBVIEW) as webview:
        r = webview.get(f"/app/handoff?code={handoff_code(client, s['delegated_token'])}", headers=NAV)
        assert r.status_code == 200
        pages = ["/app/inbox", "/app/people", "/app/archived", "/app/compose", "/app/recipients", "/app/live"]
        pages += [f"/app/conversations/{c.id}" for c in s["conversations"].values()]
        pages += [f"/app/live?conversation={c.id}&after=0" for c in s["conversations"].values()]
        texts = [(p, webview.get(p)) for p in pages]
        assert [(p, _leaks(r.text)) for p, r in texts if _leaks(r.text)] == []
        assert texts[0][1].status_code == 200 and str(s["conversations"]["helpdesk_self"].id) in texts[0][1].text


def test_the_grantees_name_never_reaches_a_recipient(app, client, session, accounts_seed):  # noqa: F811
    """C20, AS6 a): Jon (as a person, his machine, his window) never sees who wrote Help Desk's messages."""
    s = accounts_seed
    sent = client.post("/api/v1/person/send", headers=auth(s["delegated_token"]), json={
        "id": str(uuid.uuid4()), "to": "jon@example.test", "body": "From the help desk."}).json()["message"]
    ids = [sent["id"], str(s["messages"]["delegated_send"].id)]
    texts = []
    for mid in ids:
        texts.append(client.get(f"/api/v1/person/messages/{mid}", headers=auth(s["person_tokens"]["Jon"])).text)
        texts.append(client.get(f"/api/v1/messages/{mid}", headers=auth(s["machine_tokens"]["jon"])).text)
    for t in texts:
        body = json.loads(t)["message"]
        assert body["sent_by"] is None and "maya" not in json.dumps(body).lower(), body
    inbox = client.get("/api/v1/inbox", headers=auth(s["machine_tokens"]["jon"]), params={"routing": 1}).json()
    assert all(m["sent_by"] is None for m in inbox["messages"] if m["id"] in ids)
    conv = s["conversations"]["helpdesk_jon"].id
    with TestClient(app, headers=WEBVIEW) as jon:
        assert jon.get(f"/app/handoff?code={handoff_code(client, s['person_tokens']['Jon'])}",
                       headers=NAV).status_code == 200
        for page in (jon.get(f"/app/conversations/{conv}"), jon.get(f"/app/live?conversation={conv}&after=0")):
            assert page.status_code == 200 and "From the help desk." in page.text + json.dumps(page.text)
            assert "sent by" not in page.text.lower()


def test_other_teams_stay_unknown_through_a_grant(client, session, accounts_seed):  # noqa: F811
    """C19: a contoso message and conversation answer the delegate (northwind) exactly like an unknown id."""
    s = accounts_seed
    ct = s["teams"]["contoso"]
    with_lee = _pair(session, ct, s["people"]["Maya"], s["people"]["Lee"])
    msg = _message(session, ct, with_lee, body="contoso only", route="person", sender_user=s["people"]["Lee"],
                   recipient_user=s["people"]["Maya"])
    session.commit()
    h = auth(s["delegated_token"])
    for known, unknown in ((f"/api/v1/person/messages/{msg.id}", f"/api/v1/person/messages/{uuid.uuid4()}"),
                           (f"/api/v1/person/conversations/{with_lee.id}",
                            f"/api/v1/person/conversations/{uuid.uuid4()}")):
        a, b = client.get(known, headers=h), client.get(unknown, headers=h)
        assert (a.status_code, a.json()) == (b.status_code, b.json())
    listed = client.get("/api/v1/person/conversations?archived=include", headers=h).text
    assert str(with_lee.id) not in listed and "contoso" not in listed


def test_a_delegated_read_changes_nothing_the_sender_can_fetch(client, accounts_seed):  # noqa: F811
    """PR5: Maya reading as Help Desk marks Help Desk's read state only; Jon (the sender) sees no difference."""
    s = accounts_seed
    before = _sender_snapshot(client, s, "person", "Jon")
    for key in ("to_helpdesk", "held_to_helpdesk"):
        assert client.post(f"/api/v1/person/messages/{s['messages'][key].id}/ack",
                           headers=auth(s["delegated_token"])).status_code == 200
    assert _sender_snapshot(client, s, "person", "Jon") == before


OLD_PERSON = """
import os
from raincli_agent import person
cfg = os.path.join(payload["home"], "agent.json")
open(cfg, "w").write(json.dumps({"api_url": payload["url"], "token": payload["machine_token"]}))
os.chmod(cfg, 0o600)
person.save_session(cfg, payload["token"])
api = person.PersonClient.for_config(cfg)
me = api.me()
inbox, _cursor = api.inbox(include_acked=True)
convs = api.conversations(archived="include")
sent = api.send("jon@example.test", "from the old client")
print(json.dumps({"me": me, "inbox": [m["id"] for m in inbox], "convs": [c["id"] for c in convs],
                  "sent": sent, "files": sorted(os.listdir(payload["home"]))}))
"""


def test_a_v061_client_works_unchanged_as_one_person(live, session, accounts_seed, tmp_path):  # noqa: F811
    """C21: the tagged v0.6.1 client, signed in as Maya beside her live delegation, sees only Maya, sends as Maya,
    and never sees or writes anything of the accounts."""
    s = accounts_seed
    home = tmp_path / "home"
    home.mkdir()
    out = run_old("v0.6.1", tmp_path, OLD_PERSON, {"url": live, "machine_token": s["machine_tokens"]["maya"],
                                                   "token": s["person_tokens"]["Maya"], "home": str(home)})
    assert out["me"]["user"]["email"] == "maya@example.test"
    assert str(s["messages"]["to_helpdesk"].id) not in out["inbox"]
    assert str(s["conversations"]["helpdesk_jon"].id) not in out["convs"]
    assert str(s["conversations"]["jon"].id) in out["convs"]
    assert "accounts.json" not in out["files"]
    session.expire_all()
    sent, created = out["sent"]
    assert created is True and sent["sent_by"] is None
    stored = session.get(Message, uuid.UUID(sent["id"]))
    assert stored.sender_user_id == s["people"]["Maya"].id and stored.acting_user_id is None


@pytest.mark.parametrize("token_key", ["Maya", "Jon"])
def test_own_sessions_see_no_account_fields_beyond_the_additive_ones(client, accounts_seed, token_key):  # noqa: F811
    me = client.get("/api/v1/person/me", headers=auth(accounts_seed["person_tokens"][token_key])).json()
    assert me["delegated"] is False and me["acting_user"] is None and isinstance(me["unread"], int)
    assert me["toasts"] is None and me["grant_kind"] is None


def test_cli_output_through_a_grant_shows_no_agent_or_machine(live, session, accounts_seed, tmp_path, monkeypatch,  # noqa: F811
                                                              capsys):
    """A15: ``raincli me`` with a delegated session: the escalation is read and answered with no agent name or
    handle in any output, and the reply records the grantee."""
    from raincli_agent import cli, person
    from raincli_agent.config import write_config

    s = accounts_seed
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("RAINCLI_CONFIG", raising=False)
    cfg = str(tmp_path / "maya" / "agent.json")
    write_config(cfg, live, s["machine_tokens"]["maya"])
    person.save_session(cfg, s["delegated_token"])
    esc = str(s["messages"]["helpdesk_escalation"].id)
    body = tmp_path / "reply.txt"
    body.write_text("Yes, go ahead.")
    outputs = []
    for argv in (["me", "inbox", "--json"], ["me", "inbox"], ["me", "read", esc], ["me", "read", esc, "--json"],
                 ["me", "conversations", "--all", "--json"],
                 ["me", "reply", esc, "--body-file", str(body), "--json"]):
        code = cli.main(["--config", cfg, *argv])
        captured = capsys.readouterr()
        assert code == 0, (argv, captured.err)
        outputs.append(captured.out + captured.err)
    assert [(i, _leaks(o)) for i, o in enumerate(outputs) if _leaks(o)] == []
    assert "Should I reset Ryan" in outputs[2]
    reply_id = json.loads(outputs[-1])["message"]["id"]
    session.expire_all()
    stored = session.get(Message, uuid.UUID(reply_id))
    assert stored.acting_user_id == s["people"]["Maya"].id and stored.recipient_agent_name == "zq-agent-helpdesk-ops"
