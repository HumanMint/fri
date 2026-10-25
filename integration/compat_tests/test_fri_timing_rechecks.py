"""Timing changes keep the checks that came after the waits they moved (from #33's first Codex review):
an ambient reply's history read happens before its last privacy checks, and a reply target edited or
deleted while typing starts never reaches the model."""
import asyncio
import copy
import datetime as dt
import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from compat_tests.test_fri_member_profiles import world
from compat_tests.test_fri_mentions import environment
from fri.fri import Fri
from fri.fri_chat import ChatResult


def real_chat(fri, tmp_path, callback):
    fri.data = tmp_path
    fri.flush_usage = AsyncMock()
    fri.chat = SimpleNamespace(respond=callback)
    fri.chat_once = lambda text, context: Fri.chat_once(fri, text, context)


@pytest.mark.parametrize('erased_user', [20, 21])
def test_ambient_erasure_during_history(monkeypatch, tmp_path, erased_user):
    async def scenario():
        fri, agent, store, _, channel, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        trigger = message('anyone know where the marker is?')
        agent.recent[1].append({'id': str(trigger.id-1), 'user_id': '21', 'name': 'member 21',
                                'text': 'my private marker is blue', 'at': trigger.created_at.timestamp()
                                if isinstance(trigger.created_at, dt.datetime) else __import__('time').time(),
                                'revision': 0, 'gene_error': False})
        erased = []
        async def history(**kwargs):
            assert kwargs.get('after') is trigger
            store.clear_chats(str(erased_user))
            agent.forget(erased_user)
            erased.append(True)
            await asyncio.sleep(0)
            return
            yield
        channel.history = history
        async def model(text, context, ready):
            await ready()
            assert any('private marker' in row['text'] for row in context['recent'])
            return ChatResult(status='completed', reply='member 21 said their private marker is blue')
        real_chat(fri, tmp_path, model)
        try:
            await agent.respond(trigger, trigger.content, ambient=True)
            if erased:
                trigger.reply.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['edit', 'delete'])
def test_reply_target_edit_during_typing(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        target = message('my private marker is blue', user=21, age=2)
        trigger = message('fri what was that marker?')
        trigger.reference = SimpleNamespace(message_id=target.id, channel_id=1, resolved=target)
        @asynccontextmanager
        async def typing():
            if change == 'edit':
                edited = copy.copy(target)
                edited.content = 'i took that back'
                edited.edited_at = dt.datetime.now(dt.timezone.utc)
                rows[target.id] = edited
                agent.edit(1, edited)
            else:
                import discord
                def fetch(mid):
                    if mid == target.id:
                        raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
                    return rows[mid]
                channel.fetch_message.side_effect = fetch
                agent.unsend(1, [target.id])
            await asyncio.sleep(0)
            yield
        channel.typing = typing
        seen = []
        async def model(text, context, ready):
            await ready()
            seen.extend(row['text'] for row in context.get('reply_target', []))
            return ChatResult(status='completed', reply='blue')
        real_chat(fri, tmp_path, model)
        try:
            options = {'addressed': True} if 'addressed' in inspect.signature(agent.take_turn).parameters else {}
            await agent.respond(trigger, trigger.content, **options)
            assert not any('private marker' in row for row in seen)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_ambient_channel_closes_during_history(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        fri = w.fri
        closed = []
        try:
            await w.say(1, 'fri my secret marker is blue')
            trigger = w.message(2, 'anyone know what marker I mentioned?')
            trigger.reply = AsyncMock(return_value=SimpleNamespace(id=trigger.id+1,
                content='your secret marker is blue', edit=AsyncMock()))
            async def model(text, context, ready):
                await ready()
                assert context['elsewhere'][0]['input'] == 'fri my secret marker is blue'
                return ChatResult(status='completed', reply='your secret marker is blue')
            real_chat(fri, tmp_path, model)
            async def history(**kwargs):
                w.readable[1] = False
                closed.append(True)
                await asyncio.sleep(0)
                return
                yield
            w.channels[2].history = history
            await fri.agent.respond(trigger, trigger.content, ambient=True)
            if closed:
                trigger.reply.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())
