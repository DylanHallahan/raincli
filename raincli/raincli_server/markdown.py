"""Message bodies as Markdown (protocol §16.9, §16.12 C3, C13).

CommonMark through ``markdown-it-py`` with raw HTML off and linkify off. Only ``http:``, ``https:`` and
``mailto:`` links, and same-origin ``/app/conversations/`` paths, become links; anything else stays as
text. Every link gets ``rel="noopener noreferrer nofollow"``. Images are never fetched: their alt text
is shown instead. The rendered HTML is the only value a template marks safe; attachments are never
rendered.
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markupsafe import Markup

_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.-]*):", re.IGNORECASE)
_SAFE_SCHEMES = ("http", "https", "mailto")
_SAME_ORIGIN_PREFIX = "/app/conversations/"
REL = "noopener noreferrer nofollow"


def validate_link(url: str) -> bool:
    """Called with the normalized URL (entities decoded, percent-encoded) for links and autolinks."""
    candidate = url.strip()
    # Browsers ignore ASCII tab and newline inside a scheme ("java\tscript:"); refuse any control.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        return False
    scheme = _SCHEME_RE.match(candidate)
    if scheme:
        return scheme.group(1).lower() in _SAFE_SCHEMES
    # Relative URLs: only same-origin conversation links. Never //host, \\host, /app/handoff or /app/local.
    return candidate.startswith(_SAME_ORIGIN_PREFIX) and "\\" not in candidate and "%5C" not in candidate.upper()


def _image_as_text(self, tokens, idx, options, env):
    token = tokens[idx]
    alt = self.renderInlineAsText(token.children or [], options, env)
    return escapeHtml(alt)


def _link_open(self, tokens, idx, options, env):
    tokens[idx].attrSet("rel", REL)
    return self.renderToken(tokens, idx, options, env)


def _build() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    md.validateLink = validate_link
    md.add_render_rule("image", _image_as_text)
    md.add_render_rule("link_open", _link_open)
    return md


_MD = _build()


def render(body: str) -> Markup:
    """The body as sanitized HTML, ready for a template."""
    return Markup(_MD.render(body))
