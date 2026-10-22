import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from compat_tests.test_api import actor, headers
from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_runtime import event
from fri.fri_actions import Actions
from fri.fri_chat import Chat, ChatResult
from fri.fri_web import cite_reply, lookup
from fri.web_policy import public_query


def search_events(kind='completed', *, searched=True, cited=True):
    source = {'type': 'url_citation', 'url': 'https://www.python.org/downloads/', 'title': 'Python'}
    items = ([{'type': 'web_search_call', 'id': 'ws1', 'status': 'completed'}] if searched else [])
    items += [{'type': 'message', 'id': 'msg1', 'content': [{'type': 'output_text',
               'text': 'A sourced answer.', 'annotations': [source] if cited else []}]}]
    return [{'type': 'response.output_item.done', 'item': i} for i in items] + [
        {'type': 'response.'+kind, 'response': {'output': [], 'usage': {'input_tokens': 120, 'output_tokens': 40}}}]


def transport(stream):
    return httpx.MockTransport(lambda _: httpx.Response(200,
        text=''.join('data: '+json.dumps(e)+'\n\n' for e in stream)))


def test_live_lookup_uses_subscription_and_only_public_topic():
    def handle(request):
        data = json.loads(request.content)
        assert request.headers['Authorization'] == 'Bearer plan-token'
        assert data['tools'] == [{'type': 'web_search', 'external_web_access': True}]
        assert data['store'] is False and data['stream'] is True
        assert not set(data) & {'max_tool_calls', 'max_output_tokens', 'previous_response_id', 'temperature'}
        assert len(data['input']) == 1 and 'Python release' in data['input'][0]['content']
        return httpx.Response(200, text=''.join('data: '+json.dumps(e)+'\n\n' for e in search_events()))
    chat = Chat(SimpleNamespace(access_token=lambda: 'plan-token'), httpx.MockTransport(handle))
    result, counters, status = asyncio.run(lookup(chat, 'Python release', 'gpt-6.1-sol', 'high'))
    assert status == 'completed' and result['live_access'] and counters['input_tokens'] == 120
    assert result['sources'] == ['https://www.python.org/downloads/']
    reply = cite_reply('x'*1800, result)
    assert len(reply) <= 1800 and 'source 1' not in reply
    assert result['sources'][0] in cite_reply('here you go', result, include_sources=True)


@pytest.mark.parametrize('kind,searched,cited', [('failed', True, True), ('incomplete', True, True),
    ('interrupted', True, True), ('completed', False, True), ('completed', True, False)])
def test_web_requires_completed_search_and_provider_citation(kind, searched, cited):
    chat = Chat(SimpleNamespace(access_token=lambda: 'plan'), transport(search_events(kind, searched=searched, cited=cited)))
    result, _, status = asyncio.run(lookup(chat, 'Python', 'model', 'high'))
    assert status != 'completed' and 'error' in result and 'answer' not in result


def test_web_call_limit_and_denial_do_not_reveal_unverified_text():
    stream = [{'type': 'response.output_item.added', 'item': {'id': str(i), 'type': 'web_search_call'}} for i in range(6)]
    chat = Chat(SimpleNamespace(access_token=lambda: 'plan'), transport(stream+search_events()))
    result, _, status = asyncio.run(lookup(chat, 'Python', 'model', 'high'))
    assert status == 'incomplete' and 'answer' not in result
    chat.transport = httpx.MockTransport(lambda _: httpx.Response(403, json={'error': {'message': 'private diagnostic'}}))
    result, _, status = asyncio.run(lookup(chat, 'Python', 'model', 'high'))
    assert status == 'failed' and 'private diagnostic' not in str(result)


@pytest.mark.parametrize('query', ['member@private.com', 'owner 123456789012345678',
    'http://127.0.0.1/config', 'https://10.0.0.1', 'http://metadata.google.internal',
    'https://user:pass@example.com', 'token=private', 'ghp_abcdefghijklmno', 'localhost secrets'])
def test_web_queries_reject_private_identifiers_and_local_addresses(query):
    assert not public_query(query, 'fri look up '+query)


def test_web_query_cannot_export_a_fact_absent_from_current_message():
    assert not public_query('a saved private fact', 'fri tell me more')
    assert public_query('Python 3.15.0 release', 'fri what changed in Python 3.15.0 release')
    assert public_query('https://www.python.org/downloads/', 'check https://www.python.org/downloads/')


def test_search_action_requires_availability_and_current_exact_topic():
    content = json.dumps({'reply': '', 'request_draft': None, 'notes': [],
                          'action': {'kind': 'web_search', 'query': 'Python release'}})
    chat = Chat(SimpleNamespace(access_token=lambda: 'plan'), transport([response_event(content=content)]))
    assert asyncio.run(chat.respond('Python release', CONTEXT)).status == 'invalid_output'
    result = asyncio.run(chat.respond('Python release', {**CONTEXT, 'actions': {'web_search': {}}}))
    assert result.action['query'] == 'Python release'


