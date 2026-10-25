import asyncio
import itertools
import json
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
from fri.fri_agent import ERASED, BehaviorPreview
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
        await fri.record_turn(result)  # As after the preview is shown.
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


@pytest.fixture(autouse=True)
def signed_in(monkeypatch):
    async def headers(self):
        return {'Authorization': 'Bearer test'}
    monkeypatch.setattr('fri.fri_chat.Chat.headers', headers)


def gate(monkeypatch, answer='settings_change', status='completed'):
    """Stands in for the cheap settings check; the Sol settings call stays on agent.run."""
    calls = []
    async def analyze(chat, instruction, data, model, effort, schema, **kwargs):
        calls.append({'data': data, 'model': model, 'effort': effort, 'answers': schema['properties']['answer']['enum']})
        return ({'answer': answer} if status == 'completed' else {}), {'input_tokens': 40}, status
    monkeypatch.setattr('fri.fri_agent.analyze', analyze)
    return calls


MESSAGE_IDS = itertools.count(1000)


def owner_message():
    return SimpleNamespace(id=next(MESSAGE_IDS), guild=None, author=SimpleNamespace(id=10), reply=AsyncMock(),
                           channel=SimpleNamespace(typing=Mock(side_effect=nullcontext)))


@pytest.mark.parametrize('text', ["you'd be a terrible model lol", 'make a meme about bob',
                                  'i need to learn python, can you help me set up a project'])
def test_owner_chat_with_settings_words_skips_the_sol_settings_call(monkeypatch, tmp_path, text):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri.api = AsyncMock(return_value=agent.state)
        calls = gate(monkeypatch, answer='other')
        agent.run = AsyncMock()
        agent.record = AsyncMock()
        message = owner_message()
        assert not await agent.owner_control(message, text)
        agent.run.assert_not_awaited()
        message.reply.assert_not_awaited()
        message.channel.typing.assert_not_called()  # The normal reply path shows its own typing.
        # Only the owner's words go to the attention model, never settings, notes or ranges.
        assert calls == [{'data': {'message': text}, 'model': 'gpt-6-luna', 'effort': 'low',
                          'answers': ['settings_change', 'other']}]
        agent.record.assert_awaited_once_with('owner_settings', 'gpt-6-luna', 'low', {'input_tokens': 40}, 'completed',
                                              deliver=False)
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('status', ['completed', 'failed'])
def test_settings_request_or_failed_check_still_reaches_the_sol_preview(monkeypatch, tmp_path, status):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri.api = AsyncMock(return_value={**agent.state, 'config': {**DEFAULTS, 'enabled': True}, 'version': 3})
        calls = gate(monkeypatch, status=status)  # A failed check keeps the old path rather than losing the control.
        agent.record = AsyncMock()
        agent.run = AsyncMock(return_value=({'explanation': '', 'changes': [
            {'key': 'heartbeat_seconds', 'json_value': '600'}]}, {}, 'completed'))
        message = owner_message()
        assert await agent.owner_control(message, 'set heartbeat to 10 minutes')
        kwargs = agent.run.await_args.kwargs
        assert len(calls) == 1 and (kwargs['model'], kwargs['effort']) == ('gpt-6.1-sol', 'high')
        message.channel.typing.assert_called_once()
        assert message.reply.await_args.kwargs['view'].changes == {'heartbeat_seconds': 600}
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('answer', ['other', 'settings_change'])
def test_busy_settings_preview_no_longer_swallows_ordinary_owner_chat(monkeypatch, tmp_path, answer):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri.api = AsyncMock(return_value=agent.state)
        gate(monkeypatch, answer=answer)
        agent.record = AsyncMock()
        agent.run = AsyncMock()
        agent.control_queue.append(1)  # An earlier preview is being prepared.
        message = owner_message()
        handled = await agent.owner_control(message, 'you sound like an outdated model')
        agent.run.assert_not_awaited()
        if answer == 'other':
            assert not handled and not message.reply.await_count  # Answered by the normal path instead.
        else:
            assert handled and 'one settings preview at a time' in message.reply.await_args.args[0]
        assert agent.control_queue == [1]
        await fri.close()
    asyncio.run(scenario())


