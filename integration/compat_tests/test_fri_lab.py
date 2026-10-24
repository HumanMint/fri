import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from gene.api import create_app
from gene.fri_agent_store import AgentStore

from compat_tests.test_api import headers
from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_agent import setup_agent as setup_agent
from fri.fri_actions import Actions
from fri.fri_agent import Agent


def test_lab_chat_isolated_and_cannot_reach_owner_or_feature_routes(setup_agent):
    store, settings, _ = setup_agent
    settings = replace(settings, test_guild_id='999', test_channel_id='789')
    client = TestClient(create_app(settings))
    auth = headers('fri')
    scope = {'guild_id': '999', 'channel_id': '789', 'user_id': 'owner', 'conversation_id': 'public:789'}
    store.remember('guild', 'owner', 'FF private fact', '')
    store.save_chat('guild', 'public:789', 'owner', 0, 'FF secret', 'FF reply', 'model', 'high')
    assert client.post('/chat/context', headers=auth, json=scope).json()['facts'] == []
    assert client.post('/chat/context', headers=auth, json=scope).json()['turns'] == []
    saved = client.post('/chat/turn', headers=auth, json={**scope, 'revision': 0,
        'source': {'kind': 'discord_message', 'id': '100000000000000001', 'user_id': 'owner',
                   'display_name': 'owner', 'channel_id': '789', 'guild_id': '999'},
        'input': 'Lab hello', 'reply': 'hello', 'model': 'model', 'effort': 'high'})
    assert saved.status_code == 200
    assert store.chat_context('999', 'public:789', 'owner')['turns'][0]['input'] == 'Lab hello'
    assert store.chat_context('guild', 'public:789', 'owner')['turns'][0]['input'] == 'FF secret'
    for changes in ({'guild_id': 'guild'}, {'guild_id': '1000'}, {'channel_id': '1'},
                    {'conversation_id': 'private:789'}, {'conversation_id': 'public:1'}):
        assert client.post('/chat/context', headers=auth, json={**scope, **changes}).status_code == 403
    for route, extra in [('/requests', {'body': 'Please add a useful command', 'source_id': '100', 'source_url': ''}),
                         ('/agent/settings', {'changes': {'voice_notes': 'bad'}, 'version': 1}),
                         ('/agent/learning/overview', {}), ('/agent/learning/request', {}),
                         ('/pause', {'paused': True}), ('/memory/list', {})]:
        assert client.post(route, headers=auth, json={**scope, **extra}).status_code == 403
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post('/chat/context', headers=headers(role), json=scope).status_code == 401


def test_lab_attention_admission_and_cooldowns_keep_separate_scope(setup_agent):
    store, settings, _ = setup_agent
    settings = replace(settings, test_guild_id='999', test_channel_id='789')
    client = TestClient(create_app(settings))
    agent = AgentStore(store, settings)
    scope = {'guild_id': '999', 'channel_id': '789', 'user_id': 'member'}
    for channel in ('1', '456', ''):
        assert client.post('/agent/context', headers=headers('fri'), json={**scope, 'channel_id': channel}).status_code == 403
    assert client.post('/agent/context', headers=headers('fri'), json=scope).json()['summary'] == ''
    assert client.post('/agent/admit', headers=headers('fri'), json={**scope, 'space': 'dm'}).status_code == 403
    for _ in range(agent.state()['config']['public_turns']):
        assert agent.admit('member', 'public', 'guild')['allowed']
    assert not agent.admit('member', 'public', 'guild')['allowed']
    assert client.post('/agent/admit', headers=headers('fri'), json={**scope, 'space': 'public'}).json()['allowed']
    assert agent.state()['history_channels'] == ['1', '2']
    assert agent.claim()['channel_id'] in ('1', '2')


