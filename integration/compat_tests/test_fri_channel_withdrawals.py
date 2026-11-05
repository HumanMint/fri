"""A channel or thread deleted in Discord takes what was saved from it (fri #358 D1).

Deleting a channel or thread deletes every message in it, with no event per message, so nothing Fri withdrew by message
ID reached what was saved from there: a server-profile note learned in a deleted channel still reached a later
private-panel request once the channel had left Fri's cache. Its deletion now queues the channel for the controller to
withdraw, with everything saved from it and from its threads, and every saved context is held back until it has. A
deletion Fri missed is found by a later read of what the controller holds: one Discord says is gone, not one Fri can no
longer see.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_member_profiles import (
    DM,
    GAMING,
    GENERAL,
    THREAD,
    model,
    note,
    run,
    world,
)
from fri.fri_agent import Agent
from fri.fri_chat import ChatResult


def remembered(context):
    return sorted(n['evidence'] for n in context['personal_memory']['notes'])


async def asked(w):
    """What a later private-panel request's model reads of the member's notes, past Fri's own last checks."""
    seen = []

    async def respond(text, context):
        seen.append(context)
        return ChatResult(status='completed', reply='bold of you')
    model(w, respond)
    await w.say(DM, 'fri what should i play tonight')
    return remembered(seen[-1])


def queue(w):
    return getattr(w.fri.agent, 'channel_withdrawals', None)


async def landed(w):
    """Whatever the deletion queued, sent: its own send, and the heartbeat's stage after it."""
    if (channels := queue(w)) is not None:
        if channels.sending is not None:
            await channels.sending
        await channels.settle()


async def deleted(w, cid, threads=()):
    """Channel `cid` deleted in Discord: gone from Fri's cache, its deletion event dispatched as discord.py does it,
    with the threads under it the cache still held (only those get an event of their own)."""
    channel = w.channels.pop(cid)
    channel.threads = [w.channels[t] for t in threads]
    if handler := getattr(w.fri, 'on_guild_channel_delete', None):
        await handler(channel)
    for thread in threads:
        await thread_deleted(w, thread, settle=False)
    await landed(w)


async def thread_deleted(w, tid, settle=True):
    thread = w.channels.pop(tid)
    if handler := getattr(w.fri, 'on_raw_thread_delete', None):
        await handler(SimpleNamespace(thread_id=tid, guild_id=123, parent_id=thread.parent_id, thread=thread,
                                      thread_type=thread.type))
    if settle:
        await landed(w)


def needs_channels(w):
    """A test whose deletion must reach the controller: against one without channel-withdrawals (a mixed pair with an
    older controller), Fri holds back instead, which the hold's own test covers."""
    if 'channel-withdrawals' not in w.fri.capabilities:
        pytest.skip('needs a controller offering channel-withdrawals')


def withdrawals(w):
    return [body for path, body in w.calls if path == '/chat/channels/withdraw']


def gone(code):
    return discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), {'code': code, 'message': 'Unknown'})


