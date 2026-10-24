import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from gene.store import Store

from compat_tests.test_api import headers
from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_runtime import event
from compat_tests.test_fri_social import DM, confirmed, source
from compat_tests.test_fri_social import api as social_api  # noqa: F401
from compat_tests.test_gene_controls import control_body, controls_api  # noqa: F401
from fri import feedback as commands
from fri.fri_chat import ChatResult
from fri.fri_errors import ControllerRejected
from fri.fri_outcomes import fallback, receipt
from fri.fri_runner import run


@pytest.mark.parametrize('value,outcome', [
    ({'output': 'Track enqueued'}, 'completed'),
    ({'blocked': 'DJ permission required'}, 'rejected'),
    ({'error': 'Search unavailable'}, 'failed'),
    ({'uncertain': 'Gene restarted'}, 'uncertain'),
    ({'done': True}, 'uncertain'),
])
def test_outcomes_distinguish_known_refusal_failure_and_uncertainty(value, outcome):
    actual = receipt('gene_command', value)
    assert actual['outcome'] == outcome
    text = fallback([actual])
    assert 'playing' not in text.casefold()
    if outcome == 'completed':
        assert 'Track enqueued' in text
    elif outcome == 'rejected':
        assert 'dj permission required' in text and 'uncertain' not in text
    elif outcome == 'uncertain':
        assert 'not retried' in text


@pytest.mark.parametrize('failure', ['rejected', 'transport', 'model', 'deadline', 'active_deadline', 'repeat'])
def test_confirmed_tool_result_survives_followup_failure_without_reexecution(failure):
    async def scenario():
        action = {'kind': 'work_save', 'body': 'milk'}
        execute = AsyncMock(return_value={'id': 'a'*32, 'version': 2, 'state': 'open'})
        if failure == 'rejected':
            execute.side_effect = ControllerRejected('Saved work changed. Read its latest version.')
        elif failure == 'transport':
            execute.side_effect = commands.UserFeedbackCheckFailure('connection reset')
        elif failure == 'active_deadline':
            execute.side_effect = TimeoutError
        chat = AsyncMock(side_effect=TimeoutError if failure == 'deadline' else
                         commands.UserFeedbackCheckFailure('model unavailable'))
        if failure == 'repeat':
            chat = AsyncMock(return_value=ChatResult(status='completed', action=action))
        result, receipts, research = await run(SimpleNamespace(chat_once=chat),
            SimpleNamespace(execute=execute), 'save milk', {'actions': {'work_save': {}}},
            ChatResult(action=action, status='completed'))
        execute.assert_awaited_once()
        assert result.status == 'completed' and not result.action and not research
        expected = {'rejected': 'rejected', 'transport': 'uncertain', 'active_deadline': 'uncertain'}.get(failure, 'completed')
        assert receipts[-1]['outcome'] == expected
        if expected == 'completed':
            assert 'saved your work at version 2' in result.reply
        elif expected == 'rejected':
            assert 'saved work changed' in result.reply and 'uncertain' not in result.reply
        else:
            assert 'not retried' in result.reply
    asyncio.run(scenario())


@pytest.mark.parametrize('model_failure,failure_stage', [(True, 'none'), (False, 'recording'),
                                                       (True, 'recording'), (False, 'verification')])
def test_saved_work_remains_persisted_after_reply_or_recording_failure_and_restart(monkeypatch, tmp_path, controls_api, model_failure, failure_stage):  # noqa: F811
    async def scenario():
        client, controls = controls_api
        fri, _, _, _ = harness(monkeypatch, tmp_path)
        calls = []
        async def api(method, path, body=None):
            calls.append(path)
            if path in ('/chat/turn', '/chat/turn/check') and failure_stage == 'recording':
                raise commands.UserFeedbackCheckFailure('Controller temporarily unavailable')
            if path == '/chat/context' and failure_stage == 'verification' and '/workspace/save' in calls:
                raise commands.UserFeedbackCheckFailure('Controller temporarily unavailable')
            response = client.request(method, path, headers=headers('fri'), json=body)
            if response.status_code >= 400:
                raise ControllerRejected(response.json()['detail'])
            return response.json()
        fri.api = api
        actual = event(user=20, channel=1, content='save milk and bread')
        actual.id = int(control_body()['source']['id'])
        actual.webhook_id = None
        actual.channel.fetch_message = AsyncMock(return_value=actual)
        fri.chat.respond = AsyncMock(side_effect=[
            ChatResult(status='completed', action={'kind': 'work_save', 'work_id': '', 'version': 0,
                'title': 'groceries', 'body': 'milk and bread', 'state': 'open'}),
            ChatResult(status='failed' if model_failure else 'completed', reply='saved it',
                       error='model disconnected' if model_failure else '')])
        try:
            result = await fri.talk(123, 1, 20, actual.content, 'public', event=actual)
            if failure_stage != 'none':
                assert 'your work was saved at version 1.' in result.reply
                assert 'couldn’t' in result.reply
                assert 'try again' not in result.reply.casefold()
            else:
                assert result.reply == 'saved your work at version 1 (open).'
                await fri.record_turn(result)  # The delivered reply is saved afterwards.
            restarted = Store(controls.store.path)
            from gene.fri_workspace_store import Workspace
            scope = {'guild_id': '123', 'user_id': '20', 'conversation_id': 'public:1'}
            work = Workspace(restarted).read(scope)['items']
            assert len(work) == 1
            assert Workspace(restarted).read(scope, work[0]['id'])['body'] == 'milk and bread'
            assert calls.count('/workspace/save') == 1
            context = restarted.chat_context('123', 'public:1', '20')
            if failure_stage != 'none':
                assert not context['turns']
            else:  # The member's own saved-work answer is retained verbatim, not as a breadcrumb.
                assert context['turns'][-1]['reply'] == 'saved your work at version 1 (open).'
                assert 'milk and bread' not in context['turns'][-1]['reply']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_later_failure_keeps_prior_saved_work_confirmation():
    evidence = [receipt('work_save', {'version': 3, 'state': 'open'}),
                receipt('prepare_post', {'blocked': 'Channel access changed'})]
    result = fallback(evidence)
    assert 'saved your work at version 3' in result and 'channel access changed' in result


