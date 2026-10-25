"""Background work fails one piece at a time: heartbeat stages, the owner sign-in notice, usage receipts."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest

from compat_tests.test_api import actor, headers
from compat_tests.test_split_contract import Session
from fri import feedback as commands
from fri.fri import SIGN_IN_NOTICE, Fri
from fri.fri_agent import Agent
from fri.fri_agent_settings import DEFAULTS
from fri.fri_errors import ControllerRejected
from fri.fri_presence import Presence
from fri.subscription import PLAN_SCOPE, Subscription, SubscriptionError, private_json

STATE = {'config': {**DEFAULTS, 'enabled': True, 'meme_enabled': True}, 'version': 1,
         'channels': [], 'history_channels': []}


@pytest.fixture
def heartbeat(monkeypatch):
    monkeypatch.setattr('fri.fri_workspace.deliver_one', AsyncMock())
    monkeypatch.setattr('fri.fri_social.deliver_one', AsyncMock())
    monkeypatch.setattr('fri.fri_fixes.check', AsyncMock())
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
    assert all(agent.stages[name]['error'] is None for name in ('posts', 'reminders', 'learning', 'attention', 'fixes', 'memes'))
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


def test_a_controller_without_a_setting_treats_it_as_off(heartbeat, monkeypatch):
    agent = heartbeat.agent
    check = AsyncMock()
    monkeypatch.setattr('fri.fri_reply_checks.check', check)
    heartbeat.fri.capabilities = frozenset({'reply-checks'})
    config = {k: v for k, v in STATE['config'].items() if not k.startswith('reply_check') and k != 'status_enabled'}
    monkeypatch.setitem(STATE, 'config', config)
    asyncio.run(agent.heartbeat.coro(agent))
    check.assert_not_awaited()
    assert agent.heartbeat_status == 'ok' and 'reply_checks' not in agent.stages and agent.presence is None
    assert agent.stages['presence']['error'] is None
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


class Owner:
    """The owner's discord.User: send() opens the DM channel first, so failures can land before the message."""

    def __init__(self):
        self.dm = SimpleNamespace(send=AsyncMock())
        self.create_dm = AsyncMock(return_value=self.dm)


def notice_runtime(tmp_path, rejected=False):
    owner = Owner()
    subscription = Subscription(tmp_path/'session.json')
    fri = SimpleNamespace(owner_id=10, noticed_refusal=None, sign_in_notice=None, get_user=lambda _: owner,
                          chat=SimpleNamespace(subscription=subscription))
    if rejected:
        refuse(subscription)
    return fri, owner


def refuse(subscription):
    """A real refusal: the saved sign-in is missing."""
    (subscription.path).unlink(missing_ok=True)
    with pytest.raises(SubscriptionError, match='needs a ChatGPT'):
        subscription.access_token()


def work(subscription):
    private_json(subscription.path, {'access_token': 'private', 'scopes': [PLAN_SCOPE], 'expires_at': time.time() + 3600})
    assert subscription.access_token() == 'private'


def test_rejected_sign_in_sends_one_owner_dm_until_a_token_works_again(tmp_path):
    async def scenario():
        fri, owner = notice_runtime(tmp_path)
        work(fri.chat.subscription)
        await Fri.subscription_notice(fri)
        owner.create_dm.assert_not_awaited()
        refuse(fri.chat.subscription)
        for _ in range(3):
            await Fri.subscription_notice(fri)
        owner.dm.send.assert_awaited_once()
        assert owner.dm.send.await_args.args == (SIGN_IN_NOTICE,)
        work(fri.chat.subscription)
        await Fri.subscription_notice(fri)
        refuse(fri.chat.subscription)
        await Fri.subscription_notice(fri)
        assert owner.dm.send.await_count == 2
        # A token that worked and a new refusal, both between two ticks, still earn a DM.
        work(fri.chat.subscription)
        refuse(fri.chat.subscription)
        await Fri.subscription_notice(fri)
        assert owner.dm.send.await_count == 3
        refuse(fri.chat.subscription)  # The same refusal again isn't a new one.
        await Fri.subscription_notice(fri)
        assert owner.dm.send.await_count == 3
    asyncio.run(scenario())


