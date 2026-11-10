"""The heartbeat owns delivery and withdrawal retries; one optional pass runs beside it."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app

from compat_tests.background import tick
from compat_tests.test_api import headers
from compat_tests.test_fri_learning import HistoryChannel, learning, value  # noqa: F401
from compat_tests.test_fri_social import due, runtime
from compat_tests.test_fri_summary_hold_deliveries import api, depends, summary_of_20  # noqa: F401
from fri.fri_agent import Agent
from fri.fri_agent_model import ATTENTION, DEADLINE, analyze
from fri.fri_chat import Chat
from fri.fri_errors import ControllerUnavailable


def delivery_agent(controller, tmp_path, monkeypatch):
    fri, social, row = runtime(controller, tmp_path)
    with social.store.connection() as db:
        db.execute('UPDATE fri_reminders SET due=due+86400 WHERE id=?', (row['id'],))
    fri.owner_id, fri.workshop_id = 10, 456
    fri.wait_until_ready = AsyncMock()
    fri.get_guild = lambda _: None
    fri.capabilities = {'channel-withdrawals'}
    agent = fri.agent = Agent(fri)
    agent.member = AsyncMock(return_value=SimpleNamespace(id=10))
    agent.check_presence = agent.attend = agent.check_memes = agent.send_reports = AsyncMock()
    agent.memes = SimpleNamespace(expire=Mock(), verify=AsyncMock())
    monkeypatch.setattr('fri.fri_fixes.check', AsyncMock())
    real = fri.api
    async def call(method, path, body=None):
        result = await real(method, path, body)
        if path == '/agent/state':
            result['config']['enabled'] = True
        return result
    fri.api = call
    return fri, agent, social, row


async def stop(agent):
    tasks = [agent.heartbeat.get_task(), getattr(agent, 'background_task', None)]
    # Also close the superseded candidate's delivery loop when disproving that SHA.
    if hasattr(agent, 'delivery'):
        tasks.append(agent.delivery.get_task())
        agent.delivery.cancel()
    agent.close()
    await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)


@pytest.mark.parametrize('slow', ['check_presence', 'learn'])
def test_reminder_is_on_time_during_a_600_second_model_call(api, tmp_path, monkeypatch, slow):  # noqa: F811
    async def scenario():
        fri, agent, social, row = delivery_agent(api, tmp_path, monkeypatch)
        entered, release, sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def model(request):
            entered.set()
            await release.wait()
            return httpx.Response(500)
        chat = Chat(SimpleNamespace(access_token=lambda: 'synthetic'), httpx.MockTransport(model))
        async def learn():
            assert DEADLINE == 600
            await analyze(chat, 'Decide.', {}, 'gpt-6-luna', 'low', ATTENTION)
        setattr(agent, slow, learn)
        async def send(*args, **kwargs):
            sent.set()
            return {'id': '987'}
        sender = AsyncMock(side_effect=send)
        monkeypatch.setattr('fri.fri_social.send_once', sender)
        agent.heartbeat.change_interval(seconds=0.01)
        await agent.start()
        try:
            await asyncio.wait_for(entered.wait(), 1)
            due(social.store, row['id'])
            await asyncio.wait_for(sent.wait(), 0.3)
            await asyncio.sleep(0.05)
            assert not release.is_set() and sender.await_count == 1
            assert agent.background_current['stage'] == ('presence' if slow == 'check_presence' else 'learning')
            assert agent.heartbeat.current_loop >= 2
            assert social.reminder_list({'guild_id': '123', 'user_id': '10',
                'channel_id': '456', 'conversation_id': 'private:999'})['items'][0]['state'] == 'sent'
            fri.fetch_channel.return_value.fetch_message.return_value.edit.assert_awaited_once()
        finally:
            await stop(agent)
    asyncio.run(scenario())


@pytest.mark.parametrize('withdrawn', [False, True])
def test_transient_withdrawal_retries_while_worker_busy_and_restart_never_replays(api, tmp_path, monkeypatch, withdrawn):  # noqa: F811
    async def scenario():
        fri, agent, social, row = delivery_agent(api, tmp_path, monkeypatch)
        if withdrawn:
            summary_of_20(social.store, held=False)
            depends(social.store, 'fri_reminder_dependencies', 'reminder_id', row['id'])
        entered, release = asyncio.Event(), asyncio.Event()
        async def learn():
            entered.set()
            await release.wait()
        agent.learn = learn
        sender = AsyncMock(return_value={'id': '987'})
        monkeypatch.setattr('fri.fri_social.send_once', sender)
        await agent.heartbeat.coro(agent)
        await asyncio.wait_for(entered.wait(), 1)
        agent.channel_withdrawals.withdraw(123, 1, send=False)
        real, attempts = fri.api, []
        async def call(method, path, body=None):
            if path == '/chat/channels/withdraw':
                attempts.append(path)
                if len(attempts) == 1:
                    raise ControllerUnavailable('synthetic transient failure')
            return await real(method, path, body)
        fri.api = call
        due(social.store, row['id'])
        try:
            await asyncio.wait_for(agent.heartbeat.coro(agent), 1)
            assert agent.saved_turns.closed()
            sender.assert_not_awaited()
            with social.store.connection() as db:
                assert db.execute('SELECT state FROM fri_reminders WHERE id=?', (row['id'],)).fetchone()[0] == 'scheduled'
            await asyncio.wait_for(agent.heartbeat.coro(agent), 1)
            assert len(attempts) == 2 and not agent.saved_turns.closed()
            assert sender.await_count == (0 if withdrawn else 1)
            assert agent.background_current['stage'] == 'learning' and not release.is_set()
        finally:
            await stop(agent)
        # A fresh Agent and its durable receipts use the same controller state.
        restarted = fri.agent = Agent(fri)
        restarted.member = agent.member
        restarted.check_presence = restarted.learn = restarted.attend = restarted.check_memes = restarted.send_reports = AsyncMock()
        restarted.memes = SimpleNamespace(expire=Mock(), verify=AsyncMock())
        await tick(restarted)
        assert sender.await_count == (0 if withdrawn else 1)
        with social.store.connection() as db:
            assert db.execute('SELECT state FROM fri_reminders WHERE id=?', (row['id'],)).fetchone()[0] == ('cancelled' if withdrawn else 'sent')
    asyncio.run(scenario())


def test_delayed_persisted_withdrawal_keeps_delivery_behind_settlement(api, tmp_path, monkeypatch):  # noqa: F811
    async def scenario():
        fri, old, social, row = delivery_agent(api, tmp_path, monkeypatch)
        summary_of_20(social.store, held=False)
        depends(social.store, 'fri_reminder_dependencies', 'reminder_id', row['id'])
        old.channel_withdrawals.withdraw(123, 1, send=False)
        agent = fri.agent = Agent(fri)
        agent.member = old.member
        agent.check_presence = agent.learn = agent.attend = agent.check_memes = agent.send_reports = AsyncMock()
        agent.memes = old.memes
        entered, release = asyncio.Event(), asyncio.Event()
        real = fri.api
        async def call(method, path, body=None):
            if path == '/chat/channels/withdraw':
                entered.set()
                await release.wait()
            return await real(method, path, body)
        fri.api = call
        sender = AsyncMock(return_value={'id': '987'})
        monkeypatch.setattr('fri.fri_social.send_once', sender)
        due(social.store, row['id'])
        task = asyncio.create_task(agent.heartbeat.coro(agent))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert agent.saved_turns.closed()
            sender.assert_not_awaited()
            with social.store.connection() as db:
                assert db.execute('SELECT state FROM fri_reminders WHERE id=?', (row['id'],)).fetchone()[0] == 'scheduled'
            release.set()
            await asyncio.wait_for(task, 1)
            sender.assert_not_awaited()
            with social.store.connection() as db:
                assert db.execute('SELECT state FROM fri_reminders WHERE id=?', (row['id'],)).fetchone()[0] == 'cancelled'
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await stop(agent)
    asyncio.run(scenario())


@pytest.mark.parametrize('withdrawn', [False, True])
def test_settlement_mid_learning_preserves_generation_fence_and_budget(learning, tmp_path, monkeypatch, withdrawn):  # noqa: F811
    async def scenario():
        clock, store, settings, scheduler = learning
        channel = HistoryChannel()
        channel.add(clock[0]-100)
        client = TestClient(create_app(settings))
        async def call(method, path, body=None):
            result = client.request(method, path, json=body, headers=headers('fri'))
            assert result.status_code == 200, result.text
            return result.json()
        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, data=tmp_path, api=call,
            get_channel=lambda _: channel, get_guild=lambda _: None, capabilities={'channel-withdrawals', 'learning-withdrawals'},
            chat=SimpleNamespace(headers=AsyncMock(return_value={})))
        agent = Agent(fri)
        agent.memes = SimpleNamespace(expire=Mock(), verify=AsyncMock())
        agent.check_presence = agent.attend = agent.check_memes = agent.send_reports = AsyncMock()
        agent.profile = AsyncMock(side_effect=lambda cid, user: scheduler.context(str(cid), str(user)))
        entered, release = asyncio.Event(), asyncio.Event()
        async def member(user):
            entered.set()
            await release.wait()
            return SimpleNamespace(roles=[SimpleNamespace(id=90)])
        agent.member = member
        model = AsyncMock(return_value=({'summary': 'music', 'notes': []}, {'input_tokens': 2}, 'completed'))
        monkeypatch.setattr('fri.fri_agent.analyze', model)
        monkeypatch.setattr('fri.fri_workspace.deliver_one', AsyncMock())
        monkeypatch.setattr('fri.fri_social.deliver_one', AsyncMock())
        monkeypatch.setattr('fri.fri_fixes.check', AsyncMock())
        agent.record = AsyncMock()
        await agent.heartbeat.coro(agent)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            generation = agent.channel_withdrawals.generation
            agent.channel_withdrawals.withdraw(123, 1 if withdrawn else 2, send=False)
            await agent.heartbeat.coro(agent)
            assert agent.channel_withdrawals.generation > generation
            assert not agent.saved_turns.closed()
            release.set()
            await asyncio.wait_for(agent.background_task, 1)
            assert model.await_count == (0 if withdrawn else 1)
            assert agent.record.await_count == (0 if withdrawn else 1)
            assert value(store, 'budget')['used'] == (0 if withdrawn else 1)
        finally:
            release.set()
            await stop(agent)
    asyncio.run(scenario())



def test_busy_worker_is_skipped_and_optional_stages_keep_their_order(api, tmp_path, monkeypatch):  # noqa: F811
    async def scenario():
        fri, agent, _, _ = delivery_agent(api, tmp_path, monkeypatch)
        fri.capabilities |= {'reply-checks', 'member-summaries', 'summary-rechecks'}
        real = fri.api
        async def call(method, path, body=None):
            result = await real(method, path, body)
            if path == '/agent/state':
                result['config'].update(meme_enabled=True, reply_check_enabled=True, learning_enabled=True)
            return result
        fri.api = call
        order, entered, release = [], asyncio.Event(), asyncio.Event()
        def step(name):
            async def run(*args):
                order.append(name)
                if name == 'learning':
                    entered.set()
                    await release.wait()
            return run
        agent.check_presence, agent.learn = step('presence'), step('learning')
        agent.attend, agent.check_memes = step('attention'), step('memes')
        monkeypatch.setattr('fri.fri_reply_checks.check', step('reply_checks'))
        monkeypatch.setattr('fri.fri_member_summary.build', step('member_summaries'))
        monkeypatch.setattr('fri.fri_member_summary.recheck', step('summary_rechecks'))
        await agent.heartbeat.coro(agent)
        await entered.wait()
        worker = agent.background_task
        from fri.fri_watch import STALL, Watch, down
        agent.background_progress -= STALL
        try:
            for _ in range(5):
                await asyncio.wait_for(agent.heartbeat.coro(agent), 1)
                assert agent.background_task is worker
            kind, detail = Watch(fri).stalled()
            assert kind == 'stalled' and detail['loop'] == 'background'
            assert down(kind, detail, 0).startswith('my background worker’s been stuck in learning')
            assert order == ['presence', 'learning']
            release.set()
            await worker
            expected = ['presence', 'learning', 'attention', 'memes', 'reply_checks', 'member_summaries', 'summary_rechecks']
            assert order == expected
            await tick(agent)
            assert agent.background_task is not worker and order == expected*2
        finally:
            release.set()
            await stop(agent)
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['learning', 'background'])
def test_worker_errors_are_recorded_and_delivery_continues(api, tmp_path, monkeypatch, fault):  # noqa: F811
    async def scenario():
        fri, agent, social, row = delivery_agent(api, tmp_path, monkeypatch)
        agent.learn = AsyncMock(side_effect=RuntimeError('synthetic'))
        if fault == 'background':
            agent.background_pass = AsyncMock(side_effect=RuntimeError('synthetic'))
        sender = AsyncMock(return_value={'id': '987'})
        monkeypatch.setattr('fri.fri_social.send_once', sender)
        await tick(agent)
        assert agent.heartbeat_status == 'ok' and agent.background_status == 'deferred'
        assert agent.stages[fault]['error'] == 'RuntimeError'
        due(social.store, row['id'])
        await tick(agent)
        assert sender.await_count == 1 and agent.heartbeat_status == 'ok'
    asyncio.run(scenario())


def test_heartbeat_error_does_not_cancel_or_replace_a_running_worker(api, tmp_path, monkeypatch):  # noqa: F811
    async def scenario():
        fri, agent, _, _ = delivery_agent(api, tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        async def learn():
            entered.set()
            await release.wait()
        agent.learn = learn
        await agent.heartbeat.coro(agent)
        await entered.wait()
        worker = agent.background_task
        real = fri.api
        async def failed(method, path, body=None):
            if path == '/agent/state':
                raise RuntimeError('synthetic')
            return await real(method, path, body)
        fri.api = failed
        try:
            await agent.heartbeat.coro(agent)
            assert agent.heartbeat_status == 'deferred' and agent.background_task is worker
            assert not worker.done()
            release.set()
            await worker
            assert agent.background_status == 'ok'
        finally:
            release.set()
            await stop(agent)
    asyncio.run(scenario())


def test_fri_shutdown_awaits_the_cancelled_worker(tmp_path, monkeypatch):
    async def scenario():
        from compat_tests.test_fri_runtime import configured
        fri = configured(monkeypatch)
        fri.data = tmp_path
        agent = fri.agent = Agent(fri)
        agent.config['enabled'] = True
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def learn():
            entered.set()
            try:
                await asyncio.sleep(600)
            finally:
                await asyncio.sleep(0)
                stopped.set()
        agent.check_presence = agent.attend = agent.check_memes = AsyncMock()
        agent.learn = learn
        agent.kick_background()
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(fri.close(), 1)
        assert stopped.is_set() and agent.background_task.cancelled()
        assert agent.background_current is agent.background_progress is None
        assert not agent.heartbeat.is_running()
    asyncio.run(scenario())



def test_public_post_uses_the_next_serial_tick_state_while_worker_busy(api, tmp_path, monkeypatch):  # noqa: F811
    async def scenario():
        from gene.fri_workspace_store import Workspace

        from compat_tests.test_fri_workspace import authorize, prepared
        fri, agent, _, _ = delivery_agent(api, tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        async def learn():
            entered.set()
            await release.wait()
        agent.learn = learn
        await agent.heartbeat.coro(agent)
        await entered.wait()
        work = Workspace(api[0].app.state.store)
        post = prepared(work)
        authorize(work, post)
        real = fri.api
        async def switched(method, path, body=None):
            result = await real(method, path, body)
            if path == '/agent/state':
                result['config']['enabled'] = False
            return result
        fri.api = switched
        # A guild exists; disabled Fri must stop before any Discord permissions read.
        fri.get_guild = lambda _: object()
        sender = AsyncMock(return_value={'id': '987'})
        monkeypatch.setattr('fri.fri_workspace.send_once', sender)
        try:
            await asyncio.wait_for(agent.heartbeat.coro(agent), 1)
            sender.assert_not_awaited()
            assert agent.config['enabled'] is False
            assert agent.stages['posts']['error'] is None
            assert work.post(post['id'])['state'] == 'failed'  # Base's disabled-post outcome, without sending.
            assert agent.background_current['stage'] == 'learning'
        finally:
            release.set()
            await stop(agent)
    asyncio.run(scenario())


def test_transient_failure_after_claim_does_not_wait_out_the_lease(api, tmp_path, monkeypatch):  # noqa: F811
    async def scenario():
        fri, agent, social, row = delivery_agent(api, tmp_path, monkeypatch)
        entered, release, claimed, settled = (asyncio.Event() for _ in range(4))
        async def learn():
            entered.set()
            await release.wait()
        agent.learn = learn
        attempts, real = [], fri.api
        async def call(method, path, body=None):
            if path == '/chat/channels/withdraw':
                attempts.append(path)
                if len(attempts) == 1:
                    raise ControllerUnavailable('synthetic transient failure')
            result = await real(method, path, body)
            if path == '/social/reminders/claim' and result:
                claimed.set()
                agent.channel_withdrawals.withdraw(123, 2)
            if path == '/chat/channels/withdraw':
                settled.set()
            return result
        fri.api = call
        sender = AsyncMock(return_value={'id': '987'})
        monkeypatch.setattr('fri.fri_social.send_once', sender)
        agent.heartbeat.change_interval(seconds=0.01)
        if hasattr(agent, 'delivery'):  # Disprove the previous candidate with its own delivery loop.
            from fri import fri_delivery
            monkeypatch.setattr(fri_delivery, 'HOLD_WAIT_SECONDS', 0.05)
            monkeypatch.setattr(fri_delivery, 'HOLD_POLL_SECONDS', 0.01)
            agent.delivery.change_interval(seconds=0.01)
        async def expire_still_claimed():
            await claimed.wait()
            await asyncio.sleep(0.15)  # Accelerate only the pinned controller's later lease expiry.
            with social.store.connection() as db:
                db.execute("UPDATE fri_reminders SET lease_until=0 WHERE id=? AND state='claimed'", (row['id'],))
            social.reminder_claim()
        expiry = asyncio.create_task(expire_still_claimed())
        await agent.start()
        try:
            await asyncio.wait_for(entered.wait(), 1)
            due(social.store, row['id'])
            await asyncio.wait_for(claimed.wait(), 1)
            await asyncio.wait_for(settled.wait(), 0.3)
            await expiry
            assert len(attempts) == 2 and sender.await_count == 1 and not release.is_set()
            with social.store.connection() as db:
                assert db.execute('SELECT state FROM fri_reminders WHERE id=?', (row['id'],)).fetchone()[0] == 'sent'
        finally:
            await stop(agent)
            expiry.cancel()
            await asyncio.gather(expiry, return_exceptions=True)
    asyncio.run(scenario())
