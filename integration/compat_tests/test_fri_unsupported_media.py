"""The speaker's own media Fri can't inspect (a gif, a clip, three photos, a big or spoilered one) never
replaces the turn with a format notice. A question the text carries on its own still gets answered, with
the model told what it didn't see; an ask about the media itself gets a short local line and no model call.
The limits themselves stay: nothing declined is ever downloaded, and it is rechecked like an inspected post."""

import asyncio
import copy
import json
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import httpx
import pytest

from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_images import message
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_social import api, source  # noqa: F401
from fri import feedback as commands
from fri.fri import Fri
from fri.fri_actions import Actions
from fri.fri_chat import Chat
from fri.fri_images import attach, unchanged
from fri.fri_mentions import Invitation

QUESTION = 'whats 12 times 12'
MEDIA_ONLY = ('thoughts?', 'lmao', 'rate this', 'which one', 'is this ai', 'what do you think of this clip', '😭')
FORMAT_WORDS = ('png', 'jpeg', 'webp', 'mb', 'attach', 'resend', 're-upload', 'supported')


def upload(name, kind='image/png', *, n=1, channel=456, size=100, width=100, height=100, spoiler=False):
    return SimpleNamespace(url=f'https://cdn.discordapp.com/attachments/{channel}/{n}/{name}', content_type=kind,
                           size=size, width=width, height=height, is_spoiler=lambda: spoiler)


def media(case, channel=456):
    """Own uploads sources() declines, and the reason Fri gives for each."""
    return {
        'gif': ([upload('dance.gif', 'image/gif', channel=channel)], 'gif'),
        'clip': ([upload('clip.mp4', 'video/mp4', channel=channel)], 'video'),
        'three photos': ([upload(f'{n}.png', n=n, channel=channel) for n in (1, 2, 3)], 'more than two images'),
        'big photo': ([upload('big.png', size=5 * 1024 * 1024, channel=channel)], 'image too large'),
        'huge photo': ([upload('wide.png', width=8000, height=4000, channel=channel)], 'image too large'),
        'spoiler': ([upload('secret.png', spoiler=True, channel=channel)], 'spoiler'),
        'photo and gif': ([upload('ok.png', channel=channel), upload('dance.gif', 'image/gif', n=2, channel=channel)], 'gif'),
    }[case]


CASES = ('gif', 'clip', 'three photos', 'big photo', 'huge photo', 'spoiler', 'photo and gif')


def loader(monkeypatch, **kwargs):
    load = AsyncMock(return_value='data:image/png;base64,aGVsbG8=', **kwargs)
    monkeypatch.setattr('fri.fri_images.load_image', load)
    return load


@pytest.mark.parametrize('case', CASES)
def test_a_text_question_goes_on_with_the_media_marked_unseen(monkeypatch, case):
    async def scenario():
        load = loader(monkeypatch)
        event, context = message(), {}
        event.content, (event.attachments, reason) = QUESTION, media(case)
        await attach(context, event, QUESTION)
        load.assert_not_awaited()  # Declined media is never downloaded, spoilers and big ones included.
        assert 'images' not in context and 'image_sources' not in context
        from fri.fri_images import media as kinds
        assert context['own_media_not_inspected'] == {'attachments': kinds(event), 'reason': reason}
        assert 'dance' not in json.dumps(context['own_media_not_inspected'])  # No file names or links.
    asyncio.run(scenario())


@pytest.mark.parametrize('case', CASES)
@pytest.mark.parametrize('ask', MEDIA_ONLY)
def test_an_ask_about_the_media_itself_gets_a_short_local_line(monkeypatch, case, ask):
    async def scenario():
        load = loader(monkeypatch)
        event, context = message(), {}
        event.content, (event.attachments, _) = ask, media(case)
        with pytest.raises(commands.UserFeedbackCheckFailure) as declined:
            await attach(context, event, ask)
        line = str(declined.value)
        assert 0 < len(line) <= 60 and line == line.lower()
        assert not any(word in line for word in FORMAT_WORDS), line
        assert not context
        load.assert_not_awaited()
    asyncio.run(scenario())


def test_the_two_image_limit_and_spoiler_markup_still_hold(monkeypatch):
    async def scenario():
        load = loader(monkeypatch)
        event, context = message(), {}
        event.attachments = [upload('1.png'), upload('2.png', n=2)]
        await attach(context, event, QUESTION)  # Two still photos load as before.
        assert len(context['images']) == 2 and load.await_count == 2
        assert 'own_media_not_inspected' not in context
        load.reset_mock()
        event, context = message(), {}
        event.content = '||the ending|| ' + QUESTION
        await attach(context, event, event.content)  # Spoiler markup in the caption excludes its photo.
        assert context['own_media_not_inspected']['reason'] == 'spoiler' and 'images' not in context
        load.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('error,reason', [(httpx.ConnectError('cdn down'), 'did not load'),
                                          (TimeoutError(), 'did not load'),
                                          (ValueError('Not a supported still image'), 'unsupported')])
