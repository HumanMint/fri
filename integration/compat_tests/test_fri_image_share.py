"""Requested artwork exercises download, real decoding, selection and reply boundaries offline."""

import asyncio
import copy
import datetime as dt
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import httpx
import pytest
from PIL import Image, PngImagePlugin

from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_social import api, source  # noqa: F401
from fri.fri_actions import Actions
from fri.fri_chat import ChatResult
from fri.fri_image_share import (
    API,
    MAX_BYTES,
    ImageShare,
    PreparedImage,
    cdn_url,
    normalize,
    read,
    send_reply,
)

QUERY = 'share a random sfw image featuring hatsune miku from danbooru'
DETAILED_QUERY = ("let's try another one\n\n"
    'to represent a theoretical japanese word for gaming, can you share a link to a random sfw '
    'image featuring hatsune miku off of danbooru but with relevant tags and context that would '
    'be cool to "represent" the japanese word of gaming and be framed as a theoretical '
    '"miku word of the day"')
CDN = 'https://cdn.donmai.us/original/ab/cd/'+'a'*32+'.png'
LINK_QUERY = ('share a random sfw image featuring hatsune miku from danbooru, with a relaxed '
    'gaming theme, blue lighting, a controller in her hands and a visible screen in the '
    'background. for inspiration use this original post: a good reference: '
    'https://danbooru.donmai.us/posts/123')


def pixels(color='red', fmt='PNG', **kwargs):
    out = io.BytesIO()
    Image.new('RGB', (32, 24), color).save(out, fmt, **kwargs)
    return out.getvalue()


def post(number=1, **changes):
    return {'id': number, 'rating': 'g', 'is_deleted': False, 'is_banned': False,
            'is_flagged': False, 'is_pending': False, 'file_size': 100,
            'image_width': 32, 'image_height': 24, 'file_url': CDN,
            'tag_string_artist': 'some_artist', **changes}


def setup(monkeypatch, tmp_path):
    fri, agent, channels, _ = harness(monkeypatch, tmp_path)
    agent.state['web_search_enabled'] = True
    event = Mock(spec=discord.Message)
    event.id = int(source(public=True).id)
    event.created_at = dt.datetime.now(dt.timezone.utc)
    event.content, event.edited_at, event.webhook_id = QUERY, None, None
    event.author = SimpleNamespace(id=10, bot=False)
    event.channel = channels[456]
    event.channel.fetch_message = AsyncMock(return_value=event)
    event.guild = event.channel.guild
    event.guild.me = SimpleNamespace(id=99)
    event.channel.permissions_for = lambda _: SimpleNamespace(view_channel=True,
        read_message_history=True, send_messages=True, attach_files=True)
    event.reply = AsyncMock()
    event.reference = None
    event.attachments = []
    a = Actions(agent, 10, 456, True)
    a.message, a.event, a.media_allowed = QUERY, event, True
    a.scope = {'guild_id': '123', 'user_id': '10', 'channel_id': '456', 'conversation_id': 'public:456'}
    async def controller(method, path, body=None):
        if path == '/agent/state':
            return agent.state
        if path == '/chat/context':
            return {'revision': 0}
        return {'ok': True}
    fri.api = controller
    return fri, agent, event, a


def provider(posts, *, body=None, status=200, headers=None):
    calls = []
    def handle(request):
        calls.append(request)
        assert not set(request.headers) & {'authorization', 'cookie'}
        if request.url.host == 'safebooru.donmai.us':
            assert request.url.params['tags'] == 'hatsune_miku rating:g order:random'
            assert request.url.params['limit'] == '6'
            return httpx.Response(200, json=posts, headers={'set-cookie': 'secret=do-not-forward'})
        assert str(request.url) == CDN
        return httpx.Response(status, content=body or pixels(), headers=headers or {'content-type': 'image/png'})
    return httpx.MockTransport(handle), calls


@pytest.mark.parametrize('request_text', [LINK_QUERY, 'https://example.com/'+'a'*240+' '+QUERY])
def test_long_request_link_does_not_break_accepted_image_receipt(monkeypatch, tmp_path, request_text):
    async def scenario():
        from gene.fri_research import ResearchReceipt
        fri, agent, event, actions = setup(monkeypatch, tmp_path)
        actions.message = event.content = request_text
        agent.run = AsyncMock(side_effect=[({'tag': 'hatsune_miku'}, {}, 'completed'),
            ({'verdict': 'accept', 'description': 'relevant safe image'}, {}, 'completed')])
        transport, calls = provider([post()])
        try:
            receipt = await actions.image_share.execute('', transport=transport)
            assert receipt.get('image_status') == 'prepared', receipt
            assert actions.image_share.prepared is not None and len(calls) == 2
            assert receipt['query'] in request_text and len(receipt['query']) <= 240
            ResearchReceipt(**{key: receipt[key] for key in ('query', 'answer', 'sources', 'checked_at')})
            assert all(call.args[2]['request'] == request_text for call in agent.run.call_args_list)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('verdicts,expected', [(['accept'], 1), (['reject', 'accept'], 2),
    (['uncertain', 'reject', 'accept'], 3), (['reject']*3, 0), (['uncertain']*3, 0)])
