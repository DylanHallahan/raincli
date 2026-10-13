"""The homepage, concept C "Paper" (marketing landing-v2, which replaced website Phase B's direction A). The copy is
the concept's, checked against v0.6 person routing; the strings are embedded here, so the test never reads
marketing's files. Request access is an email link."""

from __future__ import annotations

import html as htmllib
import re
from pathlib import Path

import pytest

SECTIONS = [
    ("hero", [
        "Agent Direct Messaging for your team",
        "Your agent asks a teammate's agent directly. No copy-pasting.",
        "Request access", "Read the details",
        "Agents Claude Code · Codex · Herdr · any CLI agent",
        "Runs on Windows app · Linux, headless too",
        "Access Invite-only pilot",
        "The RainCLI app · fictional team",
    ]),
    ("how", [
        "01 How a message moves",
        "Agent to agent, with every hop on the record.",
        "Built for small dev teams where everyone runs their own coding agent. You address a teammate, not a "
        "machine: your agent sends from the CLI, and RainCLI delivers it to the agent they picked as their inbox. "
        "Both of you can read the thread in the app.",
        "Your agent runs raincli send jon@example.com",
        "RainCLI server stored stores it for Jon Their machine",
        "Their machine received the runtime picks it up and acknowledges it",
        "Their inbox agent submitted gets it in its session",
        "The reply replied comes back to the agent that asked, linked to your message",
        "stored On the server. The recipient's machine hasn't picked it up yet.",
        "held Waiting on their side: they haven't picked an inbox yet, their inbox is busy or offline, it's a "
        "next-turn agent, or they approve senders first.",
        "replied Their agent answered, in the same thread.",
    ]),
    ("threads", [
        "A reply is linked to the message it answers. A report travels with its checksum.",
        "Every message belongs to a conversation, one per pair of people.",
        "Markdown reports go with the message: UTF-8, up to 256 KiB each, five files and 1 MiB per message.",
    ]),
    ("cli", [
        "02 From the command line",
        "Your agent already knows how to run this.",
        "raincli people",
        "raincli send @jon@example.com --body-file - --attach audit.md",
        "raincli inbox", "raincli thread <CONV_ID>", "raincli fetch <MSG_ID>", "raincli watch",
        "raincli --skill > ~/.claude/skills/raincli/SKILL.md",
        "People, not machines A message to @jon@example.com goes to the agent Jon picked as an inbox, or waits in "
        "Jon's app until one is picked. Teammates see people and availability, never your machines or agents.",
    ]),
    ("app", [
        "03 In the app",
        "Instant or Next turn How soon their inbox agent will see your message.",
        "Ready, Busy, Offline Whether the other side is around before you send.",
        "A quiet inbox If a teammate's inbox hasn't connected in a while, or they haven't picked one yet, you're "
        "told before you send.",
        "Sam's inbox hasn't connected since Tuesday.",
        "Done, not deleted A new message brings the conversation back.",
    ]),
    ("works-with", [
        "04 Works with",
        "Mixed teams are the point.",
        "Herdr Instant as soon as the agent is idle Herdr 0.9.3 or later",
        "Codex with hooks enabled (0.145.0 or later on Windows), one-time approval in /hooks",
        "macOS hasn't been tested yet.",
    ]),
    ("install", [
        "05 Install",
        "Windows app, or a few commands on Linux.",
        "Download for Windows",
        "raincli inbox show", "raincli inbox set <agent>",
    ]),
    ("security", [
        "06 Trust and security",
        "What's protected, and what isn't yet.",
        "TLS everywhere. Machines only make outbound HTTPS connections: no inbound ports, no VPN.",
        "The sender comes from the credential, never from the message text. Messages reach only people on your team.",
        "Teammates see people and availability, never your machines, agents or sessions.",
        "Messages aren't end-to-end encrypted. The server stores them and can read them.",
        "The Windows installer isn't code-signed. The updater checks its published SHA-256, which catches a "
        "corrupted download, not a malicious release.",
    ]),
    ("source", [
        "Read the code, or run your own server.",
        "07 Source and self-hosting",
        "The client and the server are public on GitHub, with a runbook for hosting it yourself: FastAPI, PostgreSQL "
        "and Nginx, plus migrations, backups and rollback.",
    ]),
    ("faq", [
        "08 Questions",
        "Is it free? Yes, during the pilot.",
        "Does everyone need the same AI tool? No. One person can use Claude Code, another Codex, another Herdr. "
        "Everyone on the team can be reached, whatever agent they use.",
        "What if they haven't picked an inbox yet? The message waits in their app, and you see it held: waiting "
        "for them to pick an inbox. When they pick one, the waiting messages can go to it.",
        "What if their agent is offline? The message waits on the server, held while their inbox is offline. "
        "Their machine picks it up when it's back, and you'll see it move to received.",
        "Can you read our messages? Yes. They're encrypted in transit but not end to end, so the server can read "
        "them. Share what you'd share in your team chat, or run your own server.",
        "Does a next-turn agent wake up for a message? No. A Claude Code or Codex session sees it the next time "
        "it's used, and the sender sees it held until then. Use a Herdr agent if messages should be handled while "
        "you're away.",
    ]),
    ("closing", [
        "Want it for your team?",
        "It's a small invite-only pilot. Email dylan@dylanhallahan.com and say a bit about your team.",
        "Request access",
        "Already invited? Sign in",
    ]),
]
ORDER = ["hero", "how", "threads", "cli", "app", "works-with", "install", "security", "source", "faq", "closing"]
HEADER = ["RainCLI", "How it works", "CLI", "Install", "Security", "GitHub", "Sign in", "Request access"]
FOOTER = "RainCLI Free during the pilot TLS in transit · not end-to-end encrypted GitHub"
MAILTO = "mailto:dylan@dylanhallahan.com"


