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
from fri.fri_errors import GateClosed
from fri.fri_images import MAX_LOADS, look
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


@pytest.mark.parametrize('posts', [1, 2])
def test_an_outside_mention_poster_who_opts_out_during_the_last_refetch_never_reaches_the_model(monkeypatch, tmp_path, posts):
    """The nearby authors' privacy is checked after every Discord refetch, not before them."""
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path)
        fri.data = tmp_path
        memes = [message('caption '+str(user), user=user, age=user, attachments=[picture(channel.id, str(user)+'.png')])
                 for user in (30, 40)[:posts]]
        event = message('<@99> pick the best meme above')
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(side_effect=lambda url, channel: 'data:image/png;base64,'+url[-6:]))
        fri.chat_once = MethodType(Fri.chat_once, fri)
        sent = []
        def handler(request):
            sent.append(json.loads(request.content))
            answer = {'response_kind': 'reply', 'reaction': None, 'reply': 'member 30 wins', 'request_draft': None,
                      'action': None, 'notes': [], 'allow_closing': False}
            return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(answer)))+'\n\n')
        fri.chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler))
        signed_in = False

        async def headers():
            nonlocal signed_in
            signed_in = True
            return {'Authorization': 'Bearer synthetic'}

        async def fetch(mid):
            if signed_in and mid == memes[0].id:  # They opt out while Fri refetches their post.
                store.personal_control('30', 'disable')
            return rows[mid]
        fri.chat.headers = headers
        channel.fetch_message = AsyncMock(side_effect=fetch)
        try:
            invitation = await Invitation.create(agent, event)
            with pytest.raises(commands.UserFeedbackCheckFailure):
                await fri.invited_talk(invitation, 'pick the best meme above')
            assert sent == []  # Neither the caption nor the image went out.
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_download_that_fails_leaves_its_slot_to_an_older_meme(monkeypatch, tmp_path):
    async def scenario():
        fri, _, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path)
        memes = [message('', user=30, age=age, attachments=[picture(channel.id, f'{age}.png')]) for age in range(10, 130, 10)]
        tried = []

        async def load(url, channel):
            tried.append(url.rsplit('/', 1)[1])
            if url.rsplit('/', 1)[1] not in ('50.png', '120.png'):
                raise httpx.ConnectError('cdn down')
            return 'data:image/png;base64,'+url.rsplit('/', 1)[1]
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(side_effect=load))
        context = {}
        try:
            result = await look(context, memes)
            # The newest four fail, the fifth works; the hunt stops at MAX_LOADS downloads.
            assert context['images'] == ['data:image/png;base64,50.png']
            assert len(tried) == MAX_LOADS and '120.png' not in tried
            assert result == {'images_inspected': 1, 'images_not_inspected': len(memes)-1}
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('when', ['access_recheck', 'controller_check'])
def test_turning_dms_off_during_the_last_checks_stops_the_request(monkeypatch, tmp_path, when):
    async def scenario():
        w, _, _, sent = image_world(monkeypatch, tmp_path)
        w.fri.agent.config['support_dms'] = True
        general, api = w.channels[GENERAL], w.fri.api

        def off():
            w.fri.agent.config['support_dms'] = False
        if when == 'access_recheck':  # Before the request that would carry the memes.
            fetching = general.guild.fetch_channel

            async def fetch(cid):
                off()
                return await fetching(cid)
            general.guild.fetch_channel = fetch
        else:  # Before the very first request, during Gene's privacy check.
            async def check(method, path, payload=None):
                result = await api(method, path, payload)
                if path == '/chat/turn/check':
                    off()
                return result
            w.fri.api = check
        try:
            text = 'fri pick the best meme in general'
            with pytest.raises(GateClosed):
                await w.fri.talk(123, DM, 20, text, 'private', event=w.message(DM, text))
            assert len(sent) == (1 if when == 'access_recheck' else 0)
        finally:
            await w.fri.close()
    asyncio.run(scenario())
