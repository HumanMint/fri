"""A GIF link (Tenor, Giphy) is seen through the still preview Discord's own proxy made of it.
Nothing here decodes an animation: an uploaded GIF, or an animated preview, is still skipped."""

import asyncio
import copy
import io
import json
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import discord
import httpx
import pytest
from PIL import Image

from compat_tests.test_fri_chat import response_event
from compat_tests.test_fri_looking import loads, picture, read_harness
from compat_tests.test_fri_mentions import environment
from fri import feedback as commands
from fri.fri import Fri
from fri.fri_actions import Actions
from fri.fri_chat import Chat
from fri.fri_images import looked_unchanged
from fri.fri_meme_media import allowed_url, load_image, sources
from fri.fri_mentions import Invitation

PREVIEW = 'https://images-ext-1.discordapp.net/external/abc/https/media.tenor.com/x/dance.png'


def tenor_embed(url=PREVIEW):
    return discord.Embed.from_dict({'type': 'gifv', 'url': 'https://tenor.com/view/dance-123',
                                    'video': {'url': 'https://media.tenor.com/x/dance.mp4'},
                                    'thumbnail': {'proxy_url': url, 'url': 'https://media.tenor.com/x/dance.png'}})


def serve(body, mime):
    return httpx.MockTransport(lambda request: httpx.Response(200, content=body, headers={'content-type': mime}))


def test_only_discords_own_proxy_serves_a_gif_links_preview():
    assert allowed_url(PREVIEW, 1, previews=True)
    assert allowed_url('https://media.discordapp.net/external/abc/x.png', 1)
    for url in ('https://media.tenor.com/x/dance.png', 'https://images-ext-1.discordapp.net.evil.test/external/x',
                'https://images-ext-12.discordapp.net/external/x', 'https://images-ext-1.discordapp.net/attachments/1/x',
                'http://images-ext-1.discordapp.net/external/x'):
        assert not allowed_url(url, 1, previews=True)
    # The meme reactions (and anything else that doesn't ask for previews) keep their old reach.
    assert not allowed_url(PREVIEW, 1)


def test_the_meme_reactions_never_fetch_from_the_preview_proxy(monkeypatch, tmp_path):
    async def scenario():
        fetched = []
        transport = httpx.MockTransport(lambda request: fetched.append(request) or httpx.Response(500))
        with pytest.raises(ValueError, match='outside the Discord CDN scope'):
            await load_image(PREVIEW, 1, transport=transport)  # How fri_memes calls it.
        assert fetched == []
    asyncio.run(scenario())


def test_a_gif_links_preview_counts_only_when_asked_and_an_uploaded_gif_never_does():
    link = SimpleNamespace(attachments=[], content='https://tenor.com/view/dance-123', embeds=[tenor_embed()])
    upload = SimpleNamespace(attachments=[picture(1, 'dance.gif', 'image/gif')], embeds=[], content='')
    assert sources(link) is None and sources(link, gifs=True) == [PREVIEW]
    assert sources(upload) is None and sources(upload, gifs=True) is None


@pytest.mark.parametrize('fmt,mime', [('GIF', 'image/gif'), ('PNG', 'image/png'), ('WEBP', 'image/webp')])
def test_an_animated_preview_is_refused_without_decoding(fmt, mime):
    async def scenario():
        frames = [Image.new('RGB', (16, 16), color) for color in ('red', 'blue')]
        out = io.BytesIO()
        frames[0].save(out, fmt, save_all=True, append_images=frames[1:], duration=100, loop=0)
        with pytest.raises(ValueError):
            await load_image(PREVIEW, 1, transport=serve(out.getvalue(), mime), previews=True)
    asyncio.run(scenario())


def tenor(post, *, preview=PREVIEW, kind='gifv'):
    item = post('https://tenor.com/view/dance-123')
    item.embeds = [SimpleNamespace(type=kind, video=None if kind != 'gifv' else SimpleNamespace(url='x.mp4'),
                                   image=SimpleNamespace(proxy_url=preview) if kind == 'article' else None,
                                   thumbnail=SimpleNamespace(proxy_url=preview))]
    return item