def test_inspects_exact_uploaded_bytes_and_retries_at_most_three(monkeypatch, tmp_path, verdicts, expected):
    async def scenario():
        fri, agent, event, a = setup(monkeypatch, tmp_path)
        transport, requests = provider([post(i) for i in range(1, 7)])
        inspected = []
        async def analyze(role, instruction, data, schema, **kwargs):
            assert role == 'image_lookup' and data == {'request': QUERY}
            assert 'memory' not in str(data)
            if 'images' not in kwargs:
                return {'tag': 'hatsune_miku'}, {}, 'completed'
            inspected.extend(kwargs['images'])
            return {'verdict': verdicts[len(inspected)-1], 'description': 'a teal-haired character'}, {}, 'completed'
        agent.run = analyze
        try:
            receipt = await a.image_share.execute(QUERY, transport=transport)
            assert len(inspected) == (expected or 3)
            assert len(requests) == len(inspected)+1
            if expected:
                import base64
                media = a.image_share.prepared
                assert base64.b64decode(inspected[-1].split(',')[1]) == media.body
                assert receipt['image_status'] == 'prepared'
                assert receipt['sources'] == [API+f'/posts/{expected}']
                assert 'data:image' not in json.dumps(receipt) and CDN not in json.dumps(receipt)
                sent = []
                async def reply(text, **kwargs):
                    sent.append((text, kwargs['file'].fp.read()))
                    assert kwargs['allowed_mentions'].everyone is False
                event.reply = reply
                result = ChatResult(reply='this one fits.', media=media)
                expected_bytes = media.body
                await send_reply(event, result, allowed_mentions=discord.AllowedMentions.none())
                assert len(sent) == 1 and sent[0][1] == expected_bytes
                assert 'view original artwork' in sent[0][0] and 'some artist' in sent[0][0]
                assert result.media is None and not media.body
            else:
                assert 'error' in receipt and a.image_share.prepared is None
            assert 'blocked' in await a.image_share.execute(QUERY, transport=transport)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('changes', [{'rating': 's'}, {'is_banned': True}, {'is_deleted': True},
    {'file_size': MAX_BYTES+1}, {'image_width': 1000000}, {'file_url': 'http://127.0.0.1/token'}])
def test_ineligible_metadata_never_downloaded(monkeypatch, tmp_path, changes):
    async def scenario():
        fri, agent, _, a = setup(monkeypatch, tmp_path)
        agent.run = AsyncMock(return_value=({'tag': 'hatsune_miku'}, {}, 'completed'))
        transport, calls = provider([post(**changes)])
        try:
            assert 'error' in await a.image_share.execute(QUERY, transport=transport)
            assert len(calls) == 1 and agent.run.await_count == 1
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['ambient', 'private_evidence', 'not_requested', 'not_current', 'edited', 'deleted', 'disabled', 'not_member', 'revision'])
def test_no_fetch_for_unapproved_or_stale_request(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, event, a = setup(monkeypatch, tmp_path)
        transport, calls = provider([post()])
        agent.run = AsyncMock()
        query = QUERY
        if change == 'ambient':
            a.media_allowed = False
        elif change == 'private_evidence':
            a.private_evidence = True
        elif change == 'not_requested':
            a.message = query = 'i once saw a miku picture'
        elif change == 'not_current':
            a.message = 'what did another member like'
        elif change == 'edited':
            fresh = copy.copy(event)
            fresh.content = 'never mind'
            event.channel.fetch_message.return_value = fresh
        elif change == 'deleted':
            event.channel.fetch_message.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='gone'), 'gone')
        elif change == 'disabled':
            agent.state['web_search_enabled'] = False
        elif change == 'not_member':
            agent.member.return_value = None
        else:
            a.revision = 1
        try:
            assert 'blocked' in await a.image_share.execute(query, transport=transport)
            agent.run.assert_not_awaited()
            assert not calls
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('url', ['https://cdn.donmai.us.evil.example/original/a.png',
    'https://user:pw@cdn.donmai.us/original/a.png', CDN+'?redirect=http://127.0.0.1',
    CDN+'#secret', 'http'+CDN[5:], 'https://127.0.0.1/a.png', 'file:///tmp/test',
    'https://cdn.donmai.us:444/original/a.png', CDN.replace('.png', '.svg')])
