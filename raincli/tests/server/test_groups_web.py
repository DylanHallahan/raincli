"""The app's group pages (protocol §22 G6, D9, D12; C14–C18) on the shared group seed (tests/conftest_groups.py):
the Groups section of the conversation list, the read-only thread, "Delivered to N of M", the member panel, the live
update, open latest (G12 L4) and the unknown-id answers (D7, G12 M4). App mode only: the website has no group pages.

Checkout is Maya, Jon, Priya and Ryan (an old client: not taking part); infra is Sam and Maya. Olive owns the team
and is in neither. Real PostgreSQL."""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api_helpers import auth
from conftest_groups import groups_seed, make_post  # noqa: F401  (fixture)
from conftest_v06 import PASSWORD, TEAMMATE_STRINGS, v06_seed  # noqa: F401  (fixture)
from raincli_server import identity
from raincli_server.models import GroupMember, Message
from test_app_mode import NAV, WEBVIEW, WEBVIEW_UA, handoff_code
from test_v06_web import text
from test_web_app import csrf_of, login

UNKNOWN = str(uuid.uuid4())
OPENS_LATEST = {"User-Agent": WEBVIEW_UA + " RainCLIAppVersion/0.6.1"}  # §20.7: a window that opens the latest
MACHINE_STRINGS = TEAMMATE_STRINGS + ("zq-handle-maya", "zq-agent-maya-laptop", "zq-agent-maya-notes")


def same_answer(a, b):
    return (a.status_code, a.content) == (b.status_code, b.content)


def _app_for(app, client, session, seed, name, machine_key):
    token = identity.issue_person_session(session, seed["people"][name], seed["machines"][machine_key])
    session.commit()
    window = TestClient(app, headers=WEBVIEW)
    r = window.get(f"/app/handoff?code={handoff_code(client, token)}", headers=NAV, follow_redirects=False)
    assert r.status_code == 303, r.text
    return window


@pytest.fixture
def windows(app, client, session, groups_seed):  # noqa: F811
    """App windows by person: each signed in from their northwind machine."""
    opened = {}

    def open_for(name):
        if name not in opened:
            opened[name] = _app_for(app, client, session, groups_seed, name, name.lower())
        return opened[name]

    yield open_for
    for w in opened.values():
        w.close()


def gid(seed, name="checkout"):
    return str(seed["groups"][name].id)


def group_list(html):
    """The list's Groups section: (name, member line) per row, in order."""
    section = html.split('<ul class="rc-convs rc-groups"', 1)
    if len(section) < 2:
        return []
    rows = re.findall(r'<li class="rc-conv-item">(.*?)</li>', section[1].split("</ul>", 1)[0], re.S)
    return [(text(re.search(r'<span class="rc-conv-name">(.*?)</span>', r, re.S).group(1)),
             text(re.search(r'<span class="rc-conv-preview">(.*?)</span>', r, re.S).group(1)), r) for r in rows]


# C14: the Groups section -----------------------------------------------------------------------------------------

def test_the_list_shows_your_groups_with_member_counts_and_no_unread_badge(windows, groups_seed):  # noqa: F811
    rows = group_list(windows("Maya").get("/app/inbox").text)
    assert [(n, m) for n, m, _ in rows] == [("checkout", "4 members"), ("infra", "2 members")]  # newest first
    assert all("rc-count" not in r and "unread" not in r for _, _, r in rows)
    assert [(n, m) for n, m, _ in group_list(windows("Sam").get("/app/inbox").text)] == [("infra", "2 members")]
    assert group_list(windows("Olive").get("/app/inbox").text) == []  # the team owner is in neither
    # Every app page with the list shows it; the open group is the current row.
    page = windows("Maya").get(f"/app/groups/{gid(groups_seed)}").text
    assert re.search(r'href="[^"]*/app/groups/' + gid(groups_seed) + r'#latest" aria-current="page"', page)


def test_the_website_has_no_group_pages_or_rows(client, groups_seed):  # noqa: F811
    assert login(client, "maya@example.test", PASSWORD).status_code == 303
    inbox = client.get("/app/inbox").text
    assert gid(groups_seed) not in inbox and "checkout" not in inbox
    assert same_answer(client.get(f"/app/groups/{gid(groups_seed)}"), client.get(f"/app/groups/{UNKNOWN}"))
    assert client.get(f"/app/groups/{gid(groups_seed)}").status_code == 404