def plain(fragment: str) -> str:
    """The visible words: tags dropped (a block element ends a sentence with a space), entities and whitespace
    normalised."""
    fragment = re.sub(r"<!--.*?-->", "", fragment, flags=re.S)
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split()).replace(" .", ".").replace(" ,", ",")


def section(page: str, ident: str) -> str:
    m = re.search(rf'<section[^>]*\bid="{ident}"[^>]*>(.*?)</section>', page, re.S)
    assert m, ident
    return plain(m.group(1))


@pytest.fixture
def home(client):
    return client.get("/").text


@pytest.mark.parametrize("ident,expected", SECTIONS, ids=[s[0] for s in SECTIONS])
def test_each_section_has_the_copy(home, ident, expected):
    words = section(home, ident)
    for sentence in expected:
        assert sentence in words, sentence


def test_sections_come_in_the_specified_order(home):
    assert re.findall(r'<section[^>]*\bid="([a-z-]+)"', home) == ORDER


def test_header_and_footer(home):
    header = plain(re.search(r"<header.*?</header>", home, re.S).group(0))
    for word in HEADER:
        assert word in header, word
    assert FOOTER in plain(re.search(r"<footer.*?</footer>", home, re.S).group(0))


def test_request_access_is_an_email_link(home):
    """The header's, the hero's and the closing band's Request access are plain mailto links; no form; the
    sign-in line stays."""
    hero = re.search(r'<section class="hero".*?</section>', home, re.S).group(0)
    assert re.search(rf'<a class="btn primary lg" href="{MAILTO}">.*?Request access</a>', hero, re.S)
    assert '<a class="btn ghost lg" href="#how">Read the details</a>' in hero
    closing = re.search(r'<section class="closing".*?</section>', home, re.S).group(0)
    assert re.search(rf'<a class="btn primary lg" href="{MAILTO}">.*?Request access</a>', closing, re.S)
    assert f'Email <a href="{MAILTO}">dylan@dylanhallahan.com</a>' in closing
    assert "Already invited? Sign in" in plain(closing)
    header = re.search(r"<header.*?</header>", home, re.S).group(0)
    assert f'href="{MAILTO}">Request access</a>' in header
    assert "<form" not in home and "?subject" not in home
    assert set(re.findall(r'href="(mailto:[^"]*)"', home)) == {MAILTO}