def test_cdn_scope_rejects_redirectors_local_addresses_and_other_files(url):
    assert not cdn_url(url)


def test_decode_strips_metadata_and_rejects_animation_corrupt_mime_and_bombs():
    meta = PngImagePlugin.PngInfo()
    meta.add_text('private', 'hidden caption')
    normalized, _, _ = normalize(pixels(pnginfo=meta), 'image/png')
    with Image.open(io.BytesIO(normalized)) as image:
        assert 'private' not in image.info
    invalid = [(b'<svg/>', 'image/png'), (pixels(), 'image/jpeg'), (pixels()[:35], 'image/png')]
    out = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(out, 'PNG', save_all=True,
        append_images=[Image.new('RGB', (8, 8), 'blue')], duration=100, loop=0)
    invalid.append((out.getvalue(), 'image/png'))
    for body, mime in invalid:
        with pytest.raises((ValueError, OSError)):
            normalize(body, mime)


@pytest.mark.parametrize('mode', ['redirect', 'html', 'length', 'negative', 'chunked', 'encoding'])
def test_stream_rejects_untrusted_response_shapes(mode):
    async def scenario():
        headers, body, status = {'content-type': 'image/png'}, pixels(), 200
        if mode == 'redirect':
            status, headers['location'] = 302, 'http://169.254.169.254/token'
        elif mode == 'html':
            headers['content-type'] = 'text/html'
        elif mode == 'length':
            headers['content-length'] = 'bogus'
        elif mode == 'negative':
            headers['content-length'] = '-1'
        elif mode == 'chunked':
            body = b'x'*101
        else:
            headers['content-encoding'] = 'br'
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(status, content=body, headers=headers)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises((ValueError, httpx.HTTPError)):
                await read(client, CDN, 100, {'image/png'})
        assert len(requests) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['denied', 'ambiguous', 'no_attach', 'changed'])
def test_delivery_failure_is_truthful_and_never_retries_ambiguous_upload(mode):
    async def scenario():
        media = PreparedImage(pixels(), 'artwork.png', API+'/posts/1', 'some_artist',
                              SimpleNamespace(current=AsyncMock(return_value=mode != 'changed')))
        event = SimpleNamespace(guild=None, channel=SimpleNamespace(), reply=AsyncMock())
        if mode in ('denied', 'ambiguous'):
            error = discord.HTTPException(SimpleNamespace(status=403 if mode == 'denied' else 500, reason='failed'), 'failed')
            event.reply.side_effect = [error, None]
        if mode == 'no_attach':
            event.guild = SimpleNamespace(me=object())
            event.channel.permissions_for = lambda _: SimpleNamespace(attach_files=False)
        result = ChatResult(reply='here it is', media=media)
        if mode == 'ambiguous':
            with pytest.raises(discord.HTTPException):
                await send_reply(event, result)
            assert event.reply.await_count == 1
        else:
            await send_reply(event, result)
            if mode == 'changed':
                event.reply.assert_not_awaited()
            else:
                assert 'file' not in event.reply.await_args.kwargs
                assert media.page in event.reply.await_args.args[0]
        assert result.media is None and not media.body
    asyncio.run(scenario())


@pytest.mark.parametrize('query,tool_query', [(QUERY, QUERY), (DETAILED_QUERY, DETAILED_QUERY),
                                            (DETAILED_QUERY, ''),
                                            (DETAILED_QUERY, 'image featuring hatsune miku')],
                         ids=['short', 'full-brief', 'harness-brief', 'short-excerpt-keeps-details'])