def test_a_failed_load_is_not_a_format_problem(monkeypatch, error, reason):
    async def scenario():
        loader(monkeypatch, side_effect=error)
        event, context = message(), {}
        event.content = QUESTION
        await attach(context, event, QUESTION)
        assert context['own_media_not_inspected']['reason'] == reason and 'images' not in context
        with pytest.raises(commands.UserFeedbackCheckFailure) as declined:
            await attach({}, event, 'thoughts?')
        line = str(declined.value)
        assert not any(word in line for word in FORMAT_WORDS), line
        assert ('load' in line) == (reason == 'did not load')  # A CDN failure never reads as an unsupported kind.
    asyncio.run(scenario())


def test_a_repeated_attach_keeps_what_an_earlier_call_inspected_or_declined(monkeypatch):
    async def scenario():
        event, context = message(), {}
        event.content = QUESTION
        load = loader(monkeypatch)
        await attach(context, event, QUESTION)
        loader(monkeypatch, side_effect=httpx.ConnectError('cdn down'))
        await attach(context, event, QUESTION)  # A read tool attaches again; the CDN blips this time.
        assert context['images'] == ['data:image/png;base64,aGVsbG8='] and 'own_media_not_inspected' not in context
        load.reset_mock()
        gif, context = message(), {}
        gif.content, (gif.attachments, _) = QUESTION, media('gif')
        await attach(context, gif, QUESTION)
        first = dict(context)
        await attach(context, gif, QUESTION)
        assert context == first
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['none', 'deleted', 'removed', 'edited'])
@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('kind', ['clip', 'failed load'])
def test_declined_media_is_rechecked_like_an_inspected_post(monkeypatch, change, paired, kind):
    async def scenario():
        event = message()
        event.content = QUESTION
        if kind == 'clip':
            loader(monkeypatch)
            event.attachments = media('clip')[0]
        else:  # A still photo the CDN didn't serve: edited or deleted while it failed, the answer still waits.
            loader(monkeypatch, side_effect=httpx.ConnectError('cdn down'))
        post = event
        if paired:  # The clip came on its own just before the caption.
            post = copy.copy(event)
            post.id, post.content = event.id-1, ''
            event.attachments = []
        context = {}
        await attach(context, event, QUESTION, source=post)
        assert context['own_media_not_inspected']['reason'] == ('video' if kind == 'clip' else 'did not load')
        current = copy.copy(post)
        if change == 'removed':
            current.attachments = []
        elif change == 'edited':
            current.edited_at = 1
        async def fetch(identifier):
            if identifier == post.id and change == 'deleted':
                raise discord.NotFound(Mock(status=404), 'gone')
            return current if identifier == post.id else event
        event.channel.fetch_message = AsyncMock(side_effect=fetch)
        assert await unchanged(context, event) == (change == 'none')
        from fri.fri_images import image_context
        assert await unchanged(image_context(context), event) == (change == 'none')  # What a tool checks.
    asyncio.run(scenario())


def test_the_status_reaches_the_model_only_when_there_is_one():
    async def scenario():
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(
                {'reply': '144. and no, i can’t watch clips', 'request_draft': None})))+'\n\n')
        chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler))
        await chat.respond(QUESTION, dict(CONTEXT))
        status = {'attachments': ['video'], 'reason': 'video'}
        await chat.respond(QUESTION, {**CONTEXT, 'own_media_not_inspected': status})
        plain, unseen = seen
        assert 'own_media_not_inspected' not in plain['instructions']
        assert 'own_media_not_inspected' not in plain['input'][-1]['content']
        assert json.loads(unseen['input'][-1]['content'])['own_media_not_inspected'] == status
        assert 'own_media_not_inspected' in unseen['instructions']
        # The rule stays small: one short paragraph.
        assert len(unseen['instructions']) - len(plain['instructions']) < 500
    asyncio.run(scenario())


