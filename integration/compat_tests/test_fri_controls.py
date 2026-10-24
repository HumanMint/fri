import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_research import RECEIPT
from compat_tests.test_fri_runtime import event
from compat_tests.test_fri_web import transport
from fri.fri_actions import Actions
from fri.fri_chat import Chat, ChatResult


@pytest.mark.parametrize('action,expected', [('play', 'completed'), ('eval', 'invalid_output'), ('audioset', 'invalid_output')])
def test_model_cannot_choose_arbitrary_red_commands(action, expected):
    result = {'reply': '', 'request_draft': None, 'notes': [],
              'action': {'kind': 'gene_command', 'action': action, 'argument': 'Sad Machine'}}
    chat = Chat(type('Auth', (), {'access_token': lambda _: 'test'})(), transport([response_event(content=json.dumps(result))]))
    output = asyncio.run(chat.respond('fri play Sad Machine', {**CONTEXT, 'actions': {'gene_command': {'commands': {'play': ''}}}}))
    assert output.status == expected


def test_fri_rechecks_message_and_revision_before_enqueuing_and_never_offers_lab_controls(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, _ = harness(monkeypatch, tmp_path)
        actions = Actions(agent, 20, 1, True)
        actions.event = event(user=20, channel=1, content='fri play Sad Machine')
        actions.event.webhook_id = None
        actions.event.channel.fetch_message = AsyncMock(return_value=actions.event)
        actions.message = 'fri play Sad Machine'
        actions.scope = {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:1'}
        actions.revision = 0
        fri.api = AsyncMock(return_value={'revision': 1})
        result = await actions.execute({'kind': 'gene_command', 'action': 'play', 'argument': 'Sad Machine'})
        assert 'blocked' in result
        assert all(c.args[1] != '/controls' for c in fri.api.await_args_list)
        fri.api = AsyncMock(return_value={'actions': ['play', 'eval']})
        assert set((await actions.available())['gene_command']['commands']) == {'play'}
        actions.guild = 777
        actions.channels = AsyncMock(return_value=channels)
        assert 'gene_command' not in await actions.available()
        result = await actions.execute({'kind': 'gene_command', 'action': 'play', 'argument': 'Sad Machine'})
        assert 'blocked' in result
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('scenario', ['requested', 'hypothetical', 'failure', 'third_action'])
def test_web_to_gene_chain_is_bounded_and_requires_original_request(monkeypatch, tmp_path, scenario):
    async def run():
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
        text = 'fri play Python release' if scenario != 'hypothetical' else 'could I ask you to play Python release?'
        lookup = {'kind': 'web_search', **RECEIPT}
        if scenario == 'failure':
            lookup = {'error': 'Search failed'}
        action = {'kind': 'gene_command', 'action': 'play', 'argument': 'Identified Song'}
        fri.chat.respond = AsyncMock(side_effect=[
            ChatResult(status='completed', action={'kind': 'web_search', 'query': 'Python release'}),
            ChatResult(status='completed', action=action),
            ChatResult(status='completed', reply='queued it', action=action if scenario == 'third_action' else None)])
        with patch.object(Actions, 'available', new=AsyncMock(return_value={'web_search': {}, 'gene_command': {}})), \
             patch.object(Actions, 'execute', new=AsyncMock(side_effect=[lookup, {'output': 'Track enqueued'}])) as execute:
            if scenario == 'requested':
                result = await fri.talk(123, 1, 20, text, 'public', event=event(user=20, channel=1, content=text))
                assert result.reply == 'queued it'
                assert not any(path == '/chat/turn' for path, _ in calls)  # Saved only after delivery.
                await fri.record_turn(result)
                saved = next(body for path, body in calls if path == '/chat/turn')
                # Public research and the member's own command result are retained verbatim.
                assert saved['web_receipt'] == RECEIPT and saved['reply'] == 'queued it'
            elif scenario == 'hypothetical':
                fri.chat.respond.side_effect = [
                    ChatResult(status='completed', action={'kind': 'web_search', 'query': 'Python release'}),
                    ChatResult(status='completed', action=action),
                    ChatResult(status='completed', reply='you can ask me to play it')]
                result = await fri.talk(123, 1, 20, text, 'public', event=event(user=20, channel=1, content=text))
                assert result.reply == 'you can ask me to play it'
                assert fri.chat.respond.await_args.args[1]['actions'] == {}
                assert 'blocked' in fri.chat.respond.await_args.args[1]['action_result']
            else:
                result = await fri.talk(123, 1, 20, text, 'public', event=event(user=20, channel=1, content=text))
                assert not result.action
                assert ('search failed' if scenario == 'failure' else 'Track enqueued') in result.reply
            assert execute.await_count == (2 if scenario in ('requested', 'third_action') else 1)
            if execute.await_count == 2:
                assert execute.await_args.args[0] == action
                assert fri.chat.respond.await_args.args[1]['actions'] == {}
        await fri.close()
    asyncio.run(run())


def test_receipt_timeout_never_resubmits_or_cancels_the_gene_command(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        actions = Actions(agent, 20, 1, True)
        actions.event = event(user=20, channel=1, content='fri play Sad Machine')
        actions.event.webhook_id = None
        actions.event.channel.fetch_message = AsyncMock(return_value=actions.event)
        actions.message = 'fri play Sad Machine'
        actions.scope = {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:1'}
        fri.api = AsyncMock(side_effect=[{'revision': 0}, {'id': 'one-command'}])
        actions.diagnose = AsyncMock(return_value={'kind': 'gene_diagnose', 'assessment': 'Unconfirmed command'})
        with patch('fri.fri_actions.asyncio.sleep', new=AsyncMock(side_effect=TimeoutError)):
            result = await actions.execute({'kind': 'gene_command', 'action': 'play', 'argument': 'Sad Machine'})
        assert 'uncertain' in result
        assert [call.args[1] for call in fri.api.await_args_list] == ['/chat/context', '/controls']
        actions.diagnose.assert_awaited_once()
        assert result['diagnostic']['assessment'] == 'Unconfirmed command'
        await fri.close()
    asyncio.run(scenario())


def test_uncertain_receipt_diagnosis_runs_once_outside_command_wait_deadline(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        actions = Actions(agent, 20, 1, True)
        actions.event = event(user=20, channel=1, content='fri play Sad Machine')
        actions.event.webhook_id = None
        actions.event.channel.fetch_message = AsyncMock(return_value=actions.event)
        actions.message = actions.event.content
        actions.scope = {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:1'}
        fri.api = AsyncMock(side_effect=[{'revision': 0}, {'id': 'one'}, {'done': True, 'uncertain': 'Gene restarted'}])
        class Deadline:
            entered = False
            async def __aenter__(self):
                self.entered = True
            async def __aexit__(self, *args):
                self.entered = False
        deadline = Deadline()
        async def diagnose(*args):
            assert not deadline.entered
            return {'assessment': 'Cause unknown'}
        actions.diagnose = AsyncMock(side_effect=diagnose)
        with patch('fri.fri_actions.asyncio.sleep', new=AsyncMock()), \
             patch('fri.fri_actions.asyncio.timeout', return_value=deadline):
            result = await actions.execute({'kind': 'gene_command', 'action': 'play', 'argument': 'Sad Machine'})
        assert result['uncertain'] == 'Gene restarted'
        actions.diagnose.assert_awaited_once()
        assert sum(call.args[1] == '/controls' for call in fri.api.await_args_list) == 1
        await fri.close()
    asyncio.run(scenario())
