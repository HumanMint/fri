"""A channel or thread deleted in Discord takes what was saved from it (fri #358 D1).

Deleting a channel or thread deletes every message in it, with no event per message, so nothing Fri withdrew by message
ID reached what was saved from there: a server-profile note learned in a deleted channel still reached a later
private-panel request once the channel had left Fri's cache. Its deletion now queues the channel for the controller to
withdraw, with everything saved from it and from its threads, and every saved context is held back until it has. A
deletion Fri missed is found by a later read of what the controller holds: one Discord says is gone, not one Fri can no
longer see.
"""
import time
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


def rolled_back(w):
    """The controller, as one without channel-withdrawals: Fri reads it so, its contract read again too. Returns
    what puts it back."""
    real = w.fri.api
    offered = w.fri.capabilities

    async def api(method, path, payload=None):
        answer = await real(method, path, payload)
        if path == '/fri/contract':
            answer = {**answer, 'capabilities': [c for c in answer['capabilities'] if c != 'channel-withdrawals']}
        return answer
    w.fri.api, w.fri.capabilities = api, offered - {'channel-withdrawals'}

    def back():
        w.fri.api, w.fri.capabilities = real, offered
    return back


def restarted(w):
    """Fri started again on the same disk: only what's on it carries over."""
    before = w.fri.agent
    agent = w.fri.agent = Agent(w.fri)
    agent.state, agent.record = before.state, AsyncMock()
    return agent


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
            guild.fetch_channels = AsyncMock(return_value=[w.channels[GENERAL]])  # Not even in the list now.
            for answer in (discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'),
                                             {'code': 50001, 'message': 'Missing Access'}), gone(10004)):
                guild.fetch_channel = AsyncMock(side_effect=answer)  # Not Unknown Channel: nothing says it's gone.
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
            back = rolled_back(w)
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
            back()
            agent.saved_turns.reread_at = float('-inf')  # Its contract read again now, not a minute on.
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
            assert agent.channel_withdrawals.status() == {'pending': 0, 'holding': False, 'recovering': False,
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


def test_a_copy_read_before_the_channel_went_never_reaches_the_reply_model(monkeypatch, tmp_path):
    """Sol round 1 of #372, F1: a private request's context, read from the controller just before #general is deleted
    and withdrawn, goes on once the withdrawal has landed. The messages the controller says it took are taken back here
    too, so that copy's note from #general is held back, as a deleted message's is; and My memory reads again."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            await w.say(GENERAL, 'fri I like Minecraft', 'cubes', note('I like Minecraft'))
            await w.say(GAMING, 'fri I like chess', 'cool', note('I like chess'))
            from fri.fri_memory_panel import withdrawals as screening
            before = screening(w.fri)
            real, paused = w.fri.api, []

            async def api(method, path, payload=None):
                answer = await real(method, path, payload)
                if path == '/chat/context' and str((payload or {}).get('conversation_id', '')).startswith('private'):
                    if not paused:
                        paused.append(path)
                        await deleted(w, GENERAL)  # While this copy waits to be used.
                return answer
            w.fri.api = api
            assert await asked(w) == ['I like chess'] and paused
            assert screening(w.fri) != before
        finally:
            await w.fri.close()
    run(scenario)


def test_a_queue_recovering_stays_so_across_a_restart_whatever_is_written_since(monkeypatch, tmp_path):
    """Sol round 1 of #372, F2: a queue that couldn't be read, then another deletion, sent and taken: the queue written
    then still says it's recovering, so a restart before any sweep holds everything back."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            await w.say(GAMING, 'fri I like chess', 'cool', note('I like chess'))
            (tmp_path/'channel-withdrawals.json').write_text('{"pending": {')
            restarted(w)
            thread = w.channels.pop(THREAD)
            await w.fri.on_raw_thread_delete(SimpleNamespace(thread_id=THREAD, guild_id=123, parent_id=GENERAL,
                                                             thread=thread))
            await queue(w).sending  # Sent and taken; no sweep yet.
            assert withdrawals(w) == [{'guild_id': '123', 'channel_ids': [str(THREAD)]}]
            restarted(w)
            assert queue(w).holding() and await asked(w) == []
        finally:
            await w.fri.close()
    run(scenario)


def test_a_deletion_whose_queue_could_not_be_written_still_holds_after_a_restart(monkeypatch, tmp_path):
    """Sol round 1 of #372, F2: the disk is full when #general's deletion is queued, and the controller doesn't take
    channels yet. A restart can't read it from the queue, but finds the queue wasn't written: everything stays held back
    until a sweep has found what's gone."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await w.say(GENERAL, 'fri I like Minecraft', 'cubes', note('I like Minecraft'))
            back = rolled_back(w)
            assert not queue(w).holding()  # Read, and written down, once before.
            import fri.fri_channel_withdrawal as channels

            def full(path, data):
                raise OSError(28, 'No space left on device')
            monkeypatch.setattr(channels, 'private_json', full)
            await deleted(w, GENERAL)
            monkeypatch.undo()
            restarted(w)
            assert queue(w).holding() and await asked(w) == []
            back()
        finally:
            await w.fri.close()
    run(scenario)


def test_recovery_waits_for_every_read_to_be_answered(monkeypatch, tmp_path):
    """Sol round 1 of #372, F3: recovering, a sweep whose read of a channel times out, with the server's channel list
    coming back empty, hasn't found what's gone: everything stays held back."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            await w.say(GAMING, 'fri I like chess', 'cool', note('I like chess'))
            w.channels.pop(GAMING)
            (tmp_path/'channel-withdrawals.json').write_text('{"pending": {')
            agent = restarted(w)
            guild = w.fri.get_guild(123)
            guild.fetch_channels = AsyncMock(return_value=[])
            guild.fetch_channel = AsyncMock(side_effect=TimeoutError())
            await landed(w)
            assert agent.channel_withdrawals.holding() and withdrawals(w) == []
        finally:
            await w.fri.close()
    run(scenario)


def test_every_held_channel_is_read_in_turn(monkeypatch, tmp_path):
    """Sol round 1 of #372, F4: 26 channels the controller holds something from, none in the cache: the first 25 answer
    Missing Access every time, the last Unknown Channel. Sweeps take turns through them, so the last is read, and
    withdrawn, too."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            ids = [str(1000+i) for i in range(1, 27)]
            with w.store.connection() as db:
                for cid in ids:
                    db.execute('INSERT INTO chat_turns(guild_id,channel_id,user_id,input,reply,model,effort,at) '
                               "VALUES ('123',?,'20','hi','hey','model','high',strftime('%s','now'))", ('public:'+cid,))
            guild = w.fri.get_guild(123)
            guild.fetch_channels = AsyncMock(return_value=[w.channels[GENERAL]])

            async def fetch(cid):
                if str(cid) == ids[-1]:
                    raise gone(10003)
                raise discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'),
                                        {'code': 50001, 'message': 'Missing Access'})
            guild.fetch_channel = AsyncMock(side_effect=fetch)
            for _ in range(3):
                queue(w).swept = 0
                await landed(w)
            assert [body['channel_ids'] for body in withdrawals(w)] == [[ids[-1]]]
        finally:
            await w.fri.close()
    run(scenario)


def test_a_queued_time_that_isnt_a_time_never_holds_forever(monkeypatch, tmp_path):
    """Sol round 1 of #372, F5: a queue whose time is not a number, infinite, or far ahead counts from now, so it ends
    when one queued now would."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            for when in ('NaN', 'Infinity', '1e300'):
                (tmp_path/'channel-withdrawals.json').write_text(
                    '{"pending": {"5": {"guild_id": "123", "threads": [], "queued_at": %s}}}' % when)
                agent = restarted(w)
                at = agent.channel_withdrawals.pending['5']['queued_at']
                assert at == at and abs(at - time.time()) < 60, when
        finally:
            await w.fri.close()
    run(scenario)


def test_a_controller_that_offers_channels_again_is_found_while_the_queue_holds(monkeypatch, tmp_path):
    """Sol round 1 of #372, F6: Fri read the controller without channel-withdrawals; it's offered again, with nothing
    restarting. While the queue holds, the heartbeat reads its contract again (at most a minute apart), and sends."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            await w.say(GENERAL, 'fri I like Minecraft', 'cubes', note('I like Minecraft'))
            await w.say(GAMING, 'fri I like chess', 'cool', note('I like chess'))
            w.fri.capabilities = w.fri.capabilities - {'channel-withdrawals'}  # As read before; offered now.
            await deleted(w, GENERAL)
            w.fri.agent.saved_turns.reread_at = float('-inf')
            await landed(w)
            assert withdrawals(w) == [{'guild_id': '123', 'channel_ids': [str(GENERAL)]}]
            assert await asked(w) == ['I like chess']
        finally:
            await w.fri.close()
    run(scenario)


def test_my_memory_reads_again_though_the_controller_names_no_message(monkeypatch, tmp_path):
    """Sol round 1 of #372, F1: a note learned in #general before notes named their messages, so the withdrawal can
    name none. My memory still reads again: the withdrawal moves the marks' generation itself."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            with w.store.connection() as db:
                db.execute("INSERT INTO personal_notes(guild_id,channel_id,user_id,category,evidence,at,origin,provenance) "
                           "VALUES ('123','server','20','games','I like Minecraft',strftime('%s','now'),'public:1','[]')")
            from fri.fri_memory_panel import withdrawals as screening
            before = screening(w.fri)
            await deleted(w, GENERAL)
            assert withdrawals(w) and screening(w.fri) != before
        finally:
            await w.fri.close()
    run(scenario)


def test_recovery_never_trusts_an_empty_channel_list_or_an_unanswered_read(monkeypatch, tmp_path):
    """Sol round 1 of #372, F3, each alone: recovering, a server channel list that comes back empty while the cache
    holds channels says nothing, even with Discord answering Missing Access for the one channel read; and with a list,
    a read that times out is no answer. Everything stays held back either way."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            await w.say(GAMING, 'fri I like chess', 'cool', note('I like chess'))
            w.channels.pop(GAMING)
            guild = w.fri.get_guild(123)
            for listed, answer in (([], discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'),
                                                          {'code': 50001, 'message': 'Missing Access'})),
                                   ([w.channels[GENERAL]], TimeoutError())):
                (tmp_path/'channel-withdrawals.json').write_text('{"pending": {')
                agent = restarted(w)
                guild.fetch_channels = AsyncMock(return_value=listed)
                guild.fetch_channel = AsyncMock(side_effect=answer)
                await landed(w)
                assert agent.channel_withdrawals.holding() and withdrawals(w) == [], listed
        finally:
            await w.fri.close()
    run(scenario)


def test_recovery_needs_every_uncached_channel_actually_read(monkeypatch, tmp_path):
    """Sol round 2 of #372, B2: recovering, 26 held channels none of which the cache holds, the first 25 answering
    Missing Access and the 26th deleted. One sweep reads 25: the 26th, never read, isn't answered, so recovery goes on;
    the next sweep reads it, finds it gone, and only then does recovery end."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            ids = [str(1000+i) for i in range(1, 27)]
            with w.store.connection() as db:
                for cid in ids:
                    db.execute('INSERT INTO chat_turns(guild_id,channel_id,user_id,input,reply,model,effort,at) '
                               "VALUES ('123',?,'20','hi','hey','model','high',strftime('%s','now'))", ('public:'+cid,))
            (tmp_path/'channel-withdrawals.json').write_text('{"pending": {')
            agent = restarted(w)
            guild = w.fri.get_guild(123)
            guild.fetch_channels = AsyncMock(return_value=[w.channels[GENERAL]])

            async def fetch(cid):
                if str(cid) == ids[-1]:
                    raise gone(10003)
                raise discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'),
                                        {'code': 50001, 'message': 'Missing Access'})
            guild.fetch_channel = AsyncMock(side_effect=fetch)
            await landed(w)
            assert agent.channel_withdrawals.holding() and withdrawals(w) == []
            await landed(w)
            assert [body['channel_ids'] for body in withdrawals(w)] == [[ids[-1]]]
            assert not agent.channel_withdrawals.holding()
        finally:
            await w.fri.close()
    run(scenario)