def chat_world(api, monkeypatch, tmp_path, *, public, paired, attachments, caption):  # noqa: F811
    """The real chat path with a member's own declined upload, its model requests landing in `seen`."""
    client, call = api
    fri, _, _, _ = harness(monkeypatch, tmp_path)
    fri.api = call
    event = message()
    event.id, event.content = int(source(60, public=public).id), caption
    channel = 456 if public else 999
    event.guild = fri.get_channel(channel).guild if public else None
    event.author.display_name = 'Member'
    event.channel = Mock(spec=discord.TextChannel if public else discord.DMChannel, id=channel)
    event.channel.fetch_message = AsyncMock(return_value=event)
    event.reference = None
    event.attachments = attachments
    if paired:
        post = copy.copy(event)
        post.id, post.content = event.id-(13_000 << 22), ''
        post.author.bot = False
        event.attachments = []
        async def history(**kwargs):
            yield post
        event.channel.history = history
        event.channel.fetch_message = AsyncMock(side_effect=lambda identifier: post if identifier == post.id else event)
    load = loader(monkeypatch)
    monkeypatch.setattr(Actions, 'available', AsyncMock(return_value={}))
    seen = []
    def transport(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(
            {'reply': '144. can’t watch the clip though', 'request_draft': None})))+'\n\n')
    fri.chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(transport))
    return client, fri, event, channel, load, seen


@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('public', [False, True])
def test_a_clip_with_a_question_is_answered_through_the_chat_path(api, monkeypatch, tmp_path, public, paired):  # noqa: F811
    async def scenario():
        visibility = 'public' if public else 'private'
        client, fri, event, channel, load, seen = chat_world(
            api, monkeypatch, tmp_path, public=public, paired=paired,
            attachments=media('clip', 456 if public else 999)[0], caption=QUESTION)
        try:
            result = await fri.talk(123, channel, 10, QUESTION, visibility, event=event)
            assert result.status == 'completed' and len(seen) == 1
            supplied = seen[0]['input'][-1]['content']
            assert isinstance(supplied, str), 'No image was inspected, so none is sent'
            assert json.loads(supplied)['own_media_not_inspected'] == {'attachments': ['video'], 'reason': 'video'}
            load.assert_not_awaited()
            saved = client.app.state.store.chat_context('123', f'{visibility}:{channel}', '10')
            assert 'cdn.discordapp.com' not in str(saved) and 'clip.mp4' not in str(saved)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('paired', [False, True])
def test_a_media_only_ask_through_the_chat_path_spends_no_model_call(api, monkeypatch, tmp_path, paired):  # noqa: F811
    async def scenario():
        _, fri, event, channel, load, seen = chat_world(
            api, monkeypatch, tmp_path, public=True, paired=paired,
            attachments=media('gif')[0], caption='thoughts?')
        try:
            with pytest.raises(commands.UserFeedbackCheckFailure, match='gif'):
                await fri.talk(123, channel, 10, 'thoughts?', 'public', event=event)
            assert seen == [] and not load.await_count
        finally:
            await fri.close()
    asyncio.run(scenario())


def outside(monkeypatch, tmp_path, text):
    """An outside-channel mention carrying its own gif, with a model whose requests land in `sent`."""
    fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path)
    fri.data = tmp_path
    event = message('<@99> '+text, attachments=media('gif', channel.id)[0])
    load = loader(monkeypatch)
    fri.chat_once = MethodType(Fri.chat_once, fri)
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        answer = {'response_kind': 'reply', 'reaction': None, 'reply': '144. gifs are beyond me though',
                  'request_draft': None, 'action': None, 'notes': [], 'allow_closing': False}
        return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(answer)))+'\n\n')
    fri.chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler))
    return fri, agent, event, load, sent


def test_an_outside_mention_with_its_own_gif_still_answers_the_question(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, event, load, sent = outside(monkeypatch, tmp_path, QUESTION)
        try:
            invitation = await Invitation.create(agent, event)
            result = await fri.invited_talk(invitation, QUESTION)
            assert result.reply.startswith('144') and len(sent) == 1
            status = json.loads(sent[0]['input'][-1]['content'])['own_media_not_inspected']
            assert status == {'attachments': ['gif'], 'reason': 'gif'}
            load.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_an_outside_mention_about_its_own_gif_gets_the_local_line(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, event, load, sent = outside(monkeypatch, tmp_path, 'thoughts?')
        try:
            invitation = await Invitation.create(agent, event)
            with pytest.raises(commands.UserFeedbackCheckFailure, match='gif'):
                await fri.invited_talk(invitation, 'thoughts?')
            assert sent == [] and not load.await_count
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_the_image_eval_carries_an_unseen_clip_case():
    from fri.fri_image_eval import cases
    unseen = [case for case in cases() if case.get('own_media_not_inspected')]
    assert unseen and all(case['needs'] and case['banned'] for case in unseen)

    class Recorder:
        contexts = []

        async def respond(self, text, context):
            self.contexts.append(context)
            from fri.fri_chat import ChatResult
            return ChatResult(status='completed', reply='144. not watching that clip though')

    async def scenario():
        from fri.fri_eval import evaluate
        recorder = Recorder()
        async for _ in evaluate(recorder, 'model', 'low', suite='images'):
            pass
        assert any(context.get('own_media_not_inspected') for context in recorder.contexts)
    asyncio.run(scenario())
