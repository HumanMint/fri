"""fri #129: a member's message withdrawn after Fri saved their exchange stops coming back as history.

Against the real controller. One that offers turn-withdrawal drops the exchange; an older one runs Fri as before.
"""
import asyncio
import copy
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_unsend import no_debounce
from compat_tests.test_fri_withdrawn_rows import model as real_model
from fri.fri_turn_withdrawal import CAPABILITY

MARKER = 'synthetic-private-marker'
WITHDRAW = '/chat/turn/withdraw'


async def world(monkeypatch, tmp_path, capable=True):
    fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
    no_debounce(monkeypatch)
    fri.data = tmp_path
    offered = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
    assert CAPABILITY in offered
    fri.capabilities = offered if capable else offered-{CAPABILITY}
    requests = []
    real_model(fri, requests)
    fri.chat_once = type(fri).chat_once.__get__(fri)

    def said(text, user=20):
        event = message(text, user=user)
        event.reply.return_value = SimpleNamespace(id=event.id+100, content='okay', edit=AsyncMock())
        return event
    return SimpleNamespace(fri=fri, agent=agent, store=store, channel=channel, said=said, calls=calls, rows=rows,
                           requests=requests)


def saved(w, user='20'):
    return str(w.store.chat_context('123', 'public:1', user)['turns'])


async def settled(w):
    await asyncio.gather(*w.agent.saved_turns.tasks)


async def edited_while_another_turn_carries_it(w, earlier):
    """The probe: a reply to the message, which its author edits while Fri answers that reply."""
    intermediate = w.said('fri explain that caption?', user=30)
    intermediate.reference = SimpleNamespace(message_id=earlier.id, channel_id=1)
    original = w.fri.chat.transport.handler
    async def editing_during_response(request):
        response = original(request)
        current = copy.copy(earlier)
        current.content, current.edited_at = 'current safe caption', discord.utils.utcnow()
        w.rows[earlier.id] = current
        await w.fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=current))
        return response
    w.fri.chat.transport.handler = editing_during_response
    await w.agent.handle(intermediate)
    w.fri.chat.transport.handler = original
    assert str(earlier.id) in w.agent.withdrawn or earlier.id in w.agent.revised


async def later_unrelated_request(w):
    await settled(w)
    w.requests.clear()
    await w.agent.handle(w.said('fri hi'))
    assert w.requests
    return str(w.requests)


