"""Background work fails one piece at a time: heartbeat stages, the owner sign-in notice, usage receipts."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from compat_tests.test_api import actor, headers
from compat_tests.test_split_contract import Session
from fri.fri import SIGN_IN_NOTICE, Fri
from fri.fri_agent import Agent
from fri.fri_agent_settings import DEFAULTS
from fri.fri_errors import ControllerRejected

STATE = {'config': {**DEFAULTS, 'enabled': True, 'meme_enabled': True}, 'version': 1,
         'channels': [], 'history_channels': []}


@pytest.fixture
def heartbeat(monkeypatch):
    monkeypatch.setattr('fri.fri_workspace.deliver_one', AsyncMock())
    monkeypatch.setattr('fri.fri_social.deliver_one', AsyncMock())
    owner = SimpleNamespace(send=AsyncMock())
    reports = []

    async def api(method, path, payload=None):
        return {'/agent/state': STATE, '/agent/reports': reports}[path]

    fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=AsyncMock(side_effect=api),
                          get_user=lambda _: owner, wait_until_ready=AsyncMock())
    agent = Agent(fri)
    agent.learn, agent.attend, agent.check_memes = AsyncMock(), AsyncMock(), AsyncMock()
    return SimpleNamespace(agent=agent, fri=fri, owner=owner, reports=reports)


def test_undeliverable_report_card_no_longer_silences_attention_or_memes(heartbeat):
    # A Gene/Fri skew in the report card raises KeyError on every tick; it used to skip everything after it.
    heartbeat.reports.append({'id': 'a'*32, 'body': 'Learning pass', 'card': {'historical_notes': 1}})
    agent = heartbeat.agent
    asyncio.run(agent.heartbeat.coro(agent))
    heartbeat.owner.send.assert_not_awaited()
    agent.learn.assert_awaited_once()
    agent.attend.assert_awaited_once()
    agent.check_memes.assert_awaited_once()
    assert agent.heartbeat_status == 'deferred'
    assert agent.stages['reports']['error'] == 'KeyError' and agent.stages['reports']['last_ok'] is None
    assert all(agent.stages[name]['error'] is None for name in ('posts', 'reminders', 'learning', 'attention', 'memes'))
    assert ('POST', '/agent/reports/'+'a'*32+'/delivered') not in [c.args[:2] for c in heartbeat.fri.api.await_args_list]


def test_meme_failure_is_recorded_and_the_next_tick_recovers(heartbeat):
    agent = heartbeat.agent
    agent.check_memes.side_effect = RuntimeError('unexpected')
    asyncio.run(agent.heartbeat.coro(agent))
    assert agent.heartbeat_status == 'deferred' and agent.stages['memes']['error'] == 'RuntimeError'
    agent.attend.assert_awaited_once()
    failed_at = agent.stages['memes']['at']
    agent.check_memes.side_effect = None
    asyncio.run(agent.heartbeat.coro(agent))
    assert agent.heartbeat_status == 'ok' and agent.stages['memes']['error'] is None
    assert agent.stages['memes']['last_ok'] >= failed_at


def test_disabled_stages_do_not_run_or_report(heartbeat, monkeypatch):
    agent = heartbeat.agent
    asyncio.run(agent.heartbeat.coro(agent))
    assert 'memes' in agent.stages
    monkeypatch.setitem(STATE, 'config', {**STATE['config'], 'meme_enabled': False})
    asyncio.run(agent.heartbeat.coro(agent))
    agent.check_memes.assert_awaited_once()
    assert 'memes' not in agent.stages and agent.heartbeat_status == 'ok'


@pytest.mark.parametrize('stage', ['state', 'memes'])
def test_unexpected_errors_never_end_the_heartbeat_loop(heartbeat, stage):
    agent = heartbeat.agent
    error = RuntimeError('not in the old exception list')
    if stage == 'state':
        heartbeat.fri.api.side_effect = error
    else:
        agent.check_memes.side_effect = error

    async def ticks():
        while agent.heartbeat.current_loop < 3 and not agent.heartbeat.failed():
            await asyncio.sleep(0.01)

    async def scenario():
        agent.heartbeat.change_interval(seconds=0.01)
        agent.heartbeat.start()
        await asyncio.wait_for(ticks(), 5)
        assert agent.heartbeat.is_running() and not agent.heartbeat.failed()
        agent.heartbeat.cancel()
    asyncio.run(scenario())
    assert agent.heartbeat_status == 'deferred'
    assert (agent.stages == {}) if stage == 'state' else agent.stages['memes']['error'] == 'RuntimeError'


def test_heartbeat_cancellation_still_stops_a_stage():
    async def scenario():
        agent = Agent(SimpleNamespace(api=AsyncMock(return_value=STATE)))
        agent.check_presence = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await agent.heartbeat.coro(agent)
        assert agent.heartbeat_finished
    asyncio.run(scenario())


def notice_runtime(rejected=False):
    owner = SimpleNamespace(send=AsyncMock())
    fri = SimpleNamespace(owner_id=10, subscription_noticed=False, get_user=lambda _: owner,
                          chat=SimpleNamespace(subscription=SimpleNamespace(rejected=rejected)))
    return fri, owner


def test_rejected_sign_in_sends_one_owner_dm_until_a_token_works_again():
    async def scenario():
        fri, owner = notice_runtime()
        await Fri.subscription_notice(fri)
        owner.send.assert_not_awaited()
        fri.chat.subscription.rejected = True
        for _ in range(3):
            await Fri.subscription_notice(fri)
        owner.send.assert_awaited_once()
        assert owner.send.await_args.args == (SIGN_IN_NOTICE,)
        fri.chat.subscription.rejected = False
        await Fri.subscription_notice(fri)
        fri.chat.subscription.rejected = True
        await Fri.subscription_notice(fri)
        assert owner.send.await_count == 2
    asyncio.run(scenario())


def test_failed_sign_in_notice_retries_and_never_blocks_reviews_or_usage(monkeypatch):
    async def scenario():
        fri, owner = notice_runtime(rejected=True)
        owner.send.side_effect = RuntimeError('DMs closed')
        fri.subscription_notice = lambda: Fri.subscription_notice(fri)
        fri.flush_usage = AsyncMock()
        notify = AsyncMock()
        monkeypatch.setattr('fri.fri.notify_owner', notify)
        await Fri.owner_reviews.coro(fri)
        fri.flush_usage.assert_awaited_once()
        notify.assert_awaited_once_with(fri)
        assert not fri.subscription_noticed
        owner.send.side_effect = None
        await Fri.owner_reviews.coro(fri)
        assert fri.subscription_noticed and owner.send.await_count == 2
        fri.flush_usage.side_effect = OSError('read-only usage folder')
        await Fri.owner_reviews.coro(fri)  # Logged and retried next tick instead of ending the loop.
    asyncio.run(scenario())


def receipt(path, model, **changes):
    attempt = path.stem
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'attempt_id': attempt, 'model': model, 'effort': 'low', 'status': 'completed',
        'counters': {'input_tokens': 10, 'output_tokens': 2}, 'role': 'attention', **changes}))


def usage_runtime(client, tmp_path, token='fri-'+'x'*32):
    fri = SimpleNamespace(url='http://controller', token=token, session=Session(client), data=tmp_path,
                          usage_lock=asyncio.Lock())
    fri.api = lambda method, path, payload=None: Fri.api(fri, method, path, payload)
    fri.set_aside = lambda path, cause: Fri.set_aside(fri, path, cause)
    return fri


def test_refused_usage_receipts_are_set_aside_and_later_ones_still_deliver(client, tmp_path):
    usage = tmp_path/'usage'
    receipt(usage/('1'*32+'.json'), 'gpt-6-luna')
    receipt(usage/('2'*32+'.json'), 'gpt-6-luna', role='not-a-role')  # 422: a newer Fri role.
    receipt(usage/('3'*32+'.json'), 'gpt-6-luna', counters={'input_tokens': -1})  # 400: a bad counter.
    (usage/('4'*32+'.json')).write_text('{"truncated')
    receipt(usage/('5'*32+'.json'), 'gpt-6.1-sol')
    asyncio.run(Fri.flush_usage(usage_runtime(client, tmp_path)))
    assert not list(usage.glob('*.json'))
    assert sorted(p.name for p in (usage/'rejected').glob('*.json')) == [c*32+'.json' for c in '234']
    assert (usage/'rejected').stat().st_mode & 0o777 == 0o700
    totals = client.post('/chat/usage', json=actor(), headers=headers('fri')).json()
    assert {row['model']: row['attempts'] for row in totals} == {'gpt-6-luna': 1, 'gpt-6.1-sol': 1}


def test_client_level_refusals_keep_every_receipt_queued(client, tmp_path):
    usage = tmp_path/'usage'
    receipt(usage/('1'*32+'.json'), 'gpt-6-luna')
    receipt(usage/('2'*32+'.json'), 'gpt-6-luna')
    with pytest.raises(ControllerRejected) as refused:
        asyncio.run(Fri.flush_usage(usage_runtime(client, tmp_path, token='fri-'+'y'*32)))
    assert refused.value.status == 401
    assert len(list(usage.glob('*.json'))) == 2 and not (usage/'rejected').exists()
