"""A message that waited its turn is answered only as it stands now: one edited or deleted while it
waited is dropped, and a DM's waiting notice reruns the DM's own gates before it posts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_fri_mentions import environment
from fri.fri_chat import ChatResult


@pytest.mark.parametrize('change', ['edited', 'cached edit'])
def test_withdrawn_queued_message_is_neither_answered_nor_saved(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, _, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        event = message('fri repeat my personal marker lavender-bicycle')
        fri.chat_once.return_value = ChatResult(status='completed', reply='lavender-bicycle')
        sent_notice = asyncio.Event()
        fri.busy.add(20)

        async def reply(text, **kwargs):
            sent_notice.set()
            return SimpleNamespace(id=event.id + 1, content=text, delete=AsyncMock())

        event.reply.side_effect = reply
        task = None
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                task = asyncio.create_task(agent.handle(event))
                await asyncio.wait_for(sent_notice.wait(), 2)
                if change == 'edited':
                    import copy
                    import datetime as dt
                    current = copy.copy(event)
                    current.content = 'never mind'
                    current.edited_at = dt.datetime.now(dt.timezone.utc)
                    rows[event.id] = current
                else:
                    import datetime as dt
                    event.content = 'never mind'
                    event.edited_at = dt.datetime.now(dt.timezone.utc)
                fri.busy.discard(20)
                await asyncio.wait_for(task, 2)
            fri.chat_once.assert_not_awaited()
            assert not any(path == '/chat/turn' for path, _ in calls)
        finally:
            fri.busy.discard(20)
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['left', 'dms off'])
def test_dm_waiting_notice_requires_current_eligibility(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path)
        agent.config['support_dms'] = True
        agent.member = AsyncMock(return_value=SimpleNamespace(id=20))
        event = message('you there?')
        event.guild = None
        event.reply.return_value = SimpleNamespace(id=event.id + 1, content='hold on', delete=AsyncMock())
        fri.busy.add(20)
        original = fri.waiting_notices.send
        changed, notice = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            if not changed.is_set():
                changed.set()
                if change == 'left':
                    agent.member = AsyncMock(return_value=None)
                elif change == 'dms off':
                    agent.config['support_dms'] = False
            return await original(*args, **kwargs)

        fri.waiting_notices.send = send

        async def reply(*args, **kwargs):
            notice.set()
            fri.busy.discard(20)
            return SimpleNamespace(id=event.id + 1, content='hold on', delete=AsyncMock())

        event.reply.side_effect = reply
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await asyncio.wait_for(agent.handle(event), 2)
            event.reply.assert_not_awaited()
        finally:
            fri.busy.discard(20)
            await fri.close()
    asyncio.run(scenario())
