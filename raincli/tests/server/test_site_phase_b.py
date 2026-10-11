"""Website Phase B: the homepage copy is homepage-copy.md word for word (the strings are embedded here, so the test
never reads marketing's files), and the request-access slot is a placeholder."""

from __future__ import annotations

import html as htmllib
import re

import pytest

SECTIONS = [
    ("hero", [
        "Stop being the clipboard between your team's agents.",
        "Your agent sends a question or a Markdown report straight to a teammate's agent, and the answer comes back "
        "to yours. You both see every message.",
        "Request access", "How it works",
        "Free during the pilot. Invite-only for now.",
    ]),
    ("demo", [
        "Example",
        "Maya pastes her AI's answer into chat. Jon pastes it into his.",
        "Here, Maya's agent sends the question to Jon's agent itself.",
        "Maya can see how far it got.",
        "Jon's agent answers and attaches its report.",
        "Nobody pasted anything, and both of them saw the whole thread.",
    ]),
    ("how", [
        "How it works",
        "1 Install and sign in",
        "On Windows, install the RainCLI app. It needs no admin rights and no Python. On Linux, or over SSH, run "
        "raincli login. Either way you sign in with your email and password.",
        "2 Your agents show up for the team",
        "Each machine lists its Claude Code, Codex and Herdr sessions. Teammates see the names and whether each one "
        "is ready, busy or offline. They never see prompts, paths or transcripts.",
        "3 Send to an agent",
        "Your agent sends with the raincli CLI, or you write in the app. Pick the teammate and the agent. Herdr "
        "agents get it right away. Claude Code and Codex sessions get it at their next turn, and the conversation "
        "header tells you which.",
        "4 Watch it land",
        "Each message shows stored, received, submitted and replied as they happen. The app updates by itself, so "
        "replies show up without a refresh.",
    ]),
    ("app", [
        "In the app",
        "Instant or Next turn. How soon the agent will see your message.",
        "Ready, Busy, Offline. Shown next to every agent on your team.",
        "A heads-up when a machine is quiet. If a teammate's machine hasn't connected in a while, you're told before "
        "you send.",
        "Reports, attached. Markdown files go with the message, with their size and checksum.",
        "Archive when you're done. Nothing is deleted, and a new message brings the conversation back.",
        "Your own time zone. Every time is shown in local time.",
    ]),
    ("trust", [
        "Who can reach your agents",
        "Only people on your team. Their messages go straight to the agent they picked, marked as coming from a "
        "teammate.",
        "A message can ask your agent for something. It can't change the agent's instructions or permissions, and "
        "nothing runs by itself.",
        "If you'd rather approve each sender first, turn on approval mode in Settings or with "
        "raincli trust --mode list.",
    ]),
    ("works-with", [
        "Works with",
        "Claude Code, Codex and Herdr. Any other agent can use the raincli CLI and the skill that comes with it. "
        "Codex needs version 0.145.0 or later, plus a one-time hook approval in Codex.",
    ]),
    ("security", [
        "Security",
        "What's protected",
        "Traffic is encrypted with TLS. Your machines only make outbound HTTPS connections, so there are no ports "
        "to open.",
        "Each machine has its own credential. The server stores only a hash, Windows keeps it protected with DPAPI, "
        "and revoking it takes effect straight away.",
        "The sender of every message comes from that credential. Nobody can pose as a teammate by editing a message.",
        "What isn't, yet",
        "Messages aren't end-to-end encrypted. The server stores them and can read them, so treat it like your team "
        "chat.",
        "The Windows installer isn't code-signed yet. Windows may warn you the first time. Check the SHA-256 "
        "published with each release.",
    ]),
    ("faq", [
        "FAQ",
        "Is it free? Yes, during the pilot.",
        "Does everyone need the same AI tool? No. One person can use Claude Code, another Codex, another Herdr. "
        "Each agent gets its own address.",
        "Can a teammate's agent take over mine? No. It can send a request. Your agent decides what to do with it "
        "within its current job, and a message can't change instructions or permissions.",
        "What if their agent is offline? The message waits. Their machine picks it up when it's back, and you'll "
        "see it move to received.",
        "Can you read our messages? Yes. They're encrypted in transit but not end to end, so the server can read "
        "them. Share what you'd share in your team chat.",
        "Which systems does it run on? Windows, through the app. Linux, including headless servers. We haven't "
        "tested macOS yet.",
        "Is the code open? Yes. It's on GitHub: github.com/DylanHallahan/raincli",
    ]),
    ("closing", [
        "Want it for your team?",
        "It's a small invite-only pilot right now.",
        "Already invited? Sign in",
    ]),
]
HEADER = ["RainCLI", "How it works", "Security", "Install", "Sign in"]
FOOTER = ("RainCLI · Messages between your team's agents · Free during the pilot · TLS in transit, not end-to-end "
          "encrypted · GitHub")


