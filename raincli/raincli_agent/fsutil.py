"""Durable file helpers: atomic 0600 writes and private-file reads."""

import json
import os
import stat

from .errors import ConfigError


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
            os.mkdir(directory, mode)
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
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path, data, mode=0o600):
    """Write ``data`` to ``path`` atomically and durably.

    The data goes to a fresh temporary file in the same directory, which is
    fsynced, renamed over ``path``, and then the directory is fsynced, so a
    crash leaves either the old or the new content.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
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
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise ConfigError(f"{what} not found: {path}") from None
    except OSError as exc:
        raise ConfigError(f"cannot open {what} {path}: {exc.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError(f"{what} is not a regular file: {path}")
        if st.st_mode & 0o077:
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
