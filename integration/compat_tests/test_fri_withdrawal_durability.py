"""A withdrawal Gene hasn't taken is never dropped or released: not by a restart (fri #252), not by the queue's
capacity (fri #256), not by its reader's own bound and not before what it names can't be kept (fri #329). Against the
real controller."""

import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_saved_withdrawal import SAVED, asked, long_after, restarted
from compat_tests.test_fri_turn_withdrawal import MARKER, WITHDRAW, saved, settled, world
from compat_tests.test_fri_withheld_history import delete, earlier_exchange, failing, on_disk
from fri import fri_turn_withdrawal as withdrawal
from fri.fri_turn_withdrawal import SavedTurns


def capable(w):
    if 'saved-turn-withdrawal' not in w.fri.capabilities:
        pytest.skip('needs a controller offering saved-turn-withdrawal')


async def an_hour_later_asks(monkeypatch, w):
    """The local mark has run out, and the member asks something unrelated: what reaches the model."""
    monkeypatch.setattr(withdrawal, 'MARKED_SECONDS', 0)
    w.requests.clear()
    await w.agent.handle(w.said('fri hi'))
    assert w.requests
    return str(w.requests)


def test_a_failed_withdrawal_by_id_holds_its_message_back_across_a_restart(monkeypatch, tmp_path):
    """fri #252: Fri no longer knows where it saved the exchange (it saved it before a restart), so the deletion is
    asked about by ID, which times out; then Fri restarts before a retry succeeds. The withdrawal was in memory only,
    and the deleted words reached the next request. It's kept on disk as a hold: held back after the restart, never
    sent again from there, and naming no member."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        capable(w)
        try:
            earlier = await earlier_exchange(w)
            restarted(w)
            down = failing(w, SAVED)
            await delete(w, earlier, guild=123)
            assert str(earlier.id) in w.agent.saved_turns.pending
            w.agent.saved_turns = SavedTurns(w.agent)  # Started again before a retry got through.
            down['down'] = False
            before = len(asked(w))
            w.agent.saved_turns.retry()
            await settled(w)
            assert str(earlier.id) in w.agent.saved_turns.pending  # Still held back.
            assert not any(str(earlier.id) in p['message_ids'] for p in asked(w)[before:])  # Never replayed.
            assert MARKER not in await an_hour_later_asks(monkeypatch, w)
            assert '"20"' not in on_disk(tmp_path)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_capacity_never_releases_a_withdrawal_gene_hasnt_taken(monkeypatch, tmp_path):
    """fri #256, with a cap of two: one failed withdrawal of an exchange Fri saved, then two more deletions whose
    withdrawals fail too. The oldest was dropped for room, and once its hour-long mark ran out its words reached the
    next request. Capacity holds back more instead: every saved context, until what was dropped can't be kept any
    more, written down so a restart keeps it."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = await earlier_exchange(w)
            others = [w.said(text) for text in ('fri what about lunch', 'fri and after that')]
            for other in others:  # Two more exchanges Fri saved, newer.
                await w.agent.handle(other)
            monkeypatch.setattr(withdrawal, 'MOST', 2)
            failing(w, WITHDRAW)
            for message in (earlier, *others):
                await delete(w, message, guild=123)
            await settled(w)
            assert len(w.agent.saved_turns.pending) <= 2
            assert MARKER not in await an_hour_later_asks(monkeypatch, w)
            w.agent.saved_turns = SavedTurns(w.agent)  # Started again: still held back.
            assert w.agent.saved_turns.withheld([earlier.id]) == {str(earlier.id)}
            assert MARKER not in await an_hour_later_asks(monkeypatch, w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def closures_fail(monkeypatch):
    """Writing until when saved context is held back fails, as a full disk would, while `state['down']`."""
    state, real = {'down': True}, withdrawal.private_json
    def writing(path, value):
        if path.name == withdrawal.CLOSED_RECORD and state['down']:
            raise OSError(28, 'injected')
        return real(path, value)
    monkeypatch.setattr(withdrawal, 'private_json', writing)
    return state


async def exchanges(w, count):
    """`count` exchanges Fri saved, oldest first, the first holding the marker."""
    first = await earlier_exchange(w)
    later = [w.said(f'fri and then {n}') for n in range(count-1)]
    for message in later:
        await w.agent.handle(message)
    return [first, *later]


@pytest.mark.parametrize('recovered', [True, False], ids=['written at the restart', 'still not written'])
def test_a_restart_past_twice_the_capacity_releases_nothing_its_reader_drops(monkeypatch, tmp_path, recovered):
    """fri #329 item 1, the issue's repro: with a cap of two and holding saved context back failing to be written,
    none of five failed withdrawals is dropped, and all five are on disk. Fri restarts once writes work again. Its
    reader cut the five straight to two, so nothing held back what it dropped, and once the marks ran out the oldest's
    words reached the next request. The reader's overflow is handled as the queue's is: every saved context is held
    back until nothing it dropped can be kept, written down first. While that can't be written, it's held back all the
    same and the record isn't replaced, so the next start reads all five again."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            messages = await exchanges(w, 5)
            monkeypatch.setattr(withdrawal, 'MOST', 2)
            failing(w, WITHDRAW)
            closing = closures_fail(monkeypatch)
            for message in messages:
                await delete(w, message, guild=123)
            await settled(w)
            assert len(json.loads(on_disk(tmp_path))['pending']) == 5  # None dropped: nothing held back was written.
            closing['down'] = not recovered
            w.agent.saved_turns = SavedTurns(w.agent)  # Started again.
            assert w.agent.saved_turns.closed()
            assert MARKER not in await an_hour_later_asks(monkeypatch, w)
            w.agent.saved_turns.persist()
            if not recovered:  # Not replaced: the next start reads all five again.
                assert len(json.loads(on_disk(tmp_path))['pending']) == 5
            w.agent.saved_turns = SavedTurns(w.agent)  # And again.
            assert w.agent.saved_turns.closed()
            assert MARKER not in await an_hour_later_asks(monkeypatch, w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def aged(w, text, seconds):
    """A message of the member's sent `seconds` ago."""
    event = w.said(text)
    del w.rows[event.id]
    event.id = discord.utils.time_snowflake(discord.utils.utcnow()-dt.timedelta(seconds=seconds))
    w.rows[event.id] = event
    event.reply.return_value = SimpleNamespace(id=event.id+100, content='okay', edit=AsyncMock())
    return event


def later(monkeypatch, seconds):
    """The withdrawal queue's clock, `seconds` on; no one else's: the controller still keeps what it saved."""
    monkeypatch.setattr(withdrawal, 'time', SimpleNamespace(time=lambda: time.time()+seconds,
                                                            monotonic=time.monotonic))


@pytest.mark.parametrize('how', ['expiry', 'capacity'])
def test_a_withdrawal_lasts_as_long_as_what_it_names_from_when_it_was_saved(monkeypatch, tmp_path, how):
    """fri #329 item 2, the issue's repro: the message was sent two minutes before Fri saved its exchange (and, from a
    controller offering withdrawal-sources, a note from it), and its withdrawal stays pending. Fri restarts a minute
    before what was saved can no longer be kept. The withdrawal counted from the message's own time, so it had expired,
    or past the queue's capacity what held saved context back for it had ended, while the exchange lived on, and its
    words reached the next request. It counts from when it was queued, after the save, kept on disk with it. Guard:
    once that has passed too, it goes."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            horizon = withdrawal.NOTED_SECONDS if withdrawal.SOURCES in w.fri.capabilities else withdrawal.KEPT_SECONDS
            earlier = aged(w, 'fri explain '+MARKER, 120)
            await w.agent.handle(earlier)
            assert MARKER in saved(w)
            long_after(w)
            others = [w.said(text) for text in ('fri what about lunch', 'fri and after that')] if how == 'capacity' else []
            for other in others:  # Newer exchanges, so the earlier is the oldest the queue lets go.
                await w.agent.handle(other)
            if others:
                monkeypatch.setattr(withdrawal, 'MOST', 2)
            failing(w, WITHDRAW)
            for message in (earlier, *others):
                await delete(w, message, guild=123)
            await settled(w)
            later(monkeypatch, horizon-60)
            w.agent.saved_turns = SavedTurns(w.agent)  # Started again a minute before the exchange can't be kept.
            assert w.agent.saved_turns.withheld([earlier.id]) == {str(earlier.id)}
            assert MARKER not in await an_hour_later_asks(monkeypatch, w)
            later(monkeypatch, horizon+60)
            w.agent.saved_turns = SavedTurns(w.agent)  # Past that too: nothing it names can be kept any more.
            assert w.agent.saved_turns.withheld([earlier.id]) == set()
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_capacity_closure_set_while_the_clock_was_back_is_never_cut_short(monkeypatch, tmp_path):
    """Sol round 1 of #339, B1: with a cap of two, messages A, B and C were sent 200, 90 and 80 seconds ago. A is
    queued, the clock goes back an hour, and B and C are queued while withdrawals fail: B, whose names can be kept
    least long, goes, and saved context is held back until B's horizon ends. Fri restarted a second later, the clock
    still back, and cut that deadline to the longest horizon from then; once the clock was right again, half an hour
    before B's horizon ended, B's message was let through. A deadline once written down is never cut short."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            horizon = withdrawal.NOTED_SECONDS if withdrawal.SOURCES in w.fri.capabilities else withdrawal.KEPT_SECONDS
            a, b, c = (aged(w, f'fri remember {MARKER} {n}', age) for n, age in enumerate((200, 90, 80)))
            for message in (a, b, c):
                await w.agent.handle(message)
            monkeypatch.setattr(withdrawal, 'MOST', 2)
            failing(w, WITHDRAW)
            await delete(w, a, guild=123)
            later(monkeypatch, -3600)
            for message in (b, c):
                await delete(w, message, guild=123)
            await settled(w)
            assert str(b.id) not in w.agent.saved_turns.pending and w.agent.saved_turns.closed()
            later(monkeypatch, -3599)
            w.agent.saved_turns = SavedTurns(w.agent)  # Started again, the clock still back.
            assert w.agent.saved_turns.closed()
            later(monkeypatch, horizon-1800)
            w.agent.saved_turns = SavedTurns(w.agent)  # The clock right again, half an hour before B's horizon ends.
            assert w.agent.saved_turns.withheld([b.id]) == {str(b.id)}
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_when_a_withdrawal_was_queued_is_kept_whatever_the_clock_says_at_a_restart(monkeypatch, tmp_path):
    """Sol round 1 of #339, B2: a hold queued at T for a message sent ten days before is on disk. Fri restarts with
    the clock a day back and saves the queue again: it said T minus a day, so once the clock was right the hold ended
    a day early, while what it named could still be kept. What's on disk is kept."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            horizon = withdrawal.NOTED_SECONDS if withdrawal.SOURCES in w.fri.capabilities else withdrawal.KEPT_SECONDS
            now = int(time.time())
            message = discord.utils.time_snowflake(discord.utils.utcnow()-dt.timedelta(days=10))
            (tmp_path/'withdrawals.json').write_text(json.dumps({'pending': {str(message): {'hold': True,
                                                                                             'queued_at': now}}}))
            later(monkeypatch, -86400)
            w.agent.saved_turns = SavedTurns(w.agent)  # Started again, the clock a day back.
            w.agent.saved_turns.persist()
            assert json.loads(on_disk(tmp_path))['pending'][str(message)]['queued_at'] == now
            later(monkeypatch, horizon-43200)
            w.agent.saved_turns = SavedTurns(w.agent)  # The clock right, half a day before the hold's horizon ends.
            assert w.agent.saved_turns.withheld([message]) == {str(message)}
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_what_the_reader_lets_go_is_held_back_until_its_messages_own_horizon(monkeypatch, tmp_path):
    """Sol round 1 of #339, B3: with a cap of two, five holds queued at T for messages whose time says a day after T
    are on disk. The reader lets three go and held saved context back until T plus the horizon, earlier than those
    messages' own: an hour past it, what they named was let through. Their own horizon counts, however the clock
    stands."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            horizon = withdrawal.NOTED_SECONDS if withdrawal.SOURCES in w.fri.capabilities else withdrawal.KEPT_SECONDS
            now = int(time.time())
            ahead = discord.utils.time_snowflake(discord.utils.utcnow()+dt.timedelta(days=1))
            holds = {str(ahead+n): {'hold': True, 'queued_at': now} for n in range(5)}
            (tmp_path/'withdrawals.json').write_text(json.dumps({'pending': holds}))
            monkeypatch.setattr(withdrawal, 'MOST', 2)
            w.agent.saved_turns = SavedTurns(w.agent)
            gone = [i for i in holds if i not in w.agent.saved_turns.pending]
            assert gone and w.agent.saved_turns.closed()
            w.agent.saved_turns.persist()  # The queue saved again, as any withdrawal queued meanwhile does.
            assert set(json.loads(on_disk(tmp_path))['pending']).isdisjoint(gone)
            later(monkeypatch, horizon+3600)
            w.agent.saved_turns = SavedTurns(w.agent)  # An hour past T plus the horizon, a day before theirs ends.
            assert w.agent.saved_turns.withheld([int(i) for i in gone]) == set(gone)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('count', [1, 5], ids=['within the cap', 'past what the reader holds'])
def test_entries_an_older_fri_wrote_are_dated_once_not_at_every_restart(monkeypatch, tmp_path, count):
    """fri #343 item 1: holds an older Fri wrote, with no time they were queued, count from the start that reads them,
    but that was kept only once something saved the queue again. With nothing more withdrawn, every restart dated them
    again, and past what the reader holds (a cap of two, five holds) each pushed what held saved context back on by
    the time between restarts, while the record stayed as it was. The first start that reads them saves the queue, so
    they're dated once: later starts keep that date, and so does what holds saved context back, which then ends."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            horizon = withdrawal.NOTED_SECONDS if withdrawal.SOURCES in w.fri.capabilities else withdrawal.KEPT_SECONDS
            sent = discord.utils.time_snowflake(discord.utils.utcnow()-dt.timedelta(days=1))
            (tmp_path/'withdrawals.json').write_text(json.dumps({'pending': {str(sent+n): {'hold': True}
                                                                             for n in range(count)}}))
            monkeypatch.setattr(withdrawal, 'MOST', 2)
            w.agent.saved_turns = SavedTurns(w.agent)
            assert w.agent.saved_turns.withheld([sent]) == {str(sent)}
            first, dated = w.agent.saved_turns.closed_until, json.loads(on_disk(tmp_path))['pending']
            assert dated and all(isinstance(entry.get('queued_at'), int) for entry in dated.values())
            later(monkeypatch, horizon-86400)
            w.agent.saved_turns = SavedTurns(w.agent)  # A day before the horizon ends, from the first start.
            assert w.agent.saved_turns.withheld([sent]) == {str(sent)}  # Read, and still held back,
            assert w.agent.saved_turns.closed_until == first and json.loads(on_disk(tmp_path))['pending'] == dated
            later(monkeypatch, 2*horizon-2*86400)
            w.agent.saved_turns = SavedTurns(w.agent)  # Past it: nothing is held back for them any more.
            assert not w.agent.saved_turns.closed() and w.agent.saved_turns.withheld([sent]) == set()
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_fri_health_flags_a_deadline_later_than_fri_sets(monkeypatch, tmp_path):
    """fri #343 item 2: a closed marker saying 1e220 is kept (cutting it short could reopen what #339 round 1 closed),
    but `fri health` printed it as a 220-digit date and said nothing was wrong. It says the date is past any Fri sets
    and how to end it by hand; a deadline Fri sets reads as before."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            w.fri.get_guild = lambda _: None
            for until, flagged in ((1e220, True), (time.time()+86400, False)):
                (tmp_path/'withdrawals.closed.json').write_text(json.dumps({'closed_until': until}))
                w.agent.saved_turns = SavedTurns(w.agent)
                assert w.agent.saved_turns.closed() and w.agent.saved_turns.closed_until == until
                [line] = [line for line in (await w.fri.watcher.report()).splitlines() if 'saved context' in line]
                assert ('withdrawals.closed.json' in line) is flagged and ('<t:' in line) is not flagged
                assert len(line) < 400
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('cause', ['capacity', 'unreadable'])
def test_fri_health_says_why_saved_context_is_held_back(monkeypatch, tmp_path, cause):
    """fri #329 item 4: after the queue's capacity held saved context back, `fri health` said a withdrawals record
    couldn't be read. It says which, after a restart too, and nothing of the record."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            if cause == 'capacity':
                messages = await exchanges(w, 3)
                monkeypatch.setattr(withdrawal, 'MOST', 2)
                failing(w, WITHDRAW)
                for message in messages:
                    await delete(w, message, guild=123)
                await settled(w)
            else:
                (tmp_path/'withdrawals.json').write_text('{"pending": {"1": '+MARKER)
            w.fri.get_guild = lambda _: None
            for _ in range(2):
                w.agent.saved_turns = SavedTurns(w.agent)  # Started again: from what was written down.
                assert w.agent.saved_turns.closed()
                report = await w.fri.watcher.report()
                lines = [line for line in report.splitlines() if 'saved context' in line]
                assert len(lines) == 1 and MARKER not in report
                assert ("couldn't be read" in lines[0]) == (cause == 'unreadable')
                assert ('more withdrawals were queued' in lines[0]) == (cause == 'capacity')
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_the_queue_within_its_capacity_holds_nothing_more_back(monkeypatch, tmp_path):
    """Guard: under the cap, a withdrawal that fails holds back its own message only, as before."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = await earlier_exchange(w)
            failing(w, WITHDRAW)
            await delete(w, earlier, guild=123)
            assert not w.agent.saved_turns.closed()
            assert w.agent.saved_turns.withheld([earlier.id, earlier.id+5000]) == {str(earlier.id)}
        finally:
            await w.fri.close()
    asyncio.run(scenario())
