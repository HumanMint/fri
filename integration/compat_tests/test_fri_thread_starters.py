"""Where a post sits: the post a thread was opened on, Discord's "started a thread" notices, forwards."""

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from compat_tests.test_fri_links import world
from compat_tests.test_fri_looking import loads, picture, read_harness
from compat_tests.test_fri_mentions import environment
from fri.fri_actions import Actions
from fri.fri_chat import ChatResult
from fri.fri_conversation import evidence, unchanged
from fri.fri_images import details


def snowflake(age=0):
    return discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=age))


def opened(channels, post, *, starter=True, kind=discord.ChannelType.public_thread, age=600):
    """A thread in channel 1 opened on `post` `age` seconds ago, whose first message is Discord's starter notice
    (type 21) naming it, or an ordinary first message when the thread began on its own."""
    parent = channels[1]
    thread = Mock(spec=discord.Thread, id=post.id if starter else snowflake(age), guild=parent.guild,
                  parent_id=parent.id, type=kind, nsfw=False, permissions_for=parent.permissions_for,
                  created_at=dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=age))
    thread.name, thread.parent = 'thread', parent
    thread.is_private = lambda: kind == discord.ChannelType.private_thread
    thread.is_nsfw = lambda: False
    first = SimpleNamespace(id=snowflake(age), content='', type=discord.MessageType.thread_starter_message,
        reference=SimpleNamespace(message_id=post.id, channel_id=parent.id, guild_id=123, resolved=None)) if starter else \
        SimpleNamespace(id=thread.id+1, content='so anyway', type=discord.MessageType.default, reference=None)
    reads = []
    async def history(**kwargs):
        reads.append(kwargs)
        yield first
    thread.history, thread.reads = history, reads
    thread.fetch_message = AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason='x'), 'gone'))
    channels[thread.id] = thread
    return thread


def asked(thread, guild, text='fri thoughts?'):
    return SimpleNamespace(id=snowflake(), content=text, channel=thread, guild=guild,
                           author=SimpleNamespace(id=20), reference=None)


def configured(agent, thread):
    allowed = agent.allowed
    agent.allowed = lambda channel: channel is thread or allowed(channel)


def test_thoughts_in_a_thread_opened_on_a_post_reads_that_post_as_a_reply_target(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, _, post, _ = world(monkeypatch, tmp_path)
        meme = post(1, 'pineapple on pizza is elite', attachments=[picture(1, 'pizza.png')], age=900)
        thread = opened(channels, meme)
        configured(agent, thread)
        message = asked(thread, channels[1].guild)
        targets = []
        try:
            context = await evidence(agent, message, [], targets)
            [row] = context['reply_target']
            assert row['thread_starter'] is True and 'linked' not in row
            assert (row['source']['id'], row['source']['channel_id'], row['text'], row['attachments']) == (
                str(meme.id), '1', 'pineapple on pizza is elite', ['image'])
            assert row['source']['user_id'] == '30' and row['privacy_revision'] == 0
            assert targets == [meme]  # So its picture gets looked at, like a replied-to meme's.
            assert await unchanged(agent, message, context)
            assert len(thread.reads) == 1  # The thread's start is read once: it never changes.
            meme.content, meme.edited_at = 'pineapple on pizza is a crime', discord.utils.utcnow()
            assert not await unchanged(agent, message, context)  # Rechecked from its own channel, like any target.
            assert str(meme.id) in agent.withdrawn
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['began_on_its_own', 'private_thread', 'old_thread', 'opted_out', 'unconfigured',
                                  'member_cant_read_parent', 'deleted', 'dm'])
