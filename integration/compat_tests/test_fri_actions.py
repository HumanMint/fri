import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app

from compat_tests.test_api import actor, headers
from compat_tests.test_fri_agent import finish, record
from compat_tests.test_fri_agent import setup_agent as setup_agent
from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_runtime import configured, event
from fri.fri_actions import Actions
from fri.fri_agent import Agent
from fri.fri_chat import Chat, ChatResult


def test_owner_learning_endpoints_reject_other_roles_and_forged_owners(setup_agent):
    _, settings, _ = setup_agent
    client = TestClient(create_app(settings))
    for endpoint in ('overview', 'request'):
        path = '/agent/learning/'+endpoint
        for role in ('runtime', 'worker', 'deploy'):
            assert client.post(path, headers=headers(role), json=actor()).status_code == 401
        for guild, user in [('guild', 'member'), ('wrong', 'owner')]:
            assert client.post(path, headers=headers('fri'), json={
                'guild_id': guild, 'user_id': user}).status_code == 403
        assert client.post(path, headers=headers('fri'), json=actor()).status_code == 200


def test_manual_pass_keeps_budget_cursor_report_gate_and_pause(setup_agent):
    store, _, agent = setup_agent
    with store.connection() as db:
        agent.put(db, 'next_learning', time.time()+99999)
        agent.put(db, 'cursors', {'1': {'before': '150', 'done_at': 0}})
    assert agent.claim() is None
    claim = agent.claim(manual=True)
    assert claim['before'] == '150' and claim['used_today'] == 30
    assert 'blocked' in agent.claim(manual=True)
    finish(agent, claim, [record(claim)])
    with store.connection() as db:
        agent.put(db, 'manual_learning_after', 0)
    assert 'report' in agent.claim(manual=True)['blocked']
    agent.delivered(claim['id'])
    with store.connection() as db:
        agent.put(db, 'budget', {'day': 0, 'used': 360})
    assert 'allowance' in agent.claim(manual=True)['blocked']
    agent.update({'learning_enabled': False}, 1, 'owner')
    assert 'paused' in agent.claim(manual=True)['blocked']
    assert agent.overview()['daily_reserved'] == 360


def test_manual_pass_checks_new_messages_after_finishing_archive(setup_agent):
    store, _, agent = setup_agent
    with store.connection() as db:
        agent.put(db, 'cursors', {'1': {'done_at': time.time(), 'before': '1'},
                                  '2': {'done_at': 0, 'before': '20'}})
    claim = agent.claim(manual=True)
    assert claim['channel_id'] == '1' and claim['before'] is None
    assert claim['mode'] == 'recent' and int(claim['after_id']) > 1


def test_overview_uses_delivered_reports_and_respects_erasure(setup_agent):
    store, _, agent = setup_agent
    claim = agent.claim()
    finish(agent, claim, [record(claim)], summary='Friends discussed music.')
    agent.delivered(claim['id'])
    overview = agent.overview()
    assert overview['reports'][0]['delivered'] == 1
    assert 'Conversation summary: Friends discussed music.' in overview['reports'][0]['body']
    assert 'Untrusted summary:' not in overview['reports'][0]['body']
    assert overview['summaries'][0]['body'] == 'Friends discussed music.'
    store.personal_control('person', 'disable')
    assert not agent.overview()['reports'] and not agent.overview()['summaries']


