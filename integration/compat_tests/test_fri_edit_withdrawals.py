"""Edits count as withdrawals right up to delivery, saving and a memory control (review of fri #123)."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_unsend import model, no_debounce
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


def test_a_forget_me_edited_during_its_last_fetch_is_not_carried_out(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        earlier = message('fri remember ' + MARKER)
        earlier.reply.return_value = SimpleNamespace(id=earlier.id+100, content='okay', edit=AsyncMock())
        await agent.handle(earlier)
        assert store.chat_context('123', 'public:1', '20')['turns']
        control = message('fri forget me')
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
