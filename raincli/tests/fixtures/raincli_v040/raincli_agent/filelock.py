# Vendored from raincli tag v0.4.0 (commit a214082792ff64731cae1d6f12fc5cc900ae8b11), path raincli/raincli_agent/filelock.py. Test fixture for protocol 16.12 C1; do not edit.
"""Process-scoped exclusive queue locks on Unix and Windows."""
import errno
import os
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def lock(fd, blocking=True):
    if os.name != "nt":
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    while True:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            if not blocking:
                raise BlockingIOError(errno.EAGAIN, "queue already locked") from None
            time.sleep(0.05)


def unlock(fd):
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
