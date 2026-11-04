"""A withdrawals record the disk fails to read (fri #317): saved context is held back only until it reads again.

#250 closed every saved context for 90 days after any error reading the record. An error that says nothing about the
record (EIO, EACCES and the like) now leaves it where it is: every saved context stays held back while it can't be
read, withdrawals queued meanwhile go to a journal beside it, and once it reads, the two are merged toward holding back
more, never less, written back to the record, and only then is saved context opened again and the record's own
withdrawals sent. Never longer than before: 90 days from the first failure, across restarts. A record that reads but
can't be trusted, an error of any other kind, and a journal that can't be read close as before. #313's two blockers
are regression tests here.
"""
import asyncio
import errno
import json
import time

import pytest

from compat_tests.test_fri_saved_withdrawal import edit, edited
from compat_tests.test_fri_turn_withdrawal import MARKER, WITHDRAW, saved, settled, world
from compat_tests.test_fri_withdrawal_rollback import (CLOSED, SOURCES, calls, history, kept_pending, module_clock,
                                                       next_request, pending_on_disk)
from compat_tests.test_fri_withheld_history import delete, earlier_exchange, on_disk

OTHER = 'other-synthetic-marker'  # What an exchange withdrawn during the outage holds.
RECORD = 'withdrawals.json'
JOURNAL = 'withdrawals.recovering.json'
DAY = 86400


def disk_fails(monkeypatch, path, error=errno.EIO):
    """Reading `path` fails as a disk error would, until state['down'] is False; state['tries'] counts the reads.
    Writing it, and everything else, works."""
    import pathlib
    state, real = {'down': True, 'tries': 0}, pathlib.Path.open
    def opening(self, *args, **kwargs):
        mode = args[0] if args else kwargs.get('mode', 'r')
        if self == path and state['down'] and not {'w', 'a', 'x'} & set(mode):
            state['tries'] += 1
            raise OSError(error, 'injected')
        return real(self, *args, **kwargs)
    monkeypatch.setattr(pathlib.Path, 'open', opening)
    return state


def recovering(monkeypatch):
    """The record is tried again at the next chance, not 30 seconds on."""
    import fri.fri_turn_withdrawal as withdrawal
    monkeypatch.setattr(withdrawal, 'RECOVER_SECONDS', 0)


def raw(tmp_path, name=RECORD):
    """A file's text, read past any injected failure (as an older Fri, or a person, would read it)."""
    with open(tmp_path/name) as stream:
        return stream.read()


async def outage_start(w, tmp_path, monkeypatch):
    """A withdrawal left on disk that the controller won't take, another saved exchange, and a start whose read of the
    record fails. Returns the earlier message, the outage's state and the new queue."""
    from fri.fri_turn_withdrawal import SavedTurns
    earlier = await kept_pending(w, tmp_path, monkeypatch)
    await w.agent.handle(w.said('fri and lunch?'))  # Another saved exchange, nothing withdrawn.
    outage = disk_fails(monkeypatch, tmp_path/RECORD)
    turns = w.agent.saved_turns = SavedTurns(w.agent)
    assert turns.closed()
    return earlier, outage, turns


async def withdrawn_meanwhile(w):
    """An exchange saved and its message deleted while the record can't be read; the controller still times out."""
    more = await earlier_exchange(w, 'fri explain '+OTHER)
    await delete(w, more)
    assert str(more.id) in w.agent.saved_turns.pending
    return more


def test_a_record_the_disk_failed_to_read_holds_back_only_until_it_reads_again(monkeypatch, tmp_path):
    """The issue: one read error at start-up closed every saved context for 90 days. Now it's held back while the record
    can't be read, and once it reads, only what it holds stays held back. The record is never moved or replaced."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            text = raw(tmp_path)
            assert MARKER not in await next_request(w) and history(w) == 0  # Held back while it can't be read.
            assert raw(tmp_path) == text and not list(tmp_path.glob('withdrawals.unreadable-*.json'))
            outage['down'] = False
            recovering(monkeypatch)
            request = await next_request(w)
            assert MARKER not in request and history(w) > 0  # Open again; what the record holds is still held back.
            assert str(earlier.id) in turns.pending and not turns.closed()
            assert set(json.loads(on_disk(tmp_path))['pending']) == set(json.loads(text)['pending'])
            assert not (tmp_path/JOURNAL).exists() and not (tmp_path/CLOSED).exists()
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('restart', ['once the record reads', 'while it still cant be read'])
def test_the_313_repro_a_withdrawal_made_while_the_record_cant_be_read_outlasts_a_restart(monkeypatch, tmp_path,
                                                                                         restart):
    """#313's first blocker: with the record unread, queue writes were dropped, so after a restart a withdrawal made
    meanwhile was gone, and its exchange reached the next request. It's kept in the journal beside the record."""
    async def scenario():
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            earlier, outage, _ = await outage_start(w, tmp_path, monkeypatch)
            more = await withdrawn_meanwhile(w)
            if restart == 'once the record reads':
                outage['down'] = False
            turns = w.agent.saved_turns = SavedTurns(w.agent)  # Restarted.
            recovering(monkeypatch)
            request = await next_request(w)
            assert OTHER not in request and MARKER not in request
            if restart == 'while it still cant be read':
                assert history(w) == 0 and str(more.id) in turns.pending
                outage['down'] = False
                request = await next_request(w)
                assert OTHER not in request and MARKER not in request
            assert history(w) > 0 and {str(earlier.id), str(more.id)} <= set(turns.pending)
            assert {str(earlier.id), str(more.id)} <= set(json.loads(on_disk(tmp_path))['pending'])
            assert not (tmp_path/JOURNAL).exists()
        finally:
            await w.fri.close()
    asyncio.run(scenario())