def test_lab_harness_observes_only_selected_channel_and_uses_its_guild(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, _ = harness(monkeypatch, tmp_path)
        agent.state['test_scope'] = {'guild_id': '999', 'channel_id': '789'}
        lab = channels[789] = SimpleNamespace(id=789, name='tests_n_stuff', guild=SimpleNamespace(id=999),
            permissions_for=channels[1].permissions_for, history=channels[1].history)
        fri._connection.user = SimpleNamespace(id=99)
        agent.respond = AsyncMock()
        fri.api = AsyncMock(return_value={'pending': False})
        message = SimpleNamespace(guild=lab.guild, channel=lab, webhook_id=None, id=100,
            author=SimpleNamespace(id=20, display_name='friend', bot=False), content='normal chat',
            mentions=[], reference=None, reply=AsyncMock())
        await agent.handle(message)
        assert len(agent.recent[789]) == 1
        agent.respond.assert_not_awaited()
        message.content = 'fri what do you think?'
        with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
            await agent.handle(message)
        agent.respond.assert_awaited_once()
        assert agent.allowed(lab)
        assert not agent.allowed(SimpleNamespace(id=790, parent_id=789, guild=lab.guild))
        assert not agent.allowed(SimpleNamespace(id=789, guild=SimpleNamespace(id=1000)))
        fri.api = AsyncMock(return_value={'enabled': True, 'revision': 0})
        await Agent.profile(agent, 789, 20)
        assert fri.api.await_args.args[2]['guild_id'] == '999'
        actions = Actions(agent, 10, 789, True)
        assert list(await actions.channels()) == ['789']
        assert set(await actions.available()) == {'read_recent'}
        agent.member.assert_awaited_with(10, fresh=False, guild_id=999)
        assert 'blocked' in await actions.execute({'kind': 'read_recent', 'channel_id': '1'})
        assert 'blocked' in await actions.execute({'kind': 'run_learning', 'channel_id': ''})
        # Lab's Gene errors and owner-admin text cannot become FF work or expose reports.
        fri.api.reset_mock()
        message.author.id, message.author.bot = agent.gene_id, True
        message.content = 'Track error: unavailable'
        await agent.handle(message)
        await agent.investigate(789, {'text': 'error'})
        message.author.id = fri.owner_id
        assert not await agent.owner_control(message, 'show usage')
        fri.api.assert_not_awaited()
        await fri.close()
    asyncio.run(scenario())


def test_membership_cache_never_reuses_ff_member_in_lab():
    async def scenario():
        ff = SimpleNamespace(fetch_member=AsyncMock(return_value=SimpleNamespace(id=20, guild='FF')))
        lab = SimpleNamespace(fetch_member=AsyncMock(return_value=SimpleNamespace(id=20, guild='Lab')))
        fri = SimpleNamespace(guild_id=123, get_guild=lambda gid: ff if gid == 123 else lab)
        agent = Agent(fri)
        assert (await agent.member(20)).guild == 'FF'
        assert (await agent.member(20, guild_id=999)).guild == 'Lab'
        assert (await agent.member(20)).guild == 'FF'
        assert ff.fetch_member.await_count == lab.fetch_member.await_count == 1
    asyncio.run(scenario())


def test_lab_reply_dispatch_preserves_actual_guild(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        agent.state['test_scope'] = {'guild_id': '999', 'channel_id': '789'}
        from fri.fri_chat import ChatResult
        fri.talk = AsyncMock(return_value=ChatResult(reply='yeah', draft='ignored draft', status='completed'))
        channel = SimpleNamespace(id=789)
        from unittest.mock import MagicMock
        channel.typing = MagicMock(return_value=AsyncMock())
        message = SimpleNamespace(id=100000000000000002, guild=SimpleNamespace(id=999), channel=channel,
            author=SimpleNamespace(id=20, display_name='friend'), reply=AsyncMock())
        await agent.respond(message, 'hi')
        assert fri.talk.await_args.args[0] == 999
        assert message.reply.await_args.kwargs['view'] is None
        assert agent.active[(789, 20)] > time.time()
        await fri.close()
    asyncio.run(scenario())