@pytest.mark.parametrize('state,expected', [('cancelled', 'completed'), ('sending', 'rejected'), ('sent', 'rejected')])
def test_reminder_cancellation_fallback_never_claims_an_inflight_send_was_cancelled(state, expected):
    evidence = receipt('reminder_cancel', {'state': state})
    assert evidence['outcome'] == expected
    assert ('reminder cancelled.' in fallback([evidence])) is (state == 'cancelled')


def test_rejected_chat_record_does_not_publish_retrieved_private_read_results(monkeypatch, tmp_path):
    from compat_tests.test_fri_chat import CONTEXT
    async def scenario():
        fri, _, _, _ = harness(monkeypatch, tmp_path)
        private_detail = 'unrelated private member evidence'
        async def api(method, path, value=None):
            if path == '/chat/context':
                return {**CONTEXT, 'revision': 0}
            if path == '/agent/admit':
                return {'allowed': True}
            if path in ('/chat/turn', '/chat/turn/check'):
                raise ControllerRejected('Privacy changed: '+private_detail)
            return {'ok': True}
        fri.api = api
        fri.chat.respond = AsyncMock(side_effect=[
            ChatResult(status='completed', action={'kind': 'read_recent', 'channel_id': '1'}),
            ChatResult(status='completed', reply=private_detail)])
        try:
            actual = event(user=20, channel=1, content='read recent chat')
            result = await fri.talk(123, 1, 20, actual.content, 'public', event=actual)
            assert 'that check completed.' in result.reply
            assert private_detail not in result.reply and 'trance' not in result.reply
            assert not result.draft and not result.clarification
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['model', 'recording', 'access'])
def test_reminder_cancel_retains_confirmed_outcome_across_combined_turn_failures(social_api, monkeypatch, tmp_path, failure):  # noqa: F811
    from gene.fri_social_store import Social

    async def scenario():
        client, call = social_api
        social = Social(client.app.state.store)
        row = confirmed(social, 50)
        fri, _, _, _ = harness(monkeypatch, tmp_path)
        calls = []
        cancel_path = '/social/reminders/'+row['id']+'/decide'

        async def api(method, path, body=None):
            calls.append(path)
            if failure == 'recording' and path in ('/chat/turn', '/chat/turn/check'):
                raise ControllerRejected('Privacy changed')
            if failure == 'access' and path == '/chat/context' and cancel_path in calls:
                raise commands.UserFeedbackCheckFailure('Controller unavailable')
            return await call(method, path, body)

        fri.api = api
        dm = Mock(spec=discord.DMChannel, id=999, history=Mock(return_value=AsyncMock()))
        message = Mock(spec=discord.Message, id=int(source(60).id),
            content='cancel my reminder to update the mods',
            author=SimpleNamespace(id=10, display_name='Member'), guild=None,
            channel=dm, webhook_id=None, attachments=[], embeds=[], edited_at=None)
        dm.fetch_message = AsyncMock(return_value=message)
        action = ChatResult(action={'kind': 'reminder_cancel', 'reminder_id': row['id']}, status='completed')
        fri.chat_once = AsyncMock(side_effect=[action,
            commands.UserFeedbackCheckFailure('model unavailable') if failure == 'model'
            else ChatResult(reply='cancelled', status='completed')])
        try:
            result = await fri.talk(123, 999, 10, message.content, 'private', event=message)
            assert 'reminder' in result.reply.casefold() and 'cancelled' in result.reply.casefold()
            assert 'update the mods' not in result.reply
            assert not result.draft and not result.social_preview
            assert calls.count(cancel_path) == 1
            restarted = Social(Store(client.app.state.store.path))
            assert restarted.reminder_list(DM)['items'][0]['state'] == 'cancelled'
            if failure != 'model':
                assert not client.app.state.store.chat_context('123', 'private:999', '10')['turns']
        finally:
            await fri.close()

    asyncio.run(scenario())
