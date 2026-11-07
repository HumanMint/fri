import hashlib
import json

import pytest

from fri.jpcall.contract import CallRejected
from fri.jpcall.recovery import AttemptJournal

SID = 'CA' + '5' * 32
TOKEN = 'synthetic-callback-token'


def ready(directory):
    AttemptJournal.initialize(directory)
    journal = AttemptJournal(directory)
    journal.open()
    return journal


def test_explicit_initialization_and_missing_state_fail_closed(tmp_path):
    journal = AttemptJournal(tmp_path)
    with pytest.raises(CallRejected):
        journal.open()
    journal = ready(tmp_path)
    try:
        assert journal.status()['admission'] == 'ready'
        with pytest.raises((CallRejected, BlockingIOError)):
            AttemptJournal.initialize(tmp_path)
        journal.path.unlink()
        with pytest.raises(CallRejected):
            journal.status()
    finally:
        journal.close()
    with pytest.raises(CallRejected):
        AttemptJournal.initialize(tmp_path)


def test_unresolved_attempt_survives_restart_and_cannot_be_redialed(tmp_path):
    journal = ready(tmp_path)
    identity = journal.reserve(TOKEN)
    journal.update(identity, phase='creating')
    journal.close()
    restarted = AttemptJournal(tmp_path)
    restarted.open()
    try:
        assert restarted.status()['admission'] == 'blocked'
        with pytest.raises(CallRejected):
            restarted.reserve('another-token')
        assert not restarted.callback('wrong-token', {'CallSid': SID, 'CallStatus': 'completed'})
        assert restarted.callback(TOKEN, {'CallSid': SID, 'CallStatus': 'ringing'})
        assert restarted.status()['admission'] == 'blocked'
        assert restarted.callback(TOKEN, {'CallSid': SID, 'CallStatus': 'completed'})
        assert restarted.status()['admission'] == 'ready'
        # A late progress callback cannot reopen terminal recovery.
        restarted.callback(TOKEN, {'CallSid': SID, 'CallStatus': 'in-progress'})
        assert restarted.status()['evidence'] == 'completed'
        assert restarted.reserve('next-token') != identity
    finally:
        restarted.close()


def test_exclusive_owner_and_content_free_record(tmp_path):
    journal = ready(tmp_path)
    other = AttemptJournal(tmp_path)
    try:
        with pytest.raises(CallRejected):
            other.open()
        identity = journal.reserve(TOKEN)
        journal.update(identity, sid=SID, phase='uncertain')
        data = json.loads(journal.path.read_text())
        assert data['attempt']['token_hash'] == hashlib.sha256(TOKEN.encode()).hexdigest()
        assert TOKEN not in journal.path.read_text()
        assert set(data['attempt']) == {
            'id', 'token_hash', 'sid', 'started_at', 'phase', 'evidence'}
        with pytest.raises(CallRejected):
            journal.update(identity, sid='CA' + '6' * 32)
        with pytest.raises(CallRejected):
            journal.update('other-attempt', evidence='completed')
    finally:
        journal.close()


@pytest.mark.parametrize('content', [
    '{', '{"version":2,"attempt":null}',
    '{"version":1,"version":1,"attempt":null}',
    '{"version":true,"attempt":null}',
    '{"version":1,"attempt":{"phase":"terminal"}}',
])
def test_corrupt_or_unknown_state_never_initializes_itself(tmp_path, content):
    journal = ready(tmp_path)
    journal.close()
    journal.path.write_text(content)
    with pytest.raises(CallRejected):
        AttemptJournal(tmp_path).open()
    assert journal.path.read_text() == content


def test_not_created_is_explicit_terminal_evidence_and_symlinks_are_rejected(tmp_path):
    journal = ready(tmp_path)
    try:
        identity = journal.reserve(TOKEN)
        journal.update(identity, evidence='not_created')
        assert journal.status()['admission'] == 'ready'
    finally:
        journal.close()
    destination = tmp_path / 'elsewhere'
    journal.path.rename(destination)
    journal.path.symlink_to(destination)
    with pytest.raises(CallRejected):
        AttemptJournal(tmp_path).open()
