"""A withdrawals record the disk fails to read (fri #317): the closure for it ends once the same process reads it.

#250 closes every saved context for 90 days after any error reading the record, and that stays as it was: the closure
is written down first, the record is set aside unread, and every withdrawal from then on goes to the record as always,
so an older Fri, or a restart, finds exactly what main leaves. What's new: for an error that says nothing about the
record (EIO, EACCES and the like), the process that found it reads the set-aside record again. Once it reads, its entries
join what's queued, never weaker, all of it is written to the record, and only then does the closure end. A restart
meanwhile keeps the closure, as main does: what an earlier process never wrote down can't be known. So does a record
that reads but can't be trusted, an error of any other kind, or any other closure. #313's blockers, and Sol round 1's
of #352 (B1 to B3), are regression tests here.
"""
import asyncio
import errno
import json
import time

import pytest

from compat_tests.test_fri_saved_withdrawal import edit, edited
from compat_tests.test_fri_turn_withdrawal import MARKER, WITHDRAW, saved, settled, world
from compat_tests.test_fri_withdrawal_rollback import (
    CLOSED,
    SOURCES,
    calls,
    history,
    kept_pending,
    module_clock,
    next_request,
    pending_on_disk,
)
from compat_tests.test_fri_withheld_history import delete, earlier_exchange, on_disk

OTHER = 'other-synthetic-marker'  # What an exchange withdrawn during the outage holds.
DERIVED = 'derived-synthetic-marker'  # Fri's saved reply to someone else, whose request carried the message.
RECORD = 'withdrawals.json'
ASIDE = 'withdrawals.unreadable-*.json'
DAY = 86400


def disk_fails(monkeypatch, tmp_path, error=errno.EIO):
    """Reading the record fails as a disk error would, wherever it's set aside, until state['down'] is False;
    state['tries'] counts those reads. Writing, and everything else, works."""
    import pathlib
    state, real = {'down': True, 'tries': 0}, pathlib.Path.open
    def opening(self, *args, **kwargs):
        mode = args[0] if args else kwargs.get('mode', 'r')
        named = self == tmp_path/RECORD or (self.parent == tmp_path and self.match(ASIDE))
        if named and state['down'] and not {'w', 'a', 'x'} & set(mode):
            state['tries'] += 1
            raise OSError(error, 'injected')
        return real(self, *args, **kwargs)
    monkeypatch.setattr(pathlib.Path, 'open', opening)
    return state


def recovering(monkeypatch):
    """The set-aside record is read again at the next chance, not 30 seconds on."""
    import fri.fri_turn_withdrawal as withdrawal
    monkeypatch.setattr(withdrawal, 'RECOVER_SECONDS', 0, raising=False)  # Absent on main.


def raw(path):
    """A file's text, read past any injected failure (as an older Fri, or a person, would read it)."""
    with open(path) as stream:
        return stream.read()


def aside(tmp_path):
    found = sorted(tmp_path.glob(ASIDE))
    return found[0] if len(found) == 1 else None


async def outage_start(w, tmp_path, monkeypatch, error=errno.EIO):
    """A withdrawal left on disk that the controller won't take, another saved exchange, and a start whose read of the
    record fails. Returns the earlier message, the record's text, the outage's state and the new queue."""
    from fri.fri_turn_withdrawal import SavedTurns
    earlier = await kept_pending(w, tmp_path, monkeypatch)
    await w.agent.handle(w.said('fri and lunch?'))  # Another saved exchange, nothing withdrawn.
    text = raw(tmp_path/RECORD)
    outage = disk_fails(monkeypatch, tmp_path, error)
    turns = w.agent.saved_turns = SavedTurns(w.agent)
    assert turns.closed()
    return earlier, text, outage, turns


async def withdrawn_meanwhile(w):
    """An exchange saved and its message deleted while the record can't be read; the controller still times out."""
    more = await earlier_exchange(w, 'fri explain '+OTHER)
    await delete(w, more)
    assert str(more.id) in w.agent.saved_turns.pending
    return more


