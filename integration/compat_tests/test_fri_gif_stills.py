"""A gif, uploaded or linked from Tenor or Giphy, is seen as its first frame instead of skipped."""

import asyncio
import base64
import copy
import io
import json
import struct
import zlib
from functools import partial
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

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
from fri.fri_images import attach, look, looked_unchanged
from fri.fri_meme_media import GIF_BYTES, STILL_SIDE, Still, allowed_url, load_image, sources
from fri.fri_mentions import Invitation

ATTACHMENT = 'https://cdn.discordapp.com/attachments/1/2/dance.gif'
PREVIEW = 'https://images-ext-1.discordapp.net/external/abc/https/media.tenor.com/x/dance.png'


def animation(fmt, size=(64, 48), colors=((255, 0, 0), (0, 0, 255))):
    """Two frames, red then blue, so the first frame is easy to tell apart."""
    frames = [Image.new('RGB', size, color) for color in colors]
    out = io.BytesIO()
    frames[0].save(out, fmt, save_all=True, append_images=frames[1:], duration=100, loop=0)
    return out.getvalue()


def serve(body, mime):
    return httpx.MockTransport(lambda request: httpx.Response(200, content=body, headers={'content-type': mime}))


def decoded(url):
    assert url.startswith('data:image/png;base64,')
    return Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1])))


@pytest.mark.parametrize('fmt,mime', [('GIF', 'image/gif'), ('PNG', 'image/png'), ('WEBP', 'image/webp')])
def test_an_animation_comes_back_as_its_first_frame(fmt, mime):
    async def scenario():
        body = animation(fmt, size=(3000, 1500))
        still = decoded(await load_image(ATTACHMENT, 1, transport=serve(body, mime), gifs=True))
        assert getattr(still, 'n_frames', 1) == 1 and max(still.size) == STILL_SIDE
        red, _, blue = still.convert('RGB').getpixel((10, 10))
        assert red > 200 and blue < 50  # The red first frame, not the blue second (WebP is lossy).
        # The meme pipeline and other callers keep refusing animations.
        with pytest.raises(ValueError):
            await load_image(ATTACHMENT, 1, transport=serve(body, mime))
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['not_a_gif', 'too_big', 'too_many_pixels', 'static_too_big'])
def test_a_gif_that_isnt_one_or_is_too_big_is_refused(case):
    async def scenario():
        body, mime = {
            'not_a_gif': (b'GIF89a' + b'\x00' * 40, 'image/gif'),
            'too_big': (b'GIF89a' + b'\x00' * (GIF_BYTES + 1), 'image/gif'),
            'too_many_pixels': (animation('GIF', size=(6000, 6000)), 'image/gif'),
            # Only an animation earns the larger limit; a still keeps 4 MB.
            'static_too_big': (b'\x89PNG\r\n\x1a\n' + b'\x00' * (5 * 1024 * 1024), 'image/png'),
        }[case]
        with pytest.raises(ValueError):
            await load_image(ATTACHMENT, 1, transport=serve(body, mime), gifs=True)
    asyncio.run(scenario())


def test_only_discords_own_proxy_serves_a_gif_links_preview():
    assert allowed_url(PREVIEW, 1)
    assert allowed_url('https://media.discordapp.net/external/abc/x.png', 1)
    for url in ('https://media.tenor.com/x/dance.png', 'https://images-ext-1.discordapp.net.evil.test/external/x',
                'https://images-ext-12.discordapp.net/external/x', 'https://images-ext-1.discordapp.net/attachments/1/x',
                'http://images-ext-1.discordapp.net/external/x'):
        assert not allowed_url(url, 1)


def tenor(rows, post, *, preview=PREVIEW, kind='gifv'):
    item = post('https://tenor.com/view/dance-123')
    item.embeds = [SimpleNamespace(type=kind, video=None if kind != 'gifv' else SimpleNamespace(url='x.mp4'),
                                   image=SimpleNamespace(proxy_url=preview) if kind == 'article' else None,
                                   thumbnail=SimpleNamespace(proxy_url=preview))]
    return item


