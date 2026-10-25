"""A pasted link opens only what the whole audience could read, judged as Discord has it now."""

import asyncio
import copy
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from compat_tests.test_fri_looking import loads, picture
from compat_tests.test_fri_member_profiles import DM, GAMING, GENERAL, world
from fri import feedback as commands
from fri.fri import Fri
from fri.fri_actions import Actions
from fri.fri_chat import ChatResult
from fri.fri_conversation import evidence, unchanged
from fri.fri_images import image_context, look, looked_unchanged
from fri.fri_memory import open_to_everyone


def signing_in(w, change):
    """Fri's own model call; `change` runs while Fri signs in, before its last checks."""
    sent = []

    async def call(text, context):
        return await Fri.chat_once(w.fri, text, context)

    async def respond(text, context, ready=None):
        change()
        await ready()
        sent.append({'reply_target': context.get('reply_target'), 'images': list(context.get('images', []))})
        return ChatResult(status='completed', reply='bold of you')
    w.fri.chat = SimpleNamespace(respond=respond, headers=AsyncMock(return_value={'Authorization': 'Bearer test'}))
    w.fri.chat_once = AsyncMock(side_effect=call)
    return sent


def hosted(w, cid, post):
    """`post` lives in channel `cid`, fetched as it is now."""
    current = [post]

    async def fetch(mid):
        if not current or mid != post.id:
            raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
        return current[0]
    w.channels[cid].fetch_message = AsyncMock(side_effect=fetch)
    return current


def everyone_reads(w, *cids):
    for cid in cids:
        w.channels[cid].permissions_for = lambda who: (
            discord.Permissions(view_channel=True, read_message_history=True) if getattr(who, 'name', '') == '@everyone'
            else discord.Permissions.all())
        w.channels[cid].overwrites = {}


@pytest.mark.parametrize('change', ['none', 'edit', 'delete', 'access', 'image_access'])
def test_a_linked_post_that_changes_while_fri_signs_in_never_reaches_the_model(monkeypatch, tmp_path, change):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        target = w.message(GENERAL, 'kev cooked with this one', user=30, age=60)
        if change == 'image_access':
            target.attachments = [picture(GENERAL, 'meme.png')]
        current = hosted(w, GENERAL, target)
        text = f'fri thoughts on https://discord.com/channels/123/{GENERAL}/{target.id}'
        event = w.message(DM, text)

        def happen():
            if change == 'edit':
                edited = copy.copy(target)
                edited.content, edited.edited_at = 'nvm', dt.datetime.now(dt.timezone.utc)
                current[0] = edited
            elif change == 'delete':
                current.clear()
            elif change in ('access', 'image_access'):
                before = w.channels[GENERAL].permissions_for
                w.channels[GENERAL].permissions_for = lambda who: (
                    discord.Permissions.none() if getattr(who, 'id', None) == 20 else before(who))
        loads(monkeypatch)
        sent = signing_in(w, happen)
        try:
            if change == 'none':
                await w.fri.talk(123, DM, 20, text, 'private', 'member 20', event=event)
                assert [row['text'] for row in sent[0]['reply_target']] == ['kev cooked with this one']
                return
            with pytest.raises(commands.UserFeedbackCheckFailure):
                await w.fri.talk(123, DM, 20, text, 'private', 'member 20', event=event)
            assert sent == []  # Neither the old text nor the image went out.
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('image', [False, True])
@pytest.mark.parametrize('when', ['never', 'before_reading', 'while_signing_in'])
def test_a_revoke_the_cache_has_not_seen_closes_a_link_from_public(monkeypatch, tmp_path, image, when):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        everyone_reads(w, GENERAL, GAMING)
        target = w.message(GAMING, 'kev cooked with this one', user=30, age=60)
        if image:
            target.attachments = [picture(GAMING, 'meme.png')]
        hosted(w, GAMING, target)
        text = f'fri thoughts on https://discord.com/channels/123/{GAMING}/{target.id}'
        event = w.message(GENERAL, text)
        # Discord took #gaming from the speaker; the gateway cache still says they can read it.
        fresh = copy.copy(w.channels[GAMING])
        fresh.permissions_for = lambda who: discord.Permissions.none() if getattr(who, 'id', None) == 20 else \
            w.channels[GAMING].permissions_for(who)

        def revoke():
            w.channels[GENERAL].guild.fetch_channel.side_effect = lambda cid: fresh if cid == GAMING else w.channels[cid]
        if when == 'before_reading':
            revoke()
        loads(monkeypatch)
        sent = signing_in(w, revoke if when == 'while_signing_in' else lambda: None)
        try:
            if when == 'while_signing_in':
                with pytest.raises(commands.UserFeedbackCheckFailure):
                    await w.fri.talk(123, GENERAL, 20, text, 'public', 'member 20', event=event)
                assert sent == []
                return
            await w.fri.talk(123, GENERAL, 20, text, 'public', 'member 20', event=event)
            opened = when == 'never'
            assert [row['text'] for row in sent[0]['reply_target']] == (['kev cooked with this one'] if opened else [])
            assert bool(sent[0]['images']) == (opened and image)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def real_server(w, monkeypatch, overwrites):
    """discord.py's own guild, members and channel overwrites; role 50 is a role some members have."""
    state = w.fri._connection
    guild = discord.Guild(state=state, data={'id': '123', 'name': 'server', 'owner_id': '10', 'roles': [
        dict(id=str(rid), name=name, permissions='0', position=0, color=0, hoist=False, managed=False,
             mentionable=False) for rid, name in [(123, '@everyone'), (50, 'regulars')]]})
    guild.default_role._permissions = discord.Permissions(view_channel=True, read_message_history=True,
                                                           send_messages=True).value
    members = {}
    for uid, roles in [(20, []), (40, ['50']), (99, [])]:
        members[uid] = guild._members[uid] = discord.Member(data={
            'user': {'id': str(uid), 'username': f'user{uid}', 'discriminator': '0', 'avatar': None, 'bot': uid == 99},
            'roles': roles, 'flags': 0}, guild=guild, state=state)
    channels = {cid: discord.TextChannel(state=state, guild=guild, data={
        'id': str(cid), 'name': f'c{cid}', 'type': 0, 'position': 0,
        'permission_overwrites': overwrites if cid == GAMING else []}) for cid in (GENERAL, GAMING)}
    w.fri.get_channel = lambda cid: channels.get(cid, w.channels.get(cid))
    w.fri.agent.member = AsyncMock(return_value=members[20])

    async def fetch_channel(self, cid):
        return channels[cid]

    async def fetch_roles(self):
        return list(self.roles)
    monkeypatch.setattr(discord.Guild, 'fetch_channel', fetch_channel)
    monkeypatch.setattr(discord.Guild, 'fetch_roles', fetch_roles)
    return guild, members, channels


