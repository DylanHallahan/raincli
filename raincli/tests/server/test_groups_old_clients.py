"""Old clients and groups (protocol §22, C21, G4, H1's server half): the real v0.5.1 and v0.6.0 connectors, from the
tags, polling a live server, never receive a group copy; their 1:1 messages arrive unchanged; and their poll (no
``groups=1``) withdraws the copies waiting for that machine, so none can reach them later. Skipped without the tags."""

from __future__ import annotations

import uuid

import pytest

from api_helpers import auth
from conftest_groups import groups_seed  # noqa: F401  (fixture)
from conftest_v06 import v06_seed  # noqa: F401  (fixture)
from raincli_server.models import Message
from test_migration_0011 import _poll
from test_v060_cli import live  # noqa: F401  (fixture: a real uvicorn server)


@pytest.mark.parametrize("tag", ("v0.5.1", "v0.6.0"))
def test_an_old_connector_never_receives_a_group_copy(tag, live, client, session, groups_seed, tmp_path):  # noqa: F811
    s = groups_seed
    jon = s["machines"]["jon"]
    posted = client.post(f"/api/v1/groups/{s['groups']['checkout'].id}/posts", headers=auth(s["machine_tokens"]["maya"]),
                         json={"id": str(uuid.uuid4()), "body": "@Jon a group post"}).json()["post"]
    one_to_one = client.post("/api/v1/messages", headers=auth(s["machine_tokens"]["maya"]),
                             json={"id": str(uuid.uuid4()), "to": "jon@example.test", "body": "1:1 as before"})
    assert one_to_one.status_code == 201
    copy = session.query(Message).filter_by(post_id=uuid.UUID(posted["id"]), recipient_agent_id=jon.id).one()
    out = _poll(tag, tmp_path, {"url": live}, s["machine_tokens"]["jon"], start_cursor=0)
    assert str(copy.id) not in out["records"]  # never the group copy
    assert one_to_one.json()["message"]["id"] in out["records"]  # the 1:1 message, unchanged
    session.expire_all()
    assert session.get(Message, copy.id).withdrawn_at is not None  # withdrawn: no later poll can return it
    assert s["machines"]["jon"].groups_capable_at is None
    again = client.post(f"/api/v1/groups/{s['groups']['checkout'].id}/posts",
                        headers=auth(s["machine_tokens"]["maya"]),
                        json={"id": str(uuid.uuid4()), "body": "later"}).json()["post"]
    assert again["recipients_m"] == 1  # only Priya: Jon's machine doesn't take part any more
    out = _poll(tag, tmp_path, {"url": live}, s["machine_tokens"]["jon"])
    assert str(copy.id) not in out["records"]