# C15: the read-only thread -----------------------------------------------------------------------------------

def _posts(html):
    return re.findall(r'<li class="rc-msg rc-post[^"]*" id="p-([^"]+)" data-seq="(\d+)">(.*?)</li>\n', html, re.S)


def test_the_thread_is_read_only_with_agents_mention_chips_and_local_times(windows, groups_seed):  # noqa: F811
    s = groups_seed
    page = windows("Maya").get(f"/app/groups/{gid(s)}").text
    posts = _posts(page)
    assert [p[0] for p in posts] == [str(s["posts"][k].id) for k in ("ask", "jon_reply", "priya_reply")]
    assert [int(p[1]) for p in posts] == sorted(int(p[1]) for p in posts)  # team_seq order
    ask, jon, priya = (text(p[2]) for p in posts)
    assert ask.startswith("MA Maya · agent ·") and jon.startswith("JO Jon · agent ·")  # initials, name, time
    assert priya.startswith("PR Priya · agent ·")
    assert "Mentioned: @Jon , @Priya" in ask
    assert re.search(r'<span class="rc-mention">@Jon</span>.*<span class="rc-mention">@Priya</span>', posts[0][2], re.S)
    assert "Reply to Maya · agent" in jon and f'href="#p-{s["posts"]["ask"].id}"' in posts[1][2]
    assert all(re.search(r'<time datetime="[^"]+">', p[2]) for p in posts)  # localtime.js shows local times
    # Read-only: no composer, no Reply link, no trail, nothing to ack.
    assert "<textarea" not in page and "/send" not in page and "rc-reply" not in page and "data-trail" not in page
    assert "data-unread" not in page and 'name="body"' not in page and "Only agents post in groups." in text(page)


def test_your_own_agents_posts_show_delivered_to_n_of_m(windows, groups_seed, session):  # noqa: F811
    s = groups_seed
    maya_page = windows("Maya").get(f"/app/groups/{gid(s)}").text
    delivered = re.findall(r'data-delivered="([^"]+)">([^<]+)<', maya_page)
    # Jon's copy replied, Priya's received; Ryan takes no part, so no copy: 2 of 2.
    assert delivered == [(str(s["posts"]["ask"].id), "Delivered to 2 of 2")]
    jon_page = windows("Jon").get(f"/app/groups/{gid(s)}").text
    assert re.findall(r'data-delivered="([^"]+)">([^<]+)<', jon_page) == [
        (str(s["posts"]["jon_reply"].id), "Delivered to 0 of 2")]  # his own only; copies still stored
    # A withdrawn copy never counts.
    copy = session.scalars(select(Message).where(Message.post_id == s["posts"]["ask"].id,
                                                 Message.delivery_state == "received")).one()
    copy.withdrawn_at = identity.now()
    session.commit()
    again = windows("Maya").get(f"/app/groups/{gid(s)}").text
    assert re.findall(r'data-delivered="[^"]+">([^<]+)<', again) == ["Delivered to 1 of 2"]


def test_the_member_panel_is_read_only_and_names_people_only(windows, groups_seed):  # noqa: F811
    page = windows("Jon").get(f"/app/groups/{gid(groups_seed)}").text
    panel = page.split('<details class="rc-members">', 1)[1].split("</details>", 1)[0]
    members = [text(li) for li in re.findall(r"<li>(.*?)</li>", panel, re.S)]
    assert members == ["JO Jon", "MA Maya", "PR Priya", "RY Ryan not taking part"]
    assert "Ask Dylan to change who's in this group." in text(panel)
    assert "<button" not in panel and "<form" not in panel and "<input" not in panel and "href" not in panel
    assert "Group · 4 members" in text(page)
    own = ("zq-handle-jon", "zq-agent-jon-api")  # Jon's own machine may be named in his own owner notice
    for leak in MACHINE_STRINGS:  # people only: no teammate's machine, handle or agent name anywhere on the page
        assert leak in own or leak not in page, leak


def test_an_empty_group_says_so(windows, groups_seed):  # noqa: F811
    page = windows("Sam").get(f"/app/groups/{gid(groups_seed, 'infra')}").text
    assert "No posts yet." in text(page) and _posts(page) == []
    assert "Sam" in text(page) and "Maya" in text(page)


