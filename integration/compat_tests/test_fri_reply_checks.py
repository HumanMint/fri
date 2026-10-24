"""Reply checks through the real controller: identity-free pairs in, abstract findings out."""

import asyncio
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.config import Settings
from gene.fri_agent_store import AgentStore
from gene.store import Store

from compat_tests.test_api import headers
from fri.fri_agent import Agent
from fri.fri_reply_checks import SCHEMA, check, describe

FINDING = {'category': 'misunderstood', 'severity': 'medium',
           'issue': 'answered a different question than the one asked about a game',
           'fix': 'restate the request in a few words before answering when it is ambiguous'}


@pytest.fixture
def controller(tmp_path):
    settings = Settings(tmp_path/'reply-checks.db', '123', '10', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='456', fri_token='fri-'+'x'*32, agent_channels='1')
    store = Store(settings.db)
    store.update_chat_models(['gpt-6-luna', 'gpt-6.1-sol'])
    AgentStore(store, settings).update({'enabled': True, 'reply_check_enabled': True}, 0, '10')
    return store, TestClient(create_app(settings))


def save(store, text, reply, user='20', ago=0):
    source = {'kind': 'discord_message', 'id': str(discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc))),
              'user_id': user, 'channel_id': '1', 'guild_id': '123', 'display_name': 'friend'}
    store.save_chat('123', 'public:1', user, 0, text, reply, 'gpt-6.1-sol', 'high', source=source,
                    provenance=[], verbatim=True)
    with store.connection() as db:
        db.execute('UPDATE chat_turns SET at=at-? WHERE id=(SELECT max(id) FROM chat_turns)', (ago,))


def runtime(client, calls, model):
    async def api(method, path, payload=None):
        response = client.request(method, path, json=payload, headers=headers('fri'))
        calls.append(path)
        if response.status_code >= 400:
            from fri.fri_errors import ControllerRejected
            raise ControllerRejected(response.json()['detail'])
        return response.json()
    agent = Agent(SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api))
    agent.run = model
    return agent


def test_a_flagged_follow_up_becomes_an_abstract_finding(controller):
    store, client = controller
    save(store, 'whats the best class for the new raid', 'tank. obviously.', ago=90)
    save(store, 'i asked about the raid not pvp, read it again', 'fine. for the raid, healer.')
    seen, calls = [], []

    async def model(role, instruction, data, schema, model, effort):
        seen.append((role, model, effort, schema, data))
        return {'findings': [{**FINDING, 'pair_id': data['pairs'][0]['id']}]}, {'input_tokens': 9}, 'completed'

    result = asyncio.run(check(runtime(client, calls, model)))
    assert result == {'kept': 1, 'pairs': 1}
    role, name, effort, schema, data = seen[0]
    assert (role, name, effort, schema) == ('reply_check', 'gpt-6-luna', 'low', SCHEMA)
    assert data['pairs'][0]['earlier']['member'] == 'whats the best class for the new raid'
    assert '"20"' not in json.dumps(data) and 'public:1' not in json.dumps(data)
    overview = client.post('/agent/reply-checks/overview', headers=headers('fri'),
                           json={'guild_id': '123', 'user_id': '10'}).json()
    assert overview['latest'][0]['issue'] == FINDING['issue'] and overview['categories'] == {'misunderstood': 1}
    assert asyncio.run(check(runtime(client, calls, model))) is None  # Nothing new, no model call.
    assert len(seen) == 1


def test_a_failed_model_call_is_reported_and_retried(controller):
    store, client = controller
    save(store, 'remind me at 9', 'done.', ago=60)
    save(store, 'you never set it', 'ugh. setting it now.')
    statuses = iter(['failed', 'completed'])

    async def model(role, instruction, data, schema, model, effort):
        return {}, {}, next(statuses)

    calls = []
    assert asyncio.run(check(runtime(client, calls, model))) == {'kept': 0, 'pairs': 1}
    with store.connection() as db:
        AgentStore.put(db, 'next_reply_check', 0)
    assert asyncio.run(check(runtime(client, calls, model))) == {'kept': 0, 'pairs': 1}
    with store.connection() as db:
        assert [row[0] for row in db.execute('SELECT status FROM fri_reply_check_runs ORDER BY at')] == [
            'failed', 'completed']


@pytest.mark.parametrize('guild', [None, SimpleNamespace(id=123)])
def test_owner_can_read_the_summary_only_in_dms(controller, guild):
    store, client = controller
    agent = runtime(client, [], None)
    message = SimpleNamespace(author=SimpleNamespace(id=10), guild=guild, reply=AsyncMock())
    assert asyncio.run(agent.owner_control(message, 'show reply checks'))
    text = message.reply.await_args.args[0]
    assert text == 'dm me for that one.' if guild else text.startswith('no unhappy follow-ups in the last 7 days')
    member = SimpleNamespace(author=SimpleNamespace(id=20), guild=None, reply=AsyncMock())
    assert not asyncio.run(agent.owner_control(member, 'show reply checks'))


def test_describe_lists_counts_and_latest_findings():
    text = describe({'enabled': True, 'days': 7, 'checked_pairs': 40, 'flagged': 2,
                     'categories': {'wrong_info': 1, 'too_long': 1},
                     'latest': [{'day': '2026-10-24', 'space': 'public', 'severity': 'high', **FINDING}]})
    assert text.startswith('last 7 days: 2 unhappy follow-ups out of 40 checked. 1 wrong info, 1 too long.')
    assert FINDING['fix'] in text
    assert describe({'enabled': False, 'days': 7, 'checked_pairs': 0, 'flagged': 0, 'categories': {},
                     'latest': []}).startswith('reply checks are off')