def test_a_record_the_disk_failed_to_read_holds_back_only_until_this_process_reads_it(monkeypatch, tmp_path):
    """The issue: one read error at start-up closed every saved context for 90 days. It still closes, as main does,
    the record set aside unread; once this process reads it after all, only what it holds stays held back."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier, text, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            assert MARKER not in await next_request(w) and history(w) == 0
            assert raw(aside(tmp_path)) == text and (tmp_path/CLOSED).exists()  # As main leaves it.
            outage['down'] = False
            recovering(monkeypatch)
            request = await next_request(w)
            assert MARKER not in request and history(w) > 0  # Open again; what the record holds is still held back.
            assert str(earlier.id) in turns.pending and not turns.closed()
            assert str(earlier.id) in json.loads(on_disk(tmp_path))['pending']
            assert not (tmp_path/CLOSED).exists() and aside(tmp_path) is None
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('restart', ['once the record reads', 'while it still cant be read'])
def test_the_313_repro_a_withdrawal_made_while_the_record_cant_be_read_outlasts_a_restart(monkeypatch, tmp_path,
                                                                                         restart):
    """#313's first blocker: with the record unread, queue writes were dropped, so after a restart a withdrawal made
    meanwhile was gone, and its exchange reached the next request. It goes to the record as main writes it, and a
    restart keeps the closure."""
    async def scenario():
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            _, _, outage, _ = await outage_start(w, tmp_path, monkeypatch)
            more = await withdrawn_meanwhile(w)
            assert str(more.id) in json.loads(raw(tmp_path/RECORD))['pending']
            if restart == 'once the record reads':
                outage['down'] = False
            turns = w.agent.saved_turns = SavedTurns(w.agent)  # Restarted.
            recovering(monkeypatch)
            outage['down'] = False
            request = await next_request(w)
            assert OTHER not in request and MARKER not in request and history(w) == 0 and turns.closed()
            if restart == 'once the record reads':
                assert str(more.id) in turns.pending
        finally:
            await w.fri.close()
    asyncio.run(scenario())


EVENTS = ['a late edit, by ID', 'a late edit of an exchange saved here', 'its raw delete, by ID']


@pytest.mark.parametrize('event', EVENTS)
def test_the_313_repro_what_the_record_holds_is_never_weakened_by_what_came_meanwhile(monkeypatch, tmp_path, event):
    """#313's second blocker: recovery skipped the record's entry whenever the same message was queued meanwhile, losing
    where it was saved, its revision, that an earlier process queued it and that it was a deletion; a late edit even
    turned the deletion into an edit. The merged entry keeps all of that, and the later time it was queued, and it's on
    disk before saved context opens."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier, text, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            disk = json.loads(text)['pending'][str(earlier.id)]
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


@pytest.mark.parametrize('meanwhile', ['its raw delete, by ID', 'a save here and its edit'])
def test_a_hold_on_disk_stays_one_whatever_came_meanwhile(monkeypatch, tmp_path, meanwhile):
    """A hold in the record (never sent, held as long as what it names can be kept) met by anything queued while the
    record couldn't be read stays a hold, here and after a restart."""
    async def scenario():
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            held_back = w.said('said and taken back')
            (tmp_path/RECORD).write_text(json.dumps({'pending': {str(held_back.id): {'hold': True,
                                                                                     'queued_at': int(time.time())}}}))
            outage = disk_fails(monkeypatch, tmp_path)
            turns = w.agent.saved_turns = SavedTurns(w.agent)
            assert turns.closed()
            if meanwhile == 'its raw delete, by ID':
                await delete(w, held_back, guild=123)
            else:
                scope = {'guild_id': '123', 'channel_id': '1', 'conversation_id': 'public:1', 'user_id': '20'}
                turns.kept({**scope, 'revision': 0, 'source': {'kind': 'discord_message', 'id': str(held_back.id)}},
                           {'ok': True}, (time.time(), time.monotonic()))
                await edit(w, edited(held_back))
            outage['down'] = False
            recovering(monkeypatch)
            assert not turns.closed() and turns.pending[str(held_back.id)].get('hold') is True
            assert json.loads(on_disk(tmp_path))['pending'][str(held_back.id)].get('hold') is True
            w.agent.saved_turns = SavedTurns(w.agent)
            assert w.agent.saved_turns.withheld([held_back.id]) == {str(held_back.id)}
        finally:
            await w.fri.close()
    asyncio.run(scenario())