def test_sign_in_dm_is_never_repeated_after_a_send_but_failures_before_it_retry(tmp_path):
    async def scenario():
        fri, owner = notice_runtime(tmp_path, rejected=True)
        # Discord may have accepted it before the response was lost; a retry would carry a new nonce.
        owner.dm.send.side_effect = TimeoutError()
        for _ in range(3):
            await Fri.subscription_notice(fri)
        owner.dm.send.assert_awaited_once()
        work(fri.chat.subscription)
        refuse(fri.chat.subscription)
        owner.dm.send.side_effect = None
        fri.get_user = lambda _: None
        fri.fetch_user = AsyncMock(side_effect=[TimeoutError(), owner, owner])
        owner.create_dm.side_effect = [TimeoutError(), owner.dm]  # Opening the DM channel failed: nothing was sent.
        await Fri.subscription_notice(fri)
        await Fri.subscription_notice(fri)
        assert owner.dm.send.await_count == 1
        await Fri.subscription_notice(fri)
        assert owner.dm.send.await_count == 2 and owner.create_dm.await_count == 3
        await Fri.subscription_notice(fri)
        assert owner.dm.send.await_count == 2
    asyncio.run(scenario())


def test_sign_in_notice_runs_beside_reviews_and_usage(monkeypatch, tmp_path):
    async def scenario():
        fri, owner = notice_runtime(tmp_path, rejected=True)
        fri.subscription_notice = lambda: Fri.subscription_notice(fri)
        fri.flush_usage = AsyncMock()
        notify = AsyncMock()
        monkeypatch.setattr('fri.fri.notify_owner', notify)
        # discord.py sleeps out a rate limit's retry_after inside send(); later ticks must not wait for it.
        release = asyncio.Event()

        async def rate_limited(*args, **kwargs):
            await release.wait()
        owner.dm.send.side_effect = rate_limited
        for _ in range(3):
            await Fri.owner_reviews.coro(fri)
            await asyncio.sleep(0)
        assert fri.flush_usage.await_count == notify.await_count == 3
        assert owner.dm.send.await_count == 1 and not fri.sign_in_notice.done()  # One send in flight, not one per tick.
        release.set()
        await fri.sign_in_notice
        await Fri.owner_reviews.coro(fri)
        await fri.sign_in_notice
        assert owner.dm.send.await_count == 1
        fri.flush_usage.side_effect = OSError('read-only usage folder')
        await Fri.owner_reviews.coro(fri)  # Logged and retried next tick instead of ending the loop.
        assert notify.await_count == 5  # A stuck usage folder doesn't hold back owner reviews either.
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
    receipt(usage/('1'*32+'.json'), 'gpt-6-luna', purpose='reply_check')
    (usage/('0'*32+'.json')).write_text('['*1100 + '0' + ']'*1100)  # Parsing it raises RecursionError, not ValueError.
    receipt(usage/('2'*32+'.json'), 'gpt-6-luna', role='not-a-role')  # 422: a newer Fri role.
    receipt(usage/('3'*32+'.json'), 'gpt-6-luna', counters={'input_tokens': -1})  # 400: a bad counter.
    (usage/('4'*32+'.json')).write_text('{"truncated')
    receipt(usage/('5'*32+'.json'), 'gpt-6.1-sol')
    asyncio.run(Fri.flush_usage(usage_runtime(client, tmp_path)))
    assert not list(usage.glob('*.json'))
    assert sorted(p.name for p in (usage/'rejected').glob('*.json')) == [c*32+'.json' for c in '0234']
    assert (usage/'rejected').stat().st_mode & 0o777 == 0o700
    totals = client.post('/chat/usage', json=actor(), headers=headers('fri')).json()
    assert {row['model']: row['attempts'] for row in totals} == {'gpt-6-luna': 1, 'gpt-6.1-sol': 1}


class Unreadable:
    """A controller (or something in front of it) answering every request with an unreadable body."""

    def __init__(self, status, error):
        self.status, self.error, self.calls = status, error, 0

    def request(self, method, url, **_):
        self.calls += 1
        reply = self

        class Reply:
            status = reply.status

            async def json(self):
                raise reply.error

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False
        return Reply()


