"""One process per durable Fri data directory; never unlink the lock inode."""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def instance_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / 'fri-runtime.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('another fri process already owns this data directory') from None
        yield
    finally:
        os.close(fd)