class Stop(BaseException):
    """The process stops here."""


STEPS = ['before the record is written', 'after the record, before the closed marker goes',
         'after the closed marker, before the set-aside copy goes']


def stop_at(monkeypatch, tmp_path, step):
    """The process stops once, at this step of ending the closure (see SavedTurns.recover)."""
    import pathlib

    import fri.fri_turn_withdrawal as withdrawal
    left = [1]
    if step == 'before the record is written':
        real = withdrawal.private_json
        def writing(path, data):
            if path.name == RECORD and left[0]:
                left[0] -= 1
                raise Stop()
            return real(path, data)
        monkeypatch.setattr(withdrawal, 'private_json', writing)
        return
    marker = 'before the closed marker' in step
    real_unlink = pathlib.Path.unlink
    def unlinking(self, *args, **kwargs):
        if (self.name == CLOSED if marker else self.match(ASIDE)) and left[0]:
            left[0] -= 1
            raise Stop()
        return real_unlink(self, *args, **kwargs)
    monkeypatch.setattr(pathlib.Path, 'unlink', unlinking)


@pytest.mark.parametrize('step', STEPS)
def test_a_stop_at_any_step_of_ending_the_closure_loses_nothing(monkeypatch, tmp_path, step):
    """Fri stops part way through: the next start finds what main would (closed, both withdrawals held), or, once the
    marker is gone, the record written back whole."""
    async def scenario():
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            earlier, _, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            more = await withdrawn_meanwhile(w)
            outage['down'] = False
            recovering(monkeypatch)
            stop_at(monkeypatch, tmp_path, step)
            with pytest.raises(Stop):
                turns.closed()
            turns = w.agent.saved_turns = SavedTurns(w.agent)  # Started again.
            request = await next_request(w)
            assert OTHER not in request and MARKER not in request
            if step == 'before the record is written':
                assert turns.closed() and history(w) == 0 and str(more.id) in turns.pending
            elif 'before the closed marker' in step:
                assert turns.closed() and history(w) == 0
                assert {str(earlier.id), str(more.id)} <= set(turns.pending)
            else:
                assert not turns.closed() and history(w) > 0
                assert {str(earlier.id), str(more.id)} <= set(turns.pending)
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
            outage = disk_fails(monkeypatch, tmp_path)
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