def test_free_during_the_pilot_only_in_the_hero_faq_and_footer(home):
    words = plain(home).lower()
    assert words.count("free during the pilot") == 1  # the footer (pass 2a removed the hero eyebrow)
    assert "Agent-to-agent" not in home
    for tag in ('name="description"', 'property="og:description"', 'name="twitter:description"'):
        assert re.search(rf'<meta {tag} content="[^"]*Free during the pilot\.', home), tag  # the preview tags keep it
    assert "Yes, during the pilot." in section(home, "faq")
    for promise in ("pricing", "price", "per seat", "/month", "after the pilot", "will cost", "paid plan"):
        assert promise not in words, promise


def test_public_on_github_never_open_source(home):
    """There is no licence yet: the code is "public on GitHub", never "open source"."""
    words = plain(home).lower()
    assert "public on github" in words
    for claim in ("open source", "open-source", "opensource", "the code open", "mit licen", "apache licen"):
        assert claim not in words and claim not in home.lower(), claim


def test_the_anchors_still_exist(home):
    for ident in ("how", "install", "security", "closing", "hero", "demo", "app", "trust", "faq", "cli"):
        assert f'id="{ident}"' in home, ident
    for ident in ("how", "security", "install", "cli"):
        assert f'href="#{ident}"' in home


def test_no_external_resources_and_the_logo_is_inline(home):
    # Nothing is fetched from another origin (the canonical link names the page, it loads nothing).
    assert re.search(r'<(?:script|img|iframe)[^>]+src="(?:https?:)?//', home) is None
    assert re.search(r'<link (?!rel="canonical")[^>]*href="(?:https?:)?//', home) is None
    assert "<img" not in home
    assert home.count('<svg class="mark"') == 3  # the header, the demo's title bar and the footer
    assert re.search(r'style="', home) is None and "<style" not in home and "<script>" not in home


def test_link_previews_have_absolute_urls_and_the_image_is_served(home, client):
    for prop in ("og:image", "twitter:image"):
        url = re.search(rf'<meta (?:property|name)="{prop}" content="([^"]+)">', home).group(1)
        assert re.fullmatch(r"http://testserver/static/og\.png\?v=[0-9a-f]{12}", url), url
    for tag in ('property="og:title" content="Agent Direct Messaging for your team"', 'property="og:type"',
                'property="og:url" content="http://testserver/"', 'name="twitter:card" content="summary_large_image"',
                'property="og:image:width" content="1200"', 'property="og:image:height" content="630"',
                'rel="canonical" href="http://testserver/"', 'property="og:image:alt"'):
        assert tag in home, tag
    r = client.get("/static/og.png")
    assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = int.from_bytes(r.content[16:20], "big"), int.from_bytes(r.content[20:24], "big")
    assert (width, height) == (1200, 630)


def test_the_page_loads_only_its_own_css_js_and_fonts(home, client):
    from raincli_server import web

    static = Path(web.__file__).parent / "static"
    assert re.findall(r'<link rel="stylesheet" href="/static/([^"?]+)', home) == ["landing.css"]
    assert re.findall(r'<script src="/static/([^"?]+)', home) == ["landing.js"]
    css = (static / "landing.css").read_text("utf-8")
    fonts = re.findall(r'url\("fonts/([^"]+)"\)', css)
    assert fonts == ["noto-serif-display-light-v2009.woff2", "noto-serif-display-light-italic-v2009.woff2",
                     "jetbrains-mono-regular-v2304.woff2"]
    assert "font-display:swap" in css and "data:" not in css and "url(http" not in css
    for name in fonts:
        assert client.get(f"/static/fonts/{name}").status_code == 200
    for family in ("NotoSerifDisplay", "JetBrainsMono"):
        assert "SIL Open Font License" in (static / "fonts" / f"OFL-{family}.txt").read_text("utf-8")
    readme = (static / "fonts" / "README.md").read_text("utf-8")
    assert "https://github.com/JetBrains/JetBrainsMono/releases/tag/v2.304" in readme
    assert "6f6376c6ed2960ea8a963cd7387ec9d76e3f629125bc33d1fdcd7eb7012f7bbf" in readme  # the release zip
    import hashlib
    digest = hashlib.sha256((static / "fonts" / "jetbrains-mono-regular-v2304.woff2").read_bytes()).hexdigest()
    assert digest in readme


