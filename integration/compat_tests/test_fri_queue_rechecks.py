"""A message that waited its turn is answered only as it stands now: one edited or deleted while it
waited is dropped, and a DM's waiting notice reruns the DM's own gates before it posts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_unsend import GONE
from compat_tests.test_fri_withdrawn_rows import Waits
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
            # Nor can a later turn, even another member's, show the model what it said.
            assert not any(row['id'] == str(event.id) for rows in agent.recent.values() for row in rows)
        finally:
            fri.busy.discard(20)
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['edited', 'deleted'])
def test_attention_never_carries_a_queued_message_taken_back_before_its_recheck(monkeypatch, tmp_path, change):
    # fri#85: the queued message is edited or deleted while attention, for someone else's question, waits for
    # a model slot, and its author is still busy, so its own recheck hasn't run yet.
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        queued = message('fri repeat my personal marker lavender-bicycle')
        question = message('anyone know why the build is broken?', user=21)
        sent_notice = asyncio.Event()
        fri.busy.add(20)

        async def reply(text, **kwargs):
            sent_notice.set()
            return SimpleNamespace(id=queued.id + 1, content=text, delete=AsyncMock())

        queued.reply.side_effect = reply

        def fetch(mid):
            if mid not in rows:
                raise GONE
            return rows[mid]

        channel.fetch_message.side_effect = fetch
        fri.chat.headers = AsyncMock(return_value={})
        model = AsyncMock(return_value=({'action': 'quiet', 'source_id': '', 'reason': ''}, {}, 'completed'))

        async def take_back():
            if change == 'edited':
                edited = SimpleNamespace(id=queued.id, content='never mind', author=queued.author, attachments=[])
                rows[queued.id] = SimpleNamespace(**vars(edited), edited_at=object())
                await fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=edited))
            else:
                del rows[queued.id]
                await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=queued.id))

        task = None
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()), patch('fri.fri_agent.analyze', model):
                task = asyncio.create_task(agent.handle(queued))
                await asyncio.wait_for(sent_notice.wait(), 2)
                await agent.handle(question)
                assert {r['id'] for r in agent.recent[1]} == {str(queued.id), str(question.id)}
                await Waits(agent, fri).during('slot', agent.attend, take_back)
                if change == 'deleted':  # It left the live view: the batch waits for the next window.
                    model.assert_not_awaited()
                    agent.last_attention[1] = 0
                    await agent.attend()
                assert 20 in fri.busy  # Still before the queued message's own recheck.
                model.assert_awaited_once()
                sent = str(model.await_args)
                assert 'lavender-bicycle' not in sent and question.content in sent
                fri.busy.discard(20)
                await asyncio.wait_for(task, 2)
            fri.chat_once.assert_not_awaited()
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
