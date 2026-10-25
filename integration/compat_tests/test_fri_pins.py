"""Fri reads a channel's pins when someone asks what's pinned, with the same gates and fences as any read."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from compat_tests.test_fri_looking import loads, picture, read_harness
from fri.fri_actions import PINS, Actions
from fri.fri_chat import INSTRUCTIONS, WINDOWS
from fri.fri_images import details


def pinned(monkeypatch, tmp_path):
    """read_harness's channel 1, plus its pins: newest pinned first, like Discord's pins list."""
    fri, agent, channel, rows, post = read_harness(monkeypatch, tmp_path)
    pins, calls = [], []

    def pin(text, *, user=30, age=30*86400, **options):
        item = post(text, user=user, age=age, **options)
        item.pinned = True
        pins.insert(0, item.id)
        return item

    async def listing(*, limit=50, before=None, oldest_first=False):
        calls.append(limit)
        for mid in [mid for mid in pins if mid in rows][:limit]:
            yield rows[mid]
    channel.pins = listing
    return fri, agent, channel, rows, post, pin, pins, calls


def test_a_pins_read_shows_pinned_posts_of_any_age_from_members_who_allow_it(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channel, rows, post, pin, _, calls = pinned(monkeypatch, tmp_path)
        pin('server ip is play.example.net', age=90*86400)
        pin('rules: no spoilers outside #spoilers', user=40)
        pin('old bot rules', user=77, bot=True)
        pin('opted out pin', user=50)
        post('just chatting', age=60)  # Not pinned: a pins read never shows it.
        agent.profile = AsyncMock(side_effect=lambda scope, user: {'enabled': user != 50, 'revision': 0, 'summary': ''})
        actions = Actions(agent, 20, 999, False)
        actions.event = SimpleNamespace(id=1, channel=SimpleNamespace(id=999))
        try:
            receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'pins'})
            assert receipt['window'] == 'pins' and calls == [PINS]
            # Newest pinned first, as the window says; bots and members who opted out stay out.
            assert [row['text'] for row in receipt['messages']] == [
                'rules: no spoilers outside #spoilers', 'server ip is play.example.net']
            assert all(row['pinned'] is True for row in receipt['messages'])
            assert actions.sources == {'1'} and await actions.valid()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['none', 'new_pin', 'unpinned', 'edited', 'deleted'])
def test_a_pin_changed_after_the_read_fails_the_recheck(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, channel, rows, post, pin, pins, calls = pinned(monkeypatch, tmp_path)
        rules = pin('rules: be nice')
        pin('server ip is play.example.net')
        actions = Actions(agent, 20, 999, False)
        actions.event = SimpleNamespace(id=1, channel=SimpleNamespace(id=999))
        try:
            await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'pins'})
            if change == 'new_pin':
                pin('movie night friday', age=60)
            elif change == 'unpinned':
                pins.remove(rules.id)
            elif change == 'edited':
                rows[rules.id] = Mock(spec=discord.Message, **{**{k: getattr(rules, k) for k in (
                    'id', 'channel', 'guild', 'author', 'webhook_id', 'attachments', 'embeds', 'created_at')},
                    'content': 'rules: anything goes', 'edited_at': discord.utils.utcnow()})
            elif change == 'deleted':
                del rows[rules.id]
            assert await actions.reads_unchanged() == (change in ('none', 'new_pin'))
            assert await actions.valid() == (change in ('none', 'new_pin'))
            assert calls[1:] and set(calls[1:]) == {50}  # One page each time, so a new pin can't push one out.
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_pinned_meme_gets_looked_at(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channel, rows, post, pin, _, _ = pinned(monkeypatch, tmp_path)
        meme = pin('', attachments=[picture(1, 'pinned.png')])
        loads(monkeypatch)
        actions = Actions(agent, 20, 999, False)
        actions.turn_context, actions.media_allowed = {}, True
        actions.event = SimpleNamespace(id=1, channel=SimpleNamespace(id=999))
        try:
            receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'pins'})
            assert receipt['images_inspected'] == 1
            assert actions.turn_context['inspected_images'][0]['source']['id'] == str(meme.id)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_mention_reads_its_own_channels_pins_not_the_nearby_posts(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channel, rows, post, pin, _, _ = pinned(monkeypatch, tmp_path)
        pin('server ip is play.example.net')
        agent.member = AsyncMock(return_value=SimpleNamespace(id=20))
        actions = Actions(agent, 20, 1, True)
        actions.event = Mock(spec=discord.Message, id=1, channel=channel)
        actions.invitation = SimpleNamespace(outside=False, current=AsyncMock(return_value=True), require_current=AsyncMock(),
                                             collect=AsyncMock(side_effect=AssertionError('nearby read')))
        try:
            receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'pins'})
            assert [row['text'] for row in receipt['messages']] == ['server ip is play.example.net']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_fris_own_channel_shows_only_pins_from_since_fri_started_there(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channel, rows, post, pin, _, _ = pinned(monkeypatch, tmp_path)
        agent.dedicated = 1
        agent.state['started'] = discord.utils.utcnow().timestamp()-3600
        pin('an old personality pin', age=2*86400)
        pin('a pin from today', age=60)
        actions = Actions(agent, 20, 999, False)
        actions.event = SimpleNamespace(id=1, channel=SimpleNamespace(id=999))
        try:
            receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'pins'})
            assert [row['text'] for row in receipt['messages']] == ['a pin from today']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_pins_are_offered_and_explained_to_the_model(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, *_ = pinned(monkeypatch, tmp_path)
        actions = Actions(agent, 20, 999, False)
        actions.event = SimpleNamespace(id=1, channel=SimpleNamespace(id=999))
        try:
            assert 'pins' in (await actions.available())['read_recent']['windows']
        finally:
            await fri.close()
    asyncio.run(scenario())
    # WINDOWS is the response schema's enum for read_recent's window, and what the reply parser accepts.
    assert 'pins' in WINDOWS and 'window=pins' in INSTRUCTIONS and 'pinned=true' in INSTRUCTIONS


def test_details_mark_a_pinned_post_only_when_discord_says_so():
    channel = SimpleNamespace(id=1)
    assert details(SimpleNamespace(channel=channel, guild=None, pinned=True)) == {'pinned': True}
    assert details(Mock(spec=discord.Message)) == {}  # A mock's attribute isn't Discord saying so.


def test_a_catch_up_marks_a_pinned_post_too(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channel, rows, post, pin, _, _ = pinned(monkeypatch, tmp_path)
        post('nothing to see', user=40, age=600)
        pin('server ip is play.example.net', user=50, age=300)
        actions = Actions(agent, 20, 999, False)
        actions.event = SimpleNamespace(id=discord.utils.time_snowflake(discord.utils.utcnow()), channel=SimpleNamespace(id=999))
        try:
            receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'since_my_last_message'})
            assert [row.get('pinned') for row in receipt['messages']] == [None, True]
        finally:
            await fri.close()
    asyncio.run(scenario())
