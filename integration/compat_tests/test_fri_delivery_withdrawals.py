"""Confirmed delivery keeps the local holds that used to precede it in the heartbeat."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from compat_tests.test_fri_social import runtime
from compat_tests.test_fri_summary_hold_deliveries import api, depends, summary_of_20  # noqa: F401
from compat_tests.test_fri_workspace import discord_harness
from fri.fri_agent import Agent
from fri.subscription import private_json


def agent_for(fri):
    fri.workshop_id = 456
    agent = fri.agent = Agent(fri)
    agent.config['enabled'] = True
    agent.member = AsyncMock(return_value=SimpleNamespace(id=10))
    real = fri.api
    async def call(method, path, body=None):
        result = await real(method, path, body)
        if path == '/agent/state':
            result['config']['enabled'] = True
        return result
    fri.api = call
    return agent


@pytest.mark.parametrize('kind', ['reminders', 'posts'])
def test_restart_with_queued_summary_channel_withdrawal_does_not_claim(api, tmp_path, monkeypatch, kind):  # noqa: F811
    async def scenario():
        if kind == 'reminders':
            fri, store_owner, row = runtime(api, tmp_path)
            table, key = 'fri_reminder_dependencies', 'reminder_id'
        else:
            fri, store_owner, row, _ = discord_harness(api, tmp_path)
            table, key = 'fri_work_dependencies', 'work_id'
        store = store_owner.store
        summary_of_20(store, held=False)
        depends(store, table, key, row['id'] if kind == 'reminders' else row['work_id'])
        agent = agent_for(fri)
        agent.channel_withdrawals.withdraw(123, 1, send=False)
        fri.capabilities = {'channel-withdrawals'}
        # A new agent reads the persisted queue before the delayed withdrawal reaches Gene.
        agent = fri.agent = Agent(fri)
        agent.member = AsyncMock(return_value=SimpleNamespace(id=10))
        agent.config['enabled'] = True
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
        monkeypatch.setattr('fri.fri_workspace.send_once', sender)
        monkeypatch.setattr('fri.fri_workspace.capabilities', AsyncMock(return_value={'777': {'one_off_post': True}}))
        withdrawal = asyncio.create_task(agent.channel_withdrawals.send())
        try:
            await entered.wait()
            assert agent.saved_turns.closed()
            await asyncio.wait_for(agent.delivery.coro(agent), 1)
            sender.assert_not_awaited()
            table_name = 'fri_reminders' if kind == 'reminders' else 'fri_posts'
            with store.connection() as db:
                assert db.execute(f'SELECT state FROM {table_name} WHERE id=?', (row['id'],)).fetchone()[0] == (
                    'scheduled' if kind == 'reminders' else 'authorized')
            release.set()
            await withdrawal
            await asyncio.wait_for(agent.delivery.coro(agent), 1)
            sender.assert_not_awaited()
            with store.connection() as db:
                assert db.execute(f'SELECT state FROM {table_name} WHERE id=?', (row['id'],)).fetchone()[0] == 'cancelled'
        finally:
            release.set()
            await withdrawal
    asyncio.run(scenario())


@pytest.mark.parametrize('persistent', [False, True])
def test_hold_after_reminder_claim_waits_or_is_visible_in_health(api, tmp_path, monkeypatch, caplog, persistent):  # noqa: F811
    async def scenario():
        fri, social, row = runtime(api, tmp_path)
        agent = agent_for(fri)
        fri.capabilities = {'channel-withdrawals'}
        real = fri.api
        held = asyncio.Event()
        async def call(method, path, body=None):
            result = await real(method, path, body)
            if path == '/social/reminders/claim' and result:
                agent.channel_withdrawals.withdraw(123, 1, send=False)
                held.set()
            return result
        fri.api = call
        sender = AsyncMock(return_value={'id': '987'})
        monkeypatch.setattr('fri.fri_social.send_once', sender)
        if persistent:
            # Keep the production deadline named and bounded; accelerate only this timeout test.
            try:
                from fri import fri_delivery
            except ImportError:
                pass  # The candidate being disproven has no bounded wait at all.
            else:
                monkeypatch.setattr(fri_delivery, 'HOLD_WAIT_SECONDS', 0.05)
                monkeypatch.setattr(fri_delivery, 'HOLD_POLL_SECONDS', 0.01)
        tick = asyncio.create_task(agent.delivery.coro(agent))
        await held.wait()
        if not persistent:
            await asyncio.sleep(5)
            sender.assert_not_awaited()
            await agent.channel_withdrawals.send()
        await asyncio.wait_for(tick, 2)
        if persistent:
            sender.assert_not_awaited()
            assert agent.delivery_status == 'deferred'
            assert agent.delivery_hold_timeouts['reminders'] == 1
            assert agent.stages['reminders']['error'] == 'DeliveryHoldTimeout'
            assert 'delivery hold timed out' in caplog.text
            with social.store.connection() as db:
                assert db.execute('SELECT state FROM fri_reminders WHERE id=?', (row['id'],)).fetchone()[0] == 'claimed'
            assert not list((tmp_path/'reminder-receipts').glob('*.json'))
            with social.store.connection() as db:
                db.execute('UPDATE fri_reminders SET lease_until=0 WHERE id=?', (row['id'],))
            assert social.reminder_claim() is None
            with social.store.connection() as db:
                assert db.execute('SELECT state FROM fri_reminders WHERE id=?', (row['id'],)).fetchone()[0] == 'failed'
        else:
            assert sender.await_count == 1
            await agent.delivery.coro(agent)
            assert sender.await_count == 1
    asyncio.run(scenario())


def test_public_delivery_uses_fresh_off_switch_with_stale_heartbeat(api, tmp_path, monkeypatch):  # noqa: F811
    async def scenario():
        fri, work, post, _ = discord_harness(api, tmp_path)
        agent = agent_for(fri)
        real = fri.api
        enabled = True
        async def call(method, path, body=None):
            if path == '/workspace/posts/'+('f'*32)+'/finish':
                return {'state': 'sent'}  # A prior durable receipt still settles while switched off.
            result = await real(method, path, body)
            if path == '/agent/state':
                result['config']['enabled'] = enabled
            return result
        fri.api = call
        sender = AsyncMock(return_value={'id': '987'})
        monkeypatch.setattr('fri.fri_workspace.send_once', sender)
        monkeypatch.setattr('fri.fri_workspace.capabilities', AsyncMock(return_value={'777': {'one_off_post': True}}))
        entered, release = asyncio.Event(), asyncio.Event()
        async def thinking():
            entered.set()
            await release.wait()
        agent.learn = thinking
        agent.check_presence = agent.send_reports = agent.attend = agent.check_memes = AsyncMock()
        agent.channel_withdrawals.settle = AsyncMock()
        agent.memes = SimpleNamespace(expire=Mock(), verify=AsyncMock())
        tick = asyncio.create_task(agent.heartbeat.coro(agent))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            enabled = False
            private_json(tmp_path/'post-receipts'/('f'*32+'.json'), {
                'lease': 'a'*32, 'state': 'sent', 'message_id': '986'})
            await agent.delivery.coro(agent)
            assert agent.config['enabled']
            sender.assert_not_awaited()
            assert work.post(post['id'])['state'] == 'authorized'
            assert not list((tmp_path/'post-receipts').glob('*.json'))
        finally:
            tick.cancel()
            await asyncio.gather(tick, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['reminders', 'posts'])
@pytest.mark.parametrize('phase', ['discord', 'sending', 'generation'])
@pytest.mark.parametrize('withdrawn', [False, True])
def test_late_withdrawals_repeat_all_send_fences(api, tmp_path, monkeypatch, kind, phase, withdrawn):  # noqa: F811
    async def scenario():
        from fri import fri_delivery
        monkeypatch.setattr(fri_delivery, 'HOLD_POLL_SECONDS', 0.01)
        if kind == 'reminders':
            fri, owner, row = runtime(api, tmp_path)
        else:
            fri, owner, row, _ = discord_harness(api, tmp_path)
        agent = agent_for(fri)
        fri.capabilities = {'channel-withdrawals'}
        if withdrawn:
            summary_of_20(owner.store, held=False)
            depends(owner.store, 'fri_reminder_dependencies' if kind == 'reminders' else 'fri_work_dependencies',
                    'reminder_id' if kind == 'reminders' else 'work_id',
                    row['id'] if kind == 'reminders' else row['work_id'])
        fired, claimed, settling = False, False, None

        async def withdraw():
            nonlocal fired, settling
            if fired:
                return
            fired = True
            agent.channel_withdrawals.withdraw(123, 1, send=False)
            async def settle():
                await asyncio.sleep(0.03)
                await agent.channel_withdrawals.send()
            if phase == 'generation':
                await agent.channel_withdrawals.send()  # Already clear by the time the controller read returns.
            else:
                settling = asyncio.create_task(settle())

        real = fri.api
        async def call(method, path, body=None):
            nonlocal claimed
            result = await real(method, path, body)
            if path.endswith('/claim') and result:
                claimed = True
            if claimed and ((phase == 'sending' and path.endswith('/sending')) or
                            (phase == 'generation' and path == '/agent/state')):
                await withdraw()
            return result
        fri.api = call
        fetch = fri.fetch_channel
        async def channel(cid):
            result = await fetch(cid)
            if phase == 'discord':
                await withdraw()
            return result
        fri.fetch_channel = AsyncMock(side_effect=channel)
        sender = AsyncMock(return_value={'id': '987'})
        monkeypatch.setattr('fri.fri_social.send_once', sender)
        monkeypatch.setattr('fri.fri_workspace.send_once', sender)
        permissions = AsyncMock(return_value={'777': {'one_off_post': True}})
        monkeypatch.setattr('fri.fri_workspace.capabilities', permissions)
        try:
            await asyncio.wait_for(agent.delivery.coro(agent), 2)
            assert fired
            if withdrawn:
                sender.assert_not_awaited()
                with owner.store.connection() as db:
                    table = 'fri_reminders' if kind == 'reminders' else 'fri_posts'
                    assert db.execute(f'SELECT state FROM {table} WHERE id=?', (row['id'],)).fetchone()[0] == (
                        'uncertain' if phase == 'sending' else 'cancelled')
                    assert db.execute(f'SELECT body FROM {table} WHERE id=?', (row['id'],)).fetchone()[0] == ''
            else:
                assert sender.await_count == 1
                if phase != 'generation':
                    assert (agent.member.await_count if kind == 'reminders' else permissions.await_count) >= 2
                await agent.delivery.coro(agent)
                assert sender.await_count == 1
        finally:
            if settling is not None:
                await settling
    asyncio.run(scenario())


@pytest.mark.parametrize('hold', ['record', 'generation'])
def test_preclaim_record_hold_or_generation_move_skips_claim(api, tmp_path, monkeypatch, hold):  # noqa: F811
    async def scenario():
        import time
        fri, _, _ = runtime(api, tmp_path)
        agent = agent_for(fri)
        if hold == 'record':
            agent.saved_turns.pending  # Load before setting the record's fail-closed deadline.
            agent.saved_turns.closed_until = time.time()+3600
            assert not agent.channel_withdrawals.holding() and agent.saved_turns.closed()
        real = fri.api
        claims = []
        async def call(method, path, body=None):
            result = await real(method, path, body)
            if path == '/agent/state' and hold == 'generation':
                agent.channel_withdrawals.taken([], channels=['1'])
            if path.endswith('/claim'):
                claims.append(path)
            return result
        fri.api = call
        sender = AsyncMock(return_value={'id': '987'})
        monkeypatch.setattr('fri.fri_social.send_once', sender)
        await asyncio.wait_for(agent.delivery.coro(agent), 1)
        assert not claims
        sender.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['reminders', 'posts'])
def test_switch_off_during_discord_fence_is_checked_again_before_send(api, tmp_path, monkeypatch, kind):  # noqa: F811
    async def scenario():
        if kind == 'reminders':
            fri, _, _ = runtime(api, tmp_path)
        else:
            fri, _, _, _ = discord_harness(api, tmp_path)
        agent = agent_for(fri)
        real = fri.api
        enabled = True
        async def call(method, path, body=None):
            result = await real(method, path, body)
            if path == '/agent/state':
                result['config']['enabled'] = enabled
            return result
        fri.api = call
        fetch = fri.fetch_channel
        async def channel(cid):
            nonlocal enabled
            enabled = False
            return await fetch(cid)
        fri.fetch_channel = AsyncMock(side_effect=channel)
        sender = AsyncMock(return_value={'id': '987'})
        monkeypatch.setattr('fri.fri_social.send_once', sender)
        monkeypatch.setattr('fri.fri_workspace.send_once', sender)
        monkeypatch.setattr('fri.fri_workspace.capabilities', AsyncMock(return_value={'777': {'one_off_post': True}}))
        await agent.delivery.coro(agent)
        assert agent.config['enabled']
        sender.assert_not_awaited()
    asyncio.run(scenario())


def test_production_hold_budget_stays_inside_the_claim_lease():
    from fri.fri_delivery import HOLD_POLL_SECONDS, HOLD_WAIT_SECONDS
    assert HOLD_WAIT_SECONDS == 90 and 1 <= HOLD_POLL_SECONDS <= 2



def test_post_confirmation_button_uses_the_same_withdrawal_hold(api, tmp_path, monkeypatch):  # noqa: F811
    async def scenario():
        import discord

        from compat_tests.test_fri_workspace import source
        from fri.fri_workspace import PostButton
        fri, work, post, _ = discord_harness(api, tmp_path)
        agent = agent_for(fri)
        fri.capabilities = {'channel-withdrawals'}
        agent.channel_withdrawals.withdraw(123, 1, send=False)
        sender = AsyncMock(return_value={'id': '987'})
        monkeypatch.setattr('fri.fri_workspace.send_once', sender)
        monkeypatch.setattr('fri.fri_workspace.capabilities', AsyncMock(return_value={'777': {'one_off_post': True}}))
        button = PostButton(post['id'], 'send')
        interaction = Mock(spec=discord.Interaction, client=fri, id=int(source(5).id), guild_id=None, channel_id=999,
            user=SimpleNamespace(id=10, display_name='Owner', bot=False),
            message=SimpleNamespace(id=888, author=fri.user), data={'custom_id': button.item.custom_id},
            response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock(),
            followup=SimpleNamespace(send=AsyncMock()))
        await button.callback(interaction)
        sender.assert_not_awaited()
        assert work.post(post['id'])['state'] == 'authorized'
        await agent.channel_withdrawals.send()
        await agent.delivery.coro(agent)
        assert sender.await_count == 1
    asyncio.run(scenario())



def test_an_ordinary_io_timeout_is_not_counted_as_a_hold_timeout(api, tmp_path, monkeypatch):  # noqa: F811
    async def scenario():
        fri, social, row = runtime(api, tmp_path)
        agent = agent_for(fri)
        agent.member = AsyncMock(side_effect=TimeoutError())
        sender = AsyncMock(return_value={'id': '987'})
        monkeypatch.setattr('fri.fri_social.send_once', sender)
        await agent.delivery.coro(agent)
        sender.assert_not_awaited()
        assert agent.delivery_hold_timeouts == {'posts': 0, 'reminders': 0}
        with social.store.connection() as db:
            assert db.execute('SELECT state FROM fri_reminders WHERE id=?', (row['id'],)).fetchone()[0] == 'failed'
    asyncio.run(scenario())
