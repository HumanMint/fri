"""Photo captions exercise the real chat path, with provider I/O isolated."""

import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import httpx
import pytest

from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_chat import response_event
from compat_tests.test_fri_images import message
from compat_tests.test_fri_social import api, source  # noqa: F401
from fri.fri_actions import Actions
from fri.fri_chat import Chat, ChatResult
from fri.fri_notices import ReplyInProgress


@pytest.mark.parametrize('caption', ['fri is this dog a rat', 'dog or rat', 'gotcha'])
@pytest.mark.parametrize('public', [False, True])
@pytest.mark.parametrize('separate_upload', [False, True])
def test_informal_caption_reaches_model_with_pixels_and_no_image_persistence(api, monkeypatch, tmp_path, caption, public, separate_upload):  # noqa: F811
    async def scenario():
        client, call = api
        fri, _, _, _ = harness(monkeypatch, tmp_path)
        fri.api = call
        event = message()
        event.id, event.content = int(source(60, public=public).id), caption
        event.guild = SimpleNamespace(id=123) if public else None
        event.author.display_name = 'Member'
        channel = 456 if public else 999
        visibility = 'public' if public else 'private'
        event.channel = Mock(spec=discord.TextChannel if public else discord.DMChannel, id=channel)
        event.channel.fetch_message = AsyncMock(return_value=event)
        event.reference = None
        event.attachments[0].url = f'https://cdn.discordapp.com/attachments/{channel}/1/test.png'
        if separate_upload:
            upload = copy.copy(event)
            upload.id, upload.content = event.id-(13_000 << 22), ''
            upload.author.bot = False
            event.attachments = []
            async def history(**kwargs):
                assert kwargs == {'limit': 1, 'before': event, 'oldest_first': False}
                yield upload
            event.channel.history = history
            event.channel.fetch_message = AsyncMock(side_effect=lambda identifier: upload if identifier == upload.id else event)
        uri = 'data:image/png;base64,aGVsbG8='
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value=uri))
        monkeypatch.setattr(Actions, 'available', AsyncMock(return_value={}))
        # Even an exact closing caption must not discard the newly supplied photo.
        prior = source(1, public=public).model_copy(update={'id': str(event.id-(10_000 << 22))})
        client.app.state.store.save_chat('123', f'{visibility}:{channel}', '10', 0, 'hello', 'hey',
            'test', 'high', source=prior.model_dump(), allow_closing=True)
        seen = []
        def transport(request):
            payload = json.loads(request.content)
            seen.append(payload)
            supplied = payload['input'][-1]['content']
            assert isinstance(supplied, list), 'The current photo was silently dropped'
            assert supplied[1] == {'type': 'input_image', 'image_url': uri, 'detail': 'auto'}
            assert json.loads(supplied[0]['text'])['message'] == caption
            return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(
                {'reply': 'small dog, strong rat allegations.', 'request_draft': None})))+'\n\n')
        fri.chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(transport))
        try:
            result = await fri.talk(123, channel, 10, caption, visibility, event=event)
            assert result.status == 'completed' and len(seen) == 1
            saved = client.app.state.store.chat_context('123', f'{visibility}:{channel}', '10')
            assert 'data:image' not in str(saved) and 'cdn.discordapp.com' not in str(saved)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['other_user', 'other_channel', 'old', 'future', 'captioned', 'webhook'])
def test_previous_upload_does_not_borrow_unrelated_media(change):
    from fri.fri_images import select_source
    async def scenario():
        event = message()
        event.id = int(source(60).id)
        upload = copy.copy(event)
        upload.id, upload.content = event.id-(13_000 << 22), ''
        upload.author = SimpleNamespace(id=10, bot=False)
        event.attachments = []
        if change == 'other_user':
            upload.author.id = 20
        elif change == 'other_channel':
            upload.channel = SimpleNamespace(id=999)
        elif change == 'old':
            upload.id = event.id-(61_000 << 22)
        elif change == 'future':
            upload.id = event.id+1
        elif change == 'captioned':
            upload.content = 'a separate conversation'
        else:
            upload.webhook_id = 100
        async def history(**kwargs):
            yield upload
        event.channel.history = history
        assert await select_source(event) is None
    asyncio.run(scenario())


def test_separate_photo_removed_during_inference_invalidates_answer(monkeypatch):
    from fri.fri_images import attach, unchanged
    async def scenario():
        event = message()
        upload = copy.copy(event)
        upload.id, upload.content = event.id-1, ''
        event.attachments = []
        context = {}
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        event.channel.fetch_message = AsyncMock(side_effect=lambda identifier: upload if identifier == upload.id else event)
        await attach(context, event, event.content, source=upload)
        assert await unchanged(context, event)
        removed = copy.copy(upload)
        removed.attachments = []
        event.channel.fetch_message = AsyncMock(side_effect=lambda identifier: removed if identifier == upload.id else event)
        assert not await unchanged(context, event)
    asyncio.run(scenario())


