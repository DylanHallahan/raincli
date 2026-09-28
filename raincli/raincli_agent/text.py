"""Escaping of untrusted text before it reaches a terminal or an agent prompt."""

import unicodedata

# Cc: C0/C1 controls (incl. ESC), Cf: format chars (bidi overrides, zero-width),
# Zl/Zp: U+2028/2029, Cs: lone surrogates, Co: private use, Cn: unassigned.
_UNSAFE_CATEGORIES = {"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"}


def _escape_char(ch):
    cp = ord(ch)
    if cp < 0x100:
        return f"\\x{cp:02x}"
    if cp < 0x10000:
        return f"\\u{cp:04x}"
    return f"\\U{cp:08x}"


def escape_text(text, allow_newlines=True):
    """Return ``text`` with every control, escape or invisible format character
    replaced by a visible ``\\xNN``/``\\uNNNN`` escape. Newlines and tabs are
    kept when ``allow_newlines`` is true."""
    out = []
    for ch in text:
        if ch in "\n\t" and allow_newlines:
            out.append(ch)
        elif unicodedata.category(ch) in _UNSAFE_CATEGORIES:
            out.append(_escape_char(ch))
        else:
            out.append(ch)
    return "".join(out)


def escape_line(text):
    return escape_text(text, allow_newlines=False)


MAX_BODY = 16000


def _is_noncharacter(cp):
    return 0xFDD0 <= cp <= 0xFDEF or (cp & 0xFFFE) == 0xFFFE


def body_problem(body):
    """Return why ``body`` violates the protocol's body rules, or None."""
    if not isinstance(body, str) or not body:
        return "body must not be empty"
    if len(body) > MAX_BODY:
        return f"body is longer than {MAX_BODY} characters"
    if not body.strip():
        return "body must not be only whitespace"
    for ch in body:
        cp = ord(ch)
        if ch in "\n\t":
            continue
        if cp < 0x20 or 0x7F <= cp <= 0x9F:
            return f"body contains control character U+{cp:04X}"
        if cp in (0x2028, 0x2029, 0xFEFF) or 0xD800 <= cp <= 0xDFFF or _is_noncharacter(cp):
            return f"body contains disallowed character U+{cp:04X}"
    return None