# D7, G12 M4: visibility and the unknown-id answer ------------------------------------------------------------

def test_non_members_and_the_team_owner_get_the_unknown_answer(windows, groups_seed, session, app, client):  # noqa: F811
    s = groups_seed
    for who in ("Sam", "Olive"):
        w = windows(who)
        assert w.get(f"/app/groups/{gid(s)}").status_code == 404, who
        assert same_answer(w.get(f"/app/groups/{gid(s)}"), w.get(f"/app/groups/{UNKNOWN}")), who
        assert same_answer(w.get(f"/app/live?group={gid(s)}&after=0"), w.get(f"/app/live?group={UNKNOWN}&after=0"))
        assert gid(s) not in w.get("/app/inbox").text and "checkout" not in w.get("/app/inbox").text
    maya = windows("Maya")
    for bad in ("not-a-uuid", str(s["conversations"]["jon"].id), "x" * 80):  # a 1:1 id isn't a group
        assert same_answer(maya.get(f"/app/groups/{bad}"), maya.get(f"/app/groups/{UNKNOWN}")), bad
        assert same_answer(maya.get(f"/app/live?group={bad}&after=0"), maya.get(f"/app/live?group={UNKNOWN}&after=0"))
    # Maya's app signed in from her contoso machine covers contoso only (T1).
    ct = _app_for(app, client, session, s, "Maya", "maya_ct")
    assert same_answer(ct.get(f"/app/groups/{gid(s)}"), ct.get(f"/app/groups/{UNKNOWN}"))
    assert "checkout" not in ct.get("/app/inbox").text
    ct.close()


def test_removal_takes_access_away_and_re_adding_restores_it(windows, groups_seed, session):  # noqa: F811
    s = groups_seed
    priya, group = s["people"]["Priya"], s["groups"]["checkout"]
    w = windows("Priya")
    assert w.get(f"/app/groups/{gid(s)}").status_code == 200
    session.delete(session.get(GroupMember, (group.id, priya.id)))
    session.commit()
    assert same_answer(w.get(f"/app/groups/{gid(s)}"), w.get(f"/app/groups/{UNKNOWN}"))
    assert "checkout" not in w.get("/app/inbox").text
    session.add(GroupMember(conversation_id=group.id, user_id=priya.id, team_id=group.team_id))
    session.commit()
    page = w.get(f"/app/groups/{gid(s)}").text
    assert len(_posts(page)) == 3  # C4: a current member sees the full history


def test_conversation_routes_answer_a_group_id_like_an_unknown_one(windows, groups_seed):  # noqa: F811
    """G12 M4: every pre-existing conversation route, for a member too (members use /app/groups/{id})."""
    s = groups_seed
    maya = windows("Maya")
    g = gid(s)
    assert same_answer(maya.get(f"/app/conversations/{g}"), maya.get(f"/app/conversations/{UNKNOWN}"))
    assert maya.get(f"/app/conversations/{g}").status_code == 404
    assert same_answer(maya.get(f"/app/live?conversation={g}&after=0"),
                       maya.get(f"/app/live?conversation={UNKNOWN}&after=0"))
    csrf = csrf_of(maya.get("/app/compose").text)
    for verb in ("archive", "unarchive"):
        a = maya.post(f"/app/conversations/{g}/{verb}", data={"csrf_token": csrf}, follow_redirects=False)
        b = maya.post(f"/app/conversations/{UNKNOWN}/{verb}", data={"csrf_token": csrf}, follow_redirects=False)
        assert same_answer(a, b), verb
    form = {"csrf_token": csrf, "body": "hi", "message_id": str(uuid.uuid4())}
    a = maya.post(f"/app/conversations/{g}/send", data=form, follow_redirects=False)
    b = maya.post(f"/app/conversations/{UNKNOWN}/send", data={**form, "message_id": str(uuid.uuid4())},
                  follow_redirects=False)
    assert same_answer(a, b)
    # Groups never appear in the 1:1 lists.
    for page in ("/app/inbox", "/app/archived", "/app/compose", "/app/people"):
        body = maya.get(page).text
        rows = body.split('<ul class="rc-convs rc-groups"', 1)[0]
        assert f"/app/conversations/{g}" not in rows, page


