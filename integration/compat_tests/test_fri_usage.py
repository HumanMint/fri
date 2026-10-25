import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from gene.fri_agent_store import AgentStore
from gene.store import Store

from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_runtime import event
from fri.fri_chat import ChatResult
from fri.fri_usage import describe_usage, usage_requested


@pytest.mark.parametrize('text', [
    "usage is climbing so let's chat tomorrow",
    'we can check usage later', 'do not show usage', 'someone said "show usage"',
    '> show usage', '`show usage`', 'if I said show usage, what would happen?',
    'what was that usage dump?', 'show usage today',
])
def test_mentions_quotes_negation_and_unsupported_periods_are_not_report_requests(text):
    assert not usage_requested(text)


@pytest.mark.parametrize('text', [
    'show usage', 'fri show me token usage', 'can u check our usage pls?',
    'what’s our token usage so far?', 'how many tokens have we used?',
    'please show usage by model', 'usage breakdown',
])
def test_explicit_usage_requests(text):
    assert usage_requested(text)


def test_report_counts_input_and_output_once_and_labels_missing_counts(tmp_path):
    store = Store(tmp_path / 'usage.db')
    agent = AgentStore(store, SimpleNamespace(agent_channels='', agent_history_channels='',
        agent_role_id='', workshop_channel_id='', test_guild_id='', test_channel_id='',
        web_search_enabled=False))
    store.add_chat_usage('a'*32, 'model', 'high', 'completed', {
        'input_tokens': 100, 'output_tokens': 20, 'cached_input_tokens': 70,
        'reasoning_output_tokens': 15})
    store.add_chat_usage('b'*32, 'model', 'high', 'failed', {})
    rows = agent.usage()
    assert rows[0]['unknown_attempts'] == 1
    reply = describe_usage(rows)
    assert '**at least 120 tokens**' in reply and '**2 calls**' in reply
    assert 'incomplete' in reply and 'cumulative' in reply and 'coding usage is separate' in reply
    assert '```' not in reply and 'input_tokens' not in reply and len(reply) < 1950
    assert 'nothing recorded' in describe_usage([])
    assert '- chat: 120 tokens · 2 calls (70 cached input, 15 reasoning)' in reply
    assert 'only count calls that reported each one' in reply
    by_model = describe_usage(rows, by_model=True)
    assert 'model / high: 120 tokens' in by_model and '- chat:' not in by_model
    assert '(70 cached input, 15 reasoning)' in by_model


def usage_store(tmp_path):
    store = Store(tmp_path / 'usage.db')
    agent = AgentStore(store, SimpleNamespace(agent_channels='', agent_history_channels='',
        agent_role_id='', workshop_channel_id='', test_guild_id='', test_channel_id='',
        web_search_enabled=False))
    return store, agent


def counters(input_tokens, output_tokens, cached, reasoning):
    return {'input_tokens': input_tokens, 'output_tokens': output_tokens,
            'cached_input_tokens': cached, 'reasoning_output_tokens': reasoning}


def test_partial_and_zero_cache_reports_stay_counts_not_shares(tmp_path):
    store, agent = usage_store(tmp_path)
    store.add_chat_usage('a'*32, 'model', 'high', 'completed', counters(100, 20, 100, 20))
    store.add_chat_usage('b'*32, 'model', 'high', 'completed', counters(900, 180, None, None))
    store.add_chat_usage('c'*32, 'model', 'high', 'failed', counters(None, None, 100, 100), role='meme')
    store.add_chat_usage('d'*32, 'model', 'high', 'completed', counters(10, 10, 0, 0), role='meme')
    store.add_chat_usage('e'*32, 'model', 'high', 'completed', counters(50, 5, None, None), role='summary')
    reply = describe_usage(agent.usage())
    assert '- chat: 1,200 tokens · 2 calls (100 cached input, 20 reasoning)\n' in reply
    assert '- meme checks: 20 tokens · 2 calls (100 cached input, 100 reasoning)\n' in reply
    assert '- learning summaries: 55 tokens · 1 calls\n' in reply
    assert '%' not in reply and 'incomplete' in reply and 'only count calls that reported each one' in reply
    assert 'cover different calls than the totals' in reply and 'part of' not in reply
    store, agent = usage_store(tmp_path / 'zero')
    store.add_chat_usage('f'*32, 'model', 'high', 'completed', counters(100, 20, 0, 0))
    reply = describe_usage(agent.usage())
    assert '(0 cached input, 0 reasoning)' in reply and 'incomplete' not in reply
    store, agent = usage_store(tmp_path / 'none')
    store.add_chat_usage('g'*32, 'model', 'high', 'completed', counters(100, 20, None, None))
    reply = describe_usage(agent.usage())
    assert '- chat: 120 tokens · 1 calls\n' in reply and 'reported each one' not in reply


