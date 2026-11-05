"""A queued message its author sent before they turned memory off, handled once it's on again, goes unanswered and
keeps nothing (fri #331, HumanMint/gene#206). What they say after it is answered and kept as before."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_turn_withdrawal import MARKER, saved, world
from fri.fri_chat import ChatResult


def test_a_queued_message_from_before_memory_was_turned_off_and_on_goes_unanswered(monkeypatch, tmp_path):
    """fri #331: a member's message waited, they turned memory off and on again through Gene, and only then did Fri
    handle it. Fri read their new revision, kept the message out of recent context, and answered it all the same: the
    model got their words from before the erasure, and Gene saved the exchange and a note under the new revision. It
    goes unanswered, quietly, as a turn whose author changed their privacy does."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            queued = w.said('fri remember '+MARKER)
            time.sleep(0.01)  # Snowflakes count milliseconds: it was sent before what follows.
            w.store.personal_control('20', 'disable')
            w.store.personal_control('20', 'enable')
            time.sleep(0.01)
            await w.agent.handle(queued)
            assert MARKER not in str(w.requests) and MARKER not in saved(w)
            queued.reply.assert_not_awaited()
            fresh = w.said('fri and what about now?')
            await w.agent.handle(fresh)
            assert 'what about now' in str(w.requests) and 'what about now' in saved(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('where,channel_id,text', [
    ('chat', 1, 'fri what do you think?'),
    ('ping', 1, '<@99> what do you think?'),
    ('outside ping', 8, '<@99> what do you think?'),
    ('dm', 8, 'what do you think?'),
])
def test_a_queued_message_from_before_the_erasure_goes_unanswered_wherever_it_was_sent(
        monkeypatch, tmp_path, where, channel_id, text):
    """fri #331 on each way an ordinary message reaches the model: a configured channel's chat and ping, a ping outside
    the configured channels, and a DM. The one sent before the erasure gets nothing; the next one is answered."""
    async def scenario():
        fri, agent, store, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=channel_id)
        if where == 'dm':
            agent.config['support_dms'] = True
            agent.member = AsyncMock(return_value=SimpleNamespace(id=20))

        def said(age=0):
            event = message(text, age=age)
            if where == 'dm':
                event.guild = None
            event.reply.return_value = SimpleNamespace(id=event.id+1, content='sure')
            return event

        queued = said(age=30)  # Sent half a minute ago, and handled only now.
        store.personal_control('20', 'disable')  # Through Gene, so Fri hasn't forgotten anything itself.
        store.personal_control('20', 'enable')
        time.sleep(0.01)
        fri.chat_once.return_value = ChatResult(status='completed', reply='sure')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(queued)
                fri.chat_once.assert_not_awaited()
                queued.reply.assert_not_awaited()
                assert not any(path == '/chat/turn' for path, _ in calls)
                fresh = said()
                await agent.handle(fresh)
            fri.chat_once.assert_awaited_once()
            assert [item.args for item in fresh.reply.await_args_list] == [('sure',)]
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('where,channel_id,text', [
    ('chat', 1, 'fri what do you think?'),
    ('ping', 1, '<@99> what do you think?'),
    ('outside ping', 8, '<@99> what do you think?'),
    ('dm', 8, 'what do you think?'),
])
def test_with_memory_still_off_a_queued_message_is_answered_as_before(monkeypatch, tmp_path, where, channel_id, text):
    """Only memory back on holds a message to the cutoff: with it still off, Fri answers it as before."""
    async def scenario():
        fri, agent, store, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=channel_id)
        if where == 'dm':
            agent.config['support_dms'] = True
            agent.member = AsyncMock(return_value=SimpleNamespace(id=20))
        queued = message(text, age=30)
        if where == 'dm':
            queued.guild = None
        queued.reply.return_value = SimpleNamespace(id=queued.id+1, content='sure')
        store.personal_control('20', 'disable')
        time.sleep(0.01)
        fri.chat_once.return_value = ChatResult(status='completed', reply='sure')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(queued)
            fri.chat_once.assert_awaited_once()
            assert [item.args for item in queued.reply.await_args_list] == [('sure',)]
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['fri forget me', '<@99> forget me'])
def test_a_queued_memory_control_from_before_the_erasure_is_still_carried_out(monkeypatch, tmp_path, text):
    """A memory control is never held to the cutoff."""
    async def scenario():
        fri, agent, store, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        queued = message(text, age=30)
        queued.reply.return_value = SimpleNamespace(id=queued.id+1, content='done')
        store.personal_control('20', 'disable')
        store.personal_control('20', 'enable')
        time.sleep(0.01)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(queued)
            assert [payload['action'] for path, payload in calls if path == '/chat/memory/control'] == ['disable']
            assert queued.reply.await_args.args[0].startswith('done. i erased our saved chats')
            fri.chat_once.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())