# RecursionError is what json.loads raises on a body nested about 1,000 deep.
@pytest.mark.parametrize('error', [RecursionError(), ValueError('not json')], ids=['deeply nested', 'not json'])
def test_an_invalid_receipt_refusal_with_an_unreadable_body_is_still_set_aside(tmp_path, error):
    usage = tmp_path/'usage'
    receipt(usage/('1'*32+'.json'), 'gpt-6-luna')
    receipt(usage/('2'*32+'.json'), 'gpt-6-luna')
    fri = usage_runtime(None, tmp_path)
    fri.session = Unreadable(422, error)
    asyncio.run(Fri.flush_usage(fri))
    assert fri.session.calls == 2 and len(list((usage/'rejected').glob('*.json'))) == 2


@pytest.mark.parametrize('status, error', [(401, RecursionError()), (422, aiohttp.ContentTypeError(None, (), status=422))],
                         ids=['refused client', 'not the controller'])
def test_other_unreadable_refusals_keep_every_receipt_queued(tmp_path, status, error):
    # A non-JSON reply didn't come from the controller (a proxy, say), so it says nothing about the receipt.
    usage = tmp_path/'usage'
    receipt(usage/('1'*32+'.json'), 'gpt-6-luna')
    receipt(usage/('2'*32+'.json'), 'gpt-6-luna')
    fri = usage_runtime(None, tmp_path)
    fri.session = Unreadable(status, error)
    with pytest.raises(commands.UserFeedbackCheckFailure):
        asyncio.run(Fri.flush_usage(fri))
    assert fri.session.calls == 1 and len(list(usage.glob('*.json'))) == 2 and not (usage/'rejected').exists()


def test_a_receipt_that_cannot_be_set_aside_waits_without_blocking_later_ones(client, tmp_path, caplog):
    usage = tmp_path/'usage'
    receipt(usage/('1'*32+'.json'), 'gpt-6-luna', role='not-a-role')
    receipt(usage/('2'*32+'.json'), 'gpt-6.1-sol')
    (usage/'rejected').write_text('')  # Not a folder, so moving a receipt into it fails like a read-only disk.
    asyncio.run(Fri.flush_usage(usage_runtime(client, tmp_path)))
    assert [p.name for p in usage.glob('*.json')] == ['1'*32+'.json']
    assert 'could not be set aside: FileExistsError' in caplog.text
    totals = client.post('/chat/usage', json=actor(), headers=headers('fri')).json()
    assert {row['model']: row['attempts'] for row in totals} == {'gpt-6.1-sol': 1}


def test_a_usage_delivery_failure_never_discards_a_finished_reply(tmp_path):
    result = SimpleNamespace(status='completed', counters={'input_tokens': 1}, error=None)
    fri = SimpleNamespace(chat=SimpleNamespace(respond=AsyncMock(return_value=result)), data=tmp_path,
                          flush_usage=AsyncMock(side_effect=PermissionError(13, 'private')))
    assert asyncio.run(Fri.chat_once(fri, 'hi', {'model': 'gpt-6-luna', 'effort': 'low'})) is result
    agent = Agent(fri)
    asyncio.run(agent.record('attention', 'gpt-6-luna', 'low', {'input_tokens': 1}, 'completed'))
    assert len(list((tmp_path/'usage').glob('*.json'))) == 2  # Both stay queued for the next flush.


def test_a_missing_presence_switch_clears_a_showing_status(tmp_path):
    fri = SimpleNamespace(data=tmp_path, status=discord.Status.online, activity=object())
    presence = Presence(SimpleNamespace(fri=fri, config={'enabled': True}))
    presence.publish = AsyncMock()
    asyncio.run(presence.check())
    presence.publish.assert_awaited_once_with('')


def test_client_level_refusals_keep_every_receipt_queued(client, tmp_path):
    usage = tmp_path/'usage'
    receipt(usage/('1'*32+'.json'), 'gpt-6-luna')
    receipt(usage/('2'*32+'.json'), 'gpt-6-luna')
    with pytest.raises(ControllerRejected) as refused:
        asyncio.run(Fri.flush_usage(usage_runtime(client, tmp_path, token='fri-'+'y'*32)))
    assert refused.value.status == 401
    assert len(list(usage.glob('*.json'))) == 2 and not (usage/'rejected').exists()
