"""Fri opens what a member points at: a pasted message link, a replied-to meme, a post's reactions."""

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_looking import loads, picture, read_harness
from compat_tests.test_fri_mentions import environment
from fri.fri_actions import Actions
from fri.fri_chat import ChatResult
from fri.fri_conversation import evidence, links, unchanged
from fri.fri_images import details, media


def world(monkeypatch, tmp_path):
    """A server with configured channels 1 (the speaker's) and 2, a staff-only configured channel 3,
    an age-restricted configured channel 456, and channel 9 that Fri doesn't read."""
    fri, agent, _, _ = harness(monkeypatch, tmp_path)
    fri._connection.user = SimpleNamespace(id=99)
    everyone = SimpleNamespace(id=123)
    guild = SimpleNamespace(id=123, me=SimpleNamespace(id=99), default_role=everyone)
    posts, channels = {}, {}
    def channel(cid, *, public=True, nsfw=False):
        def permissions_for(who):
            return discord.Permissions(view_channel=public) if who is everyone else discord.Permissions(
                view_channel=True, read_message_history=True)
        item = SimpleNamespace(id=cid, name=f'c{cid}', guild=guild, parent_id=None, nsfw=nsfw,
                               is_nsfw=lambda: nsfw, permissions_for=permissions_for)
        async def fetch(mid):
            if mid not in posts or posts[mid].channel is not item:
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
            return posts[mid]
        item.fetch_message = AsyncMock(side_effect=fetch)
        channels[cid] = item
        return item
    for cid, options in {1: {}, 2: {}, 3: {'public': False}, 456: {'nsfw': True}, 9: {}}.items():
        channel(cid, **options)
    fri.get_channel = lambda cid: channels.get(cid)
    def post(cid, text='kev cooked with this one', *, user=30, age=600, attachments=()):
        mid = discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=age))
        while mid in posts:
            mid += 1
        item = Mock(spec=discord.Message, id=mid, content=text, channel=channels[cid], guild=guild,
            author=SimpleNamespace(id=user, display_name=f'member {user}', bot=False), webhook_id=None,
            attachments=list(attachments), embeds=[], edited_at=None, reference=None)
        posts[mid] = item
        return item
    def said(text, *, dm=False, reference=None):
        mid = discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc))
        return SimpleNamespace(id=mid, content=text, channel=SimpleNamespace(id=999) if dm else channels[1],
            guild=None if dm else guild, author=SimpleNamespace(id=20), reference=reference)
    return fri, agent, channels, posts, post, said


def link(item):
    return f'https://discord.com/channels/123/{item.channel.id}/{item.id}'


@pytest.mark.parametrize('dm', [False, True])
def test_a_pasted_link_opens_that_message_with_its_author(monkeypatch, tmp_path, dm):
    async def scenario():
        fri, agent, _, _, post, said = world(monkeypatch, tmp_path)
        there = post(2, '', attachments=[picture(2, 'meme.png')])
        here = post(1, 'the other take')
        message = said(f'fri thoughts on {link(there)} and <{link(here)}>?', dm=dm)
        try:
            context = await evidence(agent, message, [])
            rows = context['reply_target']
            assert [(row['source']['id'], row['text'], row.get('attachments'), row['linked']) for row in rows] == [
                (str(there.id), '', ['image'], True), (str(here.id), 'the other take', None, True)]
            assert all(row['source']['user_id'] == '30' and row['privacy_revision'] == 0 for row in rows)
            assert await unchanged(agent, message, context)
            there.content = 'edited later'
            assert not await unchanged(agent, message, context)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('where', ['staff_only', 'age_restricted', 'not_read_by_fri', 'other_server', 'in_code',
                                   'opted_out', 'old', 'deleted', 'member_cant_read', 'bot'])
