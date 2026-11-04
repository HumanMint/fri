"""Explicitly enabled, private development diagnostics. Never stores audio."""

from __future__ import annotations

import json
import logging
import os
import stat
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

LOG = logging.getLogger(__name__)


class DebugTimeline:
    def __init__(self, path: Path | None = None, *, clock=time.monotonic):
        self._path, self._clock = path, clock
        self._call_id = uuid.uuid4().hex
        self._started = clock()

    def record(self, event: str, **fields) -> None:
        if self._path is None:
            return
        try:
            line = json.dumps({'call': self._call_id, 'at': datetime.now(UTC).isoformat(),
                               'elapsed_s': self._clock() - self._started,
                               'event': event, **fields}, ensure_ascii=False) + '\n'
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
                         | os.O_NONBLOCK, 0o600)
            with os.fdopen(fd, 'ab') as output:
                if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
                    raise OSError
                os.fchmod(output.fileno(), 0o600)
                output.write(line.encode('utf-8'))
        except (OSError, ValueError, TypeError):
            # No path, transcript or exception payload may reach the normal log.
            LOG.warning('jpcall debug timeline unavailable')

    def close(self) -> None:
        self._path = None