def test_sources_count_a_gif_only_when_asked():
    gif = SimpleNamespace(attachments=[picture(1, 'dance.gif', 'image/gif')], embeds=[], content='')
    link = SimpleNamespace(attachments=[], content='https://tenor.com/view/dance-123', embeds=[SimpleNamespace(
        type='gifv', video=SimpleNamespace(url='x.mp4'), image=None, thumbnail=SimpleNamespace(proxy_url=PREVIEW))])
    assert sources(gif) is None and sources(link) is None  # The meme pipeline's view is unchanged.
    assert sources(gif, gifs=True) == [ATTACHMENT.replace('/1/2/', '/1/1/')]
    assert sources(link, gifs=True) == [PREVIEW]


def test_a_read_looks_at_a_tenor_gif_but_not_a_link_preview(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channel, rows, post = read_harness(monkeypatch, tmp_path)
        dance = tenor(rows, post)
        article = tenor(rows, post, preview='https://images-ext-1.discordapp.net/external/abc/news.png', kind='article')
        loader = loads(monkeypatch)
        actions = Actions(agent, 20, 999, False)
        actions.turn_context, actions.media_allowed = {}, True
        actions.event = SimpleNamespace(id=1, channel=SimpleNamespace(id=999))
        try:
            receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'recent'})
            # The gif link's preview is looked at as a still; an article's preview never is.
            assert receipt['images_inspected'] == 1 and receipt['images_not_inspected'] == 1
            assert [call.args[0] for call in loader.await_args_list] == [PREVIEW]
            row = actions.turn_context['inspected_images'][0]
            assert row['source']['id'] == str(dance.id) and row['still'] == 'gif first frame'
            assert article.id != dance.id and await looked_unchanged(actions.turn_context)
            # The poster swaps the gif: what Fri saw is gone.
            dance.embeds = [SimpleNamespace(type='gifv', video=SimpleNamespace(url='y.mp4'), image=None,
                                            thumbnail=SimpleNamespace(proxy_url=PREVIEW.replace('dance', 'other')))]
            assert not await looked_unchanged(actions.turn_context)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_an_uploaded_gif_the_speaker_replied_to_is_looked_at(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channel, rows, post = read_harness(monkeypatch, tmp_path)
        gif = post('', attachments=[picture(1, 'dance.gif', 'image/gif')])
        loads(monkeypatch)
        context = {}
        try:
            assert (await look(context, [gif]))['images_inspected'] == 1
            assert context['inspected_images'][0]['still'] == 'gif first frame'
        finally:
            await fri.close()
    asyncio.run(scenario())


def broken_apng():
    """A two-frame APNG with an empty iCCP chunk after its first IDAT: Pillow opens it, then a
    decoder raises IndexError (the round-1 review's fixture)."""
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    out = io.BytesIO()
    Image.new('RGB', (16, 16), 'red').save(out, 'PNG', save_all=True,
                                         append_images=[Image.new('RGB', (16, 16), 'blue')], duration=100, loop=0)
    body = out.getvalue()
    start = body.index(b'IDAT') - 4
    end = start + struct.unpack('>I', body[start:start+4])[0] + 12
    return body[:end] + chunk(b'iCCP', b'') + body[end:]


@pytest.mark.parametrize('path', ['load', 'look', 'attach'])
def test_an_animation_pillow_chokes_on_is_just_not_inspected(monkeypatch, path):
    async def scenario():
        transport = serve(broken_apng(), 'image/png')
        if path == 'load':
            with pytest.raises(ValueError):
                await load_image(ATTACHMENT, 1, transport=transport, gifs=True)
            return
        monkeypatch.setattr('fri.fri_images.load_image', partial(load_image, transport=transport))
        image = picture(1, 'broken.png')
        post = Mock(spec=discord.Message, id=123, content='what is this?', channel=SimpleNamespace(id=1),
                    guild=SimpleNamespace(id=123), author=SimpleNamespace(id=20, bot=False), webhook_id=None,
                    attachments=[image], embeds=[], edited_at=None)
        if path == 'look':  # Skipped, not a crash.
            assert (await look({}, [post])) == {'images_inspected': 0, 'images_not_inspected': 1}
        else:  # The usual "couldn't inspect that image" answer, not a crash.
            from fri import feedback as commands
            with pytest.raises(commands.UserFeedbackCheckFailure, match='couldn’t inspect'):
                await attach({}, post, post.content)
    asyncio.run(scenario())


def tenor_embed(url=PREVIEW):
    return discord.Embed.from_dict({'type': 'gifv', 'url': 'https://tenor.com/view/dance-123',
                                    'video': {'url': 'https://media.tenor.com/x/dance.mp4'},
                                    'thumbnail': {'proxy_url': url, 'url': 'https://media.tenor.com/x/dance.png'}})


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
            assert await looked_unchanged(context) == (change == 'none')
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_the_speakers_own_gif_is_labelled_a_first_frame_for_the_model(monkeypatch):
    async def scenario():
        async def load(url, channel, **_):
            return Still('data:image/png;base64,frame')
        monkeypatch.setattr('fri.fri_images.load_image', load)
        own = Mock(spec=discord.Message, id=123, content='what is this doing?', channel=SimpleNamespace(id=1),
                   guild=SimpleNamespace(id=123), author=SimpleNamespace(id=20, bot=False), webhook_id=None,
                   attachments=[picture(1, 'dance.gif', 'image/gif')], embeds=[], edited_at=None)
        context = {}
        await attach(context, own, own.content)
        assert context['image_still'] == 'gif first frame'
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(500)
        chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler))
        from compat_tests.test_fri_chat import CONTEXT
        await chat.respond('what is this doing?', {**CONTEXT, **context})
        sent = seen[0]['input'][-1]['content']
        assert json.loads(sent[0]['text'])['speaker_image_still'] == 'gif first frame'
    asyncio.run(scenario())