def erase(agent, how):
    if how == 'forget me':
        agent.forget(10)
    else:  # The panel, Gene or Red: only the privacy revision moves.
        agent.profile.return_value = {'enabled': True, 'revision': 1, 'summary': ''}


@pytest.mark.parametrize('how', ['forget me', 'panel'])
@pytest.mark.parametrize('when', ['chat check', 'settings check', 'busy', 'slot wait', 'settings call'])
def test_an_owner_line_erased_while_it_waits_gets_no_reply_or_preview(monkeypatch, tmp_path, how, when):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri.api = AsyncMock(return_value={**agent.state, 'config': {**DEFAULTS, 'enabled': True}, 'version': 3})
        agent.record = AsyncMock()
        agent.control_queue.extend([1] if when == 'busy' else [])
        sol = []
        async def analyze(chat, instruction, data, model, effort, schema, **kwargs):
            if 'answer' in schema['properties']:  # The cheap check.
                if when in ('chat check', 'settings check', 'busy'):
                    erase(agent, how)
                return {'answer': 'other' if when == 'chat check' else 'settings_change'}, {}, 'completed'
            sol.append(model)
            if when == 'settings call':
                erase(agent, how)
            return {'explanation': '', 'changes': [{'key': 'heartbeat_seconds', 'json_value': '600'}]}, {}, 'completed'
        monkeypatch.setattr('fri.fri_agent.analyze', analyze)
        typing = asyncio.Event()
        @asynccontextmanager
        async def typed():
            typing.set()
            yield
        message = owner_message()
        message.channel.typing = typed
        text = 'you sound like an outdated model' if when == 'chat check' else 'set heartbeat to 10 minutes'
        if when == 'slot wait':
            await agent.slots.acquire()  # A background investigation holds the model slot.
            task = asyncio.create_task(agent.owner_control(message, text))
            await typing.wait()  # Now queued behind it.
            erase(agent, how)
            agent.slots.release()
            assert await task == ERASED
        else:
            assert await agent.owner_control(message, text) == ERASED
        message.reply.assert_not_awaited()  # No preview, busy note or error about the erased line.
        assert sol == (['gpt-6.1-sol'] if when == 'settings call' else [])
        assert agent.control_queue == ([1] if when == 'busy' else [])
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('how', ['forget me', 'panel'])
def test_an_owner_line_erased_during_the_state_read_never_reaches_a_model(monkeypatch, tmp_path, how):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        state = {**agent.state, 'config': {**DEFAULTS, 'enabled': True}, 'version': 3}
        async def api(method, path, payload=None):
            erase(agent, how)
            return state
        fri.api = api
        calls = gate(monkeypatch)
        agent.record = AsyncMock()
        agent.run = AsyncMock()
        message = owner_message()
        assert await agent.owner_control(message, 'set heartbeat to 10 minutes') == ERASED
        assert calls == []
        agent.run.assert_not_awaited()
        message.reply.assert_not_awaited()
        await fri.close()
    asyncio.run(scenario())


def test_a_forget_before_the_line_was_handled_counts_from_arrival(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri.api = AsyncMock(return_value={**agent.state, 'config': {**DEFAULTS, 'enabled': True}, 'version': 3})
        calls = gate(monkeypatch)
        agent.record = AsyncMock()
        agent.run = AsyncMock()
        arrived = agent.forgotten.get(10, 0)
        agent.forget(10)  # While the message waited to be handled.
        message = owner_message()
        assert await agent.owner_control(message, 'set heartbeat to 10 minutes', arrived) == ERASED
        assert calls == []
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fence,busy', [
    ('before the check', False), ('after the check', False), ('after the check', True),
    ('in the model slot', False), ('after the settings call', False)])
