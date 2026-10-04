"""Token, password and text validation primitives shared by API and web."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

AGENT_TOKEN_PREFIX = "rca_"
INVITE_TOKEN_PREFIX = "rci_"
HANDLE_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,}$")
MESSAGE_BODY_MAX = 16000
DISPLAY_NAME_MAX = 80

_NONCHARACTERS = "﷐-﷯﻿" + "".join(
    f"{chr(plane << 16 | 0xFFFE)}{chr(plane << 16 | 0xFFFF)}" for plane in range(17)
)
_BODY_FORBIDDEN = re.compile(f"[\x00-\x08\x0b-\x1f\x7f-\x9f  \ud800-\udfff{_NONCHARACTERS}]")
_LINE_FORBIDDEN = re.compile(f"[\x00-\x1f\x7f-\x9f  \ud800-\udfff{_NONCHARACTERS}]")

_SCRYPT = {"n": 2**14, "r": 8, "p": 5, "dklen": 32}


def new_token(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    return token[:12]


def constant_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, maxmem=64 * 1024 * 1024, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def password_needs_upgrade(stored: str) -> bool:
    # Upgrade our original policy only; never silently downgrade a stronger hash.
    return stored.startswith("scrypt$16384$8$1$")


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p),
            dklen=len(digest_hex) // 2, maxmem=64 * 1024 * 1024,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def valid_handle(value: object) -> bool:
    return isinstance(value, str) and bool(HANDLE_RE.fullmatch(value))


def slugify_machine_name(name: str) -> str:
    """A computer name as a handle (protocol §15.8 L1); tests/machine_slug_vectors.json is shared with the client."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug[:1].isascii() or not slug[:1].isalpha():
        slug = "m-" + slug
    slug = slug[:32].strip("-")
    return slug if len(slug) >= 2 else "machine"


def valid_slug(value: object) -> bool:
    return isinstance(value, str) and bool(SLUG_RE.fullmatch(value))


def valid_email(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 254 and bool(EMAIL_RE.fullmatch(value))


def valid_display_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= DISPLAY_NAME_MAX
        and value.strip() != ""
        and not _LINE_FORBIDDEN.search(value)
    )


def valid_message_body(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MESSAGE_BODY_MAX
        and value.strip() != ""
        and not _BODY_FORBIDDEN.search(value)
    )


def valid_password(value: object) -> bool:
    return isinstance(value, str) and 12 <= len(value) <= 256


# Attachments (protocol §8) -------------------------------------------------------
ATTACHMENT_MAX_BYTES = 256 * 1024
ATTACHMENT_MAX_COUNT = 5
ATTACHMENT_MAX_TOTAL = 1024 * 1024
ATTACHMENT_MEDIA_TYPE = "text/markdown"
ATTACHMENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,95}\.md$")
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}


def valid_attachment_name(name: object) -> bool:
    """Safe portable Markdown filename: no paths, no leading dot, .md suffix, <=100 chars."""
    if not isinstance(name, str) or not ATTACHMENT_NAME_RE.fullmatch(name):
        return False
    stem = name[:-3].rstrip(" .")
    return bool(stem) and ".." not in name and stem.split(".")[0].lower() not in _WINDOWS_RESERVED


def attachment_content_problem(data: bytes) -> str | None:
    """None if acceptable Markdown bytes; otherwise a short reason. Bytes are stored unchanged."""
    if not 1 <= len(data) <= ATTACHMENT_MAX_BYTES:
        return f"attachment must be 1-{ATTACHMENT_MAX_BYTES} bytes"
    if b"\x00" in data:
        return "attachment contains NUL bytes; only UTF-8 Markdown text is accepted"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "attachment is not valid UTF-8 text"
    return None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
