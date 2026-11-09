"""Single-call admission and termination evidence; never stores conversation content."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import time
from pathlib import Path

from .contract import CallRejected

TERMINAL = frozenset({'completed', 'busy', 'no-answer', 'failed', 'canceled'})


class AttemptJournal:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / 'recovery.json'
        self._initialized = directory / 'recovery.initialized'
        self._lock = None

    def open(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.directory / 'recovery.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                     0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock = fd
            if not self._initialized.is_file() or self._initialized.is_symlink():
                raise ValueError
            self.read()
        except BlockingIOError:
            os.close(fd)
            self._lock = None
            raise CallRejected('Calling is already in use; stop the calling runtime first.') from None
        except BaseException:
            os.close(fd)
            self._lock = None
            raise CallRejected('Calling is unavailable: initialize or reconcile recovery state.') from None

    def close(self) -> None:
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None

    def _write(self, record: dict) -> None:
        if self._lock is None:
            raise CallRejected('Calling recovery is not locked.')
        temporary = self.directory / 'recovery.tmp'
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                         0o600)
            with os.fdopen(fd, 'w') as output:
                json.dump(record, output, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            raise CallRejected('Calling recovery could not be saved; admission remains disabled.') from None

    @staticmethod
    def _unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def read(self) -> dict:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as source:
                data = json.load(source, object_pairs_hook=self._unique)
            if set(data) != {'version', 'attempt'} or type(data['version']) is not int or data['version'] != 1:
                raise ValueError
            attempt = data['attempt']
            if attempt is not None:
                if (not isinstance(attempt, dict) or set(attempt) != {
                        'id', 'token_hash', 'sid', 'started_at', 'phase', 'evidence'}
                        or not re.fullmatch(r'[0-9a-f]{32}', attempt['id'])
                        or not re.fullmatch(r'[0-9a-f]{64}', attempt['token_hash'])
                        or not re.fullmatch(r'(?:CA[0-9a-fA-F]{32})?', attempt['sid'])
                        or type(attempt['started_at']) not in (float, int)
                        or not math.isfinite(attempt['started_at']) or attempt['started_at'] <= 0
                        or attempt['phase'] not in {'reserved', 'creating', 'uncertain', 'terminal'}
                        or attempt['evidence'] not in TERMINAL | {'', 'not_created'}
                        or (attempt['phase'] == 'terminal') != bool(attempt['evidence'])):
                    raise ValueError
            return data
        except (OSError, ValueError, TypeError, KeyError):
            raise CallRejected('Calling recovery state is missing, corrupt or incompatible.') from None

    def reserve(self, status_token: str) -> str:
        data = self.read()
        if data['attempt'] is not None and data['attempt']['phase'] != 'terminal':
            raise CallRejected('An earlier telephone attempt needs reconciliation before another call.')
        identity = secrets.token_hex(16)
        self._write({'version': 1, 'attempt': {
            'id': identity, 'token_hash': hashlib.sha256(status_token.encode()).hexdigest(),
            'sid': '', 'started_at': time.time(), 'phase': 'reserved', 'evidence': ''}})
        return identity

    def update(self, identity: str, *, sid: str = '', phase: str | None = None,
               evidence: str = '') -> None:
        data = self.read()
        attempt = data['attempt']
        if attempt is None or attempt['id'] != identity:
            raise CallRejected('Calling recovery attempt does not match.')
        if sid:
            if not re.fullmatch(r'CA[0-9a-fA-F]{32}', sid) or (attempt['sid'] and attempt['sid'] != sid):
                raise CallRejected('Calling recovery identifier does not match.')
            attempt['sid'] = sid
        if attempt['phase'] != 'terminal':
            if evidence:
                if evidence not in TERMINAL | {'not_created'}:
                    raise CallRejected('Calling recovery needs terminal evidence.')
                attempt['phase'], attempt['evidence'] = 'terminal', evidence
            elif phase is not None:
                if phase not in {'creating', 'uncertain'}:
                    raise CallRejected('Invalid calling recovery phase.')
                attempt['phase'] = phase
        self._write(data)

    def callback(self, token: str, params: dict[str, str]) -> bool:
        attempt = self.read()['attempt']
        if attempt is None or not secrets.compare_digest(
                attempt['token_hash'], hashlib.sha256(token.encode()).hexdigest()):
            return False
        status = params.get('CallStatus', '')
        self.update(attempt['id'], sid=params['CallSid'],
                    evidence=status if status in TERMINAL else '')
        return True

    def status(self) -> dict:
        attempt = self.read()['attempt']
        if attempt is None:
            return {'admission': 'ready', 'attempt': None}
        return {'admission': 'ready' if attempt['phase'] == 'terminal' else 'blocked',
                'attempt': attempt['id'], 'sid_known': bool(attempt['sid']),
                'phase': attempt['phase'], 'evidence': attempt['evidence'],
                'started_at': attempt['started_at']}

    @classmethod
    def initialize(cls, directory: Path) -> None:
        journal = cls(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(directory / 'recovery.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            journal._lock = fd
            if (journal.path.exists() or journal.path.is_symlink()
                    or journal._initialized.exists() or journal._initialized.is_symlink()):
                raise CallRejected('Recovery is already initialized; never reset an existing attempt.')
            # This one-way install fuse makes deletion of the journal fail closed.
            marker = os.open(journal._initialized, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(marker, 'w') as output:
                output.write('1\n')
                output.flush()
                os.fsync(output.fileno())
            journal._write({'version': 1, 'attempt': None})
        finally:
            journal._lock = None
            os.close(fd)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Initialize calling recovery; never resets existing state.')
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    AttemptJournal.initialize(args.directory)
