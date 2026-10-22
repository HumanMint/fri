import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from compat_tests.test_api import headers
from compat_tests.test_fri_actions import harness
from compat_tests.test_gene_controls import controls_api  # noqa: F401
from compat_tests.test_runtime_controls import runtime
from fri.fri_actions import Actions
from fri.fri_chat import ChatResult
from fri.fri_workspace import execute as execute_workspace

TARGET_TEXT = 'my recommendation was Paddington'


async def reply_workflow(monkeypatch, tmp_path, setup):
    client, controls = setup
    fri, agent, _, _ = harness(monkeypatch, tmp_path)
    fri._connection.user = SimpleNamespace(id=99)
    bot, runner, cog, _, member, channel, calls, message = await runtime()
    message.author = member
    original = copy.copy(message)
    original.id -= 1000 << 22
    original.author = SimpleNamespace(id=30, display_name='other friend', bot=False)
    original.content = TARGET_TEXT
    message.reference = SimpleNamespace(message_id=original.id, channel_id=channel.id)
    records = {message.id: message, original.id: original}
    channel.fetch_message = AsyncMock(side_effect=lambda mid: records[mid])
    requests = []
    async def api(method, path, data=None, role='fri'):
        if path == '/agent/admit':
            return {'allowed': True}
        requests.append((path, copy.deepcopy(data)))
        result = client.request(method, path, headers=headers(role), json=data)
        assert result.is_success, (path, result.text)
        return result.json()
    fri.api = api
    cog.api = lambda method, path, data=None: api(method, path, data, 'runtime')
    return fri, agent, bot, runner, cog, member, channel, calls, message, records, requests, controls


def test_target_derived_clarifier_is_replaced_before_retention_and_own_music_request_still_completes(monkeypatch, tmp_path, controls_api):  # noqa: F811
    async def scenario():
        fri, _, bot, runner, cog, member, channel, calls, message, records, requests, controls = await reply_workflow(
            monkeypatch, tmp_path, controls_api)
        message.content = 'fri play their recommendation'
        answer = copy.copy(message)
        answer.id += 1
        answer.content, answer.reference = 'blind melon no rain', None
        records[answer.id] = answer
        fri.chat.respond = AsyncMock(side_effect=[
            ChatResult(status='completed', action={'kind': 'clarify', 'goal': 'play', 'topic': '',
                'question': 'They recommended Paddington. What song from Paddington do you mean?'}),
            ChatResult(status='completed', action={'kind': 'gene_command', 'action': 'play', 'argument': answer.content}),
            ChatResult(status='completed', reply='queued it')])
        member.voice = SimpleNamespace(channel=SimpleNamespace(id=8))
        async def deliver(_):
            claim = controls.poll(['play'])
            assert claim
            outcome = await runner.execute(cog, claim)
            controls.finish(claim['id'], claim['lease'], outcome)
        try:
            result = await fri.talk(123, 1, 20, message.content, 'public', event=message)
            assert result.clarification == {'goal': 'play', 'topic': '', 'question': 'what song do you want?'}
            assert result.reply == 'what song do you want?'
            first = fri.chat.respond.await_args_list[0].args[1]
            assert first['reply_target'][0]['text'] == TARGET_TEXT
            assert not {'work_save', 'prepare_post', 'preview_voice'} & set(first['actions'])
            controls.store.personal_control('30', 'disable')
            retained = controls.store.chat_context('123', 'public:1', '20')
            assert retained['pending']['request_text'] == message.content
            assert retained['pending']['question'] == 'what song do you want?'
            with controls.store.connection() as db:
                assert 'Paddington' not in '\n'.join(db.iterdump())
            assert 'Paddington' not in str(requests)
            with patch('fri.fri_actions.asyncio.sleep', new=deliver):
                result = await fri.talk(123, 1, 20, answer.content, 'public', event=answer)
            assert result.reply == 'queued it' and calls == [(member, answer.content, answer.id)]
            assert controls.store.chat_context('123', 'public:1', '20')['pending'] is None
        finally:
            await runner.close()
            await bot.close()
            await fri.close()
    asyncio.run(scenario())


