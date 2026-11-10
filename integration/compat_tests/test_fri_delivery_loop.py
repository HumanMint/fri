"""Delivery keeps its controller fences while optional model work is suspended."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from compat_tests.test_fri_social import DM, api, due, runtime  # noqa: F401
from fri.fri_agent import Agent


@pytest.mark.parametrize('switch', ['', 'enabled', 'support_dms'])
def test_due_reminder_during_hung_learning_is_sent_once(api, tmp_path, monkeypatch, switch):  # noqa: F811
    async def scenario():
        fri, social, row = runtime(api, tmp_path)
        # The reminder becomes due only after the optional stage has started.
        with social.store.connection() as db:
            db.execute('UPDATE fri_reminders SET due=due+86400 WHERE id=?', (row['id'],))
        blocked = False
        real_api = fri.api
        async def call(method, path, body=None):
            result = await real_api(method, path, body)
            if path == '/agent/state':
                result['config']['enabled'] = True
                if blocked:
                    result['config'][switch] = False
            return result
        fri.api = call
        fri.wait_until_ready = AsyncMock()
        agent = fri.agent = Agent(fri)
        agent.member = AsyncMock(return_value=SimpleNamespace(id=10))
        agent.check_presence = agent.send_reports = agent.attend = agent.check_memes = AsyncMock()
        agent.channel_withdrawals.settle = AsyncMock()
        agent.memes = SimpleNamespace(expire=Mock(), verify=AsyncMock())
        monkeypatch.setattr('fri.fri_workspace.deliver_one', AsyncMock())
        entered, sent, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        finish_send = asyncio.Event()

        async def thinking():
            entered.set()
            await release.wait()
        agent.learn = thinking

        async def send(*args, **kwargs):
            sent.set()
            await finish_send.wait()
            return {'id': '987'}
        sender = AsyncMock(side_effect=send)
        monkeypatch.setattr('fri.fri_social.send_once', sender)
        agent.heartbeat.change_interval(seconds=0.01)
        if hasattr(agent, 'delivery'):
            agent.delivery.change_interval(seconds=0.01)
        await agent.start()
        try:
            await asyncio.wait_for(entered.wait(), 1)
            blocked = bool(switch)
            due(social.store, row['id'])
            if switch:
                await asyncio.sleep(0.05)
                sender.assert_not_awaited()
                assert agent.config[switch] is True  # The suspended heartbeat still has its old state.
                blocked = False
            await asyncio.wait_for(sent.wait(), 0.3)
            # A slow send spans multiple intervals without starting a second delivery consumer.
            await asyncio.sleep(0.04)
            assert sender.await_count == 1
            finish_send.set()
            # Several delivery ticks overlap the still-suspended model stage.
            await asyncio.sleep(0.05)
            assert not release.is_set() and sender.await_count == 1
            assert social.reminder_list(DM)['items'][0]['state'] == 'sent'
            fri.fetch_channel.return_value.fetch_message.return_value.edit.assert_awaited_once()
            assert not list((tmp_path/'reminder-receipts').glob('*.json'))
            assert agent.heartbeat_current['stage'] == 'learning'
        finally:
            loops = [agent.heartbeat] + ([agent.delivery] if hasattr(agent, 'delivery') else [])
            agent.close()
            await asyncio.gather(*(loop.get_task() for loop in loops), return_exceptions=True)
    asyncio.run(scenario())
