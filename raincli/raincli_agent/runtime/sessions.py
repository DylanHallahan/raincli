"""Hook session records and next-turn inbox files (protocol 14.3, 14.4 and 14.7).

Everything here is local to the machine. The runtime creates ``<state_dir>/
machine-salt`` and ``sessions/``; ``raincli hook`` writes one private record per
agent session there and, for the mapped next-turn inbox, claims framed message
files from ``sessions/<key>.inbox/``. Nothing here touches the network.

Next-turn handover, per message id ``M`` in ``sessions/<key>.inbox/``:

* ``M.md``          written by the connector (state ``handed_over``);
* ``M.md.claimed``  renamed by the hook when it emits the file (mtime = claim time);
* ``M.md.receipt``  written by the hook once the text is on its stdout.

A rename is the arbitration point: the hook claims ``M.md`` and the connector
reclaims it with the same kind of rename, so exactly one of them wins.
"""
import hmac
import hashlib
import json
import os
import re
import stat
import time
import unicodedata

from ..errors import ConfigError
from ..fsutil import atomic_write_bytes, atomic_write_json, create_private, mkdir_private, open_read_nofollow, read_private_file

SALT = "machine-salt"
SESSIONS = "sessions"
TYPES = ("claude", "codex", "gemini", "cursor", "opencode", "other")
HOOK_TYPES = ("claude", "codex")
# Records without a determinable process (14.9): offline after 10 minutes without
# an event, dropped after an hour. A record whose local process is alive stays
# live however long it is idle; one whose process has exited is gone.
STALE_AFTER = 600
DROP_AFTER = 3600
KEY_RE = re.compile(r"^[0-9a-f]{32}$")
RECORD_RE = re.compile(r"^[0-9a-f]{32}\.json$")
INBOX_FILE_RE = re.compile(r"^[0-9a-f-]{36}\.md$")
# Per-turn claim bound (14.7 M6): 32 KiB, and Claude Code 2.1.283 keeps
# additionalContext inline only up to 10,000 characters (it persists anything
# longer to a file and shows a preview), so the lower bound applies too.
CLAIM_CAP_BYTES = 32 * 1024
CLAIM_CAP_CHARS = 10000
SEPARATOR = "\n\n"
# A claimed file still without a receipt this long after the connector first saw
# it claimed is submission_uncertain. The hook exits within about 2 s, so by then
# it has finished (or died); counting from the connector's own observation keeps
# this independent of file timestamps (review 1, finding 8).
CLAIM_GRACE = 30


def context_chars(text):
    """Length as Claude Code (JavaScript) counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def fits_one_turn(text):
    return len(text.encode("utf-8")) <= CLAIM_CAP_BYTES and context_chars(text) <= CLAIM_CAP_CHARS


# -- salt and keys -------------------------------------------------------------

def ensure_salt(state_dir):
    """Create the per-machine salt once (32 random bytes, 0600) and return it."""
    path = os.path.join(state_dir, SALT)
    try:
        fd = create_private(path, 0o600)
    except FileExistsError:
        pass
    else:
        try:
            os.write(fd, os.urandom(32))
            os.fsync(fd)
        finally:
            os.close(fd)
    salt = read_private_file(path, "machine salt")
    if len(salt) != 32:
        raise ConfigError(f"machine salt {path} is corrupt")
    return salt


def read_salt(state_dir):
    """The salt, or None when the runtime has not created it (the hook then does nothing)."""
    try:
        salt = read_private_file(os.path.join(state_dir, SALT), "machine salt")
    except ConfigError:
        return None
    return salt if len(salt) == 32 else None


def agent_key(salt, source_id):
    """Lowercase hex HMAC-SHA256(salt, source_id), truncated to 32 characters (14.7 H3)."""
    return hmac.new(salt, source_id.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


# -- names -------------------------------------------------------------------

_UNSAFE = {"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"}
# A credential-shaped string never leaves the machine (the server rejects it too).
TOKEN_RE = re.compile(r"rc[ai]_[A-Za-z0-9_-]{20,}")


def normalize_name(name, fallback):
    """A display-name-safe name (14.7 H4): forbidden characters replaced, trimmed,
    at most 64 code points, and the type when nothing is left."""
    if not isinstance(name, str):
        name = ""
    cleaned = "".join("_" if unicodedata.category(ch) in _UNSAFE or ch in "  " else ch for ch in name)
    cleaned = TOKEN_RE.sub("[redacted]", cleaned)
    cleaned = " ".join(cleaned.split())[:64].strip()
    return cleaned or fallback


def basename(path):
    """Only the last component of a directory path is ever kept."""
    if not isinstance(path, str):
        return ""
    return os.path.basename(path.rstrip("/\\").replace("\\", "/"))


# -- private directories ------------------------------------------------------

def check_private_dir(path):
    """A real directory (no symlink), owned by us, mode 0700 (14.7 M7)."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ConfigError(f"{path} is not a plain directory")
    if os.name != "nt":
        if st.st_uid != os.getuid():
            raise ConfigError(f"{path} is not owned by the current user")
        if st.st_mode & 0o077:
            raise ConfigError(f"{path} is accessible by group or others")