@pytest.mark.parametrize('by_model', [False, True])
def test_folded_bucket_adds_only_reported_counts(tmp_path, by_model):
    roles = ('conversation', 'web_search', 'attention', 'summary', 'status', 'history', 'meme', 'investigation')
    folded = 'other models' if by_model else 'other tasks'
    for case, (details, suffix) in enumerate([({6: (30, None), 7: (0, None)}, ' (30 cached input)\n'),
                                              ({6: (None, 13), 7: (None, 17)}, ' (30 reasoning)\n'),
                                              ({6: (0, 0), 7: (None, None)}, ' (0 cached input, 0 reasoning)\n'),
                                              ({}, '\n')]):
        store, agent = usage_store(tmp_path / str(case))
        for index, role in enumerate(roles):
            cached, reasoning = details.get(index, (None, None))
            store.add_chat_usage(f'{index:032d}', f'model-{index}', 'high', 'completed',
                                 counters(200 - index, 10, cached, reasoning), role=role)
        reply = describe_usage(agent.usage(), by_model=by_model)
        assert f'- {folded}: 407 tokens · 2 calls{suffix}' in reply
        assert len(reply) < 1950


def conversation(monkeypatch, tmp_path):
    fri, agent, _, _ = harness(monkeypatch, tmp_path)
    agent.extra_context = AsyncMock(return_value={})
    store = Store(tmp_path / 'conversation.db')
    calls = []

    async def api(method, path, payload=None):
        calls.append((path, payload))
        if path == '/agent/admit':
            return {'allowed': True}
        if path == '/chat/context':
            return store.chat_context(payload['guild_id'], payload['conversation_id'], payload['user_id'])
        if path == '/agent/usage':
            return [{'role': 'conversation', 'model': 'model', 'effort': 'high', 'attempts': 2, 'input_tokens': 100,
                     'output_tokens': 20, 'unknown_attempts': 0}]
        if path == '/controls/recent':
            return {'receipts': []}
        if path == '/controls/catalog':
            return {'actions': []}
        if path == '/chat/turn/check':
            return store.check_chat(payload['user_id'], payload['revision'], payload.get('provenance', []))
        if path == '/chat/turn':
            return store.save_chat(payload['guild_id'], payload['conversation_id'], payload['user_id'],
                payload['revision'], payload['input'], payload['reply'], payload['model'], payload['effort'],
                notes=payload['notes'], source=payload['source'], provenance=payload.get('provenance'))
        return {'ok': True}

    fri.api = api
    fri.chat.respond = AsyncMock(return_value=ChatResult(reply='yeah we can leave it for later', status='completed'))
    return fri, agent, store, calls