def test_a_link_fri_may_not_open_here_adds_nothing(monkeypatch, tmp_path, where):
    async def scenario():
        fri, agent, channels, posts, post, said = world(monkeypatch, tmp_path)
        cid = {'staff_only': 3, 'age_restricted': 456, 'not_read_by_fri': 9}.get(where, 2)
        target = post(cid, age=8*86400 if where == 'old' else 600)
        url = link(target)
        if where == 'other_server':
            url = url.replace('/123/', '/777/')
        text = f'fri `{url}`' if where == 'in_code' else f'fri look {url}'
        if where == 'opted_out':
            agent.profile.return_value = {'enabled': False, 'revision': 0}
        elif where == 'deleted':
            del posts[target.id]
        elif where == 'member_cant_read':
            channels[2].permissions_for = lambda who: discord.Permissions.none() if getattr(who, 'id', None) == 10 \
                else discord.Permissions(view_channel=True, read_message_history=True)
        elif where == 'bot':
            target.author.bot = True
        try:
            assert (await evidence(agent, said(text), []))['reply_target'] == []
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_staff_link_still_opens_in_a_dm_where_only_the_member_reads_it(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, post, said = world(monkeypatch, tmp_path)
        target = post(3, 'staff note')
        try:
            rows = (await evidence(agent, said(f'fri what is {link(target)}', dm=True), []))['reply_target']
            assert [row['text'] for row in rows] == ['staff note']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_links_are_bounded_deduplicated_and_ignore_quoted_code():
    base = 'https://discord.com/channels/1/2/'
    text = f'{base}3 {base}3 https://canary.discord.com/channels/1/2/4 ```{base}5``` https://discordapp.com/channels/1/2/6'
    assert links(SimpleNamespace(content=text)) == [(1, 2, 3), (1, 2, 4)]
    assert links(SimpleNamespace(content='https://discord.com/channels/1/2')) == []


def test_an_uncaptioned_meme_someone_replied_to_is_context_and_gets_looked_at(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        meme = message('', user=30, age=120, attachments=[picture(1, 'meme.png')])
        event = message('fri is this real')
        event.reference = SimpleNamespace(message_id=meme.id, channel_id=1)
        loads(monkeypatch)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            context = fri.chat_once.await_args.args[1]
            assert [(row['text'], row['attachments']) for row in context['reply_target']] == [('', ['image'])]
            assert context['images'] == ['data:image/png;base64,meme.png']
            assert context['inspected_images'][0]['source']['id'] == str(meme.id)
            event.reply.assert_awaited_once()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_linked_meme_is_looked_at_and_fenced_until_delivery(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        meme = message('', user=30, age=300, attachments=[picture(1, 'meme.png')])
        event = message(f'fri rate this https://discord.com/channels/123/1/{meme.id}')
        loads(monkeypatch)
        async def respond(_, context):
            assert context['images'] == ['data:image/png;base64,meme.png']
            assert context['reply_target'][0]['linked'] is True
            rows[meme.id] = Mock(spec=discord.Message, **{**{k: getattr(meme, k) for k in (
                'id', 'channel', 'guild', 'author', 'webhook_id', 'attachments', 'embeds', 'reference')},
                'content': 'edited mid-reply', 'edited_at': dt.datetime.now(dt.timezone.utc)})
            return ChatResult(status='completed', reply='7/10, mostly for the font')
        fri.chat_once.side_effect = respond
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            # Only the fixed notice goes out; the answer about the old meme never does.
            assert event.reply.await_args.args[0].startswith('the conversation changed while i was typing')
            assert not any(path == '/chat/turn' for path, _ in calls)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_rows_show_reactions_and_what_a_post_replies_to(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channel, rows, post = read_harness(monkeypatch, tmp_path)
        meme = post('', attachments=[picture(1, 'meme.png')], age=120)
        meme.reactions = [SimpleNamespace(emoji='🗿', count=3, me=True),
                          SimpleNamespace(emoji=SimpleNamespace(name='kekw', id=5), count=1, me=False)]
        answer = post('lmao', user=40, age=60)
        answer.reference = SimpleNamespace(message_id=meme.id, channel_id=1)
        loads(monkeypatch)
        actions = Actions(agent, 20, 999, False)
        actions.turn_context, actions.media_allowed = {}, True
        try:
            receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'recent'})
            first, second = receipt['messages']
            assert first['reactions'] == [{'emoji': '🗿', 'count': 3, 'fri': True}, {'emoji': ':kekw:', 'count': 1}]
            # The same link the replied-to row carries as its source, so the model can pair them.
            assert second['reply_to'] == first['source'] == f'https://discord.com/channels/123/1/{meme.id}'
            assert 'reactions' not in second
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_details_never_name_who_reacted_and_media_counts_stickers():
    channel = SimpleNamespace(id=1)
    message = SimpleNamespace(channel=channel, guild=None, attachments=[], embeds=[],
        stickers=[SimpleNamespace(name='wave')],
        reactions=[SimpleNamespace(emoji='😭', count=2, me=False, users=lambda: ['someone'])],
        reference=SimpleNamespace(message_id=5, channel_id=2))  # Another channel: no reply_to.
    assert details(message) == {'attachments': ['sticker'], 'reactions': [{'emoji': '😭', 'count': 2}]}
    assert media(SimpleNamespace(stickers=Mock())) == []
    assert details(Mock(spec=discord.Message)) == {}