EVENTS = ['a late edit, by ID', 'a late edit of an exchange saved here', 'its raw delete, by ID']


@pytest.mark.parametrize('event', EVENTS)
def test_the_313_repro_what_the_record_holds_is_never_weakened_by_what_came_meanwhile(monkeypatch, tmp_path, event):
    """#313's second blocker: recovery skipped the record's entry whenever the same message was queued meanwhile, losing
    where it was saved, its revision, that an earlier process queued it and that it was a deletion; a late edit even
    turned the deletion into an edit. The merged entry keeps all of that, the later revision, and the later time it was
    queued, and it's on disk before saved context opens."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            disk = json.loads(raw(tmp_path))['pending'][str(earlier.id)]
            assert disk['deleted'] is True and 'scope' in disk and type(disk['revision']) is int
            revision = disk['revision']
            if event == 'a late edit of an exchange saved here':
                revision += 1  # Saved here since, under a later revision.
                turns.kept({**disk['scope'], 'revision': revision,
                            'source': {'kind': 'discord_message', 'id': str(earlier.id)}},
                           {'ok': True}, (time.time(), time.monotonic()))
            if event == 'its raw delete, by ID':
                await delete(w, earlier, guild=123)
            else:
                await edit(w, edited(earlier))
            meanwhile = dict(turns.pending.get(str(earlier.id)) or {})
            outage['down'] = False
            recovering(monkeypatch)
            assert not turns.closed()
            entry = turns.pending[str(earlier.id)]
            assert entry['scope'] == disk['scope'] and entry['deleted'] is True and entry.get('restored') is True
            assert entry['revision'] == revision
            assert entry['queued_at'] >= max(disk['queued_at'], meanwhile.get('queued_at', 0))
            kept = json.loads(on_disk(tmp_path))['pending'][str(earlier.id)]
            assert (kept['scope'], kept['deleted'], kept['revision']) == (disk['scope'], True, revision)
            assert turns.withheld([earlier.id]) == {str(earlier.id)}
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_hold_on_disk_stays_one_whatever_came_by_id_meanwhile(monkeypatch, tmp_path):
    """A hold in the record (never sent, held as long as what it names can be kept) met by a withdrawal by ID queued
    while the record couldn't be read: it stays a hold, as it would had the record been read first."""
    async def scenario():
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            held_back = w.said('said and taken back')
            (tmp_path/RECORD).write_text(json.dumps({'pending': {str(held_back.id): {'hold': True,
                                                                                     'queued_at': int(time.time())}}}))
            outage = disk_fails(monkeypatch, tmp_path/RECORD)
            turns = w.agent.saved_turns = SavedTurns(w.agent)
            assert turns.closed()
            await delete(w, held_back, guild=123)
            outage['down'] = False
            recovering(monkeypatch)
            assert not turns.closed() and turns.pending[str(held_back.id)].get('hold') is True
            assert json.loads(on_disk(tmp_path))['pending'][str(held_back.id)].get('hold') is True
            w.agent.saved_turns = SavedTurns(w.agent)
            assert w.agent.saved_turns.withheld([held_back.id]) == {str(held_back.id)}
        finally:
            await w.fri.close()
    asyncio.run(scenario())


class Crash(BaseException):
    """The process dies here."""


STEPS = ['before the record is written', 'after the record, before the closed marker',
         'after the closed marker, before the journal goes']


