"""What the checks #53 kept still cover: a ping's preceding photo, nearby messages deleted during the
privacy read that follows the Discord reads, and images on turns that aren't pings (from #53's first review)."""
import asyncio
import copy
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from compat_tests.test_fri_mentions import attachment, burst, environment
from fri.fri_chat import ChatResult


@pytest.mark.parametrize('change', ['delete', 'remove_attachment'])
@pytest.mark.parametrize('tool', [False, True])
def test_preceding_photo_is_checked_after_model(monkeypatch, tmp_path, change, tool):
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        photo = message('', age=10, attachments=[attachment(1)])
        question = 'what is the current Python release?'
        event = message('<@99> what does my screenshot say? also '+question)
        event.reply.return_value = SimpleNamespace(id=event.id+1, content='the screenshot contains a medical diagnosis')
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        monkeypatch.setattr('fri.fri_actions.lookup', AsyncMock(return_value=({
            'query': question, 'answer': 'Public version info', 'sources': ['https://www.python.org/'],
            'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}, {}, 'completed')))
        async def fetch(mid):
            if mid not in rows:
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'deleted')
            return rows[mid]
        channel.fetch_message.side_effect = fetch
        model_calls = []
        async def respond(_, context):
            assert context['images'] and context['image_event'].id == photo.id
            model_calls.append(context)
            if tool and len(model_calls) == 1:
                return ChatResult(status='completed', action={'kind': 'web_search', 'query': question})
            if change == 'delete':
                del rows[photo.id]
            else:
                changed = copy.copy(photo)
                changed.attachments = []
                changed.edited_at = dt.datetime.now(dt.timezone.utc)
                rows[photo.id] = changed
            return ChatResult(status='completed', reply='the screenshot contains a medical diagnosis')
        fri.chat_once.side_effect = respond
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            replies = [call.args[0] for call in event.reply.await_args_list]
            saved = store.chat_context('123', 'public:1', '20')
            assert len(model_calls) == (2 if tool else 1)
            assert not any('medical diagnosis' in reply for reply in replies)
            assert 'medical diagnosis' not in str(saved)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('channel_id', [1, 8])
def test_nearby_deleted_during_last_profile_check_stays_withheld(monkeypatch, tmp_path, channel_id):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=channel_id)
        nearby = message('sensitive third-party detail', user=30, age=20, attachments=[attachment(channel_id)])
        event = message('<@99> what did they say?')
        if channel_id == 1:
            event.reference = SimpleNamespace(message_id=nearby.id, channel_id=channel_id)
        event.reply.return_value = SimpleNamespace(id=event.id+1, content='sensitive third-party detail')
        fri.chat_once.return_value = ChatResult(status='completed', reply='sensitive third-party detail')
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        talked = []
        talk, api = fri.talk, fri.api
        async def talking(*args, **kwargs):
            result = await talk(*args, **kwargs)
            talked.append(True)
            return result
        fri.talk = talking
        async def checked(method, path, payload=None):
            result = await api(method, path, payload)
            if talked and path == '/chat/mentions/profiles':
                rows.pop(nearby.id, None)
            return result
        fri.api = checked
        async def fetch(mid):
            if mid not in rows:
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'deleted')
            return rows[mid]
        channel.fetch_message.side_effect = fetch
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert nearby.id not in rows
            replies = [call.args[0] for call in event.reply.await_args_list]
            assert not any('sensitive third-party detail' in reply for reply in replies)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_nearby_deleted_during_ready_profile_check_never_reaches_provider(monkeypatch, tmp_path):
    import httpx

    from fri.fri_chat import Chat
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path)
        fri.data = tmp_path
        nearby = message('sensitive third-party detail', user=30, age=20)
        event = message('<@99> what did they say?')
        signed_in, sent = [], []
        def access_token():
            signed_in.append(True)
            return 'fictional'
        fri.chat = Chat(SimpleNamespace(access_token=access_token), httpx.MockTransport(
            lambda request: sent.append(request) or httpx.Response(500)))
        del fri.chat_once
        api = fri.api
        async def checked(method, path, payload=None):
            result = await api(method, path, payload)
            if signed_in and path == '/chat/mentions/profiles':
                rows.pop(nearby.id, None)
            return result
        fri.api = checked
        async def fetch(mid):
            if mid not in rows:
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'deleted')
            return rows[mid]
        channel.fetch_message.side_effect = fetch
        try:
            await agent.handle(event)
            assert signed_in and nearby.id not in rows
            assert not sent
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_image_ping_edited_during_reply_target_read_cannot_start_tool(monkeypatch, tmp_path):
    import inspect

    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        target = message('ordinary context', user=30, age=20)
        question = 'what is the current Python release?'
        event = message('<@99> '+question, attachments=[attachment(1)])
        event.reference = SimpleNamespace(message_id=target.id, channel_id=channel.id)
        lookup = AsyncMock(return_value=({'query': question, 'answer': 'public information',
            'sources': ['https://www.python.org/'], 'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}, {}, 'completed'))
        monkeypatch.setattr('fri.fri_actions.lookup', lookup)
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        fri.chat_once.side_effect = [ChatResult(status='completed', action={'kind': 'web_search', 'query': question}),
                                    ChatResult(status='completed', reply='Public information')]
        changed = []
        async def fetch(mid):
            names = [frame.function for frame in inspect.stack()]
            # While the tool's conversation fence reads its reply target, after its invitation check.
            if mid == target.id and 'execute' in names and 'reply_target' in names and not changed:
                edited = copy.copy(event)
                edited.content = '<@99> never mind'
                edited.attachments = []
                edited.edited_at = dt.datetime.now(dt.timezone.utc)
                rows[event.id] = edited
                changed.append(True)
            return rows[mid]
        channel.fetch_message.side_effect = fetch
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert changed
            lookup.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('grouped', [False, True])
def test_sending_without_invitation_still_checks_image_after_context_read(monkeypatch, tmp_path, grouped):
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        first = message('<@99> hey', age=1) if grouped else None
        question = 'what is the current Python release?'
        event = message(('' if grouped else 'fri ')+'what does my screenshot say? also '+question,
                        attachments=[attachment(1)])
        event.reply.return_value = SimpleNamespace(id=event.id+1, content='the screenshot contains a medical diagnosis')
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        monkeypatch.setattr('fri.fri_actions.lookup', AsyncMock(return_value=({'query': question,
            'answer': 'Public version info', 'sources': ['https://www.python.org/'],
            'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}, {}, 'completed')))
        fri.chat_once.side_effect = [ChatResult(status='completed', action={'kind': 'web_search', 'query': question}),
                                    ChatResult(status='completed', reply='the screenshot contains a medical diagnosis')]
        api, edited = fri.api, []
        async def checked(method, path, payload=None):
            result = await api(method, path, payload)
            # Fri.talk's post-tool context refresh, after its explicit image check.
            if path == '/chat/context' and fri.chat_once.await_count == 2 and not edited:
                changed = copy.copy(event)
                changed.attachments = []
                changed.edited_at = dt.datetime.now(dt.timezone.utc)
                rows[event.id] = changed
                edited.append(True)
            return result
        fri.api = checked
        try:
            if grouped:
                await burst(agent, first, event)
            else:
                with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                    await agent.handle(event)
            assert edited and fri.chat_once.await_count == 2
            replies = [call.args[0] for call in event.reply.await_args_list]
            saved = store.chat_context('123', 'public:1', '20')
            assert not any('medical diagnosis' in reply for reply in replies)
            assert 'medical diagnosis' not in str(saved)
        finally:
            await fri.close()
    asyncio.run(scenario())