def test_casual_usage_mention_remains_a_normal_conversation(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, calls = conversation(monkeypatch, tmp_path)
        text = "usage is climbing so let's chat tomorrow"
        message = event(channel=999, guild=None, content=text)
        message.reply = AsyncMock()
        assert not await agent.owner_control(message, text)
        result = await fri.talk(123, 999, 10, text, 'private', event=message)
        await fri.record_turn(result)
        assert result.reply == 'yeah we can leave it for later'
        assert not any(path == '/agent/usage' for path, _ in calls)
        fri.chat.respond.assert_awaited_once()
        message.reply.assert_not_awaited()
        assert store.chat_context('123', 'private:999', '10')['turns'][0]['input'] == text
        await fri.close()
    asyncio.run(scenario())


def test_report_is_attributed_retained_and_seen_by_followup_without_extra_model_call(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, calls = conversation(monkeypatch, tmp_path)
        message = event(channel=999, guild=None, name='owner nickname', content='show usage by model')
        assert not await agent.owner_control(message, message.content)
        report = await fri.talk(123, 999, 10, message.content, 'private', event=message)
        await fri.record_turn(report)
        fri.chat.respond.assert_not_awaited()
        assert len([p for p, _ in calls if p == '/agent/usage']) == 1
        assert not any(path == '/chat/usage/report' for path, _ in calls)
        saved = store.chat_context('123', 'private:999', '10')['turns'][0]
        assert saved['reply'] == report.reply and saved['source']['display_name'] == 'owner nickname'
        assert saved['source']['user_id'] == '10' and saved['source']['id'] == str(message.id)
        followup = 'woah what was that'
        await fri.talk(123, 999, 10, followup, 'private',
                       event=event(channel=999, guild=None, content=followup))
        assert fri.chat.respond.await_args.args[1]['turns'][0] == saved
        assert not store.chat_context('123', 'private:998', '10')['turns']
        assert not store.chat_context('123', 'private:999', '20')['turns']
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('user,guild', [(20, 123), (10, 999)])
def test_other_members_and_lab_never_get_the_usage_endpoint(monkeypatch, tmp_path, user, guild):
    async def scenario():
        fri, agent, _, calls = conversation(monkeypatch, tmp_path)
        agent.state['test_scope'] = {'guild_id': '999', 'channel_id': '456'}
        await fri.talk(guild, 456, user, 'show usage', 'public',
                       event=event(user=user, guild=guild, content='show usage'))
        assert not any(path == '/agent/usage' for path, _ in calls)
        await fri.close()
    asyncio.run(scenario())


def test_usage_report_respects_memory_optout(monkeypatch, tmp_path):
    async def scenario():
        fri, _, store, _ = conversation(monkeypatch, tmp_path)
        store.personal_control('10', 'disable')
        report = await fri.talk(123, 999, 10, 'show usage', 'private',
                                event=event(channel=999, guild=None, content='show usage'))
        assert '120 tokens' in report.reply
        assert not store.chat_context('123', 'private:999', '10')['turns']
        await fri.close()
    asyncio.run(scenario())


def test_erasure_during_usage_lookup_cannot_repopulate_history(monkeypatch, tmp_path):
    async def scenario():
        from gene.store import Conflict
        fri, _, store, _ = conversation(monkeypatch, tmp_path)
        original = fri.api

        async def erase_after_read(method, path, payload=None):
            result = await original(method, path, payload)
            if path == '/agent/usage':
                store.personal_control('10', 'disable')
            return result

        fri.api = erase_after_read
        with pytest.raises(Conflict):
            await fri.talk(123, 999, 10, 'show usage', 'private',
                           event=event(channel=999, guild=None, content='show usage'))
        assert not store.chat_context('123', 'private:999', '10')['turns']
        assert 10 not in fri.busy
        await fri.close()
    asyncio.run(scenario())


def test_mentioned_owner_report_uses_conversation_path_even_while_chat_is_paused(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, calls = conversation(monkeypatch, tmp_path)
        fri._connection.user = SimpleNamespace(id=99)
        agent.config['enabled'] = False
        agent.respond = AsyncMock()
        message = event(content='<@99> show usage by model')
        message.author.bot = False
        message.webhook_id = None
        message.mentions = [fri.user]
        message.reference = None
        with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
            await agent.handle(message)
        agent.respond.assert_awaited_once_with(message, 'show usage by model', False, forgotten=0, revision=0, waited=False,
                                              addressed=True)
        assert not any(path == '/agent/state' for path, _ in calls)
        await fri.close()
    asyncio.run(scenario())