def crash_at(monkeypatch, step):
    """The process dies once, at this step of a recovery (see SavedTurns.settle)."""
    import pathlib

    import fri.fri_turn_withdrawal as withdrawal
    left = [1]
    if step == 'before the record is written':
        real = withdrawal.private_json
        def writing(path, data):
            if path.name == RECORD and left[0]:
                left[0] -= 1
                raise Crash()
            return real(path, data)
        monkeypatch.setattr(withdrawal, 'private_json', writing)
        return
    name = CLOSED if 'before the closed marker' in step else JOURNAL
    real_unlink = pathlib.Path.unlink
    def unlinking(self, *args, **kwargs):
        if self.name == name and left[0]:
            left[0] -= 1
            raise Crash()
        return real_unlink(self, *args, **kwargs)
    monkeypatch.setattr(pathlib.Path, 'unlink', unlinking)


@pytest.mark.parametrize('step', STEPS)
def test_a_crash_at_any_step_of_the_recovery_loses_nothing(monkeypatch, tmp_path, step):
    """Fri dies part way through putting the record back together: the next start finds the record, the journal and
    the closed marker in a state it reconciles, so both withdrawals stay held back and saved context opens."""
    async def scenario():
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            earlier, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            more = await withdrawn_meanwhile(w)
            outage['down'] = False
            recovering(monkeypatch)
            crash_at(monkeypatch, step)
            with pytest.raises(Crash):
                turns.closed()
            assert (tmp_path/JOURNAL).exists()
            turns = w.agent.saved_turns = SavedTurns(w.agent)  # Started again.
            request = await next_request(w)
            assert OTHER not in request and MARKER not in request and history(w) > 0
            assert {str(earlier.id), str(more.id)} <= set(turns.pending)
            assert {str(earlier.id), str(more.id)} <= set(json.loads(on_disk(tmp_path))['pending'])
            assert not (tmp_path/JOURNAL).exists() and not (tmp_path/CLOSED).exists()
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_what_the_record_holds_goes_out_once_it_reads_again(monkeypatch, tmp_path):
    """#313's third gap: entries read back from the record waited until something else woke the queue. They go now."""
    async def scenario():
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        if SOURCES not in w.fri.capabilities:
            pytest.skip('needs a controller offering '+SOURCES)  # A restored entry goes only with the revision guard.
        try:
            earlier = await pending_on_disk(w, tmp_path)  # The controller is back.
            outage = disk_fails(monkeypatch, tmp_path/RECORD)
            turns = w.agent.saved_turns = SavedTurns(w.agent)
            turns.retry()  # As a start does: nothing of the record's is known yet.
            await settled(w)
            w.calls.clear()
            outage['down'] = False
            recovering(monkeypatch)
            assert not turns.closed()
            await settled(w)
            assert [payload['message_id'] for payload in calls(w, WITHDRAW)] == [str(earlier.id)]
            assert MARKER not in saved(w) and str(earlier.id) not in turns.pending
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_record_that_reads_again_but_cant_be_trusted_closes_as_before(monkeypatch, tmp_path):
    """The read error passes, but what it reads is cut off: it's closed for 90 days and set aside, as #250 closes it,
    and what came meanwhile goes into the record in its place."""
    async def scenario():
        from fri.fri_turn_withdrawal import CLOSED_SECONDS, SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            _, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            more = await withdrawn_meanwhile(w)
            cut = raw(tmp_path)[:-5]
            (tmp_path/RECORD).write_text(cut)
            outage['down'] = False
            recovering(monkeypatch)
            assert turns.closed() and turns.closed_until >= time.time()+CLOSED_SECONDS-60
            assert [path.read_text() for path in tmp_path.glob('withdrawals.unreadable-*.json')] == [cut]
            assert str(more.id) in json.loads(on_disk(tmp_path))['pending'] and not (tmp_path/JOURNAL).exists()
            w.agent.saved_turns = SavedTurns(w.agent)
            assert OTHER not in await next_request(w) and history(w) == 0
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('error', ['EISDIR', 'ELOOP', 'EINVAL'])
def test_a_read_error_that_isnt_a_passing_one_closes_as_before(monkeypatch, tmp_path, error):
    """Only an error that says nothing about the record gets the shorter path; any other closes for 90 days."""
    async def scenario():
        from fri.fri_turn_withdrawal import CLOSED_SECONDS, SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            await kept_pending(w, tmp_path, monkeypatch)
            disk_fails(monkeypatch, tmp_path/RECORD, getattr(errno, error))
            turns = w.agent.saved_turns = SavedTurns(w.agent)
            assert turns.closed() and turns.closed_until >= time.time()+CLOSED_SECONDS-60
            assert list(tmp_path.glob('withdrawals.unreadable-*.json'))
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('journal', ['cut off', 'unreadable'])
def test_a_journal_that_cant_be_read_closes_as_an_unreadable_record_does(monkeypatch, tmp_path, journal):
    """What it held can't be known, whatever kept it from being read: every saved context is held back for 90 days,
    and it's set aside unread. The record is read as usual."""
    async def scenario():
        from fri.fri_turn_withdrawal import CLOSED_SECONDS, SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = await kept_pending(w, tmp_path, monkeypatch)
            (tmp_path/JOURNAL).write_text('{"pending": {"1": ')
            if journal == 'unreadable':
                disk_fails(monkeypatch, tmp_path/JOURNAL)
            turns = w.agent.saved_turns = SavedTurns(w.agent)
            assert turns.closed() and turns.closed_until >= time.time()+CLOSED_SECONDS-60
            assert str(earlier.id) in turns.pending
            assert [raw(tmp_path, path.name) for path in tmp_path.glob('withdrawals.unreadable-*.json')] == [
                '{"pending": {"1": ']
            assert not (tmp_path/JOURNAL).exists()
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_forget_me_while_the_record_cant_be_read_takes_their_withdrawals_from_it_too(monkeypatch, tmp_path):
    """A member's "forget me" takes their queued withdrawals off disk. One in the record can't go while it can't be
    read, so it goes as the record is read back, never written back to it."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            turns.forget('20')
            outage['down'] = False
            recovering(monkeypatch)
            assert not turns.closed()
            assert str(earlier.id) not in turns.pending and str(earlier.id) not in on_disk(tmp_path)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_fri_health_says_saved_context_waits_for_the_record_to_read_again(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            _, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            w.fri.get_guild = lambda _: None
            report = await w.fri.watcher.report()
            lines = [line for line in report.splitlines() if 'saved context' in line]
            assert len(lines) == 1 and 'read again' in lines[0] and MARKER not in report
            await w.fri.health.coro(w.fri)
            health = json.loads((tmp_path/'fri-health.json').read_text())['withdrawals']
            assert health['unread'] is True and health['closed_until'] is None and MARKER not in json.dumps(health)
            outage['down'] = False
            recovering(monkeypatch)
            report = await w.fri.watcher.report()
            assert not [line for line in report.splitlines() if 'saved context' in line]
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_the_record_is_tried_again_at_most_every_thirty_seconds(monkeypatch, tmp_path):
    """Every request asks whether saved context is held back: that never reads the record each time."""
    async def scenario():
        import fri.fri_turn_withdrawal as withdrawal
        w = await world(monkeypatch, tmp_path)
        try:
            _, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            tries = outage['tries']
            for _ in range(5):
                assert turns.closed()
            assert outage['tries'] == tries and withdrawal.RECOVER_SECONDS == 30
            turns.unread_at -= withdrawal.RECOVER_SECONDS
            assert turns.closed() and outage['tries'] == tries+1
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_read_error_that_never_passes_holds_back_no_longer_than_before(monkeypatch, tmp_path):
    """Never worse than #250: 90 days from the first failure, which a restart doesn't move. Then nothing the record
    held can matter, so it's set aside and what came meanwhile goes into the record in its place."""
    async def scenario():
        from fri.fri_turn_withdrawal import CLOSED_SECONDS, SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            _, outage, _ = await outage_start(w, tmp_path, monkeypatch)
            more = await withdrawn_meanwhile(w)
            module_clock(monkeypatch, 5*DAY)
            turns = w.agent.saved_turns = SavedTurns(w.agent)  # A restart days on: the clock isn't reset.
            assert turns.closed() and str(more.id) in turns.pending
            module_clock(monkeypatch, CLOSED_SECONDS-5*DAY+60)
            turns = w.agent.saved_turns = SavedTurns(w.agent)
            assert not turns.closed() and outage['down']  # The record still can't be read.
            assert list(tmp_path.glob('withdrawals.unreadable-*.json')) and not (tmp_path/JOURNAL).exists()
            await next_request(w)
            assert history(w) > 0
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_an_older_fri_started_during_the_outage_holds_back_what_the_journal_holds(monkeypatch, tmp_path):
    """An older Fri reads only the record and the closed marker's time (its first 256 characters). Meanwhile the record
    is never replaced, and the marker's time covers every withdrawal the journal holds, as long as any could be kept,
    and the record's for as long as #250 would have closed."""
    async def scenario():
        from fri.fri_turn_withdrawal import CLOSED_SECONDS, sent_at
        w = await world(monkeypatch, tmp_path)
        try:
            first = time.time()
            await outage_start(w, tmp_path, monkeypatch)
            text = raw(tmp_path)
            await withdrawn_meanwhile(w)
            marker = raw(tmp_path, CLOSED)
            assert len(marker) <= 256 and raw(tmp_path) == text
            journal = json.loads(raw(tmp_path, JOURNAL))['pending']
            needed = max(max(sent_at(int(i)), entry['queued_at']) for i, entry in journal.items())+CLOSED_SECONDS
            assert json.loads(marker)['closed_until'] >= max(needed, first+CLOSED_SECONDS)
        finally:
            await w.fri.close()
    asyncio.run(scenario())