# Live (§19.1 for groups) and no reads (C18) ----------------------------------------------------------------------

def test_live_brings_new_posts_and_delivered_counts_and_moves_the_list(windows, groups_seed, session):  # noqa: F811
    s = groups_seed
    maya = windows("Maya")
    page = maya.get(f"/app/groups/{gid(s)}").text
    after = int(re.search(r'data-group="[^"]+" data-after="(\d+)"', page).group(1))
    first = maya.get(f"/app/live?group={gid(s)}&after={after}").json()
    assert [d["text"] for d in first["delivered"]] == ["Delivered to 2 of 2"]
    rev = first["list_rev"]
    # Nothing new: no posts beyond the 30 s window's repeats, and the same list revision.
    quiet = maya.get(f"/app/live?group={gid(s)}&after={after}&list_rev={rev}").json()
    assert quiet["list"] is None and quiet["list_rev"] == rev
    assert {m["id"] for m in quiet["messages"]} <= {str(p.id) for p in s["posts"].values()}  # repeats merge by id
    post, copies = make_post(session, s["groups"]["checkout"], s["machines"]["jon"], "zq-agent-jon-api",
                             "One more thing: `currency` is now required.", mentions=[s["people"]["Maya"]])
    session.commit()
    fresh = maya.get(f"/app/live?group={gid(s)}&after={after}&list_rev={rev}").json()
    new = [m for m in fresh["messages"] if m["id"] == str(post.id)]
    assert len(new) == 1 and new[0]["seq"] > after
    assert "Jon · agent" in text(new[0]["html"]) and '<span class="rc-mention">@Maya</span>' in new[0]["html"]
    assert "data-unread" not in new[0]["html"] and "zq-" not in new[0]["html"]
    assert fresh["list"] is not None and fresh["list_rev"] != rev  # the group's latest post moved the list
    # A copy's state moves "Delivered" on the poster's view only.
    jon = windows("Jon")
    seen = jon.get(f"/app/live?group={gid(s)}&after=0").json()
    assert [d["text"] for d in seen["delivered"]] == ["Delivered to 0 of 2", "Delivered to 0 of 2"]
    copies[0].delivery_state = "received"
    session.commit()
    later = jon.get(f"/app/live?group={gid(s)}&after=0").json()
    assert sorted(d["text"] for d in later["delivered"]) == ["Delivered to 0 of 2", "Delivered to 1 of 2"]


def test_nothing_in_a_group_is_ever_acked_from_the_app(windows, groups_seed, session):  # noqa: F811
    """C18: a group copy to the viewer's own machine is never acked by POST /app/ack, and gets no person_seen_at."""
    s = groups_seed
    maya = windows("Maya")
    copy = session.scalars(select(Message).where(Message.post_id == s["posts"]["jon_reply"].id,
                                                 Message.recipient_agent_id == s["machines"]["maya"].id)).one()
    csrf = csrf_of(maya.get("/app/compose").text)
    r = maya.post("/app/ack", json={"csrf_token": csrf, "ids": [str(copy.id)]})
    assert r.status_code == 200 and r.json() == {"acked": []}
    session.refresh(copy)
    assert copy.acked_at is None and copy.person_seen_at is None and copy.delivery_state == "stored"
    # The 1:1 unread counts don't include group copies.
    assert 'rc-count' not in maya.get("/app/inbox").text.split('<ul class="rc-convs rc-groups"', 1)[-1]


# D12 (G12 L4): open latest -----------------------------------------------------------------------------------

def _newest(html):
    m = re.search(r'<a class="rc-conv" href="[^"]*/app/(conversations|groups)/([^"#]+)#latest"[^>]*data-newest', html)
    return (m.group(1), m.group(2)) if m else None


def test_open_latest_opens_a_group_when_its_post_is_newest_and_a_conversation_otherwise(  # noqa: F811
        windows, groups_seed, client, session):
    s = groups_seed
    maya = windows("Maya")
    inbox = maya.get("/app/inbox", headers=OPENS_LATEST).text
    assert "data-open-latest" in inbox
    assert _newest(inbox) == ("groups", gid(s))  # the seed's posts are newer than its 1:1 messages
    assert len(re.findall(r"data-newest", inbox)) == 1
    r = client.post("/api/v1/messages", headers=auth(s["machine_tokens"]["jon"]),
                    json={"id": str(uuid.uuid4()), "to": "maya@example.test", "body": "Newer than the group."})
    assert r.status_code == 201, r.text
    assert _newest(maya.get("/app/inbox", headers=OPENS_LATEST).text) == (
        "conversations", r.json()["message"]["conversation_id"])
    post, _ = make_post(session, s["groups"]["checkout"], s["machines"]["priya"], "zq-agent-priya-web", "Newest.")
    session.commit()
    assert _newest(maya.get("/app/inbox", headers=OPENS_LATEST).text) == ("groups", gid(s))