def harness(monkeypatch, tmp_path):
    fri = configured(monkeypatch)
    fri.data = tmp_path
    agent = fri.agent = Agent(fri)
    agent.config['enabled'] = True
    agent.state.update(channels=['1', '2', '3', '456'], history_channels=['1', '2'], started=time.time()-3600)
    agent.dedicated = 3
    agent.member = AsyncMock(return_value=SimpleNamespace(id=10))
    agent.profile = AsyncMock(return_value={'enabled': True, 'revision': 0, 'summary': ''})
    reads = []
    def message(user=20, text='I love trance', bot=False, age=60):
        return SimpleNamespace(author=SimpleNamespace(id=user, bot=bot, display_name='friend'),
            webhook_id=None, content=text, jump_url='https://discord.com/channels/123/1/100',
            created_at=dt.datetime.fromtimestamp(time.time()-age, dt.timezone.utc))
    rows = [message(), message(bot=True, text='Old bot personality'), message(user=30, text='opted out')]
    async def history(**kwargs):
        reads.append(kwargs)
        for row in rows[:kwargs['limit']]:
            if row.created_at > kwargs['after']:
                yield row
    channels = {i: SimpleNamespace(id=i, name='channel-'+str(i), guild=SimpleNamespace(id=123),
        nsfw=False, parent_id=None, history=history, permissions_for=lambda _: SimpleNamespace(
            view_channel=True, read_message_history=True)) for i in (1, 2, 3, 456)}
    fri.get_channel = lambda cid: channels.get(cid)
    return fri, agent, channels, reads


