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


def test_reply_checks_run_as_their_own_stage(heartbeat, monkeypatch):
    agent, check = heartbeat.agent, AsyncMock(side_effect=RuntimeError('unexpected'))
    monkeypatch.setattr('fri.fri_reply_checks.check', check)
    heartbeat.fri.capabilities = frozenset({'reply-checks'})
    monkeypatch.setitem(STATE, 'config', {**STATE['config'], 'reply_check_enabled': True})
    agent.check_memes.side_effect = KeyError('meme')
    asyncio.run(agent.heartbeat.coro(agent))
    check.assert_awaited_once_with(agent)  # A broken meme check no longer skips it.
    assert agent.stages['reply_checks']['error'] == 'RuntimeError' and agent.stages['memes']['error'] == 'KeyError'
    agent.check_memes.side_effect = check.side_effect = None
    asyncio.run(agent.heartbeat.coro(agent))
    assert agent.heartbeat_status == 'ok' and agent.stages['reply_checks']['error'] is None
    heartbeat.fri.capabilities = frozenset()  # The controller refused reply checks or predates them.
    asyncio.run(agent.heartbeat.coro(agent))
    assert check.await_count == 2 and 'reply_checks' not in agent.stages


def test_a_controller_without_reply_check_settings_still_runs_every_stage(heartbeat, monkeypatch):
    agent = heartbeat.agent
    check = AsyncMock()
    monkeypatch.setattr('fri.fri_reply_checks.check', check)
    heartbeat.fri.capabilities = frozenset({'reply-checks'})
    config = {k: v for k, v in STATE['config'].items() if not k.startswith('reply_check')}
    monkeypatch.setitem(STATE, 'config', config)
    asyncio.run(agent.heartbeat.coro(agent))
    check.assert_not_awaited()
    assert agent.heartbeat_status == 'ok' and 'reply_checks' not in agent.stages
    agent.check_memes.assert_awaited_once()


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
    fri = SimpleNamespace(owner_id=10, subscription_noticed=False, sign_in_notice=None, get_user=lambda _: owner,
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


def test_sign_in_dm_is_never_repeated_after_a_send_but_a_failed_lookup_retries():
    async def scenario():
        fri, owner = notice_runtime(rejected=True)
        # Discord may have accepted it before the response was lost; a retry would carry a new nonce.
        owner.send.side_effect = TimeoutError()
        for _ in range(3):
            await Fri.subscription_notice(fri)
        owner.send.assert_awaited_once()
        fri.chat.subscription.rejected = False
        await Fri.subscription_notice(fri)
        fri.chat.subscription.rejected = True
        fri.get_user = lambda _: None
        fri.fetch_user = AsyncMock(side_effect=[TimeoutError(), owner])
        await Fri.subscription_notice(fri)
        assert owner.send.await_count == 1 and not fri.subscription_noticed  # Nothing was sent yet.
        owner.send.side_effect = None
        await Fri.subscription_notice(fri)
        assert owner.send.await_count == 2 and fri.subscription_noticed
    asyncio.run(scenario())


def test_sign_in_notice_runs_beside_reviews_and_usage(monkeypatch):
    async def scenario():
        fri, owner = notice_runtime(rejected=True)
        fri.subscription_notice = lambda: Fri.subscription_notice(fri)
        fri.flush_usage = AsyncMock()
        notify = AsyncMock()
        monkeypatch.setattr('fri.fri.notify_owner', notify)
        # discord.py sleeps out a rate limit's retry_after inside send(); later ticks must not wait for it.
        release = asyncio.Event()

        async def rate_limited(*args, **kwargs):
            await release.wait()
        owner.send.side_effect = rate_limited
        for _ in range(3):
            await Fri.owner_reviews.coro(fri)
            await asyncio.sleep(0)
        assert fri.flush_usage.await_count == notify.await_count == 3
        assert owner.send.await_count == 1 and not fri.sign_in_notice.done()  # One send in flight, not one per tick.
        release.set()
        await fri.sign_in_notice
        assert fri.subscription_noticed
        fri.flush_usage.side_effect = OSError('read-only usage folder')
        await Fri.owner_reviews.coro(fri)  # Logged and retried next tick instead of ending the loop.
        assert notify.await_count == 4  # A stuck usage folder doesn't hold back owner reviews either.
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
