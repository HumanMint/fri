"""Reply checks through the real controller: identity-free pairs in, labels from fixed lists out."""

import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from fastapi.testclient import TestClient
from gene import fri_reply_checks as controller_checks
from gene.api import ChatUsage, create_app
from gene.config import Settings
from gene.fri_agent_store import AgentStore
from gene.store import Store

import fri.fri_reply_checks as module
from compat_tests.test_api import headers
from compat_tests.test_split_contract import Session
from fri import feedback as commands
from fri.fri import Fri
from fri.fri_agent import Agent
from fri.fri_errors import ControllerRejected
from fri.fri_protocol import CAPABILITIES, compatible, contract
from fri.fri_reply_checks import CATEGORIES, FIXES, SCHEMA, TOPICS, check, describe

FINDING = {'category': 'misunderstood', 'severity': 'medium', 'topic': 'games',
           'fix': 'answer_the_actual_question'}


@pytest.fixture
def controller(tmp_path):
    settings = Settings(tmp_path/'reply-checks.db', '123', '10', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='456', fri_token='fri-'+'x'*32, agent_channels='1')
    store = Store(settings.db)
    store.update_chat_models(['gpt-6-luna', 'gpt-6.1-sol'])
    agent = AgentStore(store, settings)
    agent.update({'enabled': True, 'reply_check_enabled': True}, 0, '10')
    with store.connection() as db:  # As if checks were switched on yesterday.
        agent.put(db, 'reply_check_since', time.time()-86400)
        agent.put(db, 'reply_check_cursor', None)
    return store, TestClient(create_app(settings))


def save(store, text, reply, user='20', ago=0):
    source = {'kind': 'discord_message', 'id': str(discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc))),
              'user_id': user, 'channel_id': '1', 'guild_id': '123', 'display_name': 'friend'}
    store.save_chat('123', 'public:1', user, 0, text, reply, 'gpt-6.1-sol', 'high', source=source,
                    provenance=[], verbatim=True)
    with store.connection() as db:
        db.execute('UPDATE chat_turns SET at=at-? WHERE id=(SELECT max(id) FROM chat_turns)', (ago,))


def runtime(client, calls, monkeypatch, model=None):
    async def api(method, path, payload=None):
        response = client.request(method, path, json=payload, headers=headers('fri'))
        calls.append(path)
        if response.status_code >= 400:
            raise ControllerRejected(response.json()['detail'])
        return response.json()
    agent = Agent(SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api, chat=None,
                                  capabilities=frozenset(contract()['capabilities'])))
    agent.record = AsyncMock()
    if model:
        monkeypatch.setattr(module, 'analyze', model)
    return agent


def test_a_flagged_follow_up_becomes_a_labelled_finding(controller, monkeypatch):
    store, client = controller
    save(store, 'whats the best class for the new raid', 'tank. obviously.', ago=90)
    save(store, 'i asked about the raid not pvp, read it again', 'fine. for the raid, healer.')
    seen, calls = [], []

    async def model(chat, instruction, data, name, effort, schema):
        seen.append((name, effort, schema, data))
        return {'findings': [{**FINDING, 'pair_id': data['pairs'][0]['id']}]}, {'input_tokens': 9}, 'completed'

    agent = runtime(client, calls, monkeypatch, model)
    assert asyncio.run(check(agent)) == {'kept': 1, 'pairs': 1}
    name, effort, schema, data = seen[0]
    assert (name, effort, schema) == ('gpt-6-luna', 'low', SCHEMA)
    agent.record.assert_awaited_once_with('attention', 'gpt-6-luna', 'low', {'input_tokens': 9}, 'completed',
                                          'reply_check')
    assert data['pairs'][0]['earlier']['member'] == 'whats the best class for the new raid'
    assert '"20"' not in json.dumps(data) and 'public:1' not in json.dumps(data)
    overview = client.post('/agent/reply-checks/overview', headers=headers('fri'),
                           json={'guild_id': '123', 'user_id': '10'}).json()
    assert overview['categories'] == {'misunderstood': 1}
    assert {key: overview['themes'][0][key] for key in ('category', 'topic', 'fix', 'count')} == {
        'category': 'misunderstood', 'topic': 'games', 'fix': 'answer_the_actual_question', 'count': 1}
    assert asyncio.run(check(agent)) is None  # Nothing new, no model call.
    assert len(seen) == 1


def test_labels_match_the_controller():
    assert (CATEGORIES, TOPICS, FIXES) == tuple(list(controller_checks.LABELS[key])
                                                for key in ('category', 'topic', 'fix'))
    assert set(SCHEMA['properties']['findings']['items']['properties']) == {'pair_id', *controller_checks.LABELS}


def test_the_claim_waits_for_the_model_slot(controller, monkeypatch):
    store, client = controller
    save(store, 'remind me at 9', 'done.', ago=60)
    save(store, 'you never set it', 'ugh. setting it now.')
    calls = []

    async def model(*_):
        return {'findings': []}, {}, 'completed'

    agent = runtime(client, calls, monkeypatch, model)

    async def scenario():
        await agent.slots.acquire()  # Other background work holds the model.
        task = asyncio.create_task(check(agent))
        for _ in range(5):
            await asyncio.sleep(0)
        assert calls == []  # No chat text is fetched while it waits.
        agent.slots.release()
        return await task
    assert asyncio.run(scenario()) == {'kept': 0, 'pairs': 1}


