"""Static checks on the app's bundled local pages (protocol §16.9, §16.10, §16.12 C4)."""

from __future__ import annotations

import re
from pathlib import Path

from raincli_agent.app.policy import LOCAL_PAGES
from raincli_agent.app.window import LOCAL_DIR

SERVER_TOKENS = Path(__file__).resolve().parents[2] / "raincli_server" / "web" / "static" / "tokens.css"
CSP = re.compile(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">')


def test_local_tokens_are_the_server_tokens():
    # One design-token file: rebranding the app and the website is the same change.
    assert (LOCAL_DIR / "tokens.css").read_bytes() == SERVER_TOKENS.read_bytes()


def test_no_raw_colours_outside_tokens_css():
    for path in LOCAL_DIR.iterdir():
        if path.name != "tokens.css" and path.suffix in (".css", ".html", ".js"):
            assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", path.read_text("utf-8")), path.name


def test_every_page_is_local_only_with_a_strict_csp():
    for page in LOCAL_PAGES:
        html = (LOCAL_DIR / f"{page}.html").read_text("utf-8")
        csp = CSP.search(html).group(1)
        for directive in ("default-src 'self'", "script-src 'self'", "style-src 'self'", "form-action 'none'",
                          "frame-ancestors 'none'", "object-src 'none'", "base-uri 'none'"):
            assert directive in csp, (page, directive)
        assert "unsafe" not in csp
        assert not re.search(r"(src|href)=\"(https?:)?//", html), page  # nothing from another origin
        assert not re.search(r"<script(?![^>]*\bsrc=)", html) and " style=" not in html and "<style" not in html
        assert re.search(r'href="tokens\.css"', html) and re.search(r'href="local\.css"', html)


def test_pages_reach_the_app_only_through_the_nonce_bridge():
    scripts = {p.name: p.read_text("utf-8") for p in LOCAL_DIR.glob("*.js")}
    for name, js in scripts.items():
        assert "fetch(" not in js and "XMLHttpRequest" not in js and "localStorage" not in js, name
        if name != "local.js":
            assert "pywebview.api" not in js, name  # every call goes through rc.call, which adds the nonce
    assert "[window.__rcNonce].concat(args)" in scripts["local.js"]