def private_dir(path, create):
    try:
        check_private_dir(path)
        return path
    except FileNotFoundError:
        if not create:
            return None
    try:
        mkdir_private(path, 0o700)
    except FileExistsError:
        pass
    check_private_dir(path)
    return path


def sessions_dir(state_dir, create=False):
    return private_dir(os.path.join(state_dir, SESSIONS), create)


def inbox_dir(state_dir, key, create=False):
    if not KEY_RE.fullmatch(key):
        raise ConfigError("invalid session key")
    base = sessions_dir(state_dir, create)
    if base is None:
        return None
    return private_dir(os.path.join(base, key + ".inbox"), create)


# -- session records -------------------------------------------------------------

def record_path(state_dir, key):
    return os.path.join(state_dir, SESSIONS, key + ".json")


def write_record(state_dir, record):
    atomic_write_json(record_path(state_dir, record["key"]), record)


def load_record(state_dir, key):
    try:
        data = json.loads(read_private_file(record_path(state_dir, key), "session record"))
    except (ConfigError, ValueError, UnicodeDecodeError):
        return None
    return data if valid_record(data) else None


def valid_record(data):
    return (isinstance(data, dict) and isinstance(data.get("key"), str) and KEY_RE.fullmatch(data["key"])
            and data.get("type") in TYPES and isinstance(data.get("name"), str)
            and data.get("status") in ("working", "idle", "blocked", "unknown")
            and isinstance(data.get("updated_at"), (int, float)) and not isinstance(data.get("updated_at"), bool))


def process_start(pid):
    """The kernel start time of ``pid`` (Linux), to tell a live process from a reused pid."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            return int(fh.read().rsplit(b")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def process_state(record):
    """``alive``, ``dead``, or None when the record names no determinable process."""
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or os.name == "nt":
        return None
    if record.get("pid_start") is not None and os.path.isdir("/proc"):
        return "alive" if process_start(pid) == record["pid_start"] else "dead"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "alive"
    except OSError:
        return None
    return "alive"


def remove_record(state_dir, key):
    try:
        os.unlink(record_path(state_dir, key))
    except FileNotFoundError:
        pass


def read_sessions(state_dir, now=None, drop=True):
    """Every hook session record, with ``status`` offline once stale.

    Records over an hour old are deleted when ``drop`` is true (the runtime)."""
    now = time.time() if now is None else now
    try:
        base = sessions_dir(state_dir)
    except (OSError, ConfigError):
        return []
    if base is None:
        return []
    out = []
    for entry in os.scandir(base):
        if not RECORD_RE.fullmatch(entry.name) or not entry.is_file(follow_symlinks=False):
            continue
        record = load_record(state_dir, entry.name[:-5])
        if record is None or record["key"] != entry.name[:-5]:
            continue
        liveness = process_state(record)
        age = now - record["updated_at"]
        if liveness == "dead" or (liveness is None and age > DROP_AFTER):
            if drop:
                remove_record(state_dir, record["key"])
            continue
        if liveness is None and age > STALE_AFTER:
            record["status"] = "offline"
        out.append(record)
    out.sort(key=lambda r: (r["type"], r["name"], r["key"]))
    return out


def live_sessions(state_dir, agent_type, name, now=None):
    """The live hook sessions of this type and name: process alive, or (with no
    determinable process) an event within the last 10 minutes."""
    return [r for r in read_sessions(state_dir, now, drop=False)
            if r["type"] == agent_type and r["name"] == name and r["status"] != "offline"]


# -- next-turn inbox: connector side ----------------------------------------------

def _inbox_file(state_dir, key, message_id, suffix=""):
    return os.path.join(state_dir, SESSIONS, key + ".inbox", message_id + ".md" + suffix)


def _unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def hand_over(state_dir, key, message_id, text):
    """Write the framed prompt for the hook to emit on the session's next turn."""
    inbox_dir(state_dir, key, create=True)
    for suffix in (".claimed", ".receipt", ".reclaimed"):  # left by an earlier, settled attempt
        _unlink(_inbox_file(state_dir, key, message_id, suffix))
    atomic_write_bytes(_inbox_file(state_dir, key, message_id), text.encode("utf-8"))


