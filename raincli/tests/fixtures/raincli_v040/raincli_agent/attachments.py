# Vendored from raincli tag v0.4.0 (commit a214082792ff64731cae1d6f12fc5cc900ae8b11), path raincli/raincli_agent/attachments.py. Test fixture for protocol 16.12 C1; do not edit.
"""Markdown attachments (protocol section 8): local validation, verification
and exclusive, never-overwriting writes.

The rules mirror ``raincli_server.security`` so bad files fail before upload,
and so names or bytes from a misbehaving server are rejected on download.
"""

import base64
import errno
import hashlib
import os
import re
import stat

from .errors import EXIT_CONFLICT, RainError
from .fsutil import (fsync_dir, makedirs_durable, mkdir_private, create_private,
                     open_read_nofollow, is_link)
from .text import escape_line

MAX_BYTES = 256 * 1024
MAX_COUNT = 5
MAX_TOTAL = 1024 * 1024
MEDIA_TYPE = "text/markdown"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,95}\.md$")
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)),
                     *(f"lpt{i}" for i in range(10))}
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class AttachmentError(RainError):
    pass


class AttachmentConflict(AttachmentError):
    """An existing local file differs from the attachment. It is left untouched."""

    exit_code = EXIT_CONFLICT


def valid_name(name):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return False
    stem = name[:-3].rstrip(" .")
    return bool(stem) and ".." not in name and stem.split(".")[0].lower() not in _WINDOWS_RESERVED


def content_problem(data):
    if not 1 <= len(data) <= MAX_BYTES:
        return f"attachment must be 1-{MAX_BYTES} bytes"
    if b"\x00" in data:
        return "attachment contains NUL bytes; only UTF-8 Markdown text is accepted"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "attachment is not valid UTF-8 text"
    return None


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    return escape_line(str(name))[:100]


def load_for_send(paths):
    """Read each path's exact bytes and return the ``attachments`` send payload."""
    if len(paths) > MAX_COUNT:
        raise AttachmentError(f"at most {MAX_COUNT} attachments per message")
    payload, seen, total = [], set(), 0
    for path in paths:
        name = os.path.basename(path)
        if not valid_name(name):
            raise AttachmentError(
                f"attachment name {safe_name(name)!r} is not allowed: use a plain .md filename "
                "(letters, digits, space, . _ -; no leading dot, no '..')")
        if name.lower() in seen:
            raise AttachmentError(f"duplicate attachment name {safe_name(name)!r} (names are case-insensitive)")
        seen.add(name.lower())
        try:
            # O_NOFOLLOW: a symlinked source (say report.md -> ~/.ssh/id_ed25519)
            # is refused instead of silently uploading its target.
            fd = open_read_nofollow(path)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise AttachmentError(
                    f"attachment {safe_name(name)!r} is a symlink; refusing it "
                    "(attach the real file, not a link)") from None
            raise AttachmentError(f"cannot read attachment {safe_name(name)!r}: {exc.strerror}") from None
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise AttachmentError(f"attachment {safe_name(name)!r} is not a regular file")
            data = fh.read(MAX_BYTES + 1)
        problem = content_problem(data)
        if problem:
            raise AttachmentError(f"{safe_name(name)}: {problem}")
        total += len(data)
        if total > MAX_TOTAL:
            raise AttachmentError(f"attachments exceed {MAX_TOTAL} bytes in total")
        payload.append({"filename": name, "content_b64": base64.b64encode(data).decode("ascii"),
                        "sha256": sha256_hex(data)})
    return payload


def check_metadata(meta):
    """Validate attachment metadata from the server before using it for a path."""
    name = meta.get("filename") if isinstance(meta, dict) else None
    if not valid_name(name):
        raise AttachmentError(f"server sent an unsafe attachment name {safe_name(name)!r}; refusing it")
    if not isinstance(meta.get("sha256"), str) or not SHA_RE.match(meta["sha256"]):
        raise AttachmentError(f"server sent a bad sha256 for {safe_name(name)!r}")
    size = meta.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_BYTES:
        raise AttachmentError(f"server sent a bad size for {safe_name(name)!r}")
    return name


def verify_download(meta, data, header_sha=None):
    name = meta["filename"]
    if len(data) != meta["size"]:
        raise AttachmentError(f"{safe_name(name)}: size {len(data)} does not match {meta['size']}")
    digest = sha256_hex(data)
    if digest != meta["sha256"] or (header_sha and header_sha.lower() != digest):
        raise AttachmentError(f"{safe_name(name)}: sha256 does not match; refusing the download")
    problem = content_problem(data)
    if problem:
        raise AttachmentError(f"{safe_name(name)}: {problem}")


def prepare_dir(base, *components):
    """Return ``base/components...``, creating what is missing (0700, parents fsynced).

    ``base`` is trusted (section 12.4): the connector's realpath'd state_dir,
    or fetch's user-chosen --dir or cwd. It may sit behind symlinks and is
    created if needed. Every component *below* it is ours, so each one must be
    a real directory, never a symlink; creation happens one level at a time so
    nothing is ever created behind a link."""
    current = os.path.realpath(base)
    makedirs_durable(current, 0o700)
    for name in components:
        if name in ("", ".", "..") or os.sep in name or (os.altsep and os.altsep in name):
            raise AttachmentError(f"refusing directory component {escape_line(name)!r}")
        path = os.path.join(current, name)
        try:
            mkdir_private(path, 0o700)
            fsync_dir(current)
        except FileExistsError:
            pass
        st = os.lstat(path)
        if is_link(st) or not stat.S_ISDIR(st.st_mode):
            raise AttachmentError(f"{escape_line(path)} is a symlink or not a directory; refusing it")
        current = path
    return current


def existing_matches(target, sha):
    """True if ``target`` is a regular file with this sha256, None if absent.

    Raises AttachmentConflict if something else is there."""
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return None
    if is_link(st) or not stat.S_ISREG(st.st_mode):
        raise AttachmentConflict(f"{escape_line(target)} exists and is not a regular file; left untouched")
    with os.fdopen(open_read_nofollow(target), "rb") as fh:
        if sha256_hex(fh.read()) == sha:
            return True
    raise AttachmentConflict(f"{escape_line(target)} exists with different content; left untouched")


def write_exclusive(directory, name, data, sha, mode=0o600):
    """Write ``data`` as ``directory/name`` without ever overwriting.

    Returns "saved" or "present" (identical file already there). The bytes
    go to a fsynced temp file that is then hard-linked into place, so the
    target appears complete or not at all."""
    target = os.path.join(directory, name)
    if existing_matches(target, sha):
        return "present"
    tmp = os.path.join(directory, f".{name}.{os.getpid()}.{os.urandom(4).hex()}.part")
    fd = create_private(tmp, mode)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        for _attempt in range(3):
            try:
                os.link(tmp, target)
                break
            except FileExistsError:
                if existing_matches(target, sha):
                    return "present"
                # The competing file vanished between link() and the check: retry.
            except OSError as exc:
                if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EXDEV):
                    raise
                raise AttachmentError(f"cannot create {escape_line(target)}: {exc.strerror}") from None
        else:
            raise AttachmentError(f"could not create {escape_line(target)}; nothing was written")
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    fsync_dir(directory)
    return "saved"
