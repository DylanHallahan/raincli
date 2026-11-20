"""The app's account switching pages (protocol §23 A9; brief AS7, C3, C4, C7, C10; A14 L2, L3; A16), on the shared
accounts seed (tests/conftest_accounts.py): Maya's own window and her window acting for Help Desk (a delegated
session), on her machine zq-handle-maya. The switcher on "Signed in as", GET /app/accounts, the cues (the rail
avatar, the page title, "As:"), "sent by" in the account's own view only, "Your agents" hidden under a grant, the
version gate and the sentinel safety. Real PostgreSQL."""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient

from api_helpers import auth
from conftest_accounts import accounts_seed  # noqa: F401  (fixture)
from conftest_v06 import PASSWORD, v06_seed  # noqa: F401  (fixture)
from raincli_server import accounts, markdown
from test_app_mode import NAV, WEBVIEW_UA, handoff_code
from test_v06_web import text
from test_web_app import login

UNKNOWN = str(uuid.uuid4())
V062 = {"User-Agent": WEBVIEW_UA + " RainCLIAppVersion/0.6.2"}
V061 = {"User-Agent": WEBVIEW_UA + " RainCLIAppVersion/0.6.1"}


def _window(app, client, token, headers):
    w = TestClient(app, headers=headers)
    r = w.get(f"/app/handoff?code={handoff_code(client, token)}", headers=NAV, follow_redirects=False)
    assert r.status_code == 303, r.text
    return w


@pytest.fixture
def windows(app, client, accounts_seed):  # noqa: F811
    s = accounts_seed
    opened = {"own": _window(app, client, s["person_tokens"]["Maya"], V062),
              "helpdesk": _window(app, client, s["delegated_token"], V062),
              "old": _window(app, client, s["person_tokens"]["Maya"], V061),
              "jon": _window(app, client, s["person_tokens"]["Jon"], V062)}
    yield opened
    for w in opened.values():
        w.close()


def switcher(html):
    m = re.search(r'<div class="rc-whoami rc-switcher" data-switcher>(.*?)\n</div>\n', html, re.S)
    return m.group(1) if m else None


def rows(panel):
    """(email, label text, unread) per account row of the switcher, in order."""
    out = []
    for email, inner in re.findall(r'data-account-switch="([^"]+)"[^>]*>(.*?)</button>', panel, re.S):
        count = re.search(r'<span class="rc-count">(\d+)', inner)
        out.append((email, text(inner), int(count.group(1)) if count else 0))
    return out


# C3, C7: the switcher and its counts -------------------------------------------------------------------------

def test_the_switcher_lists_the_windows_accounts_with_unread_counts(windows, accounts_seed, session):  # noqa: F811
    s = accounts_seed
    nw = s["teams"]["northwind"].id
    page = windows["own"].get("/app/inbox").text
    panel = switcher(page)
    assert panel is not None
    helpdesk_unread = accounts.unread_total(session, s["people"]["Help Desk"].id, nw)
    assert helpdesk_unread == 3  # Jon's message, Priya's agent's held one, and the ops agent's escalation
    listed = rows(panel)
    assert [(e, n) for e, _, n in listed] == [("maya@example.test", 0), ("helpdesk@example.test", 3)]  # own first
    assert 'data-account-switch="maya@example.test" aria-current="true"' in panel  # the one shown
    assert re.search(r'data-account-add="billing@example.test"', panel)  # granted, not added
    assert re.search(r'data-account-remove="helpdesk@example.test"', panel)  # delegated: removable
    assert 'data-account-remove="maya@example.test"' not in panel  # the own account isn't
    assert ">3<" in panel.split("rc-switcher-btn", 1)[1].split("</button>", 1)[0]  # the others' total on the line
    # The rail's avatar names the account and opens the switcher.
    assert re.search(r'<button type="button" class="rc-rail-avatar" data-switcher-open aria-label="Signed in as Maya\. '
                     r'Switch account" title="Maya">MA</button>', page)
    # People, never machines or agents (AS13): none of Help Desk's machine names in Maya's own window either.
    assert all(x not in page for x in s["helpdesk_strings"])


def test_get_app_accounts(windows, client, accounts_seed):  # noqa: F811
    own = windows["own"].get("/app/accounts").json()
    assert [(a["email"], a["kind"], a["active"], a["unread"]) for a in own["accounts"]] == [
        ("maya@example.test", "own", True, own["accounts"][0]["unread"]), ("helpdesk@example.test", "granted", False, 3)]
    assert own["accounts"][0]["initials"] == "MA" and own["accounts"][1]["display_name"] == "Help Desk"
    assert own["addable"] == [{"email": "billing@example.test", "display_name": "Billing"}]
    hd = windows["helpdesk"].get("/app/accounts").json()
    assert [(a["email"], a["active"]) for a in hd["accounts"]] == [("maya@example.test", False),
                                                                    ("helpdesk@example.test", True)]
    assert hd["addable"] == []  # adding is the own session's (A9)
    assert all("zq-" not in str(v) for a in own["accounts"] + hd["accounts"] for v in a.values())
    # Not on the website.
    assert login(client, "maya@example.test", PASSWORD).status_code == 303
    assert client.get("/app/accounts").status_code == 404


