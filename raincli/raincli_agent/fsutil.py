"""Atomic private-file helpers: POSIX modes/fsync and Windows ACLs.

Windows flushes file contents but has no POSIX directory-fsync guarantee;
see docs/windows-client.md for the supported storage and recovery boundary.
"""

import json
import os
import stat

from .errors import ConfigError


def is_link(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def mkdir_private(path, mode=0o700):
    if os.name == "nt":
        from . import _winfiles
        return _winfiles.mkdir_private(path, mode)
    return os.mkdir(path, mode)


def create_private(path, mode=0o600):
    if os.name == "nt":
        from . import _winfiles
        return _winfiles.create_private(path, mode)
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)


def open_read_nofollow(path):
    if os.name == "nt":
        from . import _winfiles
        return _winfiles.open_read(path)
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))


def makedirs_durable(path, mode=0o700):
    """``os.makedirs`` that fsyncs the parent of every directory it creates,
    so a new directory entry survives a crash. Returns the created paths."""
    path = os.path.abspath(path)
    missing = []
    probe = path
    while not os.path.isdir(probe):
        missing.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    created = []
    for directory in reversed(missing):
        try:
            mkdir_private(directory, mode)
        except FileExistsError:
            if not os.path.isdir(directory):
                raise
            continue
        fsync_dir(os.path.dirname(directory))
        created.append(directory)
    return created


def ensure_private_dir(path):
    makedirs_durable(path, 0o700)


def fsync_dir(path):
    # Windows has no supported directory fsync. Files are flushed separately.
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path, data, mode=0o600):
    """Write ``data`` to ``path`` atomically and durably.

    The data goes to a fresh temporary file in the same directory, which is
    fsynced, renamed over ``path``, and then the directory is fsynced, so a
    crash leaves either the old or the new content on POSIX. Windows uses
    write-through replacement, without a directory-fsync guarantee.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    fd = create_private(tmp, mode)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        if os.name == "nt":
            from . import _winfiles
            _winfiles.replace(tmp, path)
        else:
            os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    fsync_dir(directory)


def atomic_write_json(path, obj, mode=0o600):
    atomic_write_bytes(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode(), mode)


def read_private_file(path, what="file"):
    """Read a file that must be owned by us and not accessible to group/others."""
    try:
        fd = open_read_nofollow(path)
    except FileNotFoundError:
        raise ConfigError(f"{what} not found: {path}") from None
    except OSError as exc:
        raise ConfigError(f"cannot open {what} {path}: {exc.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError(f"{what} is not a regular file: {path}")
        if os.name == "nt":
            from . import _winfiles
            try:
                _winfiles.check_private(fd)
            except OSError as exc:
                raise ConfigError(f"cannot trust {what}: {exc}") from None
        elif st.st_mode & 0o077:
            raise ConfigError(
                f"{what} {path} is accessible by group or others "
                f"(mode {stat.S_IMODE(st.st_mode):04o}); run: chmod 600 {path}")
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise ConfigError(f"{what} {path} is not owned by the current user")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)
