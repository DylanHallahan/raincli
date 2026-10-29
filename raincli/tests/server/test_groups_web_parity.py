"""M4 on the pre-existing web routes (protocol §22): a group's id at the conversation page, ``/app/live?conversation=``
and the archive forms answers exactly like an unknown id, for members too (groups have their own pages); groups
never appear in the 1:1 lists. In app mode and on the website. Real PostgreSQL."""

from __future__ import annotations

import uuid

from conftest_groups import groups_seed  # noqa: F401  (fixture)
from conftest_v06 import PASSWORD, v06_seed  # noqa: F401  (fixture)
from test_team_iso import maya_app, same_answer  # noqa: F401  (fixture)
from test_v06_web import _csrf_app
from test_web_app import csrf_of, login

UNKNOWN = str(uuid.uuid4())


def _paths(gid):
    return [f"/app/conversations/{gid}", f"/app/live?conversation={gid}&after=0",
            f"/app/conversations/{gid}?reply_to={UNKNOWN}"]


def test_app_mode_answers_a_group_id_like_an_unknown_one(maya_app, groups_seed):  # noqa: F811
    gid = str(groups_seed["groups"]["checkout"].id)  # Maya is a member
    for mine, unknown in zip(_paths(gid), _paths(UNKNOWN)):
        assert same_answer(maya_app.get(mine), maya_app.get(unknown)), mine
    csrf = _csrf_app(maya_app)
    for verb in ("archive", "unarchive"):
        post = lambda ident: maya_app.post(f"/app/conversations/{ident}/{verb}", data={"csrf_token": csrf},  # noqa
                                           follow_redirects=False)
        assert same_answer(post(gid), post(UNKNOWN)), verb
    for page in ("/app/inbox", "/app/archived"):  # the app's Groups section links /app/groups/<id> (§22.10)
        assert f"/app/conversations/{gid}" not in maya_app.get(page).text, page
    assert gid not in maya_app.get("/app/archived").text


def test_the_website_answers_a_group_id_like_an_unknown_one(client, groups_seed):  # noqa: F811
    assert login(client, "maya@example.test", PASSWORD).status_code == 303
    gid = str(groups_seed["groups"]["checkout"].id)
    for mine, unknown in zip(_paths(gid)[:1], _paths(UNKNOWN)[:1]):
        assert same_answer(client.get(mine), client.get(unknown)), mine
    for page in ("/app/inbox", "/app/archived"):
        assert gid not in client.get(page).text, page
