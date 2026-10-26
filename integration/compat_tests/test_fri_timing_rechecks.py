"""Timing changes keep the checks that came after the waits they moved (from #33's Codex reviews): an
ambient reply's history read happens before its last privacy and source checks, a reply target edited or
deleted while typing starts never reaches the model, and a history read that times out stays quiet, and
nothing withdrawn while typing starts (the ping, the message, its reply target or burst, a photo it
looks at) reaches a tool or the model."""
import asyncio
import copy
import datetime as dt
import inspect
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import httpx
import pytest

from compat_tests.test_fri_member_profiles import world
from compat_tests.test_fri_mentions import attachment, environment
from fri.fri import Fri
from fri.fri_chat import Chat, ChatResult


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


@pytest.mark.parametrize('change', ['edit', 'delete'])
@pytest.mark.parametrize('change_at', ['model', 'history'])
def test_ambient_reference_retracted_during_history(monkeypatch, tmp_path, change, change_at):
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        target = message('my private marker is blue', user=21, age=3600)
        trigger = message('what was that marker?')
        trigger.reference = SimpleNamespace(message_id=target.id, channel_id=1, resolved=target)
        trigger.reply.return_value = SimpleNamespace(id=trigger.id+1, content='the private marker is blue',
                                                    edit=AsyncMock(), reply=AsyncMock())
        retracted = []
        async def model(text, context, ready):
            await ready()
            assert context['reply_target'][0]['text'] == target.content
            if change_at == 'model':
                await retract()
            return ChatResult(status='completed', reply='the private marker is blue')
        fri.data, fri.flush_usage = tmp_path, AsyncMock()
        fri.chat = SimpleNamespace(respond=model)
        fri.chat_once = lambda text, context: Fri.chat_once(fri, text, context)
        async def retract():
            if change == 'edit':
                edited = copy.copy(target)
                edited.content = 'i took that back'
                edited.edited_at = dt.datetime.now(dt.timezone.utc)
                rows[target.id] = edited
                await fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=edited))
            else:
                del rows[target.id]
                old_fetch = channel.fetch_message.side_effect
                def fetch(mid):
                    if mid == target.id:
                        raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
                    return old_fetch(mid)
                channel.fetch_message.side_effect = fetch
                await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=target.id))
            retracted.append(True)
            await asyncio.sleep(0)

        async def history(**kwargs):
            if change_at == 'history':
                await retract()
            return
            yield
        channel.history = history
        try:
            await agent.respond(trigger, trigger.content, ambient=True)
            saved = store.chat_context('123', 'public:1', '20')['turns']
            if retracted:
                assert all('private marker is blue' not in call.args[0]
                           for call in trigger.reply.await_args_list)
                assert not saved
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_ambient_history_timeout_stays_quiet(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        trigger = message('does anyone know if the server is down?')
        async def history(**kwargs):
            raise asyncio.TimeoutError('read timed out')
            yield
        channel.history = history
        try:
            await agent.respond(trigger, trigger.content, ambient=True)
            trigger.reply.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_cancel_eight_second_summons_releases_all_state(monkeypatch, tmp_path, caplog):
    async def scenario():
        import logging
        caplog.set_level(logging.INFO, logger='fri')
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        monkeypatch.setattr('fri.fri_agent.REST_SECONDS', 8)
        bare = message('<@99>')
        task = asyncio.create_task(agent.handle(bare))
        try:
            async with asyncio.timeout(2):
                while (1, 20) not in agent.summons:
                    await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert not agent.summons and not agent.bursts.rows and not agent.pending
            assert not agent.handling and not agent.replying and not agent.turns.arrived
            assert not agent.turns.holders and not fri.busy and not agent.misses
            fri.chat_once.assert_not_awaited()
            assert caplog.messages.count(f'Mention 1/{bare.id} not answered: CancelledError') == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


def test_erasure_while_unaddressed_action_starts(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        trigger = message('what is the current weather in lima')
        query = trigger.content
        asked, searched, erased = [], [], []
        @asynccontextmanager
        async def typing():
            store.clear_chats('20')
            agent.forget(20)
            erased.append(True)
            await asyncio.sleep(0)
            yield
        channel.typing = typing
        async def model(text, context, ready):
            await ready()
            asked.append(text)
            return ChatResult(status='completed', action={'kind': 'web_search', 'query': query})
        async def lookup(chat, text, *args, **kwargs):
            assert erased
            searched.append(text)
            return {}, {}, 'failed'
        monkeypatch.setattr('fri.fri_actions.lookup', lookup)
        fri.data, fri.flush_usage = tmp_path, AsyncMock()
        fri.chat = SimpleNamespace(respond=model)
        fri.chat_once = lambda text, context: Fri.chat_once(fri, text, context)
        try:
            await agent.respond(trigger, trigger.content)
            assert erased
            assert not searched, f'erased query reached researcher: {searched}; main calls: {asked}'
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['edit', 'delete'])
def test_reference_retracted_while_unaddressed_action_starts(monkeypatch, tmp_path, change):
    async def scenario():
        import discord
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        target = message('my private marker is blue', user=21, age=3600)
        trigger = message('what is the current weather in lima')
        trigger.reference = SimpleNamespace(message_id=target.id, channel_id=1, resolved=target)
        sent_after_retraction = []
        retracted, calls = [], []
        @asynccontextmanager
        async def typing():
            if change == 'edit':
                replacement = copy.copy(target)
                replacement.content = 'i took that back'
                replacement.edited_at = dt.datetime.now(dt.timezone.utc)
                rows[target.id] = replacement
                await fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=replacement))
            else:
                del rows[target.id]
                def fetch(mid):
                    if mid == target.id:
                        raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
                    return rows[mid]
                channel.fetch_message.side_effect = fetch
                await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=target.id))
            retracted.append(True)
            await asyncio.sleep(0)
            yield
        channel.typing = typing
        async def history(**kwargs):
            return
            yield
        channel.history = history
        async def model(text, context, ready):
            await ready()
            calls.append(context.get('reply_target', []))
            if retracted:
                sent_after_retraction.extend(row['text'] for row in context.get('reply_target', []))
            if len(calls) == 1:
                return ChatResult(status='completed', action={'kind': 'web_search', 'query': trigger.content})
            return ChatResult(status='completed', reply='the marker is blue')
        monkeypatch.setattr('fri.fri_actions.lookup', AsyncMock(return_value=({}, {}, 'failed')))
        fri.data, fri.flush_usage = tmp_path, AsyncMock()
        fri.chat = SimpleNamespace(respond=model)
        fri.chat_once = lambda text, context: Fri.chat_once(fri, text, context)
        try:
            await agent.respond(trigger, trigger.content, ambient=True)
            assert retracted
            assert not any('private marker' in text for text in sent_after_retraction), (
                f'retracted text reached a subsequent model request: {sent_after_retraction}')
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_ping_edited_during_typing(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        trigger = message('<@99> my private marker is blue')
        seen = []

        @asynccontextmanager
        async def typing():
            replacement = copy.copy(trigger)
            replacement.content = '<@99> i took that back'
            replacement.edited_at = dt.datetime.now(dt.timezone.utc)
            rows[trigger.id] = replacement
            await fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=replacement))
            await asyncio.sleep(0)
            yield

        channel.typing = typing

        def transport(request):
            seen.append(json.loads(json.loads(request.content)['input'][-1]['content'])['message'])
            return httpx.Response(500)

        fri.data, fri.flush_usage = tmp_path, AsyncMock()
        fri.chat = Chat(SimpleNamespace(access_token=lambda: 'fictional'), httpx.MockTransport(transport))
        del fri.chat_once
        real_sleep = asyncio.sleep

        async def immediate(seconds):
            await real_sleep(0)

        monkeypatch.setattr('fri.fri_agent.asyncio.sleep', immediate)
        try:
            await agent.handle(trigger)
            assert not seen, 'retracted ping text reached model'
        finally:
            await fri.close()

    asyncio.run(scenario())


