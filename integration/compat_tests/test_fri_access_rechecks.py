"""The last check before each model request reads the turn's own channel from Discord, not only the cache."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from compat_tests.test_fri_looking import loads, picture
from compat_tests.test_fri_mentions import environment
from fri.fri import CONVERSATION_CHANGED
from fri.fri_chat import ChatResult


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
