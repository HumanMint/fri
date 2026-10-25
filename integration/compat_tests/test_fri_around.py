"""Fri reads another channel or a thread when someone asks about it, within what this audience could read."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from compat_tests.test_fri_links import world
from fri.fri_actions import MAX_THREADS, Actions
from fri.fri_chat import INSTRUCTIONS


def around(monkeypatch, tmp_path):
    """links.world, whose channels 1 and 2 everyone can read, 3 is staff-only, 456 age-restricted and
    9 unread by Fri, plus threads and a readable history for every channel."""
    fri, agent, channels, posts, post, said = world(monkeypatch, tmp_path)
    for channel in channels.values():
        channel.threads = []
        async def history(*, limit, after, channel=channel, **_):
            for item in sorted((p for p in posts.values() if p.channel is channel), key=lambda p: p.id, reverse=True)[:limit]:
                if discord.utils.snowflake_time(item.id) > after:
                    yield item
        channel.history = history
    def thread(parent, tid, *, kind=discord.ChannelType.public_thread, archived=False, active=0):
        item = Mock(spec=discord.Thread, id=tid, guild=parent.guild, parent_id=parent.id, type=kind,
                    archived=archived, nsfw=False, last_message_id=tid+active, permissions_for=parent.permissions_for)
        item.name, item.parent = f'thread-{tid}', parent  # Mock(name=, parent=) mean something else.
        item.is_private = lambda: kind == discord.ChannelType.private_thread
        item.is_nsfw = lambda: False
        async def history(**_):
            return
            yield
        item.history = history
        parent.threads.append(item)
        channels[tid] = item
        return item
    # A fresh access check refetches the channels a turn read from, and the server's roles.
    guild = channels[1].guild
    guild.fetch_roles = AsyncMock(return_value=[])
    guild.fetch_channel = AsyncMock(side_effect=lambda cid: channels[cid])
    for posted in posts.values():
        posted.jump_url = f'https://discord.com/channels/123/{posted.channel.id}/{posted.id}'
        posted.created_at = discord.utils.snowflake_time(posted.id)
    return fri, agent, channels, posts, post, thread


@pytest.mark.parametrize('where', ['general', 'staff', 'staff_thread', 'dm'])
def test_a_look_around_offers_what_this_audience_could_read_anyway(monkeypatch, tmp_path, where):
    async def scenario():
        fri, agent, channels, _, _, thread = around(monkeypatch, tmp_path)
        thread(channels[1], 1001)
        thread(channels[1], 1002, kind=discord.ChannelType.private_thread)
        thread(channels[1], 1003, archived=True)
        staff = thread(channels[3], 3001)
        thread(channels[3], 3002, active=5)
        thread(channels[456], 4561)
        here = {'general': 1, 'staff': 3, 'staff_thread': staff.id, 'dm': 999}[where]
        actions = Actions(agent, 20, here, where != 'dm')
        try:
            plain, wide = await actions.channels(), await actions.channels(around=True)
            expected = {
                # Everyone in #general can read 1 and 2; staff threads and age-restricted ones stay out.
                'general': ['1', '2', '1001'],
                # Staff chat may read its own threads and the public channels.
                'staff': ['3', '1', '2', '3002', '3001', '1001'],
                # A staff thread has its channel's audience: its parent and siblings.
                'staff_thread': ['3001', '3', '1', '2', '3002', '1001'],
                # A DM is only the member: everything they can read, threads included.
                'dm': ['1', '2', '3', '3002', '3001', '1001'],
            }[where]
            assert list(wide) == expected
            assert list(plain) == ([str(here)] if where != 'dm' else [key for key in wide if key in plain])
            assert all(not isinstance(channel, discord.Thread) or key == str(here) for key, channel in plain.items())
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_threads_are_capped_newest_first(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, _, _, thread = around(monkeypatch, tmp_path)
        for n in range(MAX_THREADS+3):
            thread(channels[1], 1100+n, active=n)
        try:
            wide = await Actions(agent, 20, 1, True).channels(around=True)
            assert list(wide)[2:] == [str(1100+n) for n in reversed(range(3, MAX_THREADS+3))]
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_reading_another_public_channel_from_here_is_fenced_like_this_one(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, _, post, thread = around(monkeypatch, tmp_path)
        post(2, 'movie night friday?', age=300)
        post(1, 'unrelated', age=200)
        actions = Actions(agent, 20, 1, True)
        actions.event = SimpleNamespace(id=1, channel=channels[1])
        try:
            listing = (await actions.available())['read_recent']
            assert listing['default_channel_id'] == '1'
            assert [row['channel_id'] for row in listing['channels']] == ['1', '2']
            receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '2', 'window': 'recent'})
            assert [row['text'] for row in receipt['messages']] == ['movie night friday?']
            assert actions.sources == {'2'} and await actions.valid()
            # #2 goes staff-only mid-reply: what was read there no longer fits this audience.
            channels[2].permissions_for = channels[3].permissions_for
            assert not await actions.valid()
            assert (await actions.execute({'kind': 'read_recent', 'channel_id': '2', 'window': 'recent'}))['blocked']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_revoke_the_cache_has_not_seen_still_fences_another_channel_read(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, _, post, thread = around(monkeypatch, tmp_path)
        busy = thread(channels[2], 2001)
        post(2, 'movie night friday?', age=300)
        actions = Actions(agent, 20, 1, True)
        actions.event = SimpleNamespace(id=1, channel=channels[1])
        try:
            await actions.execute({'kind': 'read_recent', 'channel_id': '2001', 'window': 'recent'})
            assert actions.sources == {'2001'} and await actions.valid()
            # Discord already took #2 away from the speaker; the cached channel still lets them read it.
            fetched = SimpleNamespace(**{**vars(channels[2]), 'permissions_for': lambda who: discord.Permissions()})
            channels[1].guild.fetch_channel = AsyncMock(side_effect=lambda cid: fetched if cid == 2 else channels[cid])
            assert busy.permissions_for(channels[1].guild.me).view_channel
            assert '2001' not in await actions.channels(fresh=True, around=True)
            assert not await actions.valid()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('where', ['general', 'dm'])
def test_a_channel_that_keeps_one_role_out_is_not_read_from_public(monkeypatch, tmp_path, where):
    async def scenario():
        fri, agent, channels, _, post, thread = around(monkeypatch, tmp_path)
        thread(channels[2], 2001)
        post(2, 'movie night friday?', age=300)
        # @everyone can view #2, but one role can't: not everyone in #general could read it.
        channels[2].overwrites = {discord.Object(id=50): discord.PermissionOverwrite(view_channel=False)}
        actions = Actions(agent, 20, 1 if where == 'general' else 999, where == 'general')
        actions.event = SimpleNamespace(id=1, channel=channels[1])
        try:
            wide = await actions.channels(around=True)
            # In a DM only the member reads the answer, and they can read #2 and its thread.
            assert ('2' in wide, '2001' in wide) == ((False, False) if where == 'general' else (True, True))
            if where == 'general':
                assert (await actions.execute({'kind': 'read_recent', 'channel_id': '2', 'window': 'recent'}))['blocked']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_role_kept_out_after_the_read_drops_it_before_the_next_request(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, _, post, _ = around(monkeypatch, tmp_path)
        post(2, 'movie night friday?', age=300)
        actions = Actions(agent, 20, 1, True)
        actions.event = SimpleNamespace(id=1, channel=channels[1])
        try:
            await actions.execute({'kind': 'read_recent', 'channel_id': '2', 'window': 'recent'})
            assert actions.sources == {'2'} and await actions.valid()
            # Discord now keeps one role out of #2; the cached channel hasn't heard yet.
            fetched = SimpleNamespace(**{**vars(channels[2]), 'overwrites': {
                discord.Object(id=50): discord.PermissionOverwrite(read_message_history=False)}})
            channels[1].guild.fetch_channel = AsyncMock(side_effect=lambda cid: fetched if cid == 2 else channels[cid])
            assert '2' in await actions.channels(around=True)
            assert '2' not in await actions.channels(fresh=True, around=True)
            assert not await actions.valid()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_thread_listing_names_its_channel(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, _, post, thread = around(monkeypatch, tmp_path)
        thread(channels[1], 1001)
        actions = Actions(agent, 20, 1, True)
        actions.event = SimpleNamespace(id=1, channel=channels[1])
        try:
            rows = (await actions.available())['read_recent']['channels']
            assert rows[-1] == {'channel_id': '1001', 'name': 'thread-1001', 'thread_in': 'c1'}
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_mention_can_read_another_channel_without_its_nearby_rules(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, _, post, _ = around(monkeypatch, tmp_path)
        post(2, 'gaming tonight?', age=4*86400)  # Older than a mention's six-hour nearby window.
        actions = Actions(agent, 20, 1, True)
        actions.event = Mock(spec=discord.Message, id=1, channel=channels[1])
        actions.invitation = SimpleNamespace(outside=False, current=AsyncMock(return_value=True), require_current=AsyncMock(),
                                             collect=AsyncMock(side_effect=AssertionError('nearby read')))
        try:
            receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '2', 'window': 'recent'})
            assert [row['text'] for row in receipt['messages']] == ['gaming tonight?']
            assert await actions.valid()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_the_instructions_send_fri_to_read_the_channel_they_name():
    assert 'another channel or a thread' in INSTRUCTIONS and 'thread_in' in INSTRUCTIONS


@pytest.mark.parametrize('where', ['public', 'dm'])
def test_a_configured_forum_is_never_read_as_a_channel(monkeypatch, tmp_path, where):
    from compat_tests.test_fri_links_fences import real_server
    from compat_tests.test_fri_member_profiles import world

    async def scenario():
        w = world(monkeypatch, tmp_path)
        guild, _, channels = real_server(w, monkeypatch, [])
        channels[2] = discord.ForumChannel(state=w.fri._connection, guild=guild, data={
            'id': '2', 'name': 'games-forum', 'type': 15, 'position': 0, 'permission_overwrites': []})
        guild._channels.update(channels)
        actions = Actions(w.fri.agent, 20, 1 if where == 'public' else 999, where == 'public')
        try:
            listed = [row['channel_id'] for row in (await actions.available())['read_recent']['channels']]
            assert '2' not in listed and '1' in listed
            receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '2', 'window': 'recent'})
            assert 'blocked' in receipt and '2' not in actions.sources
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_thread_already_read_stays_valid_when_busier_threads_pass_it(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, _, post, thread = around(monkeypatch, tmp_path)
        others = [thread(channels[2], 1100+n) for n in range(MAX_THREADS)]
        read = thread(channels[2], 2001, active=50)
        actions = Actions(agent, 20, 1, True)
        actions.event = SimpleNamespace(id=1, channel=channels[1])
        try:
            await actions.execute({'kind': 'read_recent', 'channel_id': '2001', 'window': 'recent'})
            assert await actions.valid()
            for other in others:  # Six other threads get newer posts; nothing about the read changed.
                other.last_message_id = read.last_message_id+100
            assert '2001' not in await Actions(agent, 20, 1, True).channels(around=True)
            assert await actions.valid()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_thread_discord_archived_after_the_read_drops_it(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, _, post, thread = around(monkeypatch, tmp_path)
        read = thread(channels[2], 2001)
        actions = Actions(agent, 20, 1, True)
        actions.event = SimpleNamespace(id=1, channel=channels[1])
        try:
            await actions.execute({'kind': 'read_recent', 'channel_id': '2001', 'window': 'recent'})
            assert await actions.valid()
            archived = copy.copy(read)
            archived.archived = True  # The cached thread still says it's active.
            channels[1].guild.fetch_channel = AsyncMock(side_effect=lambda cid: archived if cid == 2001 else channels[cid])
            assert not await actions.valid()
        finally:
            await fri.close()
    asyncio.run(scenario())