def test_proactive_reply_keeps_photos_out_of_background_inference(monkeypatch, tmp_path):
    async def scenario():
        from fri.fri_chat import ChatResult
        fri, agent, channels, _ = harness(monkeypatch, tmp_path)
        event = message()
        event.author.display_name = 'Member'
        event.guild = SimpleNamespace(id=123)
        event.channel = channels[456]
        event.channel.typing = Mock(return_value=AsyncMock())
        async def quiet(**kwargs):  # Nobody else spoke, so the ambient reply still fits.
            return
            yield
        event.channel.history = quiet
        event.reply = AsyncMock()
        fri.talk = AsyncMock(return_value=ChatResult(status='completed', reply='hey'))
        try:
            await agent.respond(event, event.content, ambient=True)
            assert fri.talk.await_args.kwargs['inspect_images'] is False
            await agent.respond(event, event.content)
            assert fri.talk.await_args.kwargs['inspect_images'] is True
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['pending', 'busy', 'answered'])
def test_ambient_attention_does_not_race_a_direct_reply(monkeypatch, tmp_path, phase):
    async def scenario():
        fri, agent, channels, _ = harness(monkeypatch, tmp_path)
        record = {'id': '100', 'user_id': '20', 'name': 'Member', 'text': 'fri is this dog a rat',
                  'gene_error': False, 'at': time.time()}
        agent.config['proactive'] = True
        agent.recent[1].append(record)
        agent.context_messages = AsyncMock(return_value=[record])
        agent.respond = AsyncMock()
        channels[1].fetch_message = AsyncMock(return_value=SimpleNamespace(
            author=SimpleNamespace(id=20), content=record['text']))
        async def attention(*args, **kwargs):
            if phase == 'pending':
                agent.pending[(1, 20)] = 100
            elif phase == 'busy':
                fri.busy.add(20)
            else:
                agent.last_spoke[1] = time.time()
            return {'action': 'reply', 'source_id': '100'}, {}, 'completed'
        agent.run = attention
        try:
            await agent.attend()
            agent.respond.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['answered', 'busy'])
def test_ambient_yields_to_direct_reply_during_typing(monkeypatch, tmp_path, phase):
    # Ambient attention no longer types before its reply, so the last wait it can race a direct reply
    # through is its own fetch of the message.
    async def scenario():
        fri, agent, channels, _ = harness(monkeypatch, tmp_path)
        channel = channels[1]
        entered, resume, direct_started, direct_finish = (asyncio.Event() for _ in range(4))
        typed = []
        class Typing:
            async def __aenter__(self):
                typed.append(True)
            async def __aexit__(self, *args):
                return False
        channel.typing = Typing
        message = SimpleNamespace(id=100, content='fri can you help?', channel=channel,
            guild=SimpleNamespace(id=123), author=SimpleNamespace(id=20, display_name='Member'),
            reply=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())))
        record = {'id': '100', 'user_id': '20', 'name': 'Member', 'text': message.content,
                  'gene_error': False, 'at': time.time()}
        async def fetch(mid):
            if not entered.is_set():  # Attention's own fetch: the direct reply starts meanwhile.
                entered.set()
                await resume.wait()
            return message
        channel.fetch_message = AsyncMock(side_effect=fetch)
        agent.config['proactive'] = True
        agent.recent[1].append(record)
        agent.context_messages = AsyncMock(return_value=[record])
        agent.run = AsyncMock(return_value=({'action': 'reply', 'source_id': '100'}, {}, 'completed'))
        async def talk(*args, **kwargs):
            if 20 in fri.busy:
                raise ReplyInProgress()
            fri.busy.add(20)
            direct_started.set()
            try:
                if phase == 'busy':
                    await direct_finish.wait()
                return ChatResult(status='completed', reply='answer to current request')
            finally:
                fri.busy.discard(20)
        fri.talk = talk
        ambient = asyncio.create_task(agent.attend())
        direct = None
        try:
            await entered.wait()  # Attention picked the message and is fetching it.
            direct = asyncio.create_task(agent.respond(message, message.content))
            await direct_started.wait()
            if phase == 'answered':
                await direct
                assert message.reply.await_count == 1
            resume.set()
            await ambient
            if phase == 'answered':
                assert message.reply.await_count == 1, 'ambient posted a duplicate answer after direct completion'
            else:
                assert message.reply.await_count == 0, 'ambient posted a busy notice while direct work was active'
            assert not typed
        finally:
            resume.set()
            direct_finish.set()
            await asyncio.gather(ambient, *([direct] if direct else []), return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())