def test_a_deleted_channel_takes_the_profile_note_learned_there(monkeypatch, tmp_path):
    """The issue's repro: a server-profile note learned in #general, which is then deleted with no event per message,
    no longer reaches a later private-panel request. One learned in #gaming still does."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            await w.say(GENERAL, 'fri I like Minecraft', 'cubes', note('I like Minecraft'))
            await w.say(GAMING, 'fri I like chess', 'cool', note('I like chess'))
            assert await asked(w) == ['I like Minecraft', 'I like chess']
            await deleted(w, GENERAL)
            assert withdrawals(w) == [{'guild_id': '123', 'channel_ids': [str(GENERAL)]}]
            assert await asked(w) == ['I like chess']
        finally:
            await w.fri.close()
    run(scenario)


def test_a_deleted_thread_takes_what_was_learned_in_it(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            await w.say(THREAD, 'fri I like Minecraft', 'cubes', note('I like Minecraft'))
            await w.say(GENERAL, 'fri I like chess', 'cool', note('I like chess'))
            await thread_deleted(w, THREAD)
            assert withdrawals(w) == [{'guild_id': '123', 'channel_ids': [str(THREAD)]}]
            assert await asked(w) == ['I like chess']
        finally:
            await w.fri.close()
    run(scenario)


def test_a_deleted_channel_takes_its_threads_whether_fri_still_held_them_or_not(monkeypatch, tmp_path):
    """Discord deletes a channel's threads with it; discord.py sends an event only for those in its cache. An archived
    thread isn't: the controller knows which channel it's under from the turn saved there."""
    async def scenario():
        for cached in ((), (THREAD,)):
            w = world(monkeypatch, tmp_path/str(len(cached)))
            needs_channels(w)
            try:
                await w.say(THREAD, 'fri I like Minecraft', 'cubes', note('I like Minecraft'))
                await w.say(GAMING, 'fri I like chess', 'cool', note('I like chess'))
                await deleted(w, GENERAL, cached)
                assert await asked(w) == ['I like chess']
            finally:
                await w.fri.close()
    run(scenario)


def test_a_deletion_fri_missed_is_found_by_a_later_read(monkeypatch, tmp_path):
    """#gaming was deleted while Fri was down: no event, and it was never in the cache since. The heartbeat's read of
    what the controller holds finds it: Discord answers Unknown Channel for it, and the server's channel list doesn't
    have it. A thread whose read answers the same goes too."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            await w.say(GAMING, 'fri I like chess', 'cool', note('I like chess'))
            await w.say(THREAD, 'fri I like osu', 'cool', note('I like osu'))
            await w.say(GENERAL, 'fri I like Minecraft', 'cubes', note('I like Minecraft'))
            for cid in (GAMING, THREAD):
                w.channels.pop(cid)
            guild = w.fri.get_guild(123)
            guild.fetch_channels = AsyncMock(return_value=[w.channels[GENERAL]])
            guild.fetch_channel = AsyncMock(side_effect=gone(10003))
            await landed(w)
            assert sorted(body['channel_ids'][0] for body in withdrawals(w)) == [str(GAMING), str(THREAD)]
            assert await asked(w) == ['I like Minecraft']
        finally:
            await w.fri.close()
    run(scenario)


def test_a_permission_change_withdraws_nothing(monkeypatch, tmp_path):
    """#gaming is closed to everyone, and to Fri itself: nothing was deleted, so nothing is withdrawn. Fri still holds
    it in its cache; read again, Discord answers Missing Access, or Unknown Channel while the server's list still has
    it, and either way it stays."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await w.say(GAMING, 'fri I like chess', 'cool', note('I like chess'))
            w.readable[GAMING] = False
            gaming = w.channels[GAMING]
            if handler := getattr(w.fri, 'on_guild_channel_update', None):
                await handler(gaming, gaming)
            await landed(w)
            guild = w.fri.get_guild(123)
            guild.fetch_channels = AsyncMock(return_value=[w.channels[GENERAL], gaming])
            w.channels.pop(GAMING)  # Even were it to leave the cache.
            for answer in (discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'),
                                             {'code': 50001, 'message': 'Missing Access'}), gone(10003)):
                guild.fetch_channel = AsyncMock(side_effect=answer)
                if queue(w) is not None:
                    queue(w).swept = 0
                await landed(w)
            guild.fetch_channels = AsyncMock(return_value=[w.channels[GENERAL]])
            guild.fetch_channel = AsyncMock(side_effect=discord.Forbidden(
                SimpleNamespace(status=403, reason='Forbidden'), {'code': 50001, 'message': 'Missing Access'}))
            if queue(w) is not None:
                queue(w).swept = 0
            await landed(w)
            assert withdrawals(w) == []
            context = w.store.chat_context('123', 'private:20', '20', True)
            assert remembered(context) == ['I like chess']
        finally:
            await w.fri.close()
    run(scenario)


def test_without_a_controller_that_takes_channels_every_saved_context_is_held_back(monkeypatch, tmp_path):
    """A controller rolled back to one without channel-withdrawals: Fri can't have the channel withdrawn, so nothing
    saved reaches a request, across a restart too, until a controller that takes it is back."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await w.say(GENERAL, 'fri I like Minecraft', 'cubes', note('I like Minecraft'))
            await w.say(GAMING, 'fri I like chess', 'cool', note('I like chess'))
            offered = w.fri.capabilities
            w.fri.capabilities = offered - {'channel-withdrawals'}
            await deleted(w, GENERAL)
            assert withdrawals(w) == []
            assert await asked(w) == []
            # fri health says why: the channel queue, not an unreadable withdrawals record.
            assert w.fri.agent.channel_withdrawals.status()['holding'] is True
            assert w.fri.agent.saved_turns.status()['closed_until'] is None
            before = w.fri.agent
            agent = w.fri.agent = Agent(w.fri)  # Restarted: only what's on disk carries over.
            agent.state, agent.record = before.state, AsyncMock()
            await landed(w)
            assert await asked(w) == []
            if 'channel-withdrawals' not in offered:  # A mixed pair with an older controller: held back it stays.
                return
            w.fri.capabilities = offered
            await landed(w)
            assert withdrawals(w) == [{'guild_id': '123', 'channel_ids': [str(GENERAL)]}]
            assert await asked(w) == ['I like chess']
        finally:
            await w.fri.close()
    run(scenario)


