"""Serial checkpoint latency, controller traffic, and #405's withdrawal ordering failures."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from compat_tests.test_fri_reply_checks import controller  # noqa: F401
from compat_tests.test_fri_social import due, runtime
from compat_tests.test_fri_summary_hold_deliveries import api, depends, summary_of_20  # noqa: F401
from fri.fri_agent import Agent
from fri.fri_errors import ControllerUnavailable


def setup(paired, tmp_path, monkeypatch):
    fri, social, row = runtime(paired, tmp_path)
    with social.store.connection() as db:
        db.execute('UPDATE fri_reminders SET due=due+86400 WHERE id=?', (row['id'],))
    fri.owner_id, fri.workshop_id = 10, 456
    fri.get_guild = lambda _: None
    fri.capabilities = {'channel-withdrawals', 'reply-checks', 'member-summaries', 'summary-rechecks'}
    agent = fri.agent = Agent(fri)
    agent.member = AsyncMock(return_value=SimpleNamespace(id=10))
    agent.check_presence = AsyncMock()
    agent.learn, agent.attend, agent.check_memes, agent.send_reports = (AsyncMock() for _ in range(4))
    agent.memes = SimpleNamespace(expire=Mock(), verify=AsyncMock())
    for module, method in [('fri_fixes', 'check'), ('fri_fixes', 'settle'), ('fri_reply_checks', 'check'),
                           ('fri_member_summary', 'build'), ('fri_member_summary', 'recheck')]:
        monkeypatch.setattr(f'fri.{module}.{method}', AsyncMock())
    calls, clock = [], [1000.0]
    real = fri.api

    async def call(method, path, body=None):
        calls.append(path)
        result = await real(method, path, body)
        if path == '/agent/state':
            result['config'].update(enabled=True, learning_enabled=True, reply_check_enabled=True, meme_enabled=True)
        return result
    fri.api = call
    monkeypatch.setattr('fri.fri_agent.time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    sender = AsyncMock(return_value={'id': '987'})
    monkeypatch.setattr('fri.fri_social.send_once', sender)
    return fri, agent, social, row, calls, clock, sender


@pytest.mark.parametrize('slow', ['learn', 'attend', 'check_memes', 'fixes', 'reply_checks', 'summaries', 'rechecks'])
def test_due_during_slow_stage_delivers_at_next_checkpoint_once(api, tmp_path, monkeypatch, slow):  # noqa: F811
    async def scenario():
        fri, agent, social, row, calls, clock, sender = setup(api, tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()

        async def model():
            entered.set()
            await release.wait()
            clock[0] += 600  # Virtual elapsed time; don't wait ten real minutes.

        if slow in ('learn', 'attend', 'check_memes'):
            setattr(agent, slow, model)
        else:
            module, method = {'fixes': ('fri_fixes', 'check'), 'reply_checks': ('fri_reply_checks', 'check'),
                              'summaries': ('fri_member_summary', 'build'),
                              'rechecks': ('fri_member_summary', 'recheck')}[slow]
            monkeypatch.setattr(f'fri.{module}.{method}', lambda _: model())
        stage = agent.stage
        finished = False

        async def observe(name, step, *switches, **kwargs):
            nonlocal finished
            if finished and name not in ('channel_withdrawals', 'posts', 'reminders'):
                sender.assert_awaited_once()  # Before the next optional stage, not just by tick end.
            result = await stage(name, step, *switches, **kwargs)
            if entered.is_set() and release.is_set():
                finished = True
            return result
        agent.stage = observe
        tick = asyncio.create_task(agent.heartbeat.coro(agent))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            due(social.store, row['id'])
            sender.assert_not_awaited()  # No delivery task runs beside the optional stage.
            release.set()
            await asyncio.wait_for(tick, 2)
            sender.assert_awaited_once()
            assert calls.count('/social/reminders/claim') == 2
            finished = False
            await agent.heartbeat.coro(agent)
            sender.assert_awaited_once()
        finally:
            release.set()
            await tick
    asyncio.run(scenario())


def test_idle_poll_count_and_thirty_second_bound(api, tmp_path, monkeypatch):  # noqa: F811
    async def scenario():
        _, agent, _, _, calls, clock, _ = setup(api, tmp_path, monkeypatch)
        await agent.heartbeat.coro(agent)
        assert calls == ['/agent/state', '/workspace/outbox/claim', '/social/reminders/claim']
        calls.clear()

        async def short():
            clock[0] += 10
        agent.learn = agent.attend = agent.check_memes = short
        await agent.heartbeat.coro(agent)
        assert calls.count('/social/reminders/claim') == calls.count('/workspace/outbox/claim') == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('withdrawn', [False, True])
def test_settlement_finishes_before_claim_or_next_model_and_retry_has_no_open_lease(
        api, tmp_path, monkeypatch, withdrawn):  # noqa: F811
    """#405 R1/R2/R3: no summary send, expiring claim, or reader during settlement.

    A transient withdrawal fails during learning, then the checkpoint retries it. The
    retry is blocked to expose any delivery or next reader running alongside it.
    """
    async def scenario():
        fri, agent, social, row, calls, clock, sender = setup(api, tmp_path, monkeypatch)
        if withdrawn:
            summary_of_20(social.store, held=False)
            depends(social.store, 'fri_reminder_dependencies', 'reminder_id', row['id'])
        settling, release = asyncio.Event(), asyncio.Event()
        real, attempts = fri.api, []

        async def call(method, path, body=None):
            if path == '/chat/channels/withdraw':
                attempts.append(path)
                if len(attempts) == 1:
                    raise ControllerUnavailable('synthetic transient failure')
                settling.set()
                await release.wait()
            return await real(method, path, body)
        fri.api = call

        async def learn():
            due(social.store, row['id'])
            agent.channel_withdrawals.withdraw(123, 1 if withdrawn else 2, send=False)
            await agent.channel_withdrawals.send()  # First attempt fails.
            clock[0] += 600
        agent.learn = learn

        async def reader():
            assert not agent.channel_withdrawals.holding()
            assert release.is_set()
            if withdrawn:
                with social.store.connection() as db:
                    assert not db.execute("SELECT 1 FROM fri_member_summaries WHERE user_id='20'").fetchall()
        agent.attend = reader
        monkeypatch.setattr('fri.fri_reply_checks.check', lambda _: reader())
        tick = asyncio.create_task(agent.heartbeat.coro(agent))
        try:
            await asyncio.wait_for(settling.wait(), 2)
            sender.assert_not_awaited()
            assert calls.count('/social/reminders/claim') == 1  # Only the initial, not-yet-due poll.
            with social.store.connection() as db:
                assert db.execute('SELECT state FROM fri_reminders WHERE id=?', (row['id'],)).fetchone()[0] == 'scheduled'
                # The R2 lease-expiry repro: no claim exists to expire while settlement waits.
                db.execute("UPDATE fri_reminders SET lease_until=0 WHERE id=? AND state='claimed'", (row['id'],))
            release.set()
            await asyncio.wait_for(tick, 2)
            assert len(attempts) == 2
            assert sender.await_count == (0 if withdrawn else 1)
            assert agent.heartbeat_status == 'ok'
            with social.store.connection() as db:
                assert db.execute('SELECT state FROM fri_reminders WHERE id=?', (row['id'],)).fetchone()[0] == (
                    'cancelled' if withdrawn else 'sent')
        finally:
            release.set()
            await tick
    asyncio.run(scenario())


def test_reply_check_claim_reads_after_channel_settlement(controller, tmp_path, monkeypatch):  # noqa: F811
    """#405 R3: the actual reply-check model receives none of the withdrawn pair."""
    from compat_tests.test_api import headers
    from compat_tests.test_fri_reply_checks import save
    from fri.fri_reply_checks import check

    async def scenario():
        store, client = controller
        save(store, 'withdrawn checkpoint marker', 'old answer', ago=90)
        save(store, 'that answer was wrong', 'correction')

        async def call(method, path, body=None):
            response = client.request(method, path, json=body, headers=headers('fri'))
            response.raise_for_status()
            return response.json()
        fri, agent, _, _, calls, clock, _ = setup((client, call), tmp_path, monkeypatch)
        fri.chat = SimpleNamespace(headers=AsyncMock(return_value={'Authorization': 'Bearer test'}))
        model = AsyncMock(side_effect=AssertionError('withdrawn words reached the model'))
        monkeypatch.setattr('fri.fri_reply_checks.analyze', model)
        monkeypatch.setattr('fri.fri_reply_checks.check', check)

        async def learn():
            agent.channel_withdrawals.withdraw(123, 1, send=False)
            clock[0] += 600
        agent.learn = learn
        await agent.heartbeat.coro(agent)
        assert agent.heartbeat_status == 'ok'
        assert calls.index('/chat/channels/withdraw') < calls.index('/agent/reply-checks/claim')
        model.assert_not_awaited()
    asyncio.run(scenario())