def test_previous_photo_deleted_during_typing(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        photo = message('', age=1, attachments=[attachment(1)])
        trigger = message('fri describe that photo')
        seen = []

        async def history(*, before, limit, **kwargs):
            for row in sorted((m for m in rows.values() if m.id < before.id), key=lambda m: m.id, reverse=True)[:limit]:
                yield row

        channel.history = history

        def fetch(mid):
            if mid not in rows:
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
            return rows[mid]

        channel.fetch_message.side_effect = fetch

        @asynccontextmanager
        async def typing():
            del rows[photo.id]
            await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=photo.id))
            await asyncio.sleep(0)
            yield

        channel.typing = typing
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))

        def transport(request):
            content = json.loads(request.content)['input'][-1]['content']
            if isinstance(content, list):
                seen.extend(part['image_url'] for part in content if part['type'] == 'input_image')
            return httpx.Response(500)

        fri.data, fri.flush_usage = tmp_path, AsyncMock()
        fri.chat = Chat(SimpleNamespace(access_token=lambda: 'fictional'), httpx.MockTransport(transport))
        del fri.chat_once
        try:
            options = {'addressed': True} if 'addressed' in inspect.signature(agent.take_turn).parameters else {}
            await agent.respond(trigger, trigger.content, **options)
            assert not seen, 'deleted photo reached model'
        finally:
            await fri.close()

    asyncio.run(scenario())