def paused_private_context(w, during):
    """A private request's context, read from the controller, then `during()` before it's used."""
    real, paused = w.fri.api, []

    async def api(method, path, payload=None):
        answer = await real(method, path, payload)
        if path == '/chat/context' and str((payload or {}).get('conversation_id', '')).startswith('private'):
            if not paused:
                paused.append(path)
                await during()
        return answer
    w.fri.api = api
    return paused


def test_a_copy_of_a_note_with_no_message_named_never_reaches_the_reply_model(monkeypatch, tmp_path):
    """Sol round 2 of #372, B3: a profile note learned in #general before notes named their messages. A copy read before
    #general's withdrawal names no message to hold it back by; the withdrawal itself moves what the request's last check
    compares, so the copy goes."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            with w.store.connection() as db:
                db.execute("INSERT INTO personal_notes(guild_id,channel_id,user_id,category,evidence,at,origin,provenance) "
                           "VALUES ('123','server','20','games','I like Minecraft',strftime('%s','now'),'public:1','[]')")
            await w.say(GAMING, 'fri I like chess', 'cool', note('I like chess'))

            async def gone_now():
                await deleted(w, GENERAL)
            paused = paused_private_context(w, gone_now)
            assert await asked(w) == [] and paused  # Every saved context of that copy goes.
            assert await asked(w) == ['I like chess']  # Read again after: as the controller has it now.
        finally:
            await w.fri.close()
    run(scenario)


def test_a_copy_read_before_a_withdrawal_whose_first_answer_was_lost_never_reaches_the_reply_model(monkeypatch,
                                                                                                    tmp_path):
    """Sol round 2 of #372, B3: the controller took #general, but its answer was lost; asked again, it names nothing
    (it already took it all). Accepting that empty answer still moves what the request's last check compares."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        needs_channels(w)
        try:
            await w.say(GENERAL, 'fri I like Minecraft', 'cubes', note('I like Minecraft'))
            await w.say(GAMING, 'fri I like chess', 'cool', note('I like chess'))
            from fri.fri_errors import ControllerUnavailable
            real, lost = w.fri.api, []

            async def api(method, path, payload=None):
                answer = await real(method, path, payload)
                if path == '/chat/channels/withdraw' and not lost:
                    lost.append(answer)
                    raise ControllerUnavailable('its answer was lost')
                return answer
            w.fri.api = api

            async def gone_now():
                await deleted(w, GENERAL)
            paused_private_context(w, gone_now)
            assert await asked(w) == [] and lost and len(withdrawals_sent(lost)) == 1
        finally:
            await w.fri.close()
    run(scenario)