@pytest.mark.parametrize('kept_out', ['nobody', 'role', 'member', 'role_history'])
def test_a_channel_that_keeps_out_one_role_or_member_is_not_opened_from_public(monkeypatch, tmp_path, kept_out):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        deny = {'role': ('50', 0, discord.Permissions(view_channel=True)),
                'member': ('40', 1, discord.Permissions(view_channel=True)),
                'role_history': ('50', 0, discord.Permissions(read_message_history=True))}.get(kept_out)
        overwrites = [{'id': deny[0], 'type': deny[1], 'allow': '0', 'deny': str(deny[2].value)}] if deny else []
        guild, members, channels = real_server(w, monkeypatch, overwrites)
        target = w.message(GAMING, 'not for everyone', user=30, age=60)
        target.channel, target.guild = channels[GAMING], guild

        async def fetch(channel, mid):
            return target
        monkeypatch.setattr(discord.TextChannel, 'fetch_message', fetch)
        url = f'https://discord.com/channels/123/{GAMING}/{target.id}'
        public = w.message(GENERAL, f'fri quote {url}')
        public.channel, public.guild = channels[GENERAL], guild
        private = w.message(DM, f'fri quote {url}')
        try:
            # Member 40 reads #general either way; @everyone can view #gaming either way.
            assert channels[GENERAL].permissions_for(members[40]).read_message_history
            assert open_to_everyone(channels[GAMING]) == (kept_out == 'nobody')
            rows = (await evidence(w.fri.agent, public, []))['reply_target']
            assert [row['text'] for row in rows] == (['not for everyone'] if kept_out == 'nobody' else [])
            # In a DM only the speaker reads the answer, and they can read #gaming.
            rows = (await evidence(w.fri.agent, private, []))['reply_target']
            assert [row['text'] for row in rows] == ['not for everyone']
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', [discord.ForumChannel, discord.CategoryChannel])
def test_a_link_to_a_forum_or_a_category_opens_nothing_and_crashes_nothing(monkeypatch, tmp_path, kind):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        holder = Mock(spec=kind, id=GAMING, guild=w.channels[GENERAL].guild, nsfw=False, parent_id=None)
        holder.is_nsfw = lambda: False
        w.channels[GAMING] = holder  # A forum parent can be a configured channel, for its public threads.
        text = f'fri open https://discord.com/channels/123/{GAMING}/123456789'
        event = w.message(GENERAL, text)
        try:
            assert (await evidence(w.fri.agent, event, []))['reply_target'] == []
            await w.fri.talk(123, GENERAL, 20, text, 'public', 'member 20', event=event)
            w.fri.chat_once.assert_awaited_once()
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_fri_can_look_at_its_own_linked_picture(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        everyone_reads(w, GENERAL, GAMING)
        target = w.message(GAMING, 'my picture', user=99, age=60)
        target.author.bot = True
        target.attachments = [picture(GAMING, 'fri-picture.png')]
        hosted(w, GAMING, target)
        event = w.message(GENERAL, f'fri rate https://discord.com/channels/123/{GAMING}/{target.id}')
        loads(monkeypatch)
        try:
            posts = []
            context = await evidence(w.fri.agent, event, [], posts)
            assert context['reply_target'][0]['source']['user_id'] == '99'
            assert (await look(context, posts))['images_inspected'] == 1
            assert await looked_unchanged(context)
            actions = Actions(w.fri.agent, 20, GENERAL, True)
            actions.event, actions.conversation, actions.image_context = event, context, image_context(context)
            assert await actions.valid() and await unchanged(w.fri.agent, event, context)
        finally:
            await w.fri.close()
    asyncio.run(scenario())