def test_a_failed_model_call_is_reported_and_retried(controller, monkeypatch):
    store, client = controller
    save(store, 'remind me at 9', 'done.', ago=60)
    save(store, 'you never set it', 'ugh. setting it now.')
    statuses = iter(['failed', 'completed'])

    async def model(*_):
        return {}, {}, next(statuses)

    agent = runtime(client, [], monkeypatch, model)
    assert asyncio.run(check(agent)) == {'kept': 0, 'pairs': 1}
    with store.connection() as db:
        AgentStore.put(db, 'next_reply_check', 0)
    assert asyncio.run(check(agent)) == {'kept': 0, 'pairs': 1}
    with store.connection() as db:
        assert [row[0] for row in db.execute('SELECT status FROM fri_reply_check_runs ORDER BY at')] == [
            'failed', 'completed']


def test_an_older_controller_still_runs_fri_without_reply_checks(client, monkeypatch):
    older = {**contract(), 'capabilities': list(CAPABILITIES)}
    assert 'reply-checks' in contract()['capabilities'] and compatible(older)

    async def started():
        import fri.fri as fri_module
        import fri.fri_agent as agent_module
        runtime = SimpleNamespace(session=None)
        runtime.api = lambda method, path: Fri.api(SimpleNamespace(url='http://controller', token='fri-'+'x'*32,
                                                                   session=runtime.session), method, path)
        monkeypatch.setattr(fri_module.aiohttp, 'ClientSession', lambda **kwargs: Session(client, older))
        monkeypatch.setattr(agent_module, 'Agent', Mock(side_effect=LookupError))
        with pytest.raises(LookupError):  # Stop right after the handshake.
            await Fri.setup_hook(runtime)
        return runtime.capabilities
    assert 'reply-checks' not in asyncio.run(started())

    calls = []

    async def missing(method, path, payload=None):
        calls.append(path)
        raise ControllerRejected('Not Found')
    agent = Agent(SimpleNamespace(api=missing, capabilities=frozenset(['reply-checks'])))
    assert asyncio.run(check(agent)) is None  # A controller rolled back under a running Fri.
    assert 'reply-checks' not in agent.fri.capabilities and calls == ['/agent/reply-checks/claim']


def test_reply_check_usage_survives_a_paired_rollback(controller, tmp_path, monkeypatch):
    store, client = controller
    (tmp_path/'usage').mkdir()
    agent = Agent(SimpleNamespace(data=tmp_path, flush_usage=AsyncMock()))
    asyncio.run(agent.record('attention', 'gpt-6-luna', 'low', {'input_tokens': 9}, 'completed', 'reply_check'))
    receipt = json.loads(next((tmp_path/'usage').iterdir()).read_text())
    # A role every paired controller knows, plus a field older ones ignore: a receipt still queued
    # when both sides roll back is delivered instead of blocking the queue.
    assert receipt['role'] == 'attention' and receipt['purpose'] == 'reply_check'
    assert ChatUsage.model_config.get('extra', 'ignore') == 'ignore'
    assert client.post('/chat/usage/report', headers=headers('fri'), json=receipt).status_code == 200
    with store.connection() as db:
        assert db.execute('SELECT role FROM agent_usage_roles').fetchone()[0] == 'reply_check'

    import fri.fri as fri_module
    notify = AsyncMock()
    monkeypatch.setattr(fri_module, 'notify_owner', notify)
    stuck = SimpleNamespace(flush_usage=AsyncMock(side_effect=commands.UserFeedbackCheckFailure('rejected')))
    asyncio.run(Fri.owner_reviews.coro(stuck))  # Any stuck receipt still leaves owner notices running.
    notify.assert_awaited_once_with(stuck)


@pytest.mark.parametrize('guild', [None, SimpleNamespace(id=123)])
def test_owner_can_read_the_summary_only_in_dms(controller, guild, monkeypatch):
    store, client = controller
    agent = runtime(client, [], monkeypatch)
    message = SimpleNamespace(author=SimpleNamespace(id=10), guild=guild, reply=AsyncMock())
    assert asyncio.run(agent.owner_control(message, 'show reply checks'))
    text = message.reply.await_args.args[0]
    assert text == 'dm me for that one.' if guild else text.startswith('no unhappy follow-ups in the last 7 days')
    member = SimpleNamespace(author=SimpleNamespace(id=20), guild=None, reply=AsyncMock())
    assert not asyncio.run(agent.owner_control(member, 'show reply checks'))


def test_describe_lists_counts_and_top_themes():
    theme = {'category': 'wrong_info', 'topic': 'games', 'fix': 'verify_facts', 'count': 2}
    text = describe({'enabled': True, 'days': 7, 'checked_pairs': 40, 'flagged': 3,
                     'categories': {'wrong_info': 2, 'too_long': 1}, 'themes': [theme]})
    assert text == ('last 7 days: 3 unhappy follow-ups out of 40 checked. 2 wrong info, 1 too long.\n'
                    '- 2× games: wrong info → verify facts')
    assert describe({'enabled': False, 'days': 7, 'checked_pairs': 0, 'flagged': 0, 'categories': {},
                     'themes': []}).startswith('reply checks are off')