def withdrawals_sent(lost):
    return [answer for answer in lost if answer.get('ok')]


def test_a_queue_write_cut_short_before_it_lands_holds_after_a_restart(monkeypatch, tmp_path):
    """Sol round 2 of #372, B4: an install whose queue and marker were written; a deletion is queued and Fri stops
    before the new queue replaces the old one. The old marker no longer vouches for what's on disk, so a restart
    holds everything back until a sweep has found what's gone."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await w.say(GENERAL, 'fri I like Minecraft', 'cubes', note('I like Minecraft'))
            back = rolled_back(w)
            assert not queue(w).holding()  # Its empty queue and marker written once before.

            def stopped(*args):
                raise SystemExit('stopped before the new queue landed')
            monkeypatch.setattr('fri.private_files.os.replace', stopped)
            w.channels.pop(GENERAL)
            with pytest.raises(SystemExit):
                queue(w).withdraw(123, GENERAL)
            monkeypatch.undo()
            agent = restarted(w)
            assert agent.channel_withdrawals.holding() and await asked(w) == []
            back()
        finally:
            await w.fri.close()
    run(scenario)


def test_fri_health_says_when_saved_context_waits_for_deleted_channels(monkeypatch, tmp_path):
    """Sol round 2 of #372, S1: the owner's fri health names a hold for the channel queue, and how far recovery got."""
    from fri.fri_watch import Watch

    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await w.say(GENERAL, 'fri I like Minecraft', 'cubes', note('I like Minecraft'))
            back = rolled_back(w)
            await deleted(w, GENERAL)
            report = await Watch(w.fri).report()
            line = next(line for line in report.splitlines() if 'deleted channel' in line)
            assert '1 waits' in line and "doesn't take channels" in line
            back()
            (tmp_path/'channel-withdrawals.json').write_text('{"pending": {')
            restarted(w)
            report = await Watch(w.fri).report()
            line = next(line for line in report.splitlines() if 'deleted channel' in line)
            assert 'recovering' in line
        finally:
            await w.fri.close()
    run(scenario)