DEMO = [
    "Jon", "Checkout vs. the new orders API", "Priya", "Retry cap question", "Earlier", "Sam",
    "Load test report", "Maya · agent · 10:42", "Jon · agent · 10:44", "To Jon", "Ready", "Instant",
    "Demo of the RainCLI app. Maya's agent sends a question to Jon. The message shows stored, received and submitted, "
    "and Jon shows as busy. Jon's agent replies with a Markdown report attached, the message shows replied, and Jon "
    "is ready again. Maya and Jon can both read the whole thread.",
]

# The demo shows only the app (demo-app-only spec): no before scene, no team chat, no pasting.
NOT_IN_THE_DEMO = ("Team chat", "Pasted AI output", "pasting", "Nobody pasted")


def _demo(home):
    return re.search(r'<figure class="demo" id="demo".*?</figure>', home, re.S).group(0)


def test_the_demo_shows_only_the_app(home):
    from raincli_server import web

    css = (Path(web.__file__).parent / "static" / "landing.css").read_text("utf-8")
    demo = _demo(home)
    for text in NOT_IN_THE_DEMO:
        assert text not in demo and text not in css, text
    assert "b-scene" not in demo and 'class="cap' not in demo and 'class="caps' not in demo  # pass 2a: no captions
    for gone in ("<button", "data-demo-pause", "data-demo-restart", 'class="ctrl"', 'class="prog"'):
        assert gone not in demo, gone  # pass 2a: no controls; the demo just plays
    for gone in (".caps", ".cap1", ".ctrl", ".prog", "k-cap", "k-prog"):
        assert gone not in css, gone
    js = (Path(web.__file__).parent / "static" / "landing.js").read_text("utf-8")
    assert "data-demo-pause" not in js and "data-demo-restart" not in js
    assert css.count(" 16s linear infinite") == 10  # m1 m2 file, four states, ready busy dot


def test_the_demo_has_the_v06_markup(home):
    """The app's own labels: a message an agent sent is "<Name> · agent" (§21.6)."""
    words = plain(_demo(home))
    for text in DEMO:
        assert text in words, text
    assert "Maya's agent ·" not in words and "Jon's agent ·" not in words


# The "true today" copy (person routing): no addressing a teammate's agent or machine, no agent list for the team.
NOT_TODAY = ("api-agent", "laptop-agent", "web-agent", "ops-agent", "Pick the teammate and the agent",
             "Each agent gets its own address", "Shown next to every agent on your team",
             "Teammates see the names", "show up for the team", "Send to an agent", "sends the question to Jon's agent",
             "Coming in v0.6", "In v0.6", "named agents", "machine's address", "The agent list your team sees",
             "Agents only reach agents", "Sam's laptop", "last seen")


def test_no_copy_claims_any_agent_routing_or_the_team_list(home):
    words = plain(home)
    for phrase in NOT_TODAY:
        assert phrase not in words, phrase
    assert '<div class="grp">Team</div>' not in _demo(home)


def test_the_blue_band_has_no_ruled_lines(home):
    """Pass 2a (user feedback, "what's with the horizontal lines"): the band keeps its glow but no line pattern, and
    the delivery states read without row rules."""
    from raincli_server import web

    css = (Path(web.__file__).parent / "static" / "landing.css").read_text("utf-8")
    band = re.search(r"\.band::before\{[^}]*\}", css).group(0)
    assert "radial-gradient" in band and "linear-gradient" not in band
    for rule in (r"\.gloss\{[^}]*\}", r"\.gl\{[^}]*\}"):
        assert "border" not in re.search(rule, css).group(0), rule
    assert '<dl class="gloss">' in home


def test_the_sign_in_headline_matches_the_hero(client):
    page = client.get("/login").text
    assert '<p class="auth-statement">Agent Direct Messaging <em>for your team</em></p>' in page
    assert "in one place" not in page
