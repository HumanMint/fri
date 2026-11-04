"""A deletion of a message older than the queue's horizon, whose data was saved later (fri #342).

A by-ID withdrawal was queued only while the deleted message's own time was inside the horizon (a note's 90 days from a
controller offering withdrawal-sources). What's drawn from a message can be saved long after it was sent: a note
learned from an old message quoted in a newer conversation, say. Deleted after its own 90 days but inside 90 days of
that save, it queued nothing, so nothing held the note back while the controller still had it. A deletion is now
asked about by ID whatever its message's age, and its entry lasts from when it was queued (fri #329), as any does.
"""
import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import discord

from compat_tests.test_fri_saved_withdrawal import SAVED
from compat_tests.test_fri_turn_withdrawal import world
from compat_tests.test_fri_withdrawal_rollback import asked, calls, module_clock
from compat_tests.test_fri_withheld_history import LIKE, delete, failing, needs_sources, on_disk

DAY = 86400


def old_message(days=100):
    """A message sent `days` ago: past the queue's horizon by its own time."""
    when = dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=days)
    return SimpleNamespace(id=discord.utils.time_snowflake(when))


def noted(w, message):
    """A preference learned now from that message, quoted in a newer conversation; only the note holds its words."""
    source = {'kind': 'discord_message', 'id': str(message.id), 'user_id': '20', 'channel_id': '1', 'guild_id': '123',
              'display_name': 'member'}
    w.store.save_chat('123', 'public:1', '20', 0, LIKE, 'okay', 'model', 'high', source=source, provenance=[],
                      notes=[{'category': 'hobbies', 'evidence': LIKE}])
    with w.store.connection() as db:
        db.execute('DELETE FROM chat_turns')


def only_queued(monkeypatch):
    """Nothing but the queue holds a withdrawn message back: not the hour after it's taken back."""
    import fri.fri_turn_withdrawal as withdrawal
    monkeypatch.setattr(withdrawal, 'MARKED_SECONDS', 0)


def test_the_repro_a_note_saved_lately_from_an_old_message_is_held_back_once_its_deleted(monkeypatch, tmp_path):
    """The issue: the message is 100 days old, the note learned from it today. Deleted while the controller times out,
    nothing was queued, and the note reached the next request. It's held back, here and after a restart, until the
    controller takes the withdrawal."""
    async def scenario():
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        needs_sources(w)
        try:
            old = old_message()
            noted(w, old)
            assert LIKE in await asked(w)
            only_queued(monkeypatch)
            failing(w, SAVED)
            await delete(w, old, guild=123)
            assert LIKE not in await asked(w)
            assert str(old.id) in w.agent.saved_turns.pending
            w.agent.saved_turns = SavedTurns(w.agent)  # Restarted: held from disk.
            assert LIKE not in await asked(w)
            assert json.loads(on_disk(tmp_path))['pending'][str(old.id)].get('hold') is True
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_the_controller_takes_a_deletion_of_an_old_message_at_once(monkeypatch, tmp_path):
    """And drops the note, so nothing waits in the queue afterwards."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        needs_sources(w)
        try:
            old = old_message()
            noted(w, old)
            await delete(w, old, guild=123)
            assert [payload['message_ids'] for payload in calls(w, SAVED)] == [[str(old.id)]]
            assert not w.agent.saved_turns.pending
            only_queued(monkeypatch)
            assert LIKE not in await asked(w)  # Gone from the controller too.
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_its_entry_lasts_from_when_it_was_queued_not_from_the_message(monkeypatch, tmp_path):
    """Never shorter (fri #339): the old message's own 90 days are long over, the entry's aren't."""
    async def scenario():
        import fri.fri_turn_withdrawal as withdrawal
        w = await world(monkeypatch, tmp_path)
        needs_sources(w)
        try:
            old = old_message()
            noted(w, old)
            only_queued(monkeypatch)
            failing(w, SAVED)
            await delete(w, old, guild=123)
            turns = w.agent.saved_turns
            module_clock(monkeypatch, withdrawal.NOTED_SECONDS-DAY)
            turns.prune(withdrawal.time.time())
            assert str(old.id) in turns.pending
            module_clock(monkeypatch, 2*DAY)
            turns.prune(withdrawal.time.time())
            assert str(old.id) not in turns.pending
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_burst_past_the_queues_capacity_closes_as_before(monkeypatch, tmp_path):
    """Old deletions are rare, but nothing bounds them. While the controller can't take them, they wait like any;
    past the queue's capacity the oldest go and every saved context is held back until nothing they named can be kept,
    counted from when they were queued."""
    async def scenario():
        import fri.fri_turn_withdrawal as withdrawal
        w = await world(monkeypatch, tmp_path)
        needs_sources(w)
        try:
            monkeypatch.setattr(withdrawal, 'MOST', 3)
            failing(w, SAVED)
            olds = [old_message(days) for days in (200, 190, 180, 170, 160)]
            for message in olds:
                await delete(w, message, guild=123)
            turns = w.agent.saved_turns
            assert len(turns.pending) == 3 and turns.closed()
            assert turns.closed_until >= withdrawal.time.time()+withdrawal.NOTED_SECONDS-60  # From the queueing.
            assert (tmp_path/'withdrawals.closed.json').exists()
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_an_old_deletion_nothing_held_leaves_the_next_request_as_it_was(monkeypatch, tmp_path):
    """Asked about once, taken, gone: nothing pending, so nothing else changes."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        needs_sources(w)
        try:
            noted(w, w.said(LIKE))  # A note from a recent message, unrelated to the deletion.
            before = await asked(w)
            await delete(w, old_message(), guild=123)
            assert not w.agent.saved_turns.pending
            assert LIKE in await asked(w) and LIKE in before
        finally:
            await w.fri.close()
    asyncio.run(scenario())