def test_search_action_keeps_context_out_and_records_separate_usage(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        agent.state['web_search_enabled'] = True
        agent.record = AsyncMock()
        actions = Actions(agent, 20, 1, True)
        actions.message, actions.model, actions.effort = 'fri current Python release?', 'gpt-6.1-sol', 'high'
        with patch('fri.fri_actions.lookup', new=AsyncMock(return_value=(
                {'kind': 'web_search', 'sources': ['https://python.org']}, {'input_tokens': 100}, 'completed'))) as search:
            assert 'web_search' in await actions.available()
            assert 'blocked' in await actions.execute({'kind': 'web_search', 'query': 'private saved notes'})
            search.assert_not_awaited()
            result = await actions.execute({'kind': 'web_search', 'query': 'current Python release'})
            assert result['kind'] == 'web_search'
            search.assert_awaited_once_with(fri.chat, 'current Python release', 'gpt-6.1-sol', 'high')
            agent.record.assert_awaited_with('web_search', 'gpt-6.1-sol', 'high', {'input_tokens': 100}, 'completed')
            agent.member.return_value = None
            assert not await actions.valid()
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('status', ['completed', 'failed', 'incomplete', 'interrupted', 'invalid_output'])
def test_web_usage_delivery_is_accepted_and_idempotent(client, status):
    report = {'attempt_id': 'a'*32, 'model': 'gpt-6.1-sol', 'effort': 'high', 'status': status,
              'role': 'web_search', 'counters': {'input_tokens': 100}}
    for _ in range(2):
        assert client.post('/chat/usage/report', headers=headers('fri'), json=report).status_code == 200
    usage = client.post('/agent/usage', headers=headers('fri'), json=actor()).json()
    assert usage[0]['role'] == 'web_search' and usage[0]['attempts'] == 1


def test_web_reply_preserves_sources_and_keeps_only_a_neutral_receipt(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        agent.state['web_search_enabled'] = True
        calls = []
        async def api(method, path, body=None):
            calls.append((path, body))
            if path == '/agent/admit':
                return {'allowed': True}
            if path == '/chat/context':
                return {**CONTEXT, 'revision': 0}
            return {'ok': True}
        fri.api = api
        fri.chat.respond = AsyncMock(side_effect=[
            ChatResult(status='completed', action={'kind': 'web_search', 'query': 'Python release'}),
            ChatResult(status='completed', reply='heres the [current release](https://python.org/downloads/)', draft='unexpected draft')])
        receipt = {'kind': 'web_search', 'answer': 'public evidence', 'sources': ['https://python.org/downloads/'],
                   'checked_at': '2026-10-19T20:00:00+00:00'}
        with patch('fri.fri_actions.lookup', new=AsyncMock(return_value=(receipt, {'input_tokens': 20}, 'completed'))):
            result = await fri.talk(123, 1, 20, 'fri Python release?', 'public',
                event=event(user=20, channel=1, content='fri Python release?'))
        assert 'http' not in result.reply and 'current release' in result.reply and result.draft is None
        context = fri.chat.respond.await_args.args[1]
        assert 'web_search' not in context['actions'] and 'web_refine' in context['actions']
        assert context['action_result'] == {**receipt, 'outcome': 'completed'}
        saved = next(body for path, body in calls if path == '/chat/turn')
        assert saved['reply'].startswith('Earlier check: web_search') and 'public evidence' not in saved['reply']
        assert saved['web_receipt'] == {k: receipt[k] for k in ('query', 'answer', 'sources', 'checked_at')}
        roles = [body.get('role', 'conversation') for path, body in calls if path == '/chat/usage/report']
        assert roles == ['conversation', 'web_search', 'conversation']
        assert not list((tmp_path/'usage').glob('*.json'))
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('wording', ['share a link to a random sfw image featuring hatsune miku',
    'send me the URL for Python downloads', 'where did you find that Python release, source?'])
def test_first_request_for_a_link_is_fulfilled_without_an_extra_sources_turn(monkeypatch, tmp_path, wording):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        agent.state['web_search_enabled'] = True
        async def api(method, path, body=None):
            if path == '/agent/admit':
                return {'allowed': True}
            if path == '/chat/context':
                return {**CONTEXT, 'revision': 0}
            return {'ok': True}
        fri.api = api
        url = 'https://safebooru.donmai.us/posts/123'
        fri.chat.respond = AsyncMock(side_effect=[
            ChatResult(status='completed', action={'kind': 'web_search', 'query': wording}),
            ChatResult(status='completed', reply=f'here’s the [artwork]({url})')])
        receipt = {'answer': 'Selected public artwork', 'sources': [url], 'checked_at': '2026-10-22T00:00:00+00:00'}
        try:
            with patch('fri.fri_actions.lookup', new=AsyncMock(return_value=(receipt, {}, 'completed'))):
                result = await fri.talk(123, 1, 20, wording, 'public', event=event(user=20, channel=1, content=wording))
            assert result.reply == f'here’s the [artwork](<{url}>)'
            assert fri.chat.respond.await_count == 2
        finally:
            await fri.close()
    asyncio.run(scenario())
