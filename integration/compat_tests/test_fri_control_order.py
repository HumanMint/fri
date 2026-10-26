"""A member's memory controls take effect in the order they were sent, and a DM reply goes out, and is
saved, only while its author is still a member and DMs are on."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_fri_mentions import environment
from fri.fri_agent import STALE_CONTROL
from fri.fri_chat import ChatResult

ACTIONS = {'fri forget me': 'disable', 'fri remember me again': 'enable'}


def actions(calls):
    return [payload['action'] for path, payload in calls if path == '/chat/memory/control']


@pytest.mark.parametrize('first, second', [('fri remember me again', 'fri forget me'),
                                           ('fri forget me', 'fri remember me again')])
def test_controls_follow_send_order_when_the_first_arrival_read_is_slow(monkeypatch, tmp_path, first, second):
    async def scenario():
        fri, agent, _, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        earlier, later = message(first), message(second)
        for event in (earlier, later):
            event.reply.return_value = SimpleNamespace(id=event.id+1, content='done')
        api, held, reading = fri.api, asyncio.Event(), asyncio.Event()
        async def slow(method, path, payload=None):
            if path == '/agent/context' and not reading.is_set():  # The earlier control's arrival read.
                reading.set()
                await held.wait()
            return await api(method, path, payload)
        fri.api = slow
        real_sleep = asyncio.sleep
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                tasks = [asyncio.create_task(agent.handle(earlier))]
                await asyncio.wait_for(reading.wait(), 5)
                tasks.append(asyncio.create_task(agent.handle(later)))
                for _ in range(50):  # Long enough for the later one to finish, if it doesn't wait.
                    if actions(calls) or tasks[1].done():
                        break
                    await real_sleep(0.01)
                held.set()
                await asyncio.wait_for(asyncio.gather(*tasks), 5)
            assert actions(calls) == [ACTIONS[first], ACTIONS[second]]
            assert (await agent.profile(1, 20))['enabled'] == (second == 'fri remember me again')
            assert earlier.reply.await_count == later.reply.await_count == 1
        finally:
            held.set()
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('earlier', ['fri what is ash working on?', '<@99> fri forget me'])
def test_a_memory_control_never_waits_for_an_earlier_message_that_isnt_one(monkeypatch, tmp_path, earlier):
    # Chat waiting its turn, or an outside ping worded like a control (chat there), holds nothing up.
    async def scenario():
        fri, agent, _, _, channel, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        waiting = message(earlier)
        if earlier.startswith('<@99>'):
            outside = SimpleNamespace(**{**vars(channel), 'id': 8})
            waiting.channel = outside
        waiting.reply.return_value = SimpleNamespace(id=waiting.id+1, content='hold on', delete=AsyncMock())
        control = message('fri forget me')
        control.reply.return_value = SimpleNamespace(id=control.id+1, content='done')
        fri.busy.add(20)  # A reply to them that's still going, so the chat waits its turn.
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                task = asyncio.create_task(agent.handle(waiting))
                await asyncio.sleep(0)
                await asyncio.wait_for(agent.handle(control), 5)
                assert actions(calls) == ['disable']
                fri.busy.discard(20)
                await asyncio.wait_for(task, 5)
        finally:
            fri.busy.discard(20)
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('first, second', [('fri remember me again', 'fri forget me'),
                                           ('fri forget me', 'fri remember me again'),
                                           ('fri forget me', 'fri forget me')])
def test_a_control_that_comes_in_after_a_newer_one_ran_does_not_undo_it(monkeypatch, tmp_path, first, second):
    # E.g. Discord delivering the older one late: it can't wait for what already happened.
    async def scenario():
        fri, agent, _, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        earlier, later = message(first), message(second)
        for event in (earlier, later):
            event.reply.return_value = SimpleNamespace(id=event.id+1, content='done')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(later)
                await agent.handle(earlier)
            if first == second:  # Carrying it out again changes nothing the newer one didn't.
                assert actions(calls) == [ACTIONS[second], ACTIONS[first]]
                assert earlier.reply.await_args.args[0].startswith('done. i erased our saved chats')
            else:
                assert actions(calls) == [ACTIONS[second]]
                assert earlier.reply.await_args.args[0] == STALE_CONTROL
            assert (await agent.profile(1, 20))['enabled'] == (second == 'fri remember me again')
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['reply', 'react', 'quiet'])
@pytest.mark.parametrize('change', ['stays', 'left', 'dms off'])
def test_a_dm_reply_is_neither_sent_nor_saved_once_its_author_left_or_dms_went_off(monkeypatch, tmp_path, change, kind):
    async def scenario():
        fri, agent, _, _, _, message, _, rows = environment(monkeypatch, tmp_path)
        agent.config['support_dms'] = True
        present = {'member': SimpleNamespace(id=20)}
        agent.member = AsyncMock(side_effect=lambda *_, **__: present['member'])
        event = message('what is ash working on?')
        event.guild = None
        event.add_reaction, event.reactions = AsyncMock(), []
        event.reply.return_value = SimpleNamespace(id=event.id+1, content='an answer')
        async def answer(*_):  # While the model writes, after every check before the request.
            if change == 'left':
                present['member'] = None
            elif change == 'dms off':
                agent.config['support_dms'] = False
            return ChatResult(status='completed', response_kind=kind, reaction='👍' if kind == 'react' else None,
                              reply='an answer' if kind == 'reply' else '')
        fri.chat_once.side_effect = answer
        fri.record_turn = AsyncMock(return_value={'kept': True})
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            if change == 'stays':
                fri.record_turn.assert_awaited_once()
                assert event.reply.await_count == (kind == 'reply')
            else:
                fri.record_turn.assert_not_awaited()
                event.reply.assert_not_awaited()
                event.add_reaction.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['stays', 'left', 'dms off'])
def test_a_queued_dm_reply_needs_its_author_still_there_when_it_goes_out(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        agent.config['support_dms'] = True
        present = {'member': SimpleNamespace(id=20)}
        agent.member = AsyncMock(side_effect=lambda *_, **__: present['member'])
        event = message('you there?')
        event.guild = None
        replies = []
        async def reply(text, **_):
            replies.append(text)
            return SimpleNamespace(id=event.id+len(replies), content=text, delete=AsyncMock())
        event.reply.side_effect = reply
        async def answer(*_):
            if change == 'left':
                present['member'] = None
            elif change == 'dms off':
                agent.config['support_dms'] = False
            return ChatResult(status='completed', reply='yeah')
        fri.chat_once.side_effect = answer
        fri.record_turn = AsyncMock(return_value={'kept': True})
        fri.busy.add(20)  # An earlier reply to them, still going.
        real_sleep = asyncio.sleep
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                task = asyncio.create_task(agent.handle(event))
                while not replies:  # Told to hold on.
                    await real_sleep(0.01)
                fri.busy.discard(20)
                await asyncio.wait_for(task, 5)
            assert ('yeah' in replies) == (change == 'stays')
            assert fri.record_turn.await_count == (change == 'stays')
        finally:
            fri.busy.discard(20)
            await fri.close()
    asyncio.run(scenario())