def test_forget_me_during_a_privacy_read_that_returns_the_old_revision_still_counts(monkeypatch, tmp_path, fence, busy):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri.api = AsyncMock(return_value={**agent.state, 'config': {**DEFAULTS, 'enabled': True}, 'version': 3})
        agent.record = AsyncMock()
        agent.control_queue.extend([1] if busy else [])
        reads = []
        at = ['before the check', 'after the check', 'in the model slot', 'after the settings call'].index(fence)+2
        async def profile(channel, user):
            reads.append(user)
            if len(reads) == at:
                agent.forget(10)  # Lands while the controller still answers with the old revision.
            return {'enabled': True, 'revision': 0, 'summary': ''}
        agent.profile = profile
        checks, sol = [], []
        async def analyze(chat, instruction, data, model, effort, schema, **kwargs):
            if 'answer' in schema['properties']:
                checks.append(model)
                return {'answer': 'settings_change'}, {}, 'completed'
            sol.append(model)
            return {'explanation': '', 'changes': [{'key': 'heartbeat_seconds', 'json_value': '600'}]}, {}, 'completed'
        monkeypatch.setattr('fri.fri_agent.analyze', analyze)
        message = owner_message()
        assert await agent.owner_control(message, 'set heartbeat to 10 minutes') == ERASED
        assert len(checks) == (0 if fence == 'before the check' else 1)
        assert len(sol) == (1 if fence == 'after the settings call' else 0)
        message.reply.assert_not_awaited()
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('slow', ['state read', 'check'])
def test_an_earlier_settings_request_keeps_its_turn(monkeypatch, tmp_path, slow):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        state = {**agent.state, 'config': {**DEFAULTS, 'enabled': True}, 'version': 3}
        started, release = asyncio.Event(), asyncio.Event()
        reads = []
        async def api(method, path, payload=None):
            reads.append(path)
            if slow == 'state read' and len(reads) == 1:
                started.set()
                await release.wait()
            return state
        fri.api = api
        agent.record = AsyncMock()
        async def analyze(chat, instruction, data, *args, **kwargs):
            if slow == 'check' and data['message'] == 'set heartbeat to 10 minutes':
                started.set()
                await release.wait()
            return {'answer': 'settings_change'}, {}, 'completed'
        monkeypatch.setattr('fri.fri_agent.analyze', analyze)
        agent.run = AsyncMock(return_value=({'explanation': '', 'changes': [
            {'key': 'heartbeat_seconds', 'json_value': '600'}]}, {}, 'completed'))
        first, second = owner_message(), owner_message()
        task = asyncio.create_task(agent.owner_control(first, 'set heartbeat to 10 minutes'))
        try:
            await started.wait()
            assert await agent.owner_control(second, 'set the meme interval to 2 hours')
            assert 'one settings preview at a time' in second.reply.await_args.args[0]
            release.set()
            assert await task
            assert first.reply.await_args.kwargs['view'].changes == {'heartbeat_seconds': 600}
            agent.run.assert_awaited_once()
            assert agent.control_queue == []
        finally:
            release.set()
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('how', ['forget me', 'panel'])
@pytest.mark.parametrize('during', ['check', 'settings call'])
def test_an_owner_line_erased_during_sign_in_never_reaches_a_model(monkeypatch, tmp_path, how, during):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri.api = AsyncMock(return_value={**agent.state, 'config': {**DEFAULTS, 'enabled': True}, 'version': 3})
        agent.record = AsyncMock()
        signing_in, signed_in = asyncio.Event(), asyncio.Event()
        signins = []
        async def headers():
            signins.append(during)
            if len(signins) == (1 if during == 'check' else 2):
                signing_in.set()
                await signed_in.wait()  # A slow token refresh.
            return {'Authorization': 'Bearer '+str(len(signins))}
        fri.chat.headers = headers
        sent = []
        async def analyze(chat, instruction, data, model, effort, schema, **kwargs):
            sent.append(('check' if 'answer' in schema['properties'] else 'settings', kwargs['headers']))
            if 'answer' in schema['properties']:
                return {'answer': 'settings_change'}, {}, 'completed'
            return {'explanation': '', 'changes': [{'key': 'heartbeat_seconds', 'json_value': '600'}]}, {}, 'completed'
        monkeypatch.setattr('fri.fri_agent.analyze', analyze)
        message = owner_message()
        task = asyncio.create_task(agent.owner_control(message, 'set heartbeat to 10 minutes'))
        try:
            await asyncio.wait_for(signing_in.wait(), 1)
            erase(agent, how)
            signed_in.set()
            assert await task == ERASED
            # Each call is sent with the sign-in prepared before its last privacy check.
            assert sent == ([] if during == 'check' else [('check', {'Authorization': 'Bearer 1'})])
            message.reply.assert_not_awaited()
        finally:
            signed_in.set()
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('waits', ['state read', 'check'])
def test_a_request_that_waited_takes_the_preview_slot_an_earlier_one_freed(monkeypatch, tmp_path, waits):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        state = {**agent.state, 'config': {**DEFAULTS, 'enabled': True}, 'version': 3}
        first_checking, finish_first = asyncio.Event(), asyncio.Event()
        second_waiting, finish_second = asyncio.Event(), asyncio.Event()
        reads = []
        async def api(method, path, payload=None):
            reads.append(path)
            if waits == 'state read' and len(reads) == 2:  # The second request's.
                second_waiting.set()
                await finish_second.wait()
            return state
        fri.api = api
        async def analyze(chat, instruction, data, *args, **kwargs):
            if data['message'] == 'set heartbeat to 10 minutes':
                first_checking.set()
                await finish_first.wait()
            elif waits == 'check':
                second_waiting.set()
                await finish_second.wait()
            return {'answer': 'settings_change'}, {}, 'completed'
        monkeypatch.setattr('fri.fri_agent.analyze', analyze)
        agent.record = AsyncMock()
        agent.run = AsyncMock(return_value=({'explanation': '', 'changes': [
            {'key': 'heartbeat_seconds', 'json_value': '600'}]}, {}, 'completed'))
        first, second = owner_message(), owner_message()
        earlier = asyncio.create_task(agent.owner_control(first, 'set heartbeat to 10 minutes'))
        try:
            await asyncio.wait_for(first_checking.wait(), 1)
            later = asyncio.create_task(agent.owner_control(second, 'set the meme interval to 2 hours'))
            await asyncio.wait_for(second_waiting.wait(), 1)
            finish_first.set()
            assert await earlier
            finish_second.set()  # The earlier preview is out, so this one is next.
            assert await later
            assert agent.run.await_count == 2
            for message in (first, second):
                assert message.reply.await_args.kwargs['view'].changes == {'heartbeat_seconds': 600}
            assert agent.control_queue == []
        finally:
            finish_first.set()
            finish_second.set()
            await fri.close()
    asyncio.run(scenario())