def test_recent_read_is_bounded_excludes_bots_optouts_and_revalidates_sources(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, reads = harness(monkeypatch, tmp_path)
        async def profile(channel, user):
            return {'enabled': user != 30, 'revision': 0}
        agent.profile.side_effect = profile
        actions = Actions(agent, 10, 999, False)
        receipt = await actions.execute({'kind': 'read_recent', 'channel_id': '1'})
        assert receipt['scanned'] == 3 and len(receipt['messages']) == 1
        assert reads[0]['limit'] == 20 and time.time()-reads[0]['after'].timestamp() < 7*86400+1
        assert receipt['personal_notes_saved'] == 0 and not receipt['images_inspected']
        assert await actions.valid()
        agent.profile.side_effect = None
        agent.profile.return_value = {'enabled': True, 'revision': 1}
        assert not await actions.valid()
        await fri.close()
    asyncio.run(scenario())


def test_owner_private_actions_and_channel_audiences_are_enforced(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, reads = harness(monkeypatch, tmp_path)
        fri.api = AsyncMock()
        public = Actions(agent, 10, 1, True)
        assert set(await public.available()) == {'read_recent'}
        assert 'blocked' in await public.execute({'kind': 'read_recent', 'channel_id': '2'})
        assert 'blocked' in await public.execute({'kind': 'run_learning', 'channel_id': ''})
        member = Actions(agent, 20, 999, False)
        assert 'blocked' in await member.execute({'kind': 'learning_overview', 'channel_id': ''})
        fri.api.assert_not_awaited()
        assert not reads
        owner = Actions(agent, 10, 999, False)
        assert 'learning_overview' in await owner.available()
        await owner.execute({'kind': 'read_recent', 'channel_id': '1'})
        channels[1].permissions_for = lambda _: SimpleNamespace(view_channel=False, read_message_history=False)
        assert not await owner.valid()
        await fri.close()
    asyncio.run(scenario())


def test_recent_fri_chat_excludes_old_startup_archive(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, reads = harness(monkeypatch, tmp_path)
        actions = Actions(agent, 10, 999, False)
        await actions.execute({'kind': 'read_recent', 'channel_id': '3'})
        assert abs(reads[0]['after'].timestamp()-agent.state['started']) < .001
        await fri.close()
    asyncio.run(scenario())


def test_report_erasure_during_response_invalidates_owner_answer(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        report = {'id': 'batch', 'channel_id': '1', 'body': 'Music discussion'}
        overview = {'history_channels': ['1'], 'reports': [report], 'summaries': []}
        fri.api = AsyncMock(return_value=overview)
        actions = Actions(agent, 10, 999, False)
        assert (await actions.execute({'kind': 'learning_overview', 'channel_id': ''}))['evidence']['reports']
        fri.api.return_value = {**overview, 'reports': []}
        assert not await actions.valid()
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', [
    'what have u been learning about our server so far? like the memes you see?',
    'go have a look around the server', 'show me what your learning passes found',
    'please run a learning pass now',
])
def test_learning_questions_are_not_settings_commands(monkeypatch, tmp_path, text):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri.api = AsyncMock()
        message = SimpleNamespace(guild=None, author=SimpleNamespace(id=10), reply=AsyncMock())
        assert not await agent.owner_control(message, text)
        fri.api.assert_not_awaited()
        message.reply.assert_not_awaited()
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('offered,reply,kind,expected', [
    (True, '', 'read_recent', 'completed'),
    (False, '', 'read_recent', 'invalid_output'),
    (True, 'I already did it', 'read_recent', 'invalid_output'),
    (True, '', 'shell', 'invalid_output'),
])
def test_action_parser_accepts_only_offered_complete_requests(offered, reply, kind, expected):
    async def scenario():
        output = {'reply': reply, 'notes': [], 'request_draft': None, 'action': {'kind': kind, 'channel_id': '1'}}
        def handler(request):
            return httpx.Response(200, text='data: '+json.dumps(response_event('completed', json.dumps(output)))+'\n\n')
        chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler))
        result = await chat.respond('look around', {**CONTEXT, 'actions': {'read_recent': {}} if offered else {}})
        assert result.status == expected
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['completed', 'failed', 'second_action', 'erased'])
def test_two_stage_reply_counts_both_calls_and_keeps_only_safe_breadcrumb(monkeypatch, tmp_path, outcome):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        agent.extra_context = AsyncMock(return_value={})
        context = {**CONTEXT, 'revision': 0}
        calls = []
        async def api(method, path, payload=None):
            calls.append((path, payload))
            if path == '/agent/admit':
                return {'allowed': True}
            if path == '/chat/context':
                return {**context, 'revision': 1 if outcome == 'erased' and len(calls) > 2 else 0}
            return {'ok': True}
        fri.api = api
        fri.chat.respond = AsyncMock(side_effect=[
            ChatResult(status='completed', action={'kind': 'read_recent', 'channel_id': '1'}, counters={'input_tokens': 10}),
            ChatResult(status='failed' if outcome == 'failed' else 'completed', reply='mostly music chat in the bit i checked',
                       action={'kind': 'read_recent', 'channel_id': '2'} if outcome == 'second_action' else None,
                       notes=[{'category': 'music', 'evidence': 'I love trance'}], counters={'input_tokens': 20})])
        if outcome != 'erased':
            result = await fri.talk(123, 999, 10, 'look around', 'private', event=event(channel=999, content='look around'))
            if outcome == 'completed':
                assert result.reply.startswith('mostly music') and result.notes == []
            else:
                assert 'check completed' in result.reply
                assert 'trance' not in result.reply and not result.action
        else:
            result = await fri.talk(123, 999, 10, 'look around', 'private', event=event(channel=999, content='look around'))
            assert 'access or memory changed' in result.reply
            assert 'music' not in result.reply and 'trance' not in result.reply
        assert fri.chat.respond.await_count == 2
        assert 'read_recent' not in fri.chat.respond.await_args.args[1]['actions']
        assert 'work_save' not in fri.chat.respond.await_args.args[1]['actions']
        assert fri.chat.respond.await_args.args[1]['action_result']['scanned'] == 3
        turns = [body for path, body in calls if path == '/chat/turn']
        if outcome != 'erased':
            assert len(turns) == 1 and turns[0]['notes'] == []
            assert 'read_recent in channel 1' in turns[0]['reply']
            assert 'music' not in turns[0]['reply'] and 'trance' not in turns[0]['reply']
        else:
            assert not turns
        assert [body['counters']['input_tokens'] for path, body in calls if path == '/chat/usage/report'] == [10, 20]
        assert not list((tmp_path/'usage').glob('*.json'))
        await fri.close()
    asyncio.run(scenario())
