"""A post with an uploaded still beside a video link's card (YouTube, Twitch, an X video): Fri looks at the still,
and at the card's thumbnail only through Discord's preview proxy within the image limits, and says it couldn't
watch the video. Brainstorm round 2, N2a's last gap: such a post used to be skipped whole, and the speaker's own
got the canned "can't watch videos" line despite the still."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import httpx
import pytest

from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_images import message
from compat_tests.test_fri_link_cards import THUMB, outside, read, reader, youtube
from compat_tests.test_fri_looking import loads, picture, read_harness
from fri import fri_chat
from fri.fri_chat import UNSEEN_RULE, Chat
from fri.fri_images import attach, look, looked_unchanged, own_links, unchanged
from fri.fri_meme_media import sources
from fri.fri_mentions import Invitation

QUESTION = 'whats 12 times 12'
BESIDE_RULE = getattr(fri_chat, 'BESIDE_RULE', 'the rule for a still beside unseen media')
UNWATCHED = {'attachments': ['linked video'], 'reason': 'video'}


def own(ask, *, unfurled=True):
    """The speaker's own post: one uploaded still and a YouTube link, its card there on arrival or only later."""
    event = message()
    event.content = ask + ' https://youtu.be/xyz'
    event.author = SimpleNamespace(id=10, bot=False)
    event.embeds = [youtube()] if unfurled else []
    current = copy.copy(event)
    current.embeds = [youtube()]
    event.channel.fetch_message = AsyncMock(return_value=current)
    return event


@pytest.mark.parametrize('ask', ['thoughts?', 'rate this', QUESTION])
@pytest.mark.parametrize('unfurled', [True, False])
def test_the_speakers_own_still_beside_a_video_link_is_seen_and_the_video_marked_unwatched(monkeypatch, ask, unfurled):
    async def scenario():
        load = loads(monkeypatch)
        event, context = own(ask, unfurled=unfurled), {}
        await attach(context, event, event.content)  # No canned line, however the ask reads.
        await own_links(context, event)
        assert context['images'] == ['data:image/png;base64,test.png']
        assert context['image_sources'] == [str(event.attachments[0].url)]
        assert context['own_media_not_inspected'] == UNWATCHED
        assert context['message_link_cards'][0]['site'] == 'YouTube'
        assert [call.args[0] for call in load.await_args_list] == [str(event.attachments[0].url)]  # Never the thumbnail.
        assert await unchanged(context, event)  # Rechecked as Discord shows it now, card and all.
    asyncio.run(scenario())


def test_an_uploaded_clip_beside_a_still_is_still_declined(monkeypatch):
    async def scenario():
        loads(monkeypatch)
        from compat_tests.test_fri_unsupported_media import upload
        event, context = own(QUESTION), {}
        event.attachments = [upload('ok.png'), upload('clip.mp4', 'video/mp4', n=2)]
        await attach(context, event, event.content)
        assert context['own_media_not_inspected']['reason'] == 'video' and 'images' not in context
    asyncio.run(scenario())


