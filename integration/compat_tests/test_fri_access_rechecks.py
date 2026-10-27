"""The last check before each model request reads the turn's own channel from Discord, not only the cache:
its author's access, and whether everyone can still read it before their words follow them elsewhere."""

import asyncio
import copy
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from compat_tests.test_fri_looking import loads, picture
from compat_tests.test_fri_member_profiles import (
    GAMING,
    GENERAL,
    model,
    model_context,
    note,
    turns,
    world,
)
from compat_tests.test_fri_mentions import environment
from fri.fri import CONVERSATION_CHANGED
from fri.fri_chat import ChatResult
from fri.fri_memory import PROFILES
from fri.fri_mentions import CONTEXT_CHANGED


def not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'Unknown Member')


def answering(fri, tmp_path, meanwhile):
    """Fri's own model call, its last checks run once signed in; `meanwhile` runs while it signs in."""
    reached = []
    del fri.chat_once  # The real one, with its last checks before the request.
    fri.data, fri.flush_usage = tmp_path, AsyncMock()  # Its usage receipt.

    async def respond(text, context, *, ready=None):
        meanwhile(context)
        await ready()
        reached.append(context)
        return ChatResult(status='completed', reply='an answer')
    fri.chat.respond = respond
    return reached


@pytest.mark.parametrize('target', [None, 'text', 'image'])
@pytest.mark.parametrize('change', ['stays', 'left', 'revoked'])
def test_a_channel_message_needs_its_author_able_to_read_there_before_the_model(monkeypatch, tmp_path, change, target):
    # Not a ping: no invitation reads the author's access before the request. The cache still has them in.
    # A post they replied to is judged against the same read, so an uncached revoke keeps it out too (fri #58).
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        guild = channel.guild
        if target:
            post = message('the plan' if target == 'text' else '', user=30, age=120,
                           attachments=[picture(1, 'meme.png')] if target == 'image' else ())
            loads(monkeypatch)
        event = message('fri what do you think of this?')
        if target:
            event.reference = SimpleNamespace(message_id=post.id, channel_id=1)

        def meanwhile(context):
            if change == 'left':
                guild.fetch_member.side_effect = lambda user: (_ for _ in ()).throw(not_found()) if user == 20 else (
                    SimpleNamespace(id=user, bot=user == 99))
            elif change == 'revoked':  # Discord already denies them; the gateway's cached channel hasn't caught up.
                fetched = copy.copy(channel)
                fetched.permissions_for = lambda who: (discord.Permissions.none() if who.id == 20
                                                       else discord.Permissions.all())
                guild.fetch_channel.return_value = fetched
        reached = answering(fri, tmp_path, meanwhile)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            if change == 'stays':
                assert len(reached) == 1 and [item.args[0] for item in event.reply.await_args_list] == ['an answer']
                if target:
                    assert [row['source']['id'] for row in reached[0]['reply_target']] == [str(post.id)]
                    assert bool(reached[0].get('images')) is (target == 'image')
            else:  # Nothing reaches the model. A post they replied to is refetched first, as their access then
                # already shows they left: its check's fixed notice, with nothing from the post, is all that goes out.
                assert reached == []
                assert [item.args[0] for item in event.reply.await_args_list] == (
                    [CONVERSATION_CHANGED] if change == 'left' and target else [])
        finally:
            await fri.close()
    asyncio.run(scenario())


def deny(channel, user):
    """The channel as Discord has it now: one member kept out by an overwrite the cache hasn't seen."""
    fetched = copy.copy(channel)
    fetched.overwrites = {discord.Object(id=user): discord.PermissionOverwrite(view_channel=False)}
    return fetched


@pytest.mark.parametrize('denied', [False, True])
def test_words_said_where_discord_keeps_someone_out_stay_there(monkeypatch, tmp_path, denied):
    # The cache still shows #general open to everyone (fri #97).
    async def scenario():
        w = world(monkeypatch, tmp_path)
        general = w.channels[GENERAL]
        if denied:
            general.guild.fetch_channel.side_effect = lambda cid: (
                deny(general, 30) if cid == GENERAL else w.channels[cid])

        async def respond(text, context):
            return ChatResult(status='completed', reply='noted', notes=note('I like private jazz', 'music')
                              if 'jazz' in text else [])
        model(w, respond)
        try:
            _, saved, _ = await w.say(GENERAL, 'fri I like private jazz')
            assert turns(w)[-1]['audience'] == ('conversation' if denied else 'server')
            assert saved['memory_scope'] == ('conversation' if denied else 'server')
            await w.say(GAMING, 'fri recommend me something')
            seen = json.dumps(model_context(w), default=str)
            assert ('private jazz' in seen) is not denied  # Neither the note nor the raw elsewhere row.
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('denied', [False, True])
def test_a_ping_where_discord_keeps_someone_out_keeps_its_words_there(monkeypatch, tmp_path, denied):
    # A configured ping's own check read the channel already (Invitation.access): that read decides it.
    async def scenario():
        fri, agent, _, _, channel, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset({PROFILES})
        channel.guild.default_role = SimpleNamespace(id=123)
        channel.is_nsfw, channel.overwrites = (lambda: False), {}  # Open to everyone, as cached.
        if denied:
            channel.guild.fetch_channel.return_value = deny(channel, 30)
        reached = answering(fri, tmp_path, lambda context: None)
        event = message('<@99> I like private jazz')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert len(reached) == 1 and [item.args[0] for item in event.reply.await_args_list] == ['an answer']
            assert [body['audience'] for path, body in calls if path == '/chat/turn'] == [
                'conversation' if denied else 'server']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['stays', 'edited', 'deleted'])
def test_a_text_read_for_a_ping_is_checked_again_once_fri_signs_in(monkeypatch, tmp_path, change):
    # A configured ping reads its own channel's nearby posts (Actions.invited_context): no image, so no image
    # check asks the invitation. A post edited or deleted while Fri signs in for the next request stays out of
    # it, with no gateway event to mark it (fri #75).
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        nearby = message('the old plan', user=30, age=120)
        event = message('<@99> sum up what they said above')
        del fri.chat_once  # The real one, with its last checks before the request.
        fri.data, fri.flush_usage = tmp_path, AsyncMock()
        reached = []

        async def respond(text, context, *, ready=None):
            if context.get('action_result') and change == 'edited':
                changed = copy.copy(nearby)
                changed.content, changed.edited_at = 'retracted', dt.datetime.now(dt.timezone.utc)
                rows[nearby.id] = changed
            elif context.get('action_result') and change == 'deleted':
                del rows[nearby.id]
            await ready()
            reached.append(context)
            if not context.get('action_result'):
                return ChatResult(status='completed', action={'kind': 'read_recent', 'channel_id': '1'})
            return ChatResult(status='completed', reply='they wanted the old plan')
        fri.chat.respond = respond
        channel.fetch_message.side_effect = lambda mid: rows[mid] if mid in rows else (_ for _ in ()).throw(not_found())
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            texts = [item.args[0] for item in event.reply.await_args_list]
            if change == 'stays':
                assert len(reached) == 2 and texts == ['they wanted the old plan']
                assert [row['text'] for row in reached[1]['invited_context']] == ['the old plan']
            else:  # The request carrying the old words never goes out; the ping hears why.
                assert len(reached) == 1 and texts == [CONTEXT_CHANGED]
        finally:
            await fri.close()
    asyncio.run(scenario())
