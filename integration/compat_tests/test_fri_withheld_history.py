"""fri #215: saved context from a member's withdrawn message never reaches a request again, even when the withdrawal
lands while a turn is under way, or fails and has to be sent again.

Against the real controller.
1. A turn that already read the exchange drops it from its request if the message is taken back first, and withholds
   its answer, or keeps nothing of it, if it's taken back after.
2. A withdrawal that fails stays pending (on disk, so a restart keeps it) and is sent again; until the controller
   takes it, what holds the message stays out of every request.
3. Withdrawals by ID go one call at a time, ten IDs a call, each ID once, however many events ask.
Also gene #141's Fri half, from a controller offering withdrawal-sources: a note or a reply of Fri's in the channel's
view that a withdrawn message taught or reached stays out too.
"""
import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_saved_withdrawal import SAVED, edit, edited, long_after, restarted
from compat_tests.test_fri_turn_withdrawal import MARKER, WITHDRAW, saved, settled, world

SOURCES = 'withdrawal-sources'
LIKE = 'i like lavender bicycles'


def needs_sources(w):
    if SOURCES not in w.fri.capabilities:
        pytest.skip('needs a controller offering '+SOURCES)


async def delete(w, message, guild=None):
    await w.fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=message.id,
                                                      **({'guild_id': guild} if guild else {})))
    await settled(w)


def during(w, path, action, *, after=True, times=1):
    """Run `action` once a call to `path` is made: after it answers (after=True) or before it's sent."""
    api, left = w.fri.api, [times]
    async def calling(method, called, payload=None):
        if called == path and left[0] and not after:
            left[0] -= 1
            await action()
        result = await api(method, called, payload)
        if called == path and left[0] and after:
            left[0] -= 1
            await action()
        return result
    w.fri.api = calling
    return api


async def earlier_exchange(w, text='fri explain '+MARKER):
    earlier = w.said(text)
    await w.agent.handle(earlier)
    assert MARKER in saved(w) or LIKE in saved(w)
    long_after(w)  # Out of the live view: only the saved exchange holds its words now.
    w.requests.clear()
    return earlier