def handover_state(state_dir, key, message_id):
    """``pending``, ``claimed`` (no receipt yet), ``submitted`` or ``missing``."""
    if os.path.lexists(_inbox_file(state_dir, key, message_id)):
        return "pending"
    if os.path.lexists(_inbox_file(state_dir, key, message_id, ".receipt")):
        return "submitted"
    if os.path.lexists(_inbox_file(state_dir, key, message_id, ".claimed")):
        return "claimed"
    return "missing"


def reclaim(state_dir, key, message_id):
    """Take a pending file back (the session ended or went stale). True if the
    connector won the race against a claim; False if the hook claimed it first."""
    pending = _inbox_file(state_dir, key, message_id)
    taken = _inbox_file(state_dir, key, message_id, ".reclaimed")
    try:
        os.rename(pending, taken)
    except FileNotFoundError:
        return False
    _unlink(taken)
    return True


def settle(state_dir, key, message_id):
    """Remove the handover files once the local record is final."""
    for suffix in ("", ".claimed", ".receipt", ".reclaimed"):
        _unlink(_inbox_file(state_dir, key, message_id, suffix))
    try:
        os.rmdir(os.path.join(state_dir, SESSIONS, key + ".inbox"))
    except OSError:
        pass  # not empty, or already gone


# -- next-turn inbox: hook side ---------------------------------------------------

def _read_regular(path, limit):
    fd = open_read_nofollow(path)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        chunks, total = [], 0
        while total <= limit:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        return data if len(data) <= limit else None
    finally:
        os.close(fd)


def claim(state_dir, key):
    """Claim pending framed messages, oldest first, within the per-turn bound.

    Returns ``(texts, ids)``. The caller emits the texts and then calls
    ``write_receipts``. Only regular ``<uuid>.md`` files are considered."""
    directory = inbox_dir(state_dir, key)
    if directory is None:
        return [], []
    entries = []
    for entry in os.scandir(directory):
        if INBOX_FILE_RE.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
            try:
                st = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            entries.append((st.st_mtime_ns, entry.name, st.st_size))
    entries.sort()
    texts, ids, total_bytes, total_chars = [], [], 0, 0
    for _mtime, name, size in entries:
        if size > CLAIM_CAP_BYTES:
            continue  # never emitted; the connector holds such messages too_large_for_hook
        if total_bytes + size > CLAIM_CAP_BYTES:
            break
        pending = os.path.join(directory, name)
        claimed = pending + ".claimed"
        try:
            os.rename(pending, claimed)
        except FileNotFoundError:
            continue  # reclaimed by the connector
        try:
            data = _read_regular(claimed, CLAIM_CAP_BYTES)
        except FileNotFoundError:
            continue  # settled by the connector after its grace period: not ours any more
        try:
            text = data.decode("utf-8") if data is not None else None
        except UnicodeDecodeError:
            text = None
        extra = context_chars(text) + (context_chars(SEPARATOR) if texts else 0) if text is not None else 0
        if text is None or total_chars + extra > CLAIM_CAP_CHARS:
            try:
                os.rename(claimed, pending)  # not this turn; nothing was emitted
            except FileNotFoundError:
                pass
            if text is None:
                continue
            break
        texts.append(text)
        ids.append(name[:-3])
        total_bytes += size
        total_chars += extra
    return texts, ids


def write_receipts(state_dir, key, ids):
    for message_id in ids:
        if os.path.lexists(_inbox_file(state_dir, key, message_id, ".claimed")):
            atomic_write_json(_inbox_file(state_dir, key, message_id, ".receipt"), {"claimed_at": time.time()})
