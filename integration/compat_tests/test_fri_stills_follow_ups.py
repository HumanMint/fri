"""Two follow-ups to looking at a still beside a video link's card (#280): a card that unfurls late still gets the
video-unwatched note (fri #307), and a post's uploads come before its video thumbnails in the image budget (fri
#308)."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from compat_tests.test_fri_chat import response_event
from compat_tests.test_fri_link_cards import youtube
from compat_tests.test_fri_looking import loads, picture
from compat_tests.test_fri_mentions import attachment, environment
from compat_tests.test_fri_stills_beside_video import BESIDE_RULE, UNWATCHED, own
from fri.fri_chat import UNSEEN_RULE, Chat
from fri.fri_images import look, looked_unchanged
from fri.fri_mentions import Invitation


def test_a_video_card_unfurling_before_the_last_image_check_is_said_to_be_unwatched(monkeypatch, tmp_path):
    """fri #307: the speaker's own still beside a YouTube link, in a configured channel. Fri's fetch for its own
    links finds no card yet, then the card unfurls before the request's last image check, which a video card
    doesn't stop (#280). The request carried the still with nothing saying a video went unwatched. The last check
    refreshes that from the post as Discord shows it: the note, the card's text and the rule beside a still."""
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        event = message('fri whats 12 times 12 https://youtu.be/xyz')
        event.attachments = [attachment(channel.id)]
        event.embeds = []
        rows[event.id] = copy.copy(event)  # Discord's copy: no card yet.
        loads(monkeypatch)
        sent = []

        def unfurl():
            late = copy.copy(rows[event.id])
            late.embeds = [youtube()]
            rows[event.id] = late

        def access_token():
            unfurl()  # Signed in right before the last checks: the card is there by then.
            return 'synthetic'

        def handler(request):
            sent.append(json.loads(request.content))
            answer = {'response_kind': 'reply', 'reaction': None, 'reply': '144. didn’t watch the video tho',
                      'request_draft': None, 'action': None, 'notes': [], 'allow_closing': False}
            return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(answer)))+'\n\n')
        fri.chat = Chat(SimpleNamespace(access_token=access_token), httpx.MockTransport(handler))
        del fri.chat_once
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            body, = sent
            content = body['input'][-1]['content']
            data = json.loads(content[0]['text'])
            assert [part['type'] for part in content].count('input_image') == 1  # Their still, never the thumbnail.
            assert data['own_media_not_inspected'] == UNWATCHED
            assert data['message_link_cards'][0]['site'] == 'YouTube'
            assert BESIDE_RULE in body['instructions'] and UNSEEN_RULE not in body['instructions']
        finally:
            await fri.close()
    asyncio.run(scenario())


def gallery(uploads=2):
    """Another member's post: uploaded stills and a YouTube link, its card unfurled."""
    post = own('look')
    post.attachments = [picture(post.channel.id, f'{n}.png') for n in 'abcd'[:uploads]]
    post.author = SimpleNamespace(id=70, bot=False, display_name='member 70')
    post.webhook_id = None
    post.guild = SimpleNamespace(id=123)
    post.embeds = [youtube()]
    post.channel.fetch_message = AsyncMock(return_value=post)
    return post


def test_a_posts_uploads_come_before_its_video_thumbnail_in_the_image_budget(monkeypatch):
    """fri #308: two images already take the turn's budget, and another post has two uploads and a video card's
    thumbnail. The three didn't fit the two slots left, so the whole post was skipped, uploads and all. The uploads
    come first; the thumbnail only fills what's left. Checked again later as what was shown."""
    async def scenario():
        load = loads(monkeypatch)
        post = gallery()
        context = {'images': ['data:image/png;base64,one', 'data:image/png;base64,two']}
        receipt = await look(context, [post])
        assert receipt == {'images_inspected': 1, 'images_not_inspected': 0}
        assert [call.args[0] for call in load.await_args_list] == [str(a.url) for a in post.attachments]
        assert context['images'][2:] == ['data:image/png;base64,a.png', 'data:image/png;base64,b.png']
        assert [entry.get('still') for entry in context['inspected_images']] == [None]
        assert context['inspected_images'][0]['images'] == [3, 4]
        assert await looked_unchanged(context)
    asyncio.run(scenario())


def test_a_thumbnail_fills_only_what_the_uploads_leave(monkeypatch):
    """fri #308: with three slots left, two uploads and the thumbnail all fit, as before. With one left, a single
    upload is looked at and its thumbnail isn't."""
    async def scenario():
        load = loads(monkeypatch)
        post = gallery()
        context = {'images': ['data:image/png;base64,one']}
        await look(context, [post])
        assert len(load.await_args_list) == 3
        assert [entry.get('still') for entry in context['inspected_images']] == [None, 'link preview']
        load = loads(monkeypatch)
        single = gallery(uploads=1)
        context = {'images': ['data:image/png;base64,'+n for n in ('one', 'two', 'three')]}
        await look(context, [single])
        assert [call.args[0] for call in load.await_args_list] == [str(single.attachments[0].url)]
        assert [entry.get('still') for entry in context['inspected_images']] == [None]
        assert await looked_unchanged(context)
    asyncio.run(scenario())


def test_a_pings_check_finds_the_pictures_it_showed_when_a_thumbnail_was_left_out(monkeypatch):
    """fri #308, a ping's own check (Invitation.pictures_unchanged): the uploads it showed without the thumbnail are
    found again as shown, not refused for the thumbnail it left out."""
    async def scenario():
        loads(monkeypatch)
        post = gallery()
        invitation = Invitation.__new__(Invitation)
        invitation.pictures = {}
        context = {'images': ['data:image/png;base64,one', 'data:image/png;base64,two']}
        await look(context, [post], invitation=invitation)
        assert list(invitation.pictures) == [post.id]
        assert await invitation.pictures_unchanged()
    asyncio.run(scenario())