def test_taken_back_while_a_turn_prepares_its_request_stays_out_of_it(monkeypatch, tmp_path):
    """The issue's repro: the next turn read the exchange, then its message went before the request did."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = await earlier_exchange(w)
            later = w.said('fri and lunch?')
            during(w, '/chat/turn/check', lambda: delete(w, earlier))  # The check right before the request.
            await w.agent.handle(later)
            assert w.requests and MARKER not in str(w.requests)
            later.reply.assert_awaited()  # Answered without it, not refused.
            assert later.reply.await_args.args[0] == 'okay'
            assert MARKER not in saved(w) and 'fri and lunch?' in saved(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_taken_back_after_the_request_withholds_the_answer(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = await earlier_exchange(w)
            later = w.said('fri and lunch?')
            original = w.fri.chat.transport.handler
            async def deleting_during_response(request):
                response = original(request)
                await delete(w, earlier)
                return response
            w.fri.chat.transport.handler = deleting_during_response
            await w.agent.handle(later)
            assert MARKER in str(w.requests)  # The request had it: what came back may repeat it.
            assert all(call.args[0] != 'okay' for call in later.reply.await_args_list)
            assert 'fri and lunch?' not in saved(w) and MARKER not in saved(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('landed', [False, True])
def test_taken_back_while_the_save_is_on_its_way_keeps_nothing_of_it(monkeypatch, tmp_path, landed):
    """Before the save lands, the controller keeps nothing; after, its reply keeps only a breadcrumb (gene #141)."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        needs_sources(w)
        try:
            earlier = await earlier_exchange(w)
            later = w.said('fri and lunch?')
            during(w, '/chat/turn', lambda: delete(w, earlier), after=landed)
            await w.agent.handle(later)
            assert later.reply.await_args.args[0] == 'okay'  # Delivered before the delete.
            turns = w.store.chat_context('123', 'public:1', '20')['turns']
            assert MARKER not in str(turns)
            assert [turn['reply'] for turn in turns if turn['input'] == 'fri and lunch?'] == (
                ['Earlier answer drawing on a message since edited or deleted. Nothing from it is retained here.']
                if landed else [])
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def failing(w, path):
    api, state = w.fri.api, {'down': True}
    async def calling(method, called, payload=None):
        if called == path and state['down']:
            raise TimeoutError()
        return await api(method, called, payload)
    w.fri.api = calling
    return state


def test_a_failed_withdrawal_keeps_its_exchange_out_and_is_sent_again(monkeypatch, tmp_path, caplog):
    """The issue's repro: the withdrawal times out, the controller comes back, an unrelated message follows."""
    caplog.set_level(logging.DEBUG)
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = await earlier_exchange(w)
            state = failing(w, WITHDRAW)
            await delete(w, earlier)
            assert MARKER in saved(w)  # The controller never got it.
            state['down'] = False
            later = w.said('fri hi')
            await w.agent.handle(later)
            assert w.requests and MARKER not in str(w.requests)  # Kept out until it lands.
            w.agent.saved_turns.retry()
            await settled(w)
            assert MARKER not in saved(w) and not w.agent.saved_turns.pending
            assert MARKER not in caplog.text
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_pending_withdrawal_survives_a_restart(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = await earlier_exchange(w)
            state = failing(w, WITHDRAW)
            await delete(w, earlier)
            kept = (tmp_path/'withdrawals.json').read_text()
            assert str(earlier.id) in kept and MARKER not in kept  # IDs and where, never words.
            from fri.fri_turn_withdrawal import SavedTurns
            w.agent.saved_turns = SavedTurns(w.agent)  # Started again: what it saved this run is forgotten.
            state['down'] = False
            await w.agent.handle(w.said('fri hi'))
            assert w.requests and MARKER not in str(w.requests)
            w.agent.saved_turns.retry()  # As a start does.
            await settled(w)
            assert MARKER not in saved(w) and not json.loads((tmp_path/'withdrawals.json').read_text())['pending']
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_failed_withdrawal_by_id_is_sent_again(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        if 'saved-turn-withdrawal' not in w.fri.capabilities:
            pytest.skip('needs a controller offering saved-turn-withdrawal')
        try:
            earlier = await earlier_exchange(w)
            restarted(w)
            state = failing(w, SAVED)
            await edit(w, edited(earlier))
            state['down'] = False
            await w.agent.handle(w.said('fri hi'))
            assert w.requests and MARKER not in str(w.requests)
            w.agent.saved_turns.retry()
            await settled(w)
            assert MARKER not in saved(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_withdrawals_by_id_go_one_call_at_a_time_ten_ids_a_call_each_once(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        if 'saved-turn-withdrawal' not in w.fri.capabilities:
            pytest.skip('needs a controller offering saved-turn-withdrawal')
        api, gate, calls, flying = w.fri.api, asyncio.Event(), [], [0, 0]
        async def slow(method, path, payload=None):
            if path == SAVED:
                calls.append(list(payload['message_ids']))
                flying[0] += 1
                flying[1] = max(flying)
                await gate.wait()
                flying[0] -= 1
            return await api(method, path, payload)
        w.fri.api = slow
        try:
            first = w.said('one of many').id
            for i in range(30):  # Separate events, each a delete of one message, five of them twice.
                await w.fri.on_raw_message_delete(SimpleNamespace(channel_id=1, guild_id=123,
                                                                  message_id=first+(i % 25)))
            await asyncio.sleep(0)
            gate.set()
            await settled(w)
            assert flying[1] == 1  # Never two at once.
            assert sorted(i for call in calls for i in call) == sorted(str(first+i) for i in range(25))
            assert all(len(call) <= 10 for call in calls) and len(calls) <= 4
        finally:
            await w.fri.close()
    asyncio.run(scenario())


# gene #141's Fri half.

def test_a_note_from_a_withdrawn_message_stays_out_while_its_withdrawal_is_pending(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        needs_sources(w)
        try:
            said = w.said(LIKE)
            source = {'kind': 'discord_message', 'id': str(said.id), 'user_id': '20', 'channel_id': '1',
                      'guild_id': '123', 'display_name': 'member'}
            w.store.save_chat('123', 'public:1', '20', 0, LIKE, 'okay', 'model', 'high', source=source,
                              provenance=[], notes=[{'category': 'hobbies', 'evidence': LIKE}])
            with w.store.connection() as db:  # Its exchange trimmed since: only the note holds the words.
                db.execute('DELETE FROM chat_turns')
            restarted(w)
            state = failing(w, SAVED)
            await edit(w, edited(said))
            state['down'] = False
            await w.agent.handle(w.said('fri what do i like'))
            assert w.requests and LIKE not in str(w.requests)
            w.agent.saved_turns.retry()
            await settled(w)
            assert LIKE not in str(w.store.chat_context('123', 'public:1', '20')['personal_memory'])
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_reply_in_view_that_carried_a_withdrawn_message_leaves_the_view(monkeypatch, tmp_path):
    """Fri's reply to another member, in the channel's view, carried the message: once it's taken back, neither the
    message nor that reply reaches the next request (and the saved reply keeps a breadcrumb, see gene #141)."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        needs_sources(w)
        original = w.fri.chat.transport.handler
        try:
            nearby = w.said('my secret plan is '+MARKER)
            w.agent.recent[1].append({'id': str(nearby.id), 'user_id': '20', 'name': 'member', 'text': nearby.content,
                                      'at': discord.utils.utcnow().timestamp(), 'revision': 0, 'gene_error': False})
            asked = w.said('fri what did they say?', user=30)
            asked.reply.return_value = SimpleNamespace(id=asked.id+100, content='they said '+MARKER, edit=AsyncMock())
            await w.agent.handle(asked)
            assert any(row['text'] == 'they said '+MARKER for row in w.agent.recent[1])  # Echoed into the view.
            await delete(w, nearby, guild=123)
            w.fri.chat.transport.handler = original
            w.requests.clear()
            await w.agent.handle(w.said('fri sup', user=40))
            assert w.requests and MARKER not in str(w.requests)
            assert MARKER not in saved(w, '30')
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_an_ordinary_turn_asks_and_sends_nothing_new_to_an_older_controller(monkeypatch, tmp_path):
    """Guard: without withdrawal-sources, chat context is asked for as before and turns carry no drew_on."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        w.fri.capabilities = w.fri.capabilities-{SOURCES}
        try:
            await w.agent.handle(w.said('fri hi'))
            contexts = [payload for path, payload in w.calls if path == '/chat/context']
            turns = [payload for path, payload in w.calls if path in ('/chat/turn', '/chat/turn/check')]
            assert contexts and all('sources' not in payload for payload in contexts)
            assert turns and all('drew_on' not in payload for payload in turns)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_pending_withdrawals_hold_no_words_and_tolerate_a_damaged_record(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async def scenario():
        (tmp_path/'withdrawals.json').write_text('{not json')
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = await earlier_exchange(w)
            await delete(w, earlier)
            assert MARKER not in saved(w)
            assert MARKER not in (tmp_path/'withdrawals.json').read_text()
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_message_old_enough_only_for_a_note_is_asked_about_by_a_capable_controller(monkeypatch, tmp_path):
    """A note lasts 90 days, an exchange a week: past the week, only a controller that drops notes with their message
    is asked."""
    async def scenario():
        import datetime as dt
        w = await world(monkeypatch, tmp_path)
        if 'saved-turn-withdrawal' not in w.fri.capabilities:
            pytest.skip('needs a controller offering saved-turn-withdrawal')
        try:
            old = discord.utils.time_snowflake(discord.utils.utcnow()-dt.timedelta(days=30))
            await w.fri.on_raw_message_delete(SimpleNamespace(channel_id=1, guild_id=123, message_id=old))
            await settled(w)
            asked = [payload['message_ids'] for path, payload in w.calls if path == SAVED]
            assert asked == ([[str(old)]] if SOURCES in w.fri.capabilities else [])
        finally:
            await w.fri.close()
    asyncio.run(scenario())
