"""A cutoff Fri can't verify holds the post back (fri #337's round 1): an author's removed_at that's missing, malformed
(a string, an object, a boolean), NaN or negative leaves their post out of a reply target or a ping's nearby context,
without failing the reply, which is still answered. The speaker's own photo from before their erasure, paired with
their next message, is held to it too."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_fri_mentions import attachment, environment
from fri.fri_chat import ChatResult

MARKER = 'the juniper marker'
MISSING = object()
VALUES = {'a string': 'invalid-cutoff', 'an object': {'at': 1}, 'a boolean': True, 'NaN': float('nan'),
          'negative': -1.0, 'missing': MISSING}


def spoil(profile, value):
    """The author's profile with its removed_at replaced, or dropped, on both clocks: a controller that says it on
    Discord's too (fri #337 items 4 and 5) has Fri read that one."""
    profile = dict(profile)
    for key in ('removed_at', 'removed_at_discord'):
        if value is MISSING:
            profile.pop(key, None)
        else:
            profile[key] = value
    return profile


@pytest.mark.parametrize('value', list(VALUES))
@pytest.mark.parametrize('where', ['reply', 'ping'])
def test_a_post_whose_authors_cutoff_cant_be_verified_is_left_out_and_the_reply_goes_on(monkeypatch, tmp_path, value,
                                                                                         where):
    async def scenario():
        fri, agent, store, *_, message, calls, rows = environment(monkeypatch, tmp_path,
                                                                  channel_id=1 if where == 'reply' else 8)
        post = message('my answer is '+MARKER, user=30, age=20)
        if where == 'reply':  # A target's author is read through /agent/context (see target_row).
            profile = agent.profile

            async def read(channel, user):
                answer = await profile(channel, user)
                return spoil(answer, VALUES[value]) if user == 30 else answer
            agent.profile = read
            event = message('fri what do you make of that?', user=20)
        else:  # A ping's nearby authors, through the mention profiles (see Invitation.collect).
            api = fri.api

            async def answering(method, path, payload=None):
                answer = await api(method, path, payload)
                if path == '/chat/mentions/profiles' and '30' in answer['profiles']:
                    answer = {'profiles': {**answer['profiles'], '30': spoil(answer['profiles']['30'], VALUES[value])}}
                return answer
            fri.api = answering
            event = message('<@99> what do you make of that?', user=20)
        event.reference = SimpleNamespace(message_id=post.id, channel_id=1 if where == 'reply' else 8)
        event.reply.return_value = SimpleNamespace(id=event.id+1, content='hm')
        fri.chat_once.return_value = ChatResult(status='completed', reply='hm')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            fri.chat_once.assert_awaited_once()
            assert MARKER not in str(fri.chat_once.await_args)
            assert [item.args for item in event.reply.await_args_list] == [('hm',)]
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('value', ['a string', 'an object'])
def test_a_speakers_own_malformed_cutoff_doesnt_fail_their_reply(monkeypatch, tmp_path, value):
    """Their own message is still answered (only what it would draw on is held back), with nothing raised."""
    async def scenario():
        fri, agent, store, *_, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        profile = agent.profile

        async def read(channel, user):
            answer = await profile(channel, user)
            return {**answer, 'removed_at': VALUES[value], 'erased_at': VALUES[value]}
        agent.profile = read
        event = message('fri you there?', user=20)
        event.reply.return_value = SimpleNamespace(id=event.id+1, content='yep')
        fri.chat_once.return_value = ChatResult(status='completed', reply='yep')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert [item.args for item in event.reply.await_args_list] == [('yep',)]
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('when', ['before the erasure', 'after it', 'with memory still off'])
def test_the_speakers_photo_from_before_their_erasure_isnt_paired_with_their_next_message(monkeypatch, tmp_path, when):
    """fri #337's round 1: member 20 posts an uncaptioned photo, turns memory off and on again, then replies to it
    within a minute. The photo left the reply target, but was still paired as their photo just before (see
    select_source), so it reached the model. Posted after the erasure, it's paired as before, and so it is while
    their memory is still off: their own message then is answered with what it shows, as before."""
    async def scenario():
        fri, agent, store, *_, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        uri = 'data:image/png;base64,aGVsbG8='
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value=uri))
        if when == 'after it':
            store.personal_control('20', 'disable')
            store.personal_control('20', 'enable')
            time.sleep(0.01)
        photo = message('', user=20, age=0 if when == 'after it' else 20, attachments=[attachment(1)])
        if when != 'after it':
            store.personal_control('20', 'disable')
            if when == 'before the erasure':
                store.personal_control('20', 'enable')
            time.sleep(0.01)
        event = message('fri what is this?', user=20)
        event.reference = SimpleNamespace(message_id=photo.id, channel_id=1)
        event.reply.return_value = SimpleNamespace(id=event.id+1, content='a cat')
        fri.chat_once.return_value = ChatResult(status='completed', reply='a cat')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            fri.chat_once.assert_awaited_once()
            assert (uri in str(fri.chat_once.await_args)) is (when != 'before the erasure')
        finally:
            await fri.close()
    asyncio.run(scenario())
