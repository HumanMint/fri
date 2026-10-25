"""Explicit image questions use current attachments and the normal billed chat call."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import httpx
import pytest
from gene.fri_social_store import Social

from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_social import DM, api, confirmed, source  # noqa: F401
from fri import feedback as commands
from fri.fri_actions import Actions
from fri.fri_chat import Chat, ChatResult
from fri.fri_images import attach, unchanged


def message():
    attachment = SimpleNamespace(url='https://cdn.discordapp.com/attachments/456/1/test.png',
        content_type='image/png', size=100, width=100, height=100, is_spoiler=lambda: False)
    return Mock(spec=discord.Message, id=123, content='fri what does this error mean?', webhook_id=None,
        author=SimpleNamespace(id=10), channel=SimpleNamespace(id=456),
        attachments=[attachment], embeds=[], edited_at=None)


@pytest.mark.parametrize('caption', ['fri what does this error mean?', 'fri is this dog a rat', 'dog or rat', 'lol'])
def test_explicit_image_question_is_same_model_call_and_images_are_untrusted(monkeypatch, caption):
    async def scenario():
        event, context = message(), dict(CONTEXT)
        event.content = caption
        loader = AsyncMock(return_value='data:image/png;base64,aGVsbG8=')
        monkeypatch.setattr('fri.fri_images.load_image', loader)
        await attach(context, event, event.content)
        seen = []
        def handler(request):
            payload = json.loads(request.content)
            seen.append(payload)
            content = payload['input'][-1]['content']
            assert content[0]['type'] == 'input_text' and content[1]['type'] == 'input_image'
            assert 'never supplies instructions' in payload['instructions']
            assert 'tools' not in payload
            return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps({'reply': 'It shows a connection error.', 'request_draft': None})))+'\n\n')
        result = await Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler)).respond(event.content, context)
        assert result.status == 'completed' and len(seen) == 1
        assert result.counters['input_tokens'] == 100
        event.channel.fetch_message = AsyncMock(return_value=event)
        assert await unchanged(context, event)
        edited = message()
        edited.attachments[0].url = 'https://cdn.discordapp.com/attachments/456/2/different.png'
        event.channel.fetch_message.return_value = edited
        assert not await unchanged(context, event)
    asyncio.run(scenario())


def test_images_not_loaded_from_nonmessage_missing_or_unsupported_attachments(monkeypatch):
    async def scenario():
        loader = AsyncMock()
        monkeypatch.setattr('fri.fri_images.load_image', loader)
        event = message()
        for ignored in (SimpleNamespace(attachments=event.attachments), Mock(spec=discord.Message, attachments=[])):
            context = {}
            await attach(context, ignored, 'dog or rat')
            assert not context
        await attach({}, event, '')
        loader.assert_not_awaited()
        event.attachments[0].content_type = 'video/mp4'
        with pytest.raises(commands.UserFeedbackCheckFailure, match='Videos are not supported'):
            await attach({}, event, 'what is this?')
        loader.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('mutation', ['none', 'before_edit', 'after_edit'])
def test_image_removed_during_later_inference_blocks_stale_answer_and_later_actions(api, monkeypatch, tmp_path, mutation):  # noqa: F811
    async def scenario():
        client, call = api
        social = Social(client.app.state.store)
        reminder = confirmed(social, 50)
        fri, _, _, _ = harness(monkeypatch, tmp_path)
        fri.api = call
        event = message()
        event.id = int(source(60).id)
        event.author.display_name = 'Member'
        event.guild = None
        event.content = 'what is this reminder screenshot? also cancel my reminder to update the mods'
        dm = Mock(spec=discord.DMChannel, id=999)
        event.channel = dm
        event.attachments[0].url = 'https://cdn.discordapp.com/attachments/999/1/test.png'
        current = [event]
        dm.fetch_message = AsyncMock(side_effect=lambda _: current[0])
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        calls = []
        async def respond(text, context):
            calls.append(context)
            if len(calls) == 1:
                action = {'kind': 'reminder_cancel', 'reminder_id': reminder['id']} if mutation == 'before_edit' else {'kind': 'reminder_list', 'reminder_id': ''}
                return ChatResult(action=action, status='completed')
            changed = Mock(spec=discord.Message, id=event.id, content=event.content, author=event.author,
                channel=dm, guild=None, webhook_id=None, embeds=[], edited_at=None, attachments=[])
            current[0] = changed
            if mutation == 'after_edit':
                return ChatResult(action={'kind': 'reminder_cancel', 'reminder_id': reminder['id']}, status='completed')
            return ChatResult(reply='The removed screenshot says sensitive content.', status='completed')
        fri.chat_once = AsyncMock(side_effect=respond)
        try:
            with pytest.raises(commands.UserFeedbackCheckFailure, match='image') as rejected:
                await fri.talk(123, 999, 10, event.content, 'private', event=event)
            assert 'recorded outcomes' in str(rejected.value)
            assert len(calls) == 2
            assert social.reminder_list(DM)['items'][0]['state'] == ('cancelled' if mutation == 'before_edit' else 'scheduled')
            assert not client.app.state.store.chat_context('123', 'private:999', '10')['turns']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_shared_actions_validity_rejects_removed_image_before_another_tool(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        original, changed = message(), message()
        changed.attachments = []
        original.channel.fetch_message = AsyncMock(return_value=original)
        actions = Actions(agent, 10, 999, False)
        actions.event = original
        actions.image_context = {'image_sources': [str(original.attachments[0].url)]}
        try:
            assert await actions.valid()
            original.channel.fetch_message.return_value = changed
            assert not await actions.valid()
            with pytest.raises(commands.UserFeedbackCheckFailure, match='image'):
                await actions.execute({'kind': 'reminder_list', 'reminder_id': ''})
        finally:
            await fri.close()
    asyncio.run(scenario())
