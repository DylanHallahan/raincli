"""Markdown rendering (protocol §16.9, §16.12 C3, C13): an XSS corpus, links, images and the one safe value."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from raincli_server.markdown import REL, render

TEMPLATES = Path(__file__).resolve().parents[2] / "raincli_server" / "web" / "templates"

CORPUS = [
    "[x](javascript:alert(1))", "[x](JaVaScRiPt:alert(1))", "[x](  javascript:alert(1))",
    "[x](&#106;avascript:alert(1))", "[x](&#x6A;avascript:alert(1))", "[x](java&#x09;script:alert(1))",
    "[x](java&#10;script:alert(1))", "[x](%6Aavascript:alert(1))", "[x](javascript&colon;alert(1))",
    "[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)", "[x](DATA:text/html,<script>alert(1)</script>)",
    "[x](vbscript:msgbox(1))", "[x](VbScRiPt:msgbox(1))", "[x](file:///etc/passwd)",
    "<javascript:alert(1)>", "<JAVASCRIPT:alert(1)>", "<data:text/html,x>", "<vbscript:x>",
    "![x](javascript:alert(1))", "![x](https://evil.example/pixel.png)", "![x\" onerror=\"alert(1)](x.png)",
    "<img src=x onerror=alert(1)>", "<script>alert(1)</script>", "<svg onload=alert(1)>", "<iframe src=x>",
    "<a href=\"javascript:alert(1)\">x</a>", "[x](https://ok.example \"t\\\" onmouseover=\\\"alert(1)\")",
    "[x](//evil.example)", "[x](\\\\evil.example)", "[x](/\\evil.example)", "[x](/app/handoff?code=rch_x)",
    "[x](/app/local/settings)", "[x](/app/account)", "[x]: javascript:alert(1)\n\n[x]",
    "`<script>alert(1)</script>`", "```\n<script>alert(1)</script>\n```",
    "*<b onclick=alert(1)>x</b>*", "[x](https://ok.example)<style>body{display:none}</style>",
]


class Tags(HTMLParser):
    """Every start tag with its attributes, as a browser would parse them."""

    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


ALLOWED_TAGS = {"p", "a", "strong", "em", "code", "pre", "ul", "ol", "li", "blockquote", "h1", "h2", "h3", "h4",
                "h5", "h6", "hr", "br"}


@pytest.mark.parametrize("source", CORPUS)
def test_xss_corpus_renders_inert(source):
    html = str(render(source))
    lowered = html.lower()
    assert "<script" not in lowered and "<img" not in lowered and "<svg" not in lowered
    assert "<iframe" not in lowered and "<style" not in lowered and "<b " not in lowered
    parser = Tags()
    parser.feed(html)
    for tag, attrs in parser.tags:
        assert tag in ALLOWED_TAGS, html
        assert not [a for a in attrs if a.startswith("on") or a in ("style", "src", "srcset")], html
    for href in re.findall(r'href="([^"]*)"', html):
        assert re.match(r"^(https?:|mailto:|/app/conversations/)", href), href
    assert not re.search(r'href="[^"]*(javascript|vbscript|data):', lowered)


def test_allowed_links_get_rel_and_handoff_paths_stay_text():
    for source, href in (("[a](https://example.com/x)", "https://example.com/x"), ("<https://example.com>", "https://example.com"),
                         ("[m](mailto:a@example.com)", "mailto:a@example.com"),
                         ("[c](/app/conversations/123#m-1)", "/app/conversations/123#m-1")):
        html = str(render(source))
        assert f'href="{href}"' in html and f'rel="{REL}"' in html, source
    for source in ("[h](/app/handoff?code=rch_secret)", "[l](/app/local/settings)"):
        assert "<a " not in str(render(source))


def test_images_show_alt_text_only_and_linkify_is_off():
    assert str(render("![a *diagram*](https://example.com/d.png)")).strip() == "<p>a diagram</p>"
    assert "<a " not in str(render("see https://example.com and www.example.com"))


def test_markdown_formatting_renders():
    html = str(render("# Title\n\n**bold** and `code`\n\n- one\n- two\n\n> quote"))
    for tag in ("<h1>", "<strong>bold</strong>", "<code>code</code>", "<ul>", "<blockquote>"):
        assert tag in html


def test_the_rendered_body_is_the_only_safe_value_in_templates():
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text("utf-8")
        assert "|safe" not in text.replace(" ", ""), path.name
        assert "autoescape false" not in text, path.name
    uses = [p.name for p in TEMPLATES.rglob("*.html") if "|markdown" in p.read_text("utf-8").replace(" ", "")]
    assert uses and all("attach" not in name for name in uses)


def test_conversation_view_renders_markdown(client, world, session):
    from test_web_app import add_message, login

    msg = add_message(session, world["agents"]["bob"], world["agents"]["alice"],
                      body="**bold** [site](https://example.com) [bad](javascript:alert(1)) <script>x</script>")
    login(client)
    page = client.get(f"/app/conversations/{msg.conversation_id}").text
    assert "<strong>bold</strong>" in page
    assert f'<a href="https://example.com" rel="{REL}">site</a>' in page
    assert "[bad](javascript:alert(1))" in page and "<script>x</script>" not in page
