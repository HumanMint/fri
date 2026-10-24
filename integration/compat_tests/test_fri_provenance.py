import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from gene.store import Store

from compat_tests.test_api import actor, headers
from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_runtime import configured, event
from fri.fri_chat import Chat, ChatResult
from fri.fri_context import ChatSource, discord_source
from fri.fri_eval import evaluate


def test_live_chat_rejects_synthetic_calls_before_reading_or_saving_anything(monkeypatch):
    async def scenario():
        fri = configured(monkeypatch)
        fri.api = AsyncMock()
        fri.chat.respond = AsyncMock()
        with pytest.raises(ValueError, match='Discord event'):
            await fri.talk(123, 456, 10, 'just finished work, absolutely cooked', 'public', 'humanmint')
        fri.api.assert_not_awaited()
        fri.chat.respond.assert_not_awaited()
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', [{'user_id': 'other'}, {'channel_id': '2'}, {'guild_id': 'other'}])
def test_controller_rejects_source_identity_mismatch(client, change):
    source = {'kind': 'discord_message', 'id': '100000000000000001', 'user_id': 'member',
              'display_name': 'Owner lookalike', 'channel_id': '1', 'guild_id': 'guild'}
    body = {**actor('member'), 'channel_id': 'workshop', 'conversation_id': 'public:1',
            'revision': 0, 'input': 'hi', 'reply': 'yo', 'model': 'model', 'effort': 'high'}
    assert client.post('/chat/turn', headers=headers('fri'), json=body).status_code == 422
    body['source'] = {**source, **change}
    assert client.post('/chat/turn', headers=headers('fri'), json=body).status_code == 400
    assert client.post('/chat/context', headers=headers('fri'), json=body).json()['turns'] == []
    body['source'] = source
    assert client.post('/chat/turn', headers=headers('fri'), json=body).status_code == 200
    saved = client.post('/chat/context', headers=headers('fri'), json=body).json()['turns'][0]
    assert saved['source']['user_id'] == 'member'
    assert saved['source']['display_name'] == 'Owner lookalike'
    assert saved['source']['url'].endswith('/guild/1/100000000000000001')
    assert saved['source']['at']


def test_source_types_and_timestamp_ranges_fail_closed():
    data = discord_source(event()).model_dump()
    for changes in ({'kind': 'test'}, {'kind': 'system'}, {'id': 'not-a-snowflake'}, {'id': '9'*25}):
        with pytest.raises(ValueError):
            ChatSource.model_validate({**data, **changes}).check(123, 'public:456', 10)
    with pytest.raises(ValueError, match='Public chat'):
        discord_source(event(guild=None)).check(123, 'public:456', 10)
    assert discord_source(event(guild=None)).check(123, 'private:456', 10).guild_id is None
    assert discord_source(event(kind='interaction')).check(123, 'private:456', 10).kind == 'discord_interaction'
    with pytest.raises(ValueError, match='text does not match'):
        discord_source(event(content='yo'), 'fabricated work story')
    assert discord_source(event(content='<@99> yo'), 'yo', 99).kind == 'discord_message'


def test_legacy_migration_preserves_data_without_inventing_verified_authorship(tmp_path):
    path = tmp_path/'legacy.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE chat_turns (id INTEGER PRIMARY KEY, guild_id TEXT, channel_id TEXT,'
                   'user_id TEXT,input TEXT,reply TEXT,model TEXT,effort TEXT,at REAL,input_tokens INTEGER,output_tokens INTEGER)')
        import time
        db.execute('INSERT INTO chat_turns VALUES (1,?,?,?,?,?,?,?,?,?,?)',
                   ('123', 'public:456', '10', 'legacy input', 'legacy reply', 'm', 'high', time.time(), 1, 1))
    store = Store(path)
    old = store.chat_context('123', 'public:456', '10')['turns'][0]
    assert old['source']['kind'] == 'legacy_unverified' and 'at' not in old['source']
    source = discord_source(event(name='Old Nick')).model_dump()
    store.save_chat('123', 'public:456', '10', 0, 'actual input', 'reply', 'm', 'high', source=source)
    store = Store(path)  # Migration is repeatable and source survives restart.
    saved = store.chat_context('123', 'public:456', '10')['turns'][-1]
    assert saved['source']['display_name'] == 'Old Nick' and saved['source']['user_id'] == '10'
    assert not store.chat_context('123', 'public:456', '20')['turns']


def test_payload_has_current_id_nickname_and_attributed_past_turns():
    source = discord_source(event(name='old nick')).evidence()
    context = {**CONTEXT, 'turns': [{'input': 'old user line', 'reply': 'old bot line', 'source': source}],
               'speaker': {'user_id': '10', 'display_name': 'new nick'},
               'recent': [{'user_id': '20', 'name': 'new nick', 'text': 'a different member said this'}]}
    def handle(request):
        payload = json.loads(request.content)
        history = json.loads(payload['input'][0]['content'])
        current = json.loads(payload['input'][-1]['content'])
        assert history['source']['user_id'] == '10' and history['source']['display_name'] == 'old nick'
        assert current['speaker'] == {'user_id': '10', 'display_name': 'new nick'}
        assert current['recent_channel_context'][0]['user_id'] == '20'
        assert 'a different member' not in payload['instructions']
        return httpx.Response(200, text='data: '+json.dumps(response_event())+'\n\n')
    chat = Chat(SimpleNamespace(access_token=lambda: 'test-plan'), httpx.MockTransport(handle))
    assert asyncio.run(chat.respond('who said that?', context)).status == 'completed'


