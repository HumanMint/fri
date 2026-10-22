import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.fri_agent_store import DEFAULTS

from compat_tests.test_api import headers
from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_agent import setup_agent as setup_agent
from compat_tests.test_fri_chat import CONTEXT
from compat_tests.test_fri_runtime import event
from fri import feedback as commands
from fri.fri_actions import Actions
from fri.fri_agent import BehaviorPreview
from fri.fri_chat import ChatResult


def test_voice_clarification_prepares_settings_without_a_feature_job(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        agent.extra_context = AsyncMock(return_value={})
        calls = []
        async def api(method, path, payload=None):
            calls.append((path, payload))
            if path == '/agent/admit':
                return {'allowed': True}
            if path == '/chat/context':
                return {**CONTEXT, 'revision': 0}
            if path == '/agent/state':
                return agent.state
            return {'ok': True}
        fri.api = api
        fri.chat.respond = AsyncMock(return_value=ChatResult(status='completed',
            action={'kind': 'preview_voice', 'voice_notes': 'Sharper dry humor. Keep replies brief.'}))
        result = await fri.talk(123, 999, 10, 'sharper to start', 'private',
                               event=event(channel=999, guild=None, content='sharper to start'))
        assert result.settings_preview == {'changes': {'voice_notes': 'Sharper dry humor. Keep replies brief.'}, 'version': 0}
        assert result.draft is None and result.notes == []
        assert not any(path.startswith('/requests') or path == '/agent/settings' for path, _ in calls)
        assert fri.chat.respond.await_count == 1
        saved = next(payload for path, payload in calls if path == '/chat/turn')
        assert saved['source']['user_id'] == '10' and 'Nothing is saved' in saved['reply']
        await fri.close()
    asyncio.run(scenario())


def test_preview_action_rechecks_owner_version_and_blocks_secrets(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        action = {'kind': 'preview_voice', 'voice_notes': 'Sharper sarcasm.'}
        fri.api = AsyncMock(return_value={**agent.state, 'version': 1})
        for user, public, channel in ((20, False, 999), (10, True, 1)):
            actions = Actions(agent, user, channel, public)
            assert 'preview_voice' not in await actions.available()
            assert 'blocked' in await actions.execute(action)
        fri.api.assert_not_awaited()
        owner = Actions(agent, 10, 999, False)
        assert 'preview_voice' in await owner.available()
        assert 'blocked' in await owner.execute(action)
        assert 'blocked' in await owner.execute({**action, 'voice_notes': 'token=secret123456789'})
        assert not any(call.args[0] == 'POST' for call in fri.api.await_args_list)
        await fri.close()
    asyncio.run(scenario())


def test_direct_tone_preview_keeps_original_version_during_background_refresh(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        initial = {**agent.state, 'config': dict(DEFAULTS), 'version': 7}
        fri.api = AsyncMock(return_value=initial)
        async def prepare(*args, **kwargs):
            agent.state = {**initial, 'version': 8, 'config': {**DEFAULTS, 'voice_notes': 'Newer preference.'}}
            return {'changes': [{'key': 'voice_notes', 'json_value': '"Sharper dry humor."'}]}, {}, 'completed'
        agent.run = AsyncMock(side_effect=prepare)
        message = SimpleNamespace(guild=None, author=SimpleNamespace(id=10), reply=AsyncMock())
        assert await agent.owner_control(message, 'i want to tune your sarcasm and tone down the assistant tone')
        sent = message.reply.await_args
        assert 'Sharper dry humor.' in sent.args[0] and '```' not in sent.args[0]
        assert sent.kwargs['view'].version == 7
        assert not any(call.args[0] == 'POST' for call in fri.api.await_args_list)
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('stale', [False, True])
def test_apply_requires_full_preview_and_exact_settings_version(setup_agent, stale):
    async def scenario():
        store, settings, agent_store = setup_agent
        client = TestClient(create_app(settings))
        async def api(method, path, payload):
            response = client.request(method, path, headers=headers('fri'), json=payload)
            if response.status_code != 200:
                raise commands.UserFeedbackCheckFailure(response.json()['detail'])
            return response.json()
        agent = SimpleNamespace(fri=SimpleNamespace(owner_id='owner', guild_id='guild',
            user=SimpleNamespace(id='fri'), api=AsyncMock(side_effect=api)), member=AsyncMock(return_value=object()),
            identity=lambda user: {'guild_id': 'guild', 'user_id': user})
        changes = {'voice_notes': ('Keep it dry and brief. '*55).strip()}
        view = BehaviorPreview(agent, changes, 1)
        interaction = SimpleNamespace(user=SimpleNamespace(id='owner'), guild_id=None,
            message=SimpleNamespace(author=SimpleNamespace(id='fri')),
            response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock()), edit_original_response=AsyncMock())
        await view.apply(interaction)
        agent.fri.api.assert_not_awaited()
        while view.next_button.disabled is False:
            await view.next(interaction)
        if stale:
            agent_store.update({'voice_notes': 'Newer owner preference.'}, 1, 'owner')
        await view.apply(interaction)
        current = agent_store.state()
        assert current['config']['voice_notes'] == ('Newer owner preference.' if stale else changes['voice_notes'])
        assert current['version'] == 2
        assert not store.recent('guild')
        assert interaction.edit_original_response.await_args.kwargs['view'] is None
        if stale:
            assert 'fresh preview' in interaction.edit_original_response.await_args.kwargs['content']
    asyncio.run(scenario())