def test_a_withdrawn_messages_saved_turn_stays_out_of_a_later_unrelated_request(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = w.said('fri explain '+MARKER)
            await w.agent.handle(earlier)
            assert MARKER in saved(w)
            await edited_while_another_turn_carries_it(w, earlier)
            assert MARKER not in await later_unrelated_request(w)
            assert MARKER not in saved(w)
            assert [p for p, _ in w.calls].count(WITHDRAW) == 1  # Once, though both an edit and a revise saw it.
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_deleted_messages_saved_turn_goes_too(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = w.said('fri explain '+MARKER)
            await w.agent.handle(earlier)
            await w.agent.handle(w.said('fri and lunch?'))
            del w.rows[earlier.id]
            await w.fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=earlier.id))
            assert MARKER not in await later_unrelated_request(w)
            assert 'fri and lunch?' in saved(w)  # Only the exchange holding it.
            await w.fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=earlier.id+5000))
            await w.fri.on_raw_bulk_message_delete(SimpleNamespace(channel_id=1, message_ids={earlier.id}))
            await settled(w)
            assert [p for p, _ in w.calls].count(WITHDRAW) == 1  # Never for a message it saved nothing for.
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_withdrawn_while_its_save_was_on_the_way(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        api = w.fri.api
        earlier = w.said('fri explain '+MARKER)
        async def saving(method, path, payload=None):
            result = await api(method, path, payload)
            if path == '/chat/turn':  # Landed; the edit comes in before Fri hears back.
                w.agent.withdraw(earlier)
            return result
        w.fri.api = saving
        try:
            await w.agent.handle(earlier)
            w.fri.api = api
            assert MARKER not in await later_unrelated_request(w)
            assert MARKER not in saved(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_withdrawn_after_a_save_whose_reply_was_lost(monkeypatch, tmp_path):
    """Sol r1 of fri #143: the save landed, but Fri lost the reply (the API reports the controller unreachable)."""
    async def scenario():
        from fri.fri_errors import ControllerUnavailable
        w = await world(monkeypatch, tmp_path)
        api = w.fri.api
        earlier = w.said('fri explain '+MARKER)
        async def saving(method, path, payload=None):
            result = await api(method, path, payload)
            if path == '/chat/turn':  # Landed, then the connection dropped before the reply.
                raise ControllerUnavailable('controller unreachable')
            return result
        w.fri.api = saving
        try:
            await w.agent.handle(earlier)
            w.fri.api = api
            w.agent.withdraw(earlier)
            await asyncio.sleep(0)
            for _ in range(5):  # The withdrawal call runs in the background.
                await asyncio.sleep(0.01)
            assert MARKER not in await later_unrelated_request(w)
            assert MARKER not in saved(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_an_older_controller_runs_as_before(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path, capable=False)
        try:
            earlier = w.said('fri explain '+MARKER)
            await w.agent.handle(earlier)
            await edited_while_another_turn_carries_it(w, earlier)
            await w.fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=earlier.id))
            await later_unrelated_request(w)
            assert WITHDRAW not in [p for p, _ in w.calls] and not w.agent.saved_turns.saved
            assert MARKER in saved(w)  # What an older controller keeps, as before.
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_failed_withdrawal_is_logged_without_words_and_the_turn_goes_on(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        api = w.fri.api
        async def failing(method, path, payload=None):
            if path == WITHDRAW:
                raise TimeoutError()
            return await api(method, path, payload)
        w.fri.api = failing
        try:
            earlier = w.said('fri explain '+MARKER)
            await w.agent.handle(earlier)
            await w.fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=earlier.id))
            later = w.said('fri hi')
            await settled(w)
            await w.agent.handle(later)
            later.reply.assert_awaited()
            assert any('not dropped' in line and 'TimeoutError' in line for line in caplog.messages)
            assert MARKER not in caplog.text
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('capable', [True, False])
def test_the_panel_and_mentions_ask_nothing(capable):
    from fri.fri_turn_withdrawal import SavedTurns
    agent = SimpleNamespace(fri=SimpleNamespace(capabilities=frozenset({CAPABILITY} if capable else ())),
                            withdrawn={}, revised={}, unsent={})
    turns = SavedTurns(agent)
    scope = {'guild_id': '123', 'channel_id': '1', 'conversation_id': 'public:1', 'user_id': '20'}
    turns.kept({**scope, 'source': {'kind': 'discord_interaction', 'id': '5'}}, {'ok': True}, (0, 0))
    turns.kept({**scope, 'source': {'kind': 'discord_message', 'id': '6'}, 'mention_grant': 'a'*64}, {'ok': True}, (0, 0))
    turns.kept({**scope, 'source': {'kind': 'discord_message', 'id': '7'}}, {'ok': True, 'kept': False}, (0, 0))
    assert not turns.saved


@pytest.mark.parametrize('how', ['edit', 'delete'])
def test_the_issues_scenario_a_message_taken_back_after_its_turn_was_saved(monkeypatch, tmp_path, how):
    """fri #129 as reported: the member's message was answered and its exchange saved, nothing was handling or
    carrying it any more, and then its author edited or deleted it. A later unrelated turn's request, and the
    controller's copy of their exchanges, no longer hold the old words."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = w.said('fri explain '+MARKER)
            await w.agent.handle(earlier)
            await settled(w)
            assert MARKER in saved(w) and not w.agent.handling[earlier.id]
            if how == 'edit':
                current = copy.copy(earlier)
                current.content, current.edited_at = 'fri explain the safe caption', discord.utils.utcnow()
                w.rows[earlier.id] = current
                await w.fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=current))
            else:
                del w.rows[earlier.id]
                await w.fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=earlier.id))
            assert MARKER not in await later_unrelated_request(w)
            assert MARKER not in saved(w)
            assert [p for p, _ in w.calls].count(WITHDRAW) == 1
        finally:
            await w.fri.close()
    asyncio.run(scenario())
