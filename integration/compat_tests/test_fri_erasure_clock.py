"""The erasure cutoff on Discord's clock (fri #337 items 4 and 5, with the paired Gene change). Gene stamped a member's
erasure with its own clock, and Fri compared it with a message's snowflake, on Discord's. With Gene's clock behind, a
message sent just before the erasure counted as after it (item 4); ahead, one sent just after it counted as before
(item 5). Fri sends the message or interaction that asked with the control, and compares with the cutoff Gene dates by
it."""

import asyncio
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_fri_mentions import environment
from fri.fri_chat import ChatResult

MARKER = 'the larch marker'
REAL = time.time


@contextmanager
def gene_clock(offset):
    """Gene's clock `offset` seconds off Discord's, which the snowflakes here keep."""
    time.time = lambda: REAL() + offset
    try:
        yield
    finally:
        time.time = REAL


def world(monkeypatch, tmp_path, offset, dm=False):
    """The mention harness, with Gene's clock `offset` seconds off Discord's while it carries out a memory control."""
    fri, agent, store, *_, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=8 if dm else 1)
    if dm:
        agent.config['support_dms'] = True
        agent.member = AsyncMock(return_value=SimpleNamespace(id=20))
    api = fri.api

    async def clocked(method, path, payload=None):
        if path == '/chat/memory/control':
            with gene_clock(offset):
                return await api(method, path, payload)
        return await api(method, path, payload)
    fri.api = clocked

    def said(text, *, user=20, age=0):
        event = message(text, user=user, age=age)
        if dm:
            event.guild = None
        event.reply.return_value = SimpleNamespace(id=event.id+1, content='sure')
        return event

    async def erase_and_restore(user=20):
        """Memory off and on again, through Fri, as the member asks for it."""
        for text in ('fri forget me', 'fri remember me again'):
            event = said(text, user=user)
            await fri.talk(123, event.channel.id, user, text, 'private' if dm else 'public', event=event)
            time.sleep(0.01)  # Snowflakes count milliseconds: what follows is after it.
    fri.chat_once.return_value = ChatResult(status='completed', reply='sure')
    return fri, agent, said, erase_and_restore


@pytest.mark.parametrize('where', ['chat', 'dm'])
def test_gene_behind_a_queued_message_sent_just_before_the_erasure_goes_unanswered(monkeypatch, tmp_path, where):
    """Item 4: Gene's clock two seconds behind Discord's. The member's message went out half a second before their
    "forget me", and was handled once memory was on again. Gene's stamp put it after the erasure, so it was answered,
    their words from before the erasure in the request. On Discord's clock it's before: it goes unanswered."""
    async def scenario():
        fri, agent, said, erase_and_restore = world(monkeypatch, tmp_path, -2, dm=where == 'dm')
        try:
            queued = said('fri remember '+MARKER, age=0.5)
            await erase_and_restore()
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(queued)
            fri.chat_once.assert_not_awaited()
            queued.reply.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('where', ['chat', 'dm'])
def test_gene_ahead_a_fresh_message_after_the_reenable_is_answered(monkeypatch, tmp_path, where):
    """Item 5: Gene's clock two seconds ahead. A fresh message right after the member turned memory on again counted
    as before the erasure, and went unanswered. On Discord's clock it's after: it's answered."""
    async def scenario():
        fri, agent, said, erase_and_restore = world(monkeypatch, tmp_path, 2, dm=where == 'dm')
        try:
            await erase_and_restore()
            fresh = said('fri whats up')
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(fresh)
            fri.chat_once.assert_awaited_once()
            assert [item.args for item in fresh.reply.await_args_list] == [('sure',)]
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('how', ['reply', 'ping'])
@pytest.mark.parametrize('offset', [-2, 2])
def test_a_post_is_held_to_its_authors_erasure_on_discords_clock(monkeypatch, tmp_path, how, offset):
    """Items 4 and 5 for what a reply reads: member 30's post, replied to or pinged near by member 20, after member 30
    turned memory off and on again through Fri. With Gene behind, their post from half a second before was read as
    after the erasure and reached the model; with Gene ahead, their fresh post after it was left out. On Discord's
    clock the first is left out and the second is read."""
    async def scenario():
        fri, agent, said, erase_and_restore = world(monkeypatch, tmp_path, offset)
        try:
            if offset < 0:
                post = said('my answer is '+MARKER, user=30, age=0.5)
                await erase_and_restore(user=30)
            else:
                await erase_and_restore(user=30)
                post = said('my answer is '+MARKER, user=30)
            event = said('<@99> what do you make of that?' if how == 'ping' else 'fri what do you make of that?')
            event.reference = SimpleNamespace(message_id=post.id, channel_id=1)
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            fri.chat_once.assert_awaited_once()
            assert (MARKER in str(fri.chat_once.await_args)) is (offset > 0)
        finally:
            await fri.close()
    asyncio.run(scenario())