def test_no_starter_row_unless_its_a_new_public_thread_on_a_post_this_audience_may_read(monkeypatch, tmp_path, case):
    async def scenario():
        fri, agent, channels, posts, post, said = world(monkeypatch, tmp_path)
        meme = post(1, 'pineapple on pizza is elite', age=900)
        thread = opened(channels, meme, starter=case != 'began_on_its_own', age=7*3600 if case == 'old_thread' else 600,
                        kind=discord.ChannelType.private_thread if case == 'private_thread' else
                        discord.ChannelType.public_thread)
        configured(agent, thread)
        if case == 'unconfigured':  # Not one of Fri's channels: an outside ping reads its own way.
            agent.allowed = lambda channel: False
        if case == 'opted_out':
            agent.profile = AsyncMock(return_value={'enabled': False, 'revision': 0})
        elif case == 'member_cant_read_parent':
            channels[1].permissions_for = lambda who: discord.Permissions(view_channel=who.id == 99,
                                                                           read_message_history=True)
        elif case == 'deleted':
            del posts[meme.id]
        message = said('fri thoughts?', dm=True) if case == 'dm' else asked(thread, channels[1].guild)
        try:
            assert (await evidence(agent, message, []))['reply_target'] == []
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_link_to_the_starter_post_is_one_row(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, _, post, _ = world(monkeypatch, tmp_path)
        meme = post(1, 'pineapple on pizza is elite', age=900)
        thread = opened(channels, meme)
        configured(agent, thread)
        message = asked(thread, channels[1].guild, f'fri is https://discord.com/channels/123/1/{meme.id} right')
        try:
            rows = (await evidence(agent, message, []))['reply_target']
            assert [(row['source']['id'], row.get('thread_starter')) for row in rows] == [(str(meme.id), True)]
        finally:
            await fri.close()
    asyncio.run(scenario())


def notice(post, name='movie night'):
    """Discord's "started a thread" system message in the parent channel: its content is the thread's name."""
    item = post(name, user=40)
    item.type = discord.MessageType.thread_created
    return item


def test_a_channel_read_leaves_out_started_a_thread_notices(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channel, rows, post = read_harness(monkeypatch, tmp_path)
        post('who is in for friday', age=120)
        notice(post)
        loads(monkeypatch)
        actions = Actions(agent, 20, 999, False)
        actions.turn_context = {}
        try:
            receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'recent'})
            assert [row['text'] for row in receipt['messages']] == ['who is in for friday']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_pings_nearby_read_leaves_out_started_a_thread_notices(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path)
        message('who is in for friday', user=30, age=120)
        name = message('movie night', user=40, age=60)
        name.type = discord.MessageType.thread_created
        event = message('<@99> thoughts?')
        contexts = []
        fri.chat_once.side_effect = lambda _, context: contexts.append(context) or ChatResult(
            status='completed', reply='friday works')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            [context] = contexts
            assert [row['text'] for row in context['invited_context']] == ['who is in for friday']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_forward_is_not_labelled_a_reply():
    channel = SimpleNamespace(id=1)
    def posted(kind):
        reference = discord.MessageReference(message_id=555, channel_id=1, guild_id=123, type=kind)
        return SimpleNamespace(channel=channel, guild=SimpleNamespace(id=123), reference=reference, attachments=[],
                               embeds=[], reactions=[], pinned=False, author=SimpleNamespace(bot=False),
                               webhook_id=None, content='')
    assert details(posted(discord.MessageReferenceType.default)) == {
        'reply_to': 'https://discord.com/channels/123/1/555'}
    assert 'reply_to' not in details(posted(discord.MessageReferenceType.forward))  # It copies that post instead.


def test_a_history_search_leaves_out_started_a_thread_notices(monkeypatch, tmp_path):
    from compat_tests.test_fri_history_search import query, setup
    async def scenario():
        fri, agent, actions, _, rows, messages = setup(monkeypatch, tmp_path)
        messages[int(rows[0]['id'])].type = discord.MessageType.thread_created  # A thread named "played game 0".
        try:
            found = [row['text'] for row in (await actions.execute(query()))['messages']]
            assert 'played game 0' not in found and 'played game 1' in found
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_started_a_thread_notice_stays_out_of_the_live_channel_view(monkeypatch, tmp_path):
    """fri #179: the notice's text is the thread's name, so a later question doesn't show it as the member's words."""
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        contexts = []
        fri.chat_once.side_effect = lambda _, context: contexts.append(context) or ChatResult(
            status='completed', reply='friday works')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(message('who is in for friday', user=30, age=120))
                name = message('movie night', user=40, age=60)
                name.type = discord.MessageType.thread_created
                await agent.handle(name)
                await agent.handle(message('fri what is the plan'))
            [context] = contexts
            texts = [row['text'] for row in context['recent']]
            assert 'who is in for friday' in texts and 'movie night' not in texts
            assert [row['text'] for row in agent.recent[1]].count('movie night') == 0
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('name', ['fri movie night', '<@99> movie night'])
def test_a_started_a_thread_notice_naming_fri_is_not_answered(monkeypatch, tmp_path, name):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        item = notice(lambda text, user: message(text, user=user), name)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(item)
            fri.chat_once.assert_not_awaited()
            item.reply.assert_not_awaited()
            assert not agent.recent[1]
        finally:
            await fri.close()
    asyncio.run(scenario())