# PR5: nothing read-related on a group page ------------------------------------------------------------------------

VOLATILE = r'data-states-since="[^"]*"|name="message_id" value="[^"]*"|"now":"[^"]*"'


def test_a_copy_being_delivered_changes_only_delivered_on_the_posters_pages(windows, groups_seed, session,  # noqa: F811
                                                                           client):
    s = groups_seed
    post, copies = make_post(session, s["groups"]["checkout"], s["machines"]["maya"], "zq-agent-maya-laptop",
                             "Can someone check the totals rounding?")
    session.commit()
    maya = windows("Maya")
    paths = (f"/app/groups/{gid(s)}", "/app/inbox", f"/app/live?group={gid(s)}&after=0")

    def snapshot():
        return {p: re.sub(VOLATILE, "", maya.get(p).text) for p in paths}

    before = snapshot()
    tokens = {s["machines"][k].id: s["machine_tokens"][k] for k in ("jon", "priya")}
    for c in copies:  # Jon's and Priya's machines ack their copies through the API
        r = client.post(f"/api/v1/messages/{c.id}/ack", headers=auth(tokens[c.recipient_agent_id]))
        assert r.status_code == 200, r.text
    session.expire_all()
    assert all(m.delivery_state == "received" for m in session.scalars(select(Message).where(
        Message.post_id == post.id)))
    after = snapshot()
    assert after["/app/inbox"] == before["/app/inbox"]
    for p in (paths[0], paths[2]):
        changed = [line for line in after[p].replace("},{", "}\n{").splitlines()
                   if line not in before[p].replace("},{", "}\n{").splitlines()]
        assert changed and all("Delivered to 2 of 2" in line for line in changed), (p, changed)


# §21.13 (C20): the team-iso diff probe, group pages ------------------------------------------------------------

def test_another_teams_groups_and_posts_change_nothing_on_a_members_group_pages(windows, groups_seed, session,  # noqa: F811
                                                                                client):
    """Snapshot every group page, live answer and list a northwind member can fetch; contoso makes a group with
    posts and sends 1:1 messages; snapshot again: identical (the numbers are per team)."""
    from conftest_groups import make_capable, make_group

    s = groups_seed
    paths = ["/app/inbox", "/app/compose"] + [f"/app/groups/{g.id}" for g in s["groups"].values()] + [
        f"/app/live?group={g.id}&after=0" for g in s["groups"].values()] + ["/app/live?after=0"]

    def everything():
        out = {}
        for who in ("Maya", "Jon", "Sam"):
            w = windows(who)
            for p in paths:
                r = w.get(p)
                out[(who, p)] = (r.status_code, re.sub(VOLATILE, "", r.text))
        return out

    before = everything()
    ct = s["teams"]["contoso"]
    make_capable(session, s["machines"]["lee"])
    there = make_group(session, ct, "checkout", [s["people"]["Maya"], s["people"]["Lee"]])  # the same name, contoso
    for i in range(3):
        make_post(session, there, s["machines"]["lee"], "zq-agent-lee-desk", f"contoso post {i}")
    session.commit()
    for i in range(3):
        r = client.post("/api/v1/messages", headers=auth(s["machine_tokens"]["lee"]),
                        json={"id": str(uuid.uuid4()), "to": "maya@example.test", "body": f"contoso {i}"})
        assert r.status_code == 201, r.text
    after = everything()
    assert after.keys() == before.keys()
    changed = [k for k in before if before[k] != after[k]]
    assert changed == [], changed
    for who in ("Maya", "Jon"):
        assert str(there.id) not in windows(who).get("/app/inbox").text
        assert same_answer(windows(who).get(f"/app/groups/{there.id}"), windows(who).get(f"/app/groups/{UNKNOWN}"))
