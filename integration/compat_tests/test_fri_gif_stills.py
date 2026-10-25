"""A gif, uploaded or linked from Tenor or Giphy, is seen as its first frame instead of skipped."""

import asyncio
import base64
import io
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from compat_tests.test_fri_looking import loads, picture, read_harness
from fri.fri_actions import Actions
from fri.fri_images import look, looked_unchanged
from fri.fri_meme_media import GIF_BYTES, STILL_SIDE, allowed_url, load_image, sources

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