def test_a_record_that_reads_again_but_cant_be_trusted_keeps_the_closure(monkeypatch, tmp_path):
    """The read error passes, but what it reads is cut off: closed for 90 days as main closes it, and not read again."""
    async def scenario():
        from fri.fri_turn_withdrawal import CLOSED_SECONDS
        w = await world(monkeypatch, tmp_path)
        try:
            _, text, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            more = await withdrawn_meanwhile(w)
            aside(tmp_path).write_text(text[:-5])
            outage['down'] = False
            recovering(monkeypatch)
            assert turns.closed() and turns.closed_until >= time.time()+CLOSED_SECONDS-60
            assert getattr(turns, 'unread', None) is None and raw(aside(tmp_path)) == text[:-5]
            assert str(more.id) in json.loads(on_disk(tmp_path))['pending']
            assert OTHER not in await next_request(w) and history(w) == 0
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('error', ['EISDIR', 'ELOOP', 'EINVAL'])
def test_a_read_error_that_isnt_a_passing_one_closes_as_before(monkeypatch, tmp_path, error):
    """Only an error that says nothing about the record ends early; any other closes for 90 days, and isn't retried."""
    async def scenario():
        from fri.fri_turn_withdrawal import CLOSED_SECONDS
        w = await world(monkeypatch, tmp_path)
        try:
            _, _, outage, turns = await outage_start(w, tmp_path, monkeypatch, getattr(errno, error))
            outage['down'] = False
            recovering(monkeypatch)
            assert turns.closed() and turns.closed_until >= time.time()+CLOSED_SECONDS-60
            assert getattr(turns, 'unread', None) is None
            assert aside(tmp_path) is not None
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_another_closure_keeps_the_closure_whatever_reads_again(monkeypatch, tmp_path):
    """Closed for something else too (more queued than the queue keeps, here): that can't be told apart, so the
    closure stands, as main keeps it."""
    async def scenario():
        import fri.fri_turn_withdrawal as withdrawal
        w = await world(monkeypatch, tmp_path)
        try:
            _, _, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            monkeypatch.setattr(withdrawal, 'MOST', 1)
            await withdrawn_meanwhile(w)
            await delete(w, w.said('fri one more'))
            outage['down'] = False
            recovering(monkeypatch)
            assert turns.closed() and getattr(turns, 'unread', None) is None
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_closure_already_in_force_at_start_never_ends_early(monkeypatch, tmp_path):
    async def scenario():
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            await kept_pending(w, tmp_path, monkeypatch)
            (tmp_path/CLOSED).write_text(json.dumps({'closed_until': time.time()+DAY, 'why': ['capacity']}))
            outage = disk_fails(monkeypatch, tmp_path)
            turns = w.agent.saved_turns = SavedTurns(w.agent)
            assert turns.closed() and getattr(turns, 'unread', None) is None
            outage['down'] = False
            recovering(monkeypatch)
            assert turns.closed()
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_forget_me_while_the_record_cant_be_read_takes_their_withdrawals_from_it_too(monkeypatch, tmp_path):
    """A member's "forget me" takes their queued withdrawals. One in the record can't go while it can't be read, so it
    goes as the record is read back, never written back to it."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier, _, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            turns.forget('20')
            outage['down'] = False
            recovering(monkeypatch)
            assert not turns.closed()
            assert str(earlier.id) not in turns.pending and str(earlier.id) not in on_disk(tmp_path)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_fri_health_says_the_closure_ends_once_the_record_reads(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            _, _, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            w.fri.get_guild = lambda _: None
            report = await w.fri.watcher.report()
            lines = [line for line in report.splitlines() if 'saved context' in line]
            assert len(lines) == 2 and 'read again' in lines[0] and '<t:' in lines[1] and MARKER not in report
            await w.fri.health.coro(w.fri)
            health = json.loads((tmp_path/'fri-health.json').read_text())['withdrawals']
            assert health['unread'] is True and health['closed_until'] and MARKER not in json.dumps(health)
            outage['down'] = False
            recovering(monkeypatch)
            report = await w.fri.watcher.report()
            assert not [line for line in report.splitlines() if 'saved context' in line]
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_the_record_is_read_again_at_most_every_thirty_seconds(monkeypatch, tmp_path):
    """Every request asks whether saved context is held back: that never reads the record each time."""
    async def scenario():
        import fri.fri_turn_withdrawal as withdrawal
        w = await world(monkeypatch, tmp_path)
        try:
            _, _, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            tries = outage['tries']
            for _ in range(5):
                assert turns.closed()
            assert outage['tries'] == tries and withdrawal.RECOVER_SECONDS == 30
            turns.unread_at -= withdrawal.RECOVER_SECONDS
            assert turns.closed() and outage['tries'] == tries+1
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_read_error_that_never_passes_holds_back_as_main_does(monkeypatch, tmp_path):
    """Never longer than before: read again all along, never readable, and still open 90 days on, as main."""
    async def scenario():
        from fri.fri_turn_withdrawal import CLOSED_SECONDS
        w = await world(monkeypatch, tmp_path)
        try:
            _, _, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            await withdrawn_meanwhile(w)
            recovering(monkeypatch)
            module_clock(monkeypatch, 5*DAY)
            assert turns.closed() and getattr(turns, 'unread', None) is not None
            module_clock(monkeypatch, CLOSED_SECONDS-5*DAY+60)
            assert not turns.closed() and outage['down']
            await next_request(w)
            assert history(w) > 0
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_record_that_reads_again_but_cant_be_written_stays_held_back(monkeypatch, tmp_path):
    """Sol round 1 of #352, S1: ended only once it's on disk. A write that fails (a full disk) keeps every saved context
    held back and is tried again; a restart meanwhile finds the closure, as main does."""
    async def scenario():
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            earlier, _, outage, turns = await outage_start(w, tmp_path, monkeypatch)
            more = await withdrawn_meanwhile(w)
            outage['down'] = False
            recovering(monkeypatch)
            restore = failing_writes(monkeypatch, OSError(errno.ENOSPC, 'injected'), names={RECORD})
            assert turns.closed() and turns.closed()  # Read again each time, never written.
            assert (tmp_path/CLOSED).exists() and aside(tmp_path) is not None
            restore()
            assert not turns.closed()  # Written now.
            assert {str(earlier.id), str(more.id)} <= set(json.loads(on_disk(tmp_path))['pending'])
            restore = failing_writes(monkeypatch, OSError(errno.ENOSPC, 'injected'), names={RECORD})
            w.agent.saved_turns = SavedTurns(w.agent)
            assert OTHER not in await next_request(w) and MARKER not in str(w.requests)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


# Sol round 1 of #352: three NEW blockers in the journal design, which this one has none of. Each stays a test.


def failing_writes(monkeypatch, error, names=None, times=None):
    """Fri's private writes to these files (any but the closed marker, for None) fail: `error` raised; `times` of them
    (all, for None). Returns what undoes it."""
    import fri.fri_turn_withdrawal as withdrawal
    real, left = withdrawal.private_json, [times]
    def writing(path, data):
        if (path.name in names if names else path.name != CLOSED) and (left[0] is None or left[0] > 0):
            if left[0] is not None:
                left[0] -= 1
            raise error
        return real(path, data)
    monkeypatch.setattr(withdrawal, 'private_json', writing)
    return lambda: monkeypatch.setattr(withdrawal, 'private_json', real)


@pytest.mark.parametrize('failure', ['the process stops before it is written', 'its write fails on a full disk'])
def test_the_b1_repro_a_withdrawal_never_written_down_stays_held_back_after_a_restart(monkeypatch, tmp_path, failure):
    """B1: a withdrawal made while the record couldn't be read, whose own write never landed (the process stopped
    before it, or the disk was full), and a restart once the record reads: its exchange stays out of the next model
    request, as main's closure keeps it."""
    async def scenario():
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            _, _, outage, _ = await outage_start(w, tmp_path, monkeypatch)
            more = await earlier_exchange(w, 'fri explain '+OTHER)
            restore = failing_writes(monkeypatch, Stop() if failure.startswith('the process') else
                                     OSError(errno.ENOSPC, 'injected'))
            if failure.startswith('the process'):
                with pytest.raises(Stop):
                    await delete(w, more)
            else:
                await delete(w, more)
            restore()
            outage['down'] = False
            w.agent.saved_turns = SavedTurns(w.agent)  # Restarted; the record reads.
            recovering(monkeypatch)
            request = await next_request(w)
            assert OTHER not in request and MARKER not in request
        finally:
            await w.fri.close()
    asyncio.run(scenario())