def test_a_stalled_settings_check_gives_way_to_the_sol_preview(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri.api = AsyncMock(return_value={**agent.state, 'config': {**DEFAULTS, 'enabled': True}, 'version': 3})
        monkeypatch.setattr('fri.fri_agent.CHECK_SECONDS', 0.05)
        stalled = asyncio.Event()
        async def analyze(*args, **kwargs):
            stalled.set()
            await asyncio.Event().wait()  # A stream that never finishes.
        monkeypatch.setattr('fri.fri_agent.analyze', analyze)
        agent.record = AsyncMock()
        agent.run = AsyncMock(return_value=({'explanation': '', 'changes': [
            {'key': 'heartbeat_seconds', 'json_value': '600'}]}, {}, 'completed'))
        message = owner_message()
        try:
            assert await asyncio.wait_for(agent.owner_control(message, 'set heartbeat to 10 minutes'), 2)
            assert stalled.is_set()
            assert agent.record.await_args.args[0] == 'owner_settings' and agent.record.await_args.args[3:] == ({}, 'interrupted')
            message.channel.typing.assert_called_once()
            assert message.reply.await_args.kwargs['view'].changes == {'heartbeat_seconds': 600}
            assert agent.control_queue == []
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_slow_usage_report_does_not_hold_up_the_settings_preview(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri.api = AsyncMock(return_value={**agent.state, 'config': {**DEFAULTS, 'enabled': True}, 'version': 3})
        monkeypatch.setattr('fri.fri_agent.CHECK_SECONDS', 0.05)
        async def analyze(chat, instruction, data, model, effort, schema, **kwargs):
            if 'answer' in schema['properties']:
                await asyncio.Event().wait()  # The check stalls, so the Sol call takes over.
            return ({'explanation': '', 'changes': [{'key': 'heartbeat_seconds', 'json_value': '600'}]},
                    {'input_tokens': 900}, 'completed')
        monkeypatch.setattr('fri.fri_agent.analyze', analyze)
        reporting = asyncio.Event()
        async def flush_usage():
            await reporting.wait()  # The controller is slow to take usage reports.
        fri.flush_usage = AsyncMock(side_effect=flush_usage)
        message = owner_message()
        try:
            assert await asyncio.wait_for(agent.owner_control(message, 'set heartbeat to 10 minutes'), 2)
            assert message.reply.await_args.kwargs['view'].changes == {'heartbeat_seconds': 600}
            fri.flush_usage.assert_not_awaited()  # The usage loop delivers both saved receipts.
            saved = [json.loads(path.read_text()) for path in (tmp_path/'usage').glob('*.json')]
            assert sorted((r['role'], r['model'], r['status']) for r in saved) == [
                ('owner_settings', 'gpt-6-luna', 'interrupted'), ('owner_settings', 'gpt-6.1-sol', 'completed')]
        finally:
            reporting.set()
            await fri.close()
    asyncio.run(scenario())


def test_disabled_fri_skips_the_check_since_only_the_settings_call_can_answer(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri.api = AsyncMock(return_value={**agent.state, 'config': {**DEFAULTS, 'enabled': False}, 'version': 3})
        calls = gate(monkeypatch, answer='other')
        agent.record = AsyncMock()
        agent.run = AsyncMock(return_value=({'explanation': '', 'changes': [
            {'key': 'enabled', 'json_value': 'true'}]}, {}, 'completed'))
        message = owner_message()
        assert await agent.owner_control(message, 'resume your heartbeat')
        assert calls == []
        assert message.reply.await_args.kwargs['view'].changes == {'enabled': True}
        await fri.close()
    asyncio.run(scenario())


def test_settings_check_does_not_wait_behind_background_model_work(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        gate(monkeypatch, answer='other')
        agent.record = AsyncMock()
        async with agent.slots:  # A background investigation holds the model slot.
            assert not await asyncio.wait_for(agent.settings_requested('make it rain', AsyncMock(return_value=False)), 1)
        await fri.close()
    asyncio.run(scenario())


def test_direct_tone_preview_keeps_original_version_during_background_refresh(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        initial = {**agent.state, 'config': {**DEFAULTS, 'enabled': True}, 'version': 7}
        fri.api = AsyncMock(return_value=initial)
        gate(monkeypatch)
        agent.record = AsyncMock()
        async def prepare(*args, **kwargs):
            agent.state = {**initial, 'version': 8, 'config': {**DEFAULTS, 'enabled': True, 'voice_notes': 'Newer preference.'}}
            return {'changes': [{'key': 'voice_notes', 'json_value': '"Sharper dry humor."'}]}, {}, 'completed'
        agent.run = AsyncMock(side_effect=prepare)
        message = owner_message()
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