def test_runtime_uses_event_author_not_supplied_display_name_and_saves_source(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        calls = []
        async def api(method, path, body=None):
            calls.append((path, body))
            if path == '/agent/admit':
                return {'allowed': True}
            if path == '/chat/context':
                return {**CONTEXT, 'revision': 0}
            return {'ok': True}
        fri.api = api
        fri.chat.respond = AsyncMock(return_value=ChatResult(reply='yo', status='completed'))
        source = event(user=20, channel=1, name='current nickname')
        await fri.record_turn(await fri.talk(123, 1, 20, 'yo', 'public', 'forged name', event=source))
        context = fri.chat.respond.await_args.args[1]
        assert context['speaker'] == {'user_id': '20', 'display_name': 'current nickname'}
        saved = next(body for path, body in calls if path == '/chat/turn')
        assert saved['source']['display_name'] == 'current nickname'
        assert saved['source']['user_id'] == '20' and saved['source']['id'] == str(source.id)
        await fri.close()
    asyncio.run(scenario())


def test_evaluation_has_no_actions_or_memory_and_only_records_usage(monkeypatch, tmp_path, capsys):
    from fri import fri_eval
    observed = []
    async def respond(text, context):
        observed.append(context)
        assert context['speaker']['user_id'] == '101'
        assert context['facts'] == [] and not context['actions']
        assert context['personal_memory'] == {'enabled': False, 'notes': []}
        assert context['effort'] == 'high'
        return ChatResult(reply='yo', status='completed', counters={'input_tokens': 10})
    fake_chat = SimpleNamespace(respond=respond)
    direct = asyncio.run(_collect_evals(fake_chat))
    assert len(direct) == 8
    paths = []
    def handle(request):
        paths.append((request.method, request.url.path))
        if request.url.path == '/status':
            return httpx.Response(200, json={'chat_model': 'gpt-6.1-sol', 'chat_effort': 'high', 'effort': 'low'})
        if request.url.path == '/agent/state':
            return httpx.Response(200, json={'config': {'voice_notes': 'brief'}})
        assert request.url.path == '/chat/usage/report' and request.method == 'POST'
        body = json.loads(request.content)
        assert body['role'] == 'evaluation' and 'user_id' not in body and 'input' not in body
        return httpx.Response(200, json={'ok': True})
    client_type = httpx.AsyncClient
    monkeypatch.setenv('GENE_CONTROLLER_URL', 'http://controller.test')
    monkeypatch.setenv('GENE_FRI_TOKEN', 'test-service-token')
    monkeypatch.setenv('FRI_DATA', str(tmp_path))
    with patch.object(fri_eval, 'Chat', return_value=fake_chat), patch.object(fri_eval.httpx, 'AsyncClient',
            side_effect=lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw)):
        asyncio.run(fri_eval.main())
    assert len(paths) == 10 and not list((tmp_path/'usage').glob('*'))
    assert len(capsys.readouterr().out.splitlines()) == 8


async def _collect_evals(chat):
    return [case async for case in evaluate(chat, 'gpt-6.1-sol', 'high')]


def test_evaluation_usage_is_visible_without_identity(client):
    report = {'attempt_id': 'b'*32, 'role': 'evaluation', 'model': 'gpt-6.1-sol', 'effort': 'high',
              'status': 'completed', 'counters': {'input_tokens': 10}}
    assert client.post('/chat/usage/report', headers=headers('fri'), json=report).status_code == 200
    usage = client.post('/agent/usage', headers=headers('fri'), json=actor()).json()
    assert usage[0]['role'] == 'evaluation' and usage[0]['input_tokens'] == 10


def test_usage_delivery_tolerates_evaluator_delivering_the_same_receipt(monkeypatch, tmp_path):
    async def scenario():
        fri = configured(monkeypatch)
        fri.data = tmp_path
        path = tmp_path/'usage'/'receipt.json'
        path.parent.mkdir()
        path.write_text(json.dumps({'attempt_id': 'a'*32}))
        async def deliver(*args):
            path.unlink()  # Simulate the evaluator's successful delivery while Fri waits for its POST.
        fri.api = AsyncMock(side_effect=deliver)
        await fri.flush_usage()
        assert fri.api.await_count == 1
        with patch('pathlib.Path.glob', return_value=[path]):
            await fri.flush_usage()  # Receipt disappeared between listing and reading.
        assert fri.api.await_count == 1
        await fri.close()
    asyncio.run(scenario())