def test_reply_turn_drops_notes_and_request_draft_before_any_saved_copy(monkeypatch, tmp_path, controls_api):  # noqa: F811
    async def scenario():
        fri, _, bot, runner, _, _, _, _, message, _, requests, controls = await reply_workflow(
            monkeypatch, tmp_path, controls_api)
        message.content = 'fri what should we do with their suggestion? I like Minecraft'
        fri.chat.respond = AsyncMock(return_value=ChatResult(status='completed', reply='they suggested Paddington',
            draft='Add a Paddington recommendation feature', notes=[
                {'category': 'games', 'evidence': 'I like Minecraft'},
                {'category': 'hobbies', 'evidence': 'I like Paddington'}]))
        try:
            result = await fri.talk(123, 1, 20, message.content, 'public', event=message)
            assert result.draft is None and result.notes == []
            assert 'Paddington' in result.reply  # The live answer can discuss the current visible message.
            controls.store.personal_control('30', 'disable')
            retained = controls.store.chat_context('123', 'public:1', '20')
            assert retained['personal_memory']['notes'] == []
            assert 'Paddington' not in str(retained) and 'Paddington' not in str(requests)
            with controls.store.connection() as db:
                assert db.execute('SELECT count(*) FROM fri_work').fetchone()[0] == 0
                assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
        finally:
            await runner.close()
            await bot.close()
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['work_save', 'prepare_post', 'preview_voice'])
def test_private_evidence_sticky_fence_omits_and_blocks_persistent_model_write_tools(monkeypatch, tmp_path, kind):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri._connection.user = SimpleNamespace(id=99)
        fri.api = AsyncMock(return_value={'actions': []})
        actions = Actions(agent, 10, 999, False)
        actions.event = Mock(spec=discord.Message, guild=None, channel=Mock(spec=discord.DMChannel, id=999))
        actions.private_evidence = True
        actions.message = 'save this, prepare a post and change your voice'
        try:
            offered = await actions.available()
            assert kind not in offered
            fri.api.reset_mock()
            result = await actions.execute({'kind': kind, 'body': TARGET_TEXT, 'voice_notes': TARGET_TEXT})
            assert 'blocked' in result
            fri.api.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['work_save', 'prepare_post'])
def test_workspace_adapter_itself_refuses_persistent_writes_after_read_evidence(kind):
    async def scenario():
        event = Mock(spec=discord.Message, guild=None, content='save and send this',
            author=SimpleNamespace(id=10), webhook_id=None)
        event.channel = Mock(spec=discord.DMChannel)
        event.channel.fetch_message = AsyncMock(return_value=event)
        actions = SimpleNamespace(event=event, user=10, owner=True, revision=0, scope={}, private_evidence=True,
            fri=SimpleNamespace(api=AsyncMock(return_value={'revision': 0, 'personal_memory': {'enabled': True}})))
        result = await execute_workspace(actions, {'kind': kind})
        assert 'blocked' in result and actions.fri.api.await_count == 1
        assert actions.fri.api.await_args.args[1] == '/chat/context'
    asyncio.run(scenario())


def test_reply_target_cannot_be_copied_into_durable_music_argument(monkeypatch, tmp_path, controls_api):  # noqa: F811
    async def scenario():
        fri, _, bot, runner, _, _, _, calls, message, _, requests, controls = await reply_workflow(
            monkeypatch, tmp_path, controls_api)
        message.content = 'fri play their recommendation'
        fri.chat.respond = AsyncMock(side_effect=[
            ChatResult(status='completed', action={'kind': 'gene_command', 'action': 'play', 'argument': 'Paddington'}),
            ChatResult(status='completed', reply='tell me the song in your own message')])
        try:
            await fri.talk(123, 1, 20, message.content, 'public', event=message)
            assert not calls and not any(path == '/controls' for path, _ in requests)
            controls.store.personal_control('30', 'disable')
            with controls.store.connection() as db:
                assert 'Paddington' not in '\n'.join(db.iterdump())
                assert db.execute('SELECT count(*) FROM gene_controls').fetchone()[0] == 0
        finally:
            await runner.close()
            await bot.close()
            await fri.close()
    asyncio.run(scenario())