async def derived_reply(w, message):
    """Member 30's saved exchange, whose model request carried `message` and whose reply repeats it."""
    import inspect
    if 'drew_on' not in inspect.signature(w.store.save_chat).parameters:
        pytest.skip('needs a controller that records what a reply drew on')
    asked = w.said('fri what did they mean', user=30)
    source = {'kind': 'discord_message', 'id': str(asked.id), 'user_id': '30', 'channel_id': '1', 'guild_id': '123',
              'display_name': 'member'}
    w.store.save_chat('123', 'public:1', '30', 0, 'fri what did they mean', 'they meant '+DERIVED, 'model', 'high',
                      source=source, provenance=[], drew_on=[str(message.id)], audience='server')
    assert DERIVED in saved(w, '30')


async def asked_by_30(w):
    w.requests.clear()
    await w.agent.handle(w.said('fri hi', user=30))
    assert w.requests
    return str(w.requests)


@pytest.mark.parametrize('when', ['after a restart', 'in the same process'])
def test_the_b2_repro_a_deletion_is_never_weakened_by_an_earlier_edit(monkeypatch, tmp_path, when):
    """B2: the record holds an edit's withdrawal for a message; while it can't be read, the message is deleted, and the
    controller takes neither. Whatever reads them back together, the deletion stands: Fri's saved reply to someone else
    that carried the message never reaches the model, as the controller retires it only for a deletion."""
    async def scenario():
        from compat_tests.test_fri_saved_withdrawal import SAVED
        from compat_tests.test_fri_withheld_history import failing
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = await earlier_exchange(w)
            down = failing(w, WITHDRAW)
            await edit(w, edited(earlier))
            assert json.loads(on_disk(tmp_path))['pending'][str(earlier.id)]['deleted'] is False
            await derived_reply(w, earlier)
            by_id = failing(w, SAVED)
            outage = disk_fails(monkeypatch, tmp_path)
            turns = w.agent.saved_turns = SavedTurns(w.agent)
            assert turns.closed()
            await delete(w, earlier, guild=123)  # Its raw delete, by ID: the controller takes neither.
            outage['down'], down['down'], by_id['down'] = False, False, False
            recovering(monkeypatch)
            if when == 'after a restart':
                w.agent.saved_turns = SavedTurns(w.agent)
                w.agent.saved_turns.retry()  # As a start does.
            w.agent.saved_turns.closed()
            await settled(w)
            assert DERIVED not in await asked_by_30(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_the_b3_repro_an_older_fri_after_the_first_deadline_still_holds_a_later_withdrawal_back(monkeypatch, tmp_path):
    """B3: a day into the outage a withdrawal is made, and one write of the closed marker fails. Rolled back after the
    first deadline, an older Fri reads only the record and the marker's time: the message is in one or the other, until
    nothing it names can be kept."""
    async def scenario():
        from compat_tests.test_fri_withheld_history import needs_sources
        from fri.fri_turn_withdrawal import CLOSED_SECONDS, deadline, read_record, sent_at
        w = await world(monkeypatch, tmp_path)
        needs_sources(w)  # A note's 90 days: the withdrawal outlasts the first deadline.
        try:
            first = time.time()
            await outage_start(w, tmp_path, monkeypatch)
            module_clock(monkeypatch, DAY)
            more = await earlier_exchange(w, 'fri explain '+OTHER)
            restore = failing_writes(monkeypatch, OSError(errno.ENOSPC, 'injected'), names={CLOSED}, times=1)
            await delete(w, more)
            restore()
            await delete(w, w.said('fri another one'))  # Later writes go as they would.
            until = deadline(raw(tmp_path/CLOSED)) or 0
            later = first+CLOSED_SECONDS+60  # Past the first deadline, with the withdrawal still able to matter.
            queued_at = w.agent.saved_turns.pending[str(more.id)]['queued_at']  # A day in, by the queue's clock.
            assert max(sent_at(more.id), queued_at)+CLOSED_SECONDS > later
            in_record = str(more.id) in read_record(raw(tmp_path/RECORD))
            assert in_record or until >= later
        finally:
            await w.fri.close()
    asyncio.run(scenario())