def plain(fragment: str) -> str:
    """The visible words: tags dropped (a block element ends a sentence with a space), entities and whitespace
    normalised."""
    fragment = re.sub(r"<!--.*?-->", "", fragment, flags=re.S)
    fragment = re.sub(r"</?(p|li|h[1-6]|summary|div|span class=\"step-num\"[^>]*)>", " ", fragment)
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split()).replace(" .", ".")


def section(page: str, ident: str) -> str:
    m = re.search(rf'<section id="{ident}"[^>]*>(.*?)</section>', page, re.S)
    assert m, ident
    return plain(m.group(1))


@pytest.fixture
def home(client):
    return client.get("/").text


@pytest.mark.parametrize("ident,expected", SECTIONS, ids=[s[0] for s in SECTIONS])
def test_each_section_has_the_copy_word_for_word(home, ident, expected):
    words = section(home, ident)
    for sentence in expected:
        assert sentence in words, sentence


def test_sections_come_in_the_specified_order(home):
    ids = re.findall(r'<section id="([a-z-]+)"', home)
    assert ids == [s[0] for s in SECTIONS[:6]] + ["security", "install", "faq", "closing"]


def test_header_and_footer(home):
    header = plain(re.search(r"<header.*?</header>", home, re.S).group(0))
    for word in HEADER:
        assert word in header, word
    assert FOOTER in plain(re.search(r"<footer.*?</footer>", home, re.S).group(0))


def test_request_access_is_a_placeholder(home, client):
    raw = client.get("/").text
    assert raw.count('href="#closing"') == 1 and "Request access" in raw
    closing = re.search(r'<section id="closing".*?</section>', raw, re.S).group(0)
    assert "<form" not in closing and "mailto:" not in raw and "@" not in plain(closing)
    assert "<form" not in raw  # no waitlist form anywhere yet
    template = (client.app.state.templates.env.loader.get_source(client.app.state.templates.env, "home.html")[0]
                if hasattr(client.app.state, "templates") else None)
    if template is not None:
        assert template.count("TODO(request-access)") >= 2


def test_free_during_the_pilot_only_in_the_hero_faq_and_footer(home):
    words = plain(home)
    assert words.count("Free during the pilot") == 2  # the hero line and the footer (copy file's footer)
    assert "Yes, during the pilot." in section(home, "faq")
    for promise in ("pricing", "price", "per seat", "/month", "after the pilot", "will cost", "paid plan"):
        assert promise not in words.lower(), promise


def test_the_anchors_still_exist(home):
    for ident in ("how", "install", "security", "closing", "hero", "demo", "app", "trust", "faq"):
        assert f'id="{ident}"' in home, ident
    for ident in ("how", "security", "install"):
        assert f'href="#{ident}"' in home


def test_no_external_resources_and_the_logo_is_inline(home):
    assert re.search(r'<(?:script|link|img|iframe)[^>]+(?:src|href)="(?:https?:)?//', home) is None
    assert "<img" not in home and "fonts" not in home
    assert home.count('<svg class="mark"') == 2  # the header and the demo's sidebar
    assert re.search(r'style="', home) is None and "<style" not in home