def test_someone_elses_still_beside_a_video_card_is_seen_with_the_thumbnail(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, post = read_harness(monkeypatch, tmp_path)
        both = post('look https://youtu.be/xyz', user=70, age=120, attachments=[picture(1, 'meme.png')])
        both.embeds = [youtube(thumb=THUMB.replace('xyz', 'both'))]
        loader = loads(monkeypatch)
        actions = reader(agent)
        try:
            receipt = await read(actions)
            assert receipt['images_inspected'] == 1 and receipt['images_not_inspected'] == 0
            assert actions.turn_context['images'] == ['data:image/png;base64,meme.png',
                                                      'data:image/png;base64,maxresdefault.jpg']
            assert [(call.args[0], call.kwargs) for call in loader.await_args_list] == [
                (str(both.attachments[0].url), {'previews': True}), (THUMB.replace('xyz', 'both'), {'previews': True})]
            upload, thumbnail = actions.turn_context['inspected_images']
            assert upload == {'images': [1], 'source': upload['source']} and upload['source']['id'] == str(both.id)
            assert thumbnail == {'images': [2], 'source': upload['source'], 'still': 'link preview'}
            assert await looked_unchanged(actions.turn_context)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('thumbnail', ['no proxy copy', 'not the preview proxy', 'over the pixel limit', 'no size'])
def test_a_thumbnail_outside_the_proxy_or_its_limits_leaves_only_the_still(monkeypatch, tmp_path, thumbnail):
    async def scenario():
        fri, agent, _, _, post = read_harness(monkeypatch, tmp_path)
        both = post('look https://youtu.be/xyz', user=70, age=120, attachments=[picture(1, 'meme.png')])
        card = {'no proxy copy': youtube(thumb=None), 'over the pixel limit': youtube(width=5000, height=4001),
                'not the preview proxy': youtube(thumb='https://i.ytimg.com/vi/xyz/maxresdefault.jpg'),
                'no size': youtube(width=None, height=None)}[thumbnail]
        both.embeds = [card]
        loads(monkeypatch)
        actions = reader(agent)
        try:
            await read(actions)
            assert actions.turn_context['images'] == ['data:image/png;base64,meme.png']
            assert [entry.get('still') for entry in actions.turn_context['inspected_images']] == [None]
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_four_stills_beside_a_video_card_keep_to_the_image_budget(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, post = read_harness(monkeypatch, tmp_path)
        gallery = post('look https://youtu.be/xyz', user=70, age=120,
                       attachments=[picture(1, f'{n}.png') for n in 'abcd'])
        gallery.embeds = [youtube()]
        loads(monkeypatch)
        actions = reader(agent)
        try:
            await read(actions)
            assert actions.turn_context['images'] == ['data:image/png;base64,'+n+'.png' for n in 'abcd']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_spoilered_bot_or_webhook_post_is_still_not_looked_at(monkeypatch):
    async def scenario():
        loads(monkeypatch)
        for change in ('spoiler', 'bot', 'webhook'):
            post = own('look')
            post.content = '||look|| https://youtu.be/xyz' if change == 'spoiler' else post.content
            post.author = SimpleNamespace(id=10, bot=change == 'bot')
            post.webhook_id = 5 if change == 'webhook' else None
            context = {}
            await look(context, [post])
            assert 'images' not in context, change
    asyncio.run(scenario())


def test_the_meme_reactions_still_skip_a_post_with_a_video_card():
    post = own('look')
    assert sources(post) is None  # fri_memes reads sources() as before: never beside a video it can't see.
    assert sources(post, cards=True) == [str(post.attachments[0].url)]


def test_a_pings_own_still_beside_a_video_card_reaches_the_model_with_the_video_unwatched(monkeypatch, tmp_path):
    async def scenario():
        from compat_tests.test_fri_mentions import attachment
        fri, agent, channel, event, sent, loader = outside(
            monkeypatch, tmp_path, '<@99> thoughts? https://youtu.be/xyz', unfurled=[youtube()])
        event.attachments = [attachment(channel.id)]
        event.embeds = [youtube()]
        try:
            invitation = await Invitation.create(agent, event)
            await fri.invited_talk(invitation, 'thoughts? https://youtu.be/xyz')
            body, = sent
            data = json.loads(body['input'][-1]['content'][0]['text'])
            assert data['own_media_not_inspected'] == UNWATCHED
            assert [part['type'] for part in body['input'][-1]['content']].count('input_image') == 1
            assert BESIDE_RULE in body['instructions'] and UNSEEN_RULE not in body['instructions']
            assert [call.args[0] for call in loader.await_args_list] == [str(event.attachments[0].url)]
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_the_rule_beside_a_still_reaches_the_model_only_then():
    async def scenario():
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(
                {'reply': 'clean setup. didn’t watch the video tho', 'request_draft': None})))+'\n\n')
        chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler))
        await chat.respond(QUESTION, dict(CONTEXT))
        await chat.respond(QUESTION, {**CONTEXT, 'own_media_not_inspected': {'attachments': ['video'], 'reason': 'video'}})
        await chat.respond(QUESTION, {**CONTEXT, 'own_media_not_inspected': UNWATCHED,
                                      'images': ['data:image/png;base64,aGVsbG8=']})
        plain, unseen, beside = seen
        assert BESIDE_RULE not in plain['instructions'] and UNSEEN_RULE not in plain['instructions']
        assert UNSEEN_RULE in unseen['instructions'] and BESIDE_RULE not in unseen['instructions']
        assert BESIDE_RULE in beside['instructions'] and UNSEEN_RULE not in beside['instructions']
    asyncio.run(scenario())
