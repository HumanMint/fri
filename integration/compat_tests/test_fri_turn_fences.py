"""Save-after-delivery fences: what is checked before Discord, and what is recorded after."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_runtime import configured
from fri.fri import MAX_PROVENANCE, UNATTRIBUTED
from fri.fri_chat import ChatResult
from fri.fri_errors import ControllerRejected
from fri.panels import PrivatePanels


def staged(fri, context, result=None):
    scope = {'guild_id': '123', 'channel_id': '1', 'user_id': '20', 'conversation_id': 'public:1'}
    source = SimpleNamespace(model_dump=lambda: {'kind': 'discord_message', 'id': '1', 'user_id': '20'})
    return fri.stage_turn(scope, source, 'yo', {'revision': 0, 'model': 'm', 'effort': 'high', **context},
                          result or ChatResult(reply='an answer', status='completed'))


def test_overflowing_provenance_refuses_instead_of_dropping_sources(monkeypatch):
    fri = configured(monkeypatch)
    rows = [{'user_id': str(1000+n), 'revision': 0} for n in range(MAX_PROVENANCE+1)]
    from fri import feedback as commands
    with pytest.raises(commands.UserFeedbackCheckFailure, match='too many people'):
        staged(fri, {'recent': rows})
    assert len(staged(fri, {'recent': rows[:MAX_PROVENANCE]}).turn['provenance']) == MAX_PROVENANCE


def test_unattributed_fri_reference_answers_live_but_retains_nothing_derived(monkeypatch):
    async def scenario():
        fri = configured(monkeypatch)
        fri._connection.user = SimpleNamespace(id=99)
        fri.api = AsyncMock(return_value={'messages': {}})  # Unknown, e.g. sent before provenance existed.
        context = {'reply_target': [{'source': {'id': '777', 'user_id': '99'}, 'text': 'old fri answer',
                                     'privacy_revision': None}]}
        scope = {'guild_id': '123', 'user_id': '20'}
        await fri.resolve_references(scope, context)
        assert context['unattributed_reference'] and context['reference_provenance'] == []
        result = staged(fri, context)
        assert result.reply == 'an answer'  # The live reply is unchanged.
        assert result.turn['reply'] == UNATTRIBUTED and not result.turn['verbatim'] and result.echo is None
        fri.api = AsyncMock(return_value={'messages': {'777': {'verbatim': False, 'provenance': []}}})
        context.pop('unattributed_reference')
        await fri.resolve_references(scope, context)
        assert context['unattributed_reference']  # A breadcrumbed private read is never re-derived.
        fri.api = AsyncMock(return_value={'messages': {'777': {'verbatim': True, 'provenance': [
            {'user_id': '30', 'revision': 2}, {'user_id': '40', 'revision': 0}]}}})
        context.pop('unattributed_reference')
        await fri.resolve_references(scope, context)
        assert 'unattributed_reference' not in context
        assert staged(fri, context).turn['provenance'] == [{'user_id': '30', 'revision': 2},
                                                          {'user_id': '40', 'revision': 0}]
        await fri.close()
    asyncio.run(scenario())


def test_post_delivery_record_failure_keeps_the_delivered_reply(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, channel, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        controller = fri.api

        async def api(method, path, payload=None):
            if path == '/chat/turn':
                calls.append((path, payload))
                raise ControllerRejected('synthetic refusal after delivery')
            return await controller(method, path, payload)

        fri.api = api
        fri.chat_once.return_value = ChatResult(status='completed', reply='delivered anyway')
        event = message('fri yo')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            event.reply.assert_awaited_once()
            assert event.reply.await_args.args[0] == 'delivered anyway'
            paths = [path for path, _ in calls]
            assert paths.index('/chat/turn/check') < paths.index('/chat/turn')  # Checked, delivered, then saved.
            assert not store.chat_context('123', 'public:1', '20')['turns']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_superseded_panel_answer_is_never_recorded(monkeypatch):
    async def scenario():
        fri = configured(monkeypatch)
        fri.private_panels = PrivatePanels()
        interaction = SimpleNamespace(id=100, user=SimpleNamespace(id=20, display_name='bo'), guild_id=123,
            channel_id=456, response=SimpleNamespace(is_done=lambda: True),
            message=SimpleNamespace(id=1234, flags=SimpleNamespace(ephemeral=True)),
            edit_original_response=AsyncMock(return_value=SimpleNamespace(id=1234)),
            delete_original_response=AsyncMock())

        async def talk(*args, **kwargs):
            fri.private_panels.latest[(123, 20)] = 200  # A newer panel interaction wins meanwhile.
            return ChatResult(reply='this answer was never displayed', status='completed',
                              turn={'reply': 'this answer was never displayed'})

        fri.talk = AsyncMock(side_effect=talk)
        fri.api = AsyncMock(return_value={'ok': True})
        await fri.chat_interaction(interaction, 'a question')
        assert not fri.api.await_args_list  # Nothing displayed, so nothing recorded.
        await fri.close()
    asyncio.run(scenario())


def test_displayed_panel_answer_is_recorded(monkeypatch):
    async def scenario():
        fri = configured(monkeypatch)
        fri.private_panels = PrivatePanels()
        interaction = SimpleNamespace(id=100, user=SimpleNamespace(id=20, display_name='bo'), guild_id=123,
            channel_id=456, response=SimpleNamespace(is_done=lambda: True),
            message=SimpleNamespace(id=1234, flags=SimpleNamespace(ephemeral=True)),
            edit_original_response=AsyncMock(return_value=SimpleNamespace(id=1234)),
            delete_original_response=AsyncMock())
        fri.talk = AsyncMock(return_value=ChatResult(reply='shown', status='completed', turn={'reply': 'shown'}))
        fri.api = AsyncMock(return_value={'ok': True})
        await fri.chat_interaction(interaction, 'a question')
        assert [call.args[1] for call in fri.api.await_args_list] == ['/chat/turn']
        await fri.close()
    asyncio.run(scenario())