def test_a_nearby_animated_png_or_webp_is_labelled_too(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channel, rows, post = read_harness(monkeypatch, tmp_path)
        apng = post('', attachments=[picture(1, 'spin.png')])

        async def load(url, channel, **_):  # It downloads as an animated PNG and comes back as a frame.
            return Still('data:image/png;base64,frame')
        monkeypatch.setattr('fri.fri_images.load_image', load)
        context = {}
        try:
            await look(context, [apng])
            assert context['inspected_images'][0]['still'] == 'gif first frame'
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['none', 'removed', 'swapped'])
def test_an_outside_mentions_gif_preview_is_rechecked_before_the_model_sees_it(monkeypatch, tmp_path, change):
    """Outside a configured channel the invitation's own check runs before the request; its
    fingerprint doesn't cover embeds, so a gif link's preview is refetched too."""
    async def scenario():
        fri, agent, _, _, _, message, _, rows = environment(monkeypatch, tmp_path)
        fri.data = tmp_path
        post = message('https://tenor.com/view/dance-123', user=30, age=60)
        post.embeds = [tenor_embed()]
        event = message('<@99> which gif is this?')
        loads(monkeypatch)
        fri.chat_once = MethodType(Fri.chat_once, fri)
        sent = []

        def handler(request):
            sent.append(json.loads(request.content))
            answer = {'response_kind': 'reply', 'reaction': None, 'reply': 'the dancing one',
                      'request_draft': None, 'action': None, 'notes': [], 'allow_closing': False}
            return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(answer)))+'\n\n')
        fri.chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler))

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
                await fri.invited_talk(invitation, 'which gif is this?')
                assert len(sent) == 1
                return
            with pytest.raises(commands.UserFeedbackCheckFailure):
                await fri.invited_talk(invitation, 'which gif is this?')
            assert sent == []
        finally:
            await fri.close()
    asyncio.run(scenario())