def test_the_counts_move_with_the_live_list(windows, accounts_seed, client):  # noqa: F811
    s = accounts_seed
    first = windows["own"].get("/app/live?after=0").json()
    before = rows(switcher(first["list"]["html"]))
    r = client.post("/api/v1/messages", headers=auth(s["machine_tokens"]["jon"]),
                    json={"id": str(uuid.uuid4()), "to": "helpdesk@example.test", "body": "And the printer."})
    assert r.status_code == 201, r.text
    moved = windows["own"].get(f"/app/live?after=0&list_rev={first['list_rev']}").json()
    assert moved["list"] is not None and moved["list_rev"] != first["list_rev"]  # C7: the 5 s poll carries it
    after = rows(switcher(moved["list"]["html"]))
    assert after[1][2] == before[1][2] + 1 and after[0] == before[0]


# L3: only a 0.6.2 window --------------------------------------------------------------------------------------

def test_an_older_window_and_the_website_keep_the_plain_line(windows, client, accounts_seed):  # noqa: F811
    old = windows["old"].get("/app/inbox").text
    assert switcher(old) is None and "rc-rail-avatar" not in old and "data-account-" not in old
    assert "Signed in as <strong>Maya</strong> (maya@example.test)" in old
    assert login(client, "maya@example.test", PASSWORD).status_code == 303
    site = client.get("/app/inbox").text
    assert "data-switcher" not in site and "data-account-" not in site


# C4: the cues -----------------------------------------------------------------------------------------------------

def test_a_delegated_window_says_as_whom_everywhere(windows, accounts_seed):  # noqa: F811
    s = accounts_seed
    hd = windows["helpdesk"]
    conv = str(s["conversations"]["helpdesk_jon"].id)
    page = hd.get(f"/app/conversations/{conv}").text
    assert re.search(r"<title>[^<]* · Help Desk</title>", page)
    assert '<span class="rc-as">As: <strong>Help Desk</strong></span>' in page
    assert re.search(r'class="rc-rail-avatar rc-rail-avatar-for"[^>]*aria-label="Signed in as Help Desk\. Switch account"'
                     r'[^>]*>HD</button>', page)
    assert "As <strong>Help Desk</strong>" in switcher(page) and "Maya · switch account" in text(switcher(page))
    assert "Sent as Help Desk." in hd.get("/app/compose").text
    own = windows["own"].get(f"/app/conversations/{s['conversations']['jon'].id}").text
    assert "rc-as" not in own and not re.search(r"<title>[^<]* · Maya</title>", own)  # the own window: no "As:"


# C10, AS6: "sent by" in the account's own view only ---------------------------------------------------------------

def test_sent_by_shows_only_to_the_account(windows, accounts_seed, app, client, session):  # noqa: F811
    s = accounts_seed
    from raincli_server import identity

    conv = str(s["conversations"]["helpdesk_jon"].id)
    sent = str(s["messages"]["delegated_send"].id)
    hd = windows["helpdesk"].get(f"/app/conversations/{conv}").text
    line = hd.split(f'id="m-{sent}"', 1)[1].split("</li>", 1)[0]
    assert '<span class="rc-sent-by">sent by Maya</span>' in line
    # Help Desk's own session (its own machine, no grant) sees it too: "with any session".
    own_hd = identity.issue_person_session(session, s["people"]["Help Desk"], s["machines"]["helpdesk"])
    session.commit()
    w = _window(app, client, own_hd, V062)
    assert "sent by Maya" in w.get(f"/app/conversations/{conv}").text
    w.close()
    # Jon, the recipient: Help Desk only, never Maya.
    jon = windows["jon"].get(f"/app/conversations/{conv}").text
    assert sent in jon and "sent by" not in jon and "Maya" not in jon.split('<main', 1)[1]
    live = windows["jon"].get(f"/app/live?conversation={conv}&after=0").json()
    assert "sent by" not in str(live) and "Maya" not in "".join(m["html"] for m in live["messages"])


# A16: "Your agents" hidden under a grant ---------------------------------------------------------------------------

def test_your_agents_is_hidden_under_a_grant(windows, accounts_seed):  # noqa: F811
    s = accounts_seed
    hd = windows["helpdesk"]
    oa = str(s["conversations"]["helpdesk_own_agents"].id)
    for page in ("/app/inbox", "/app/archived", "/app/compose"):
        assert oa not in hd.get(page).text, page
    for path in (f"/app/conversations/{oa}", f"/app/live?conversation={oa}&after=0"):
        a, b = hd.get(path), hd.get(path.replace(oa, UNKNOWN))
        assert (a.status_code, a.content) == (b.status_code, b.content) and a.status_code == 404, path
    # "From your agents" (the self conversation) stays.
    assert str(s["conversations"]["helpdesk_self"].id) in hd.get("/app/inbox").text


# A14 L2: the sentinels ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["switch", "add-account", "remove-account"])
def test_a_sentinel_in_a_browser_explains_and_reflects_nothing(windows, name):  # noqa: F811
    r = windows["own"].get(f"/app/local/{name}?account=evil%40example.test&nonce=x<script>")
    assert r.status_code == 200 and "evil@example.test" not in r.text and "<script>x" not in r.text
    # Never a link in a message, nor a handoff destination.
    rendered = str(markdown.render(f"[go](/app/local/{name}?account=helpdesk%40example.test)"))
    assert "href" not in rendered and "<a" not in rendered  # left as text, never a link
    from raincli_server.web import present
    assert present.handoff_next(f"/app/local/{name}") is None
