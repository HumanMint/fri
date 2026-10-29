"""Edits count as withdrawals right up to delivery, saving and a memory control (review of fri #123)."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_unsend import heard, model, no_debounce
from compat_tests.test_fri_withdrawn_rows import model as real_model
from fri.fri_chat import ChatResult

MARKER = 'synthetic-private-marker'  # Synthetic words a withdrawal must keep from going anywhere.


def test_a_reply_to_a_message_withdrawn_earlier_never_sends_its_old_saved_words(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        fri.data = tmp_path
        requests = []
        real_model(fri, requests)
        fri.chat_once = type(fri).chat_once.__get__(fri)
        earlier = message('fri explain ' + MARKER)
        earlier.reply.return_value = SimpleNamespace(id=earlier.id+100, content='okay', edit=AsyncMock())
        try:
            await agent.handle(earlier)
            assert MARKER in str(store.chat_context('123', 'public:1', '20')['turns'])
            intermediate = message('fri explain that caption?', user=30)
            intermediate.reference = SimpleNamespace(message_id=earlier.id, channel_id=1)
            original_handler = fri.chat.transport.handler
            async def editing_during_response(request):
                response = original_handler(request)
                current = copy.copy(earlier)
                current.content = 'current safe caption'
                current.edited_at = discord.utils.utcnow()
                rows[earlier.id] = current
                await fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=current))
                return response
            fri.chat.transport.handler = editing_during_response
            await agent.handle(intermediate)
            fri.chat.transport.handler = original_handler
            assert str(earlier.id) in agent.withdrawn
            requests.clear()
            later = message('fri explain the current caption?')
            later.reply.return_value = SimpleNamespace(id=later.id+100, content='okay', edit=AsyncMock())
            later.reference = SimpleNamespace(message_id=earlier.id, channel_id=1)
            await agent.handle(later)
            assert MARKER not in str(requests)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_message_edited_while_the_model_answers_it_is_neither_delivered_nor_saved(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, _, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri explain ' + MARKER)
        trigger.reply.return_value = SimpleNamespace(id=trigger.id+100, content=MARKER, edit=AsyncMock())
        async def respond(context, ready):
            await ready()
            current = copy.copy(trigger)
            current.content = 'never mind'
            current.edited_at = discord.utils.utcnow()
            rows[trigger.id] = current
            await fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=current))
            return ChatResult(status='completed', reply=MARKER)
        model(fri, tmp_path, respond)
        try:
            await agent.handle(trigger)
            turns = store.chat_context('123', 'public:1', '20')['turns']
            assert MARKER not in str(turns)
            assert not trigger.reply.await_count
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['fri forget me', 'fri forget about me'])
def test_a_forget_me_edited_during_its_last_fetch_is_not_carried_out(monkeypatch, tmp_path, text):
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        earlier = message('fri remember ' + MARKER)
        earlier.reply.return_value = SimpleNamespace(id=earlier.id+100, content='okay', edit=AsyncMock())
        await agent.handle(earlier)
        assert store.chat_context('123', 'public:1', '20')['turns']
        control = message(text)
        original = channel.fetch_message.side_effect
        changed = False
        async def fetch(mid):
            nonlocal changed
            item = original(mid)
            if mid == control.id and not changed:
                changed = True
                current = copy.copy(control)
                current.content = 'never mind'
                current.edited_at = discord.utils.utcnow()
                rows[mid] = current
                await fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=current))
            return item
        channel.fetch_message.side_effect = fetch
        try:
            await agent.handle(control)
            enabled = store.chat_context('123', 'public:1', '20')['personal_memory']['enabled']
            assert enabled
        finally:
            await fri.close()
    asyncio.run(scenario())


async def edited(fri, rows, item, text='current safe words'):
    """A member's edit, as Discord's raw event brings it."""
    current = copy.copy(item)
    current.content, current.edited_at = text, discord.utils.utcnow()
    rows[item.id] = current
    await fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=current))
    return current


def test_a_nearby_post_edited_after_the_model_read_it_is_neither_delivered_nor_saved(monkeypatch, tmp_path):
    """fri #125: the post's own handling is over, but the turn's model request carried it."""
    async def scenario():
        fri, agent, store, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        regret, trigger = message('someone said ' + MARKER, user=21), message('fri what did they say')
        async def respond(context, ready):
            await ready()
            assert any(MARKER in r['text'] for r in context.get('recent', []))
            assert regret.id in agent.looked and agent.handling[regret.id] == 0
            await edited(fri, rows, regret)
            return ChatResult(status='completed', reply=MARKER)
        model(fri, tmp_path, respond)
        try:
            await heard(agent, regret)
            await agent.handle(trigger)
            assert MARKER not in str(trigger.reply.await_args_list)
            assert MARKER not in str(store.chat_context('123', 'public:1', '20')['turns'])
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_post_edited_after_an_earlier_turn_carried_it_is_read_fresh_by_later_turns(monkeypatch, tmp_path):
    """fri #125: the edit stops only turns that may hold its old words. A turn after it reads the current
    text, from the live view or as the post it replies to, and answers."""
    async def scenario():
        fri, agent, _, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        regret = message('someone said ' + MARKER, user=21)
        requested = []
        def asking(text, **kwargs):
            item = message(text, user=20)
            item.reply.return_value = SimpleNamespace(id=item.id+1000, content='safe reply', edit=AsyncMock())
            return item
        async def respond(context, ready):
            await ready()
            requested.append(str({key: context.get(key) for key in ('recent', 'reply_target')}))
            return ChatResult(status='completed', reply='safe reply')
        model(fri, tmp_path, respond)
        try:
            await heard(agent, regret)
            first = asking('fri hey')
            await agent.handle(first)
            assert MARKER in requested[-1] and regret.id in agent.looked
            await edited(fri, rows, regret)
            assert regret.id in agent.revised
            replying = asking('fri explain this')
            replying.reference = SimpleNamespace(message_id=regret.id, channel_id=1)
            for later in (asking('fri what now'), replying):
                await agent.handle(later)
                assert MARKER not in requested[-1] and 'current safe words' in requested[-1]
                assert later.reply.await_args.args[0] == 'safe reply'
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_reply_target_edited_after_its_last_fetch_never_reaches_the_model(monkeypatch, tmp_path):
    """fri #125: edited while the last checks still waited (here the channel gate), after the reply target
    was fetched again. The turn's copy holds the old words, so the request is refused."""
    async def scenario():
        fri, agent, store, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        regret = message('someone said ' + MARKER, user=21)
        trigger = message('fri what does this mean')
        trigger.reference = SimpleNamespace(message_id=regret.id, channel_id=1)
        requested = []
        async def respond(context, ready):
            await ready()
            requested.append(str(context.get('reply_target')))
            return ChatResult(status='completed', reply='safe reply')
        model(fri, tmp_path, respond)
        can_read, editing = agent.can_read, []
        async def reading(*args):
            if regret.id in agent.looked and not editing:  # In the last checks, after the target's fetch.
                editing.append(await edited(fri, rows, regret))
            return await can_read(*args)
        agent.can_read = reading
        try:
            await agent.handle(trigger)
            assert editing and not requested
            assert MARKER not in str(trigger.reply.await_args_list)
            assert not store.chat_context('123', 'public:1', '20')['turns']
        finally:
            await fri.close()
    asyncio.run(scenario())