@pytest.mark.parametrize('fault', ['', 'final_model', 'recording', 'edit_before_send'])
def test_complete_chat_tool_selection_persistence_and_upload(api, monkeypatch, tmp_path, fault, query, tool_query):  # noqa: F811
    from compat_tests.test_fri_chat import response_event
    from fri.fri_chat import Chat
    async def scenario():
        client, call = api
        fri, agent, event, _ = setup(monkeypatch, tmp_path)
        event.content = query
        event.author.display_name = 'Member'
        event.channel.typing = Mock(return_value=AsyncMock())
        async def empty_history(**kwargs):
            for row in []:
                yield row
        event.channel.history = empty_history
        async def controller(method, path, body=None):
            if path == '/agent/state':
                return agent.state
            if path in ('/chat/turn', '/chat/turn/check') and fault == 'recording':
                raise commands.UserFeedbackCheckFailure('recording unavailable')
            return await call(method, path, body)
        fri.api = controller
        network, _ = provider([post()])
        real_find = ImageShare.find
        async def find(self, query, **kwargs):
            return await real_find(self, query, transport=network)
        monkeypatch.setattr(ImageShare, 'find', find)
        async def inspect(role, instruction, data, schema, **kwargs):
            assert data['request'] == query  # Keep even the requirements after character 240.
            return (({'verdict': 'accept', 'description': 'teal-haired character'} if kwargs.get('images')
                     else {'tag': 'hatsune_miku'}), {}, 'completed')
        agent.run = AsyncMock(side_effect=inspect)
        seen = []
        def respond(request):
            payload = json.loads(request.content)
            seen.append(payload)
            if len(seen) == 1:
                assert 'image_search' in json.loads(payload['input'][-1]['content'])['available_actions']
                output = {'reply': '', 'request_draft': None, 'notes': [],
                          'action': {'kind': 'image_search', 'query': tool_query}}
            else:
                if fault == 'final_model':
                    return httpx.Response(503)
                output = {'reply': 'this one fits.', 'request_draft': None, 'notes': []}
            return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(output)))+'\n\n')
        fri.chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(respond))
        try:
            result = await fri.talk(123, 456, 10, query, 'public', event=event)
            if fault == 'recording':
                assert result.media is None
            else:
                assert result.media is not None
                if fault == 'edit_before_send':
                    changed = copy.copy(event)
                    changed.content = 'never mind'
                    event.channel.fetch_message.return_value = changed
                sent = await send_reply(event, result, allowed_mentions=discord.AllowedMentions.none())
                if fault == 'edit_before_send':
                    event.reply.assert_not_awaited()
                    assert sent is None
                else:
                    assert 'file' in event.reply.await_args.kwargs
                    await fri.record_turn(result, sent)  # Saved only after Discord accepts it.
            saved = client.app.state.store.chat_context('123', 'public:456', '10')
            assert 'data:image' not in str(saved) and 'cdn.donmai.us' not in str(saved)
            assert 'teal-haired' not in str(saved['turns'])
            if fault in ('recording', 'edit_before_send'):
                assert not saved['web_research']  # Undelivered turns keep no research receipt either.
            else:
                assert saved['web_research'][0]['sources'] == [API+'/posts/1']
            assert len(seen) == 2
        finally:
            await fri.close()
    from fri import feedback as commands
    asyncio.run(scenario())


@pytest.mark.parametrize('private', ['password=not-public', 'https://127.0.0.1/private',
    'http://internal.local/admin', '<@123456789012345678>', 'sk-'+'a'*24])
def test_full_image_brief_blocks_private_content_after_safe_excerpt(monkeypatch, tmp_path, private):
    async def scenario():
        fri, agent, event, a = setup(monkeypatch, tmp_path)
        a.message = event.content = QUERY+'\n'+private
        agent.run = AsyncMock()
        network, calls = provider([post()])
        try:
            assert 'blocked' in await a.image_share.execute(QUERY, transport=network)
            assert not calls
            agent.run.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_image_brief_bounds_and_ordinary_web_limits_stay_separate():
    from gene.fri_research import ResearchReceipt

    from fri.fri_image_request import MAX_REQUEST, image_request, receipt_query
    from fri.web_policy import public_query
    assert image_request('', DETAILED_QUERY) == DETAILED_QUERY
    assert image_request('private saved context', DETAILED_QUERY) == ''
    assert image_request('', 'a'*(MAX_REQUEST+1)) == ''
    assert image_request(None, DETAILED_QUERY) == ''
    assert not public_query(DETAILED_QUERY, DETAILED_QUERY)
    assert not public_query('hello\nworld', 'hello\nworld')
    label = receipt_query(DETAILED_QUERY)
    assert public_query(label, DETAILED_QUERY)
    ResearchReceipt(query=label, answer='verified image', sources=[API+'/posts/1'],
                    checked_at=dt.datetime.now(dt.timezone.utc).isoformat())


def test_dm_disable_cancels_prepared_image_delivery(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, event, _ = setup(monkeypatch, tmp_path)
        event.guild = None
        event.channel = Mock(spec=discord.DMChannel, id=999,
                             fetch_message=AsyncMock(return_value=event))
        a = Actions(agent, 10, 999, False)
        a.event, a.message, a.media_allowed = event, QUERY, True
        a.scope = {'guild_id': '123', 'user_id': '10', 'channel_id': '456', 'conversation_id': 'private:999'}
        a.image_share.source_text, a.image_share.source_edited = QUERY, None
        agent.config['support_dms'] = True
        try:
            assert await a.image_share.current()
            media = PreparedImage(pixels(), 'artwork.png', API+'/posts/1', '', a.image_share)
            result = ChatResult(reply='here it is', media=media)
            agent.config['support_dms'] = False
            await send_reply(event, result)
            event.reply.assert_not_awaited()
            assert not media.body and result.media is None
        finally:
            await fri.close()
    asyncio.run(scenario())
