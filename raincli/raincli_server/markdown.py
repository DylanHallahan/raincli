"""Message bodies as Markdown (protocol §16.9, §16.12 C3, C13).

CommonMark through ``markdown-it-py`` with raw HTML off and linkify off. Only ``http:``, ``https:`` and
``mailto:`` links, and same-origin ``/app/conversations/`` paths, become links; anything else stays as
text. Every link gets ``rel="noopener noreferrer nofollow"``. Images are never fetched: their alt text
is shown instead. The rendered HTML is the only value a template marks safe; attachments are never
rendered.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markupsafe import Markup

_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.-]*):", re.IGNORECASE)
_SAFE_SCHEMES = ("http", "https", "mailto")
_SAME_ORIGIN_PREFIX = "/app/conversations/"
# The secret-carrying and app-only paths never become links (§16.12 C3, §16.14 S2), under any root path and,
# since the service may have other names, on any host.
_EXCLUDED_PATH = re.compile(r"(?:^|/)app/(?:handoff|local)(?:/|$)", re.IGNORECASE)
REL = "noopener noreferrer nofollow"


def _fully_decoded(text: str) -> str | None:
    """Percent-decoded until stable (double and triple encodings included); None if it never settles."""
    for _ in range(5):
        decoded = unquote(text)
        if decoded == text:
            return text
        text = decoded
    return None


def _resolved_path(path: str) -> str | None:
    """The decoded path with ``.`` segments resolved, or None when it has a ``..`` segment in any encoding
    or a control character. Backslashes count as slashes, as browsers treat them in http(s) URLs."""
    decoded = _fully_decoded(path)
    if decoded is None or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in decoded):
        return None
    segments = decoded.replace("\\", "/").split("/")
    if any(segment.strip() == ".." for segment in segments):
        return None
    return "/".join(segment for segment in segments if segment != ".")


def validate_link(url: str) -> bool:
    """Called with the normalized URL (entities decoded, percent-encoded) for links and autolinks."""
    candidate = url.strip()
    # Browsers ignore ASCII tab and newline inside a scheme ("java\tscript:"); refuse any control.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        return False
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return False
    scheme = _SCHEME_RE.match(candidate)
    if scheme:
        name = scheme.group(1).lower()
        if name not in _SAFE_SCHEMES:
            return False
        if name == "mailto":
            return True
        path = _resolved_path(parts.path)
        return path is not None and not _EXCLUDED_PATH.search(path)
    # Relative URLs: only same-origin conversation links. Never //host, \\host, /app/handoff or /app/local,
    # and never a path that leaves /app/conversations/ once decoded and resolved.
    if not candidate.startswith(_SAME_ORIGIN_PREFIX) or "\\" in candidate or parts.netloc:
        return False
    path = _resolved_path(parts.path)
    return path is not None and path.startswith(_SAME_ORIGIN_PREFIX) and not _EXCLUDED_PATH.search(path)


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
