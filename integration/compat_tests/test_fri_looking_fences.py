"""Each model request after a look rechecks the posts Fri inspected and the requester's access from
fetched overwrites and roles, after the subscription signs in and before the request goes out."""
import asyncio
import copy
import datetime as dt
import json
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import discord
import httpx
import pytest

from compat_tests.test_fri_chat import response_event
from compat_tests.test_fri_member_profiles import DM, GENERAL, world
from compat_tests.test_fri_mentions import environment
from fri import feedback as commands
from fri.fri import Fri
from fri.fri_chat import Chat
from fri.fri_mentions import Invitation


def picture(channel, name):
    return SimpleNamespace(url=f'https://cdn.discordapp.com/attachments/{channel}/1/{name}',
                           content_type='image/png', size=100, width=100, height=100,
                           is_spoiler=lambda: False)


def model(sent, channel_id, reply):
    """A model that reads the channel first, then answers; every request it gets lands in `sent`."""
    def handler(request):
        sent.append(json.loads(request.content))
        answer = {'response_kind': 'reply', 'reaction': None, 'reply': reply,
                  'request_draft': None, 'action': None, 'notes': [], 'allow_closing': False}
        if len(sent) == 1:
            answer['reply'] = ''
            answer['action'] = {'kind': 'read_recent', 'channel_id': str(channel_id), 'window': 'recent'}
        return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(answer)))+'\n\n')
    return Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler))


def image_world(monkeypatch, tmp_path):
    w = world(monkeypatch, tmp_path)
    general = w.channels[GENERAL]
    post = w.message(GENERAL, 'original caption', user=30, age=60)
    post.attachments = [picture(GENERAL, 'original.png')]
    post.created_at = discord.utils.snowflake_time(post.id)
    post.jump_url = f'https://discord.com/channels/123/{GENERAL}/{post.id}'
    rows = {post.id: post}

    async def history(**kwargs):
        yield post

    async def fetch(mid):
        if mid not in rows:
            raise discord.NotFound(SimpleNamespace(status=404, reason='deleted'), 'deleted')
        return rows[mid]

    general.history = history
    general.fetch_message = AsyncMock(side_effect=fetch)
    monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,AA=='))
    w.fri.chat_once = MethodType(Fri.chat_once, w.fri)
    sent = []
    w.fri.chat = model(sent, GENERAL, 'the original image wins')
    return w, post, rows, sent


def edited(post):
    changed = copy.copy(post)
    changed.content = 'retracted caption'
    changed.edited_at = dt.datetime.now(dt.timezone.utc)
    return changed


@pytest.mark.parametrize('change', ['edit', 'delete'])
def test_a_meme_changed_during_sign_in_never_reaches_the_next_request(monkeypatch, tmp_path, change):
    async def scenario():
        w, post, rows, sent = image_world(monkeypatch, tmp_path)
        sign_ins = 0

        async def sign_in():
            nonlocal sign_ins
            sign_ins += 1
            if sign_ins == 2:  # After the read and its own checks, while Fri signs in for the next request.
                if change == 'delete':
                    del rows[post.id]
                else:
                    rows[post.id] = edited(post)
            return {'Authorization': 'Bearer synthetic'}

        w.fri.chat.headers = sign_in
        try:
            with pytest.raises(commands.UserFeedbackCheckFailure):
                text = 'fri pick the best meme in general'
                await w.fri.talk(123, DM, 20, text, 'private', event=w.message(DM, text))
            assert sign_ins == 2 and len(sent) == 1
        finally:
            await w.fri.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('revoked', [False, True])
def test_access_is_judged_from_fetched_overwrites_before_the_next_request(monkeypatch, tmp_path, revoked):
    async def scenario():
        w, post, rows, sent = image_world(monkeypatch, tmp_path)
        general = w.channels[GENERAL]
        fetched = copy.copy(general)
        general.guild.fetch_channel = AsyncMock(return_value=fetched)
        sign_ins = 0

        async def sign_in():
            nonlocal sign_ins
            sign_ins += 1
            # Discord already denies the requester; the gateway's cached channel hasn't caught up.
            if sign_ins == 2 and revoked:
                fetched.permissions_for = lambda member: (
                    discord.Permissions.none() if member.id == 20 else discord.Permissions.all())
            return {'Authorization': 'Bearer synthetic'}

        w.fri.chat.headers = sign_in
        try:
            text = 'fri pick the best meme in general'
            result = await w.fri.talk(123, DM, 20, text, 'private', event=w.message(DM, text))
            if revoked:  # The next request never goes out, and the reply is the fixed access line.
                assert len(sent) == 1 and 'original image' not in result.reply
                assert 'access or memory changed' in result.reply
            else:
                assert result.reply == 'the original image wins' and len(sent) == 2
                assert sent[-1]['input'][-1]['content'][-1]['type'] == 'input_image'
            general.guild.fetch_channel.assert_awaited()
        finally:
            await w.fri.close()

    asyncio.run(scenario())


def test_a_configured_mention_drops_a_meme_edited_during_sign_in(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, controller, channel, message, calls, rows = environment(
            monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        post = message('original caption', user=30, age=60,
                       attachments=[picture(channel.id, 'original.png')])
        event = message('<@99> whats ur favorite meme out of the memes above')
        fri.chat_once = MethodType(Fri.chat_once, fri)
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,AA=='))
        sent = []
        fri.chat = model(sent, channel.id, 'original wins')
        sign_ins = 0

        async def sign_in():
            nonlocal sign_ins
            sign_ins += 1
            if sign_ins == 2:
                rows[post.id] = edited(post)
            return {'Authorization': 'Bearer synthetic'}

        fri.chat.headers = sign_in
        try:
            with pytest.raises(commands.UserFeedbackCheckFailure):
                await fri.talk(123, channel.id, 20, 'whats ur favorite meme out of the memes above',
                               'public', event=event,
                               invitation=await Invitation.create(agent, event))
            assert len(sent) == 1
        finally:
            await fri.close()

    asyncio.run(scenario())