def test_a_queue_that_cant_be_read_holds_everything_back_until_a_sweep(monkeypatch, tmp_path):
    """What it named would come back were it read as empty: until a sweep has read what the controller holds, and
    found nothing gone that isn't withdrawn, nothing saved reaches a request."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            await w.say(GENERAL, 'fri I like Minecraft', 'cubes', note('I like Minecraft'))
            (tmp_path/'channel-withdrawals.json').write_text('{"pending": {')
            before = w.fri.agent
            agent = w.fri.agent = Agent(w.fri)  # Restarted.
            agent.state, agent.record = before.state, AsyncMock()
            assert await asked(w) == []
            await landed(w)
            assert agent.channel_withdrawals.status() == {'pending': 0, 'holding': False, 'unreadable': False,
                                                          'swept_at': agent.channel_withdrawals.swept}
            assert await asked(w) == ['I like Minecraft']
        finally:
            await w.fri.close()
    run(scenario)


def test_a_deletion_in_another_server_queues_nothing(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await w.say(GENERAL, 'fri I like Minecraft', 'cubes', note('I like Minecraft'))
            elsewhere = SimpleNamespace(id=4242, guild=SimpleNamespace(id=777), threads=[])
            if handler := getattr(w.fri, 'on_guild_channel_delete', None):
                await handler(elsewhere)
            if handler := getattr(w.fri, 'on_raw_thread_delete', None):
                await handler(SimpleNamespace(thread_id=4243, guild_id=777, parent_id=4242, thread=None))
            await landed(w)
            assert withdrawals(w) == [] and await asked(w) == ['I like Minecraft']
        finally:
            await w.fri.close()
    run(scenario)


def test_a_sweep_names_the_channel_of_a_thread_saved_before_the_controller_kept_it(monkeypatch, tmp_path):
    """A thread whose turns an older controller saved: the sweep names its channel from the cache, so that channel's
    deletion takes it too, though no event comes for the thread."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            await w.say(THREAD, 'fri I like osu', 'cool', note('I like osu'))
            await w.say(GAMING, 'fri I like chess', 'cool', note('I like chess'))
            with w.store.connection() as db:
                db.execute('DELETE FROM chat_thread_parents')  # As an older controller saved it.
            await landed(w)
            with w.store.connection() as db:
                assert [tuple(row) for row in db.execute('SELECT thread_id,parent_id FROM chat_thread_parents')] == [
                    (str(THREAD), str(GENERAL))]
            await deleted(w, GENERAL)
            assert await asked(w) == ['I like chess']
        finally:
            await w.fri.close()
    run(scenario)