def test_a_read_looks_at_a_tenor_gif_but_not_a_link_preview(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channel, rows, post = read_harness(monkeypatch, tmp_path)
        dance = tenor(post)
        tenor(post, preview='https://images-ext-1.discordapp.net/external/abc/news.png', kind='article')
        loader = loads(monkeypatch)
        actions = Actions(agent, 20, 999, False)
        actions.turn_context, actions.media_allowed = {}, True
        actions.event = SimpleNamespace(id=1, channel=SimpleNamespace(id=999))
        try:
            receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'recent'})
            # The gif link's preview is looked at; an article's preview never is.
            assert receipt['images_inspected'] == 1 and receipt['images_not_inspected'] == 1
            assert [call.args[0] for call in loader.await_args_list] == [PREVIEW]
            row = actions.turn_context['inspected_images'][0]
            assert row['source']['id'] == str(dance.id) and row['still'] == 'gif preview'
            assert await looked_unchanged(actions.turn_context)
            dance.embeds = [SimpleNamespace(type='gifv', video=SimpleNamespace(url='y.mp4'), image=None,
                                            thumbnail=SimpleNamespace(proxy_url=PREVIEW.replace('dance', 'other')))]
            assert not await looked_unchanged(actions.turn_context)  # The poster swapped the gif.
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['none', 'removed', 'swapped'])
def test_a_mentions_nearby_gif_swapped_after_the_look_fails_the_recheck(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, _, _, _, message, _, rows = environment(monkeypatch, tmp_path)
        post = message('https://tenor.com/view/dance-123', user=30, age=60)
        post.embeds = [tenor_embed()]
        event = message('<@99> which gif is this?')
        loads(monkeypatch)
        try:
            invitation = await Invitation.create(agent, event)
            assert (await invitation.admit())['allowed']
            await invitation.collect()
            context = {}
            assert (await invitation.look(context))['images_inspected'] == 1
            if change != 'none':  # Same text, attachments and edit time: only the embed differs.
                changed = copy.copy(post)
                changed.embeds = [] if change == 'removed' else [tenor_embed(PREVIEW.replace('dance', 'other'))]
                rows[post.id] = changed
            assert await invitation.current() == (change == 'none')
            assert await looked_unchanged(context) == (change == 'none')
        finally:
            await fri.close()
    asyncio.run(scenario())


def outside_world(monkeypatch, tmp_path, caption='https://tenor.com/view/dance-123'):
    """An outside-channel mention below a Tenor post, with a model whose requests land in `sent`."""
    fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path)
    fri.data = tmp_path
    post = message(caption, user=30, age=60)
    post.embeds = [tenor_embed()]
    event = message('<@99> pick the best gif above')
    loads(monkeypatch)
    fri.chat_once = MethodType(Fri.chat_once, fri)
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        answer = {'response_kind': 'reply', 'reaction': None, 'reply': 'the dancing one',
                  'request_draft': None, 'action': None, 'notes': [], 'allow_closing': False}
        return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(answer)))+'\n\n')
    fri.chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler))
    return fri, agent, store, channel, rows, post, event, sent


@pytest.mark.parametrize('change', ['none', 'removed', 'swapped'])
def test_an_outside_mentions_gif_preview_is_rechecked_before_the_model_sees_it(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, _, _, rows, post, event, sent = outside_world(monkeypatch, tmp_path)

        async def headers():  # The poster swaps or removes the gif while Fri signs in.
            if change != 'none':
                changed = copy.copy(post)
                changed.embeds = [] if change == 'removed' else [tenor_embed(PREVIEW.replace('dance', 'other'))]
                rows[post.id] = changed
            return {'Authorization': 'Bearer synthetic'}
        fri.chat.headers = headers
        try:
            invitation = await Invitation.create(agent, event)
            if change == 'none':
                await fri.invited_talk(invitation, 'pick the best gif above')
                assert len(sent) == 1
                return
            with pytest.raises(commands.UserFeedbackCheckFailure):
                await fri.invited_talk(invitation, 'pick the best gif above')
            assert sent == []
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_an_opt_out_during_the_last_refetch_keeps_an_outside_gif_out(monkeypatch, tmp_path):
    """The invitation's own check refetches the post, previews included, then checks privacy last."""
    async def scenario():
        fri, agent, store, channel, rows, post, event, sent = outside_world(
            monkeypatch, tmp_path, caption='https://tenor.com/view/dance-123 my caption')
        signed_in = False

        async def headers():
            nonlocal signed_in
            signed_in = True
            return {'Authorization': 'Bearer synthetic'}

        async def fetch(mid):
            if signed_in and mid == post.id:  # The poster opts out while Fri refetches their post.
                store.personal_control('30', 'disable')
            return rows[mid]
        fri.chat.headers = headers
        channel.fetch_message = AsyncMock(side_effect=fetch)
        try:
            invitation = await Invitation.create(agent, event)
            with pytest.raises(commands.UserFeedbackCheckFailure):
                await fri.invited_talk(invitation, 'pick the best gif above')
            assert sent == []
        finally:
            await fri.close()
    asyncio.run(scenario())
