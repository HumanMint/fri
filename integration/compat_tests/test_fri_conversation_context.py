import asyncio
import copy
import datetime as dt
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import httpx
import pytest
from gene.control_store import Controls

from compat_tests.test_api import headers
from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_endings import context as closing_context
from compat_tests.test_gene_controls import control_body, controls_api  # noqa: F401
from compat_tests.test_runtime_controls import runtime
from fri.fri_chat import Chat, ChatResult
from fri.fri_context import discord_source
from fri.fri_conversation import Bursts, burst_continuation, checked_burst, evidence, unchanged
from fri.fri_responses import quick_closure


def pair(**changes):
    body = control_body(request_text='blind melon no rain', argument='blind melon no rain', **changes)
    original = {'source': {**body['source'], 'id': str(int(body['source']['id'])-(1000 << 22))}, 'text': 'fri play'}
    return {**body, 'burst_origin': original, 'continuation_id': original['source']['id']}


@pytest.mark.parametrize('private', [False, True])
@pytest.mark.parametrize('memory_off', [False, True])
def test_split_command_reaches_real_red_checks_once_and_fences_replay(monkeypatch, tmp_path, controls_api, private, memory_off):  # noqa: F811
    async def scenario():
        client, controls = controls_api
        if memory_off:
            controls.store.personal_control('20', 'disable')
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri._connection.user = SimpleNamespace(id=99)
        bot, runner, cog, _, member, channel, calls, message = await runtime()
        body = pair()
        original = copy.copy(message)
        original.id, original.content = int(body['burst_origin']['source']['id']), 'fri play'
        message.id, message.content = int(body['source']['id']), 'blind melon no rain'
        original.author = message.author = member
        if private:
            original.guild = message.guild = None
            original.channel = message.channel = SimpleNamespace(id=999)
        source_channel = message.channel
        source_channel.fetch_message = AsyncMock(side_effect=lambda mid: original if mid == original.id else message)
        member.voice = SimpleNamespace(channel=SimpleNamespace(id=8))
        async def api(method, path, data=None, role='fri'):
            if path == '/agent/admit':
                return {'allowed': True}
            result = client.request(method, path, headers=headers(role), json=data)
            assert result.is_success, (path, result.text)
            return result.json()
        fri.api = api
        cog.api = lambda method, path, data=None: api(method, path, data, 'runtime')
        fri.chat.respond = AsyncMock(side_effect=[
            ChatResult(status='completed', action={'kind': 'gene_command', 'action': 'play', 'argument': body['argument']}),
            ChatResult(status='completed', reply='queued it')])
        async def deliver(_):
            claim = controls.poll(['play'])
            assert claim and claim['continuation']['burst']
            result = await runner.execute(cog, claim)
            controls.finish(claim['id'], claim['lease'], result)
        burst = [(time.monotonic(), original, original.content), (time.monotonic(), message, message.content)]
        try:
            with patch('fri.fri_actions.asyncio.sleep', new=deliver):
                result = await fri.talk(123, message.channel.id, 20, message.content,
                    'private' if private else 'public', event=message, burst=burst)
            assert result.reply == 'queued it' and len(calls) == 1
            assert calls[0][0] is member and calls[0][1] == 'blind melon no rain'
            first_context = fri.chat.respond.await_args_list[0].args[1]
            assert [row['text'] for row in first_context['message_burst']] == ['fri play', 'blind melon no rain']
            scope = 'private:999' if private else 'public:1'
            turns = controls.store.chat_context('123', scope, '20')['turns']
            if memory_off:
                assert turns == []
            else:
                assert turns[-1]['input'] == message.content and len(turns[-1]['burst']) == 2
            assert controls.poll(['play']) is None
            replay = pair()
            replay.update(source=discord_source(original).model_dump(), request_text=original.content,
                          burst_origin=None, continuation_id='')
            if private:
                replay.update(conversation_id='private:999', channel_id='456', target_channel_id='2')
            with pytest.raises(ValueError, match='already handled'):
                Controls(controls.store).enqueue(replay)
        finally:
            await runner.close()
            await bot.close()
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['actor', 'channel', 'guild', 'old', 'gap', 'same', 'cancel', 'argument', 'example', 'complete'])
def test_split_permission_rejects_scope_age_cancellation_and_invented_arguments(store, change):
    body = pair()
    original = body['burst_origin']
    if change in ('actor', 'channel', 'guild'):
        original['source'][{'actor': 'user_id', 'channel': 'channel_id', 'guild': 'guild_id'}[change]] = '777'
    elif change == 'old':
        body['source']['id'] = str(int(body['source']['id'])-(301000 << 22))
        original['source']['id'] = str(int(body['source']['id'])-(1000 << 22))
    elif change == 'gap':
        original['source']['id'] = str(int(body['source']['id'])-(6000 << 22))
    elif change == 'same':
        original['source']['id'] = body['source']['id']
    elif change == 'cancel':
        body['request_text'] = body['argument'] = 'never mind'
    elif change == 'argument':
        body['argument'] = 'someone else song'
    elif change == 'example':
        original['text'] = 'for example fri play'
    elif change == 'complete':
        original['text'] = 'fri play a different song'
    assert burst_continuation(original, body['source'], body['request_text'], 'play', body['argument'], 0) is None
    with pytest.raises(ValueError):
        Controls(store).enqueue(body)
    assert Controls(store).poll(['play']) is None


@pytest.mark.parametrize('tamper', ['edited', 'deleted', 'author', 'no_voice'])
def test_runtime_rechecks_split_origin_before_start(store, tamper):
    async def scenario():
        body = pair()
        controls = Controls(store)
        controls.enqueue(body)
        claim = controls.poll(['play'])
        bot, runner, cog, _, member, channel, calls, message = await runtime()
        message.id, message.content = int(body['source']['id']), body['request_text']
        message.author = member
        original = copy.copy(message)
        original.id, original.content = int(body['burst_origin']['source']['id']), 'fri play'
        if tamper == 'edited':
            original.content = 'never mind'
        if tamper == 'author':
            original.author = SimpleNamespace(id=30)
        member.voice = None if tamper == 'no_voice' else SimpleNamespace(channel=SimpleNamespace(id=8))
        async def fetch(mid):
            if mid == original.id and tamper == 'deleted':
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
            return original if mid == original.id else message
        channel.fetch_message = AsyncMock(side_effect=fetch)
        try:
            if tamper == 'deleted':
                with pytest.raises(discord.NotFound):
                    await runner.execute(cog, claim)
            else:
                assert 'blocked' in await runner.execute(cog, claim)
            assert not calls
            if tamper != 'no_voice':
                cog.api.assert_not_awaited()
        finally:
            await runner.close()
            await bot.close()
    asyncio.run(scenario())


def test_burst_retention_is_scoped_ephemeral_and_bounded():
    bursts = Bursts()
    def msg(mid, user=20, channel=1):
        return SimpleNamespace(id=mid, content='hello', author=SimpleNamespace(id=user),
                               channel=SimpleNamespace(id=channel))
    bursts.add(msg(1))
    assert bursts.has(1, 20) and not bursts.has(1, 30) and not bursts.has(2, 20)
    bursts.forget(20)
    assert not bursts.has(1, 20)
    for index in range(5):
        bursts.add(msg(index))
    with pytest.raises(ValueError):
        bursts.add(msg(6))
    assert not bursts.has(1, 20)
    bursts.add(msg(7))
    with patch('fri.fri_conversation.time.monotonic', return_value=time.monotonic()+6):
        assert not bursts.has(1, 20)


@pytest.mark.parametrize('change', ['none', 'edited', 'deleted', 'wrong_channel', 'no_access', 'optout', 'old', 'webhook', 'clear'])
def test_reply_context_has_actual_cross_member_author_and_fails_closed(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri._connection.user = SimpleNamespace(id=99)
        bot, runner, _, _, member, channel, _, message = await runtime()
        message.author = member
        original = copy.copy(message)
        original.id -= 1000 << 22
        original.content = 'the second film is good'
        original.author = SimpleNamespace(id=30, display_name='same nickname', bot=False)
        message.reference = SimpleNamespace(message_id=original.id, channel_id=channel.id)
        message.content = 'what did they mean, fri?'
        channel.fetch_message = AsyncMock(return_value=original)
        channel.guild.me = fri.user
        if change == 'wrong_channel':
            original.channel = SimpleNamespace(id=2)
        elif change == 'no_access':
            channel.permissions_for = lambda _: discord.Permissions.none()
        elif change == 'optout':
            agent.profile.return_value['enabled'] = False
        elif change == 'old':
            original.id = discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=8))
            message.reference.message_id = original.id
        elif change == 'webhook':
            original.webhook_id = 321
        elif change == 'deleted':
            channel.fetch_message.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
        try:
            context = await evidence(agent, message, [])
            if change in ('none', 'edited', 'clear'):
                assert context['reply_target'][0]['source']['user_id'] == '30'
                assert context['reply_target'][0]['text'] == original.content
                assert await unchanged(agent, message, context)
                if change == 'edited':
                    original.content = 'edited after inference'
                    assert not await unchanged(agent, message, context)
                if change == 'clear':
                    agent.profile.return_value['revision'] = 1
                    assert not await unchanged(agent, message, context)
            else:
                assert context['reply_target'] == []
        finally:
            await runner.close()
            await bot.close()
            await fri.close()
    asyncio.run(scenario())


def test_conversation_model_receives_source_linked_burst_and_reply_without_misattribution():
    async def scenario():
        body = pair()
        rows = checked_burst([body['burst_origin'], {'source': body['source'], 'text': body['request_text']}],
                             body['source'], body['request_text'])
        target = [{'source': {**body['source'], 'user_id': '30'}, 'text': 'a different member recommendation'}]
        captured = []
        async def transport(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, text='data: '+json.dumps(response_event())+'\n\n')
        chat = Chat(SimpleNamespace(access_token=lambda: 'fictional'), httpx.MockTransport(transport))
        await chat.respond(body['request_text'], {**CONTEXT, 'message_burst': rows, 'reply_target': target})
        supplied = json.loads(captured[0]['input'][-1]['content'])
        assert supplied['reply_target'] == target and supplied['message_burst'] == rows
        assert supplied['message'] == body['request_text']
        assert 'possibly by another person' in captured[0]['instructions']
    asyncio.run(scenario())


def test_new_reply_target_or_unanswered_burst_never_uses_old_exchange_closing_shortcut():
    context = closing_context()
    assert quick_closure('gotcha', context)
    assert not quick_closure('gotcha', {**context, 'reply_target': [{'text': 'a new question?'}]})
    assert not quick_closure('gotcha', {**context, 'message_burst': [{'text': 'a new question?'}, {'text': 'gotcha'}]})


def test_reply_derived_answer_does_not_persist_another_members_words(monkeypatch, tmp_path, controls_api):  # noqa: F811
    async def scenario():
        client, controls = controls_api
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        fri._connection.user = SimpleNamespace(id=99)
        bot, runner, _, _, member, channel, _, message = await runtime()
        message.author = member
        original = copy.copy(message)
        original.id -= 1000 << 22
        original.author = SimpleNamespace(id=30, display_name='same nickname', bot=False)
        original.content = 'my recommendation was Paddington'
        message.content = 'what did they recommend, fri?'
        message.reference = SimpleNamespace(message_id=original.id, channel_id=channel.id)
        channel.fetch_message = AsyncMock(return_value=original)
        async def api(method, path, data=None):
            if path == '/agent/admit':
                return {'allowed': True}
            result = client.request(method, path, headers=headers('fri'), json=data)
            assert result.is_success, (path, result.text)
            return result.json()
        fri.api = api
        fri.chat.respond = AsyncMock(return_value=ChatResult(status='completed',
            reply='they said '+original.content, allow_closing=True))
        try:
            result = await fri.talk(123, 1, 20, message.content, 'public', event=message)
            assert original.content in result.reply
            saved = controls.store.chat_context('123', 'public:1', '20')['turns'][-1]
            assert 'Paddington' not in str(saved) and not saved['allow_closing']
            assert 'reply-context check' in saved['reply']
            controls.store.personal_control('30', 'disable')
            assert 'Paddington' not in str(controls.store.chat_context('123', 'public:1', '20'))
        finally:
            await runner.close()
            await bot.close()
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('private', [False, True])
@pytest.mark.parametrize('memory_off', [False, True])
def test_agent_coalesces_split_invitation_without_existing_active_window(monkeypatch, tmp_path, private, memory_off):
    async def scenario():
        fri, agent, channels, _ = harness(monkeypatch, tmp_path)
        fri._connection.user = SimpleNamespace(id=99)
        fri.api = AsyncMock(return_value={'pending': False})
        agent.profile.return_value['enabled'] = not memory_off
        agent.respond = AsyncMock()
        channel = channels[1]
        first = Mock(spec=discord.Message, id=100, content='fri help me pick a movie',
            author=SimpleNamespace(id=20, bot=False, display_name='friend'), channel=channel,
            guild=None if private else channel.guild, mentions=[], reference=None, webhook_id=None)
        last = copy.copy(first)
        last.id, last.content = 101, 'something funny'
        real_sleep = asyncio.sleep
        async def yield_once(_):
            await real_sleep(0)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=yield_once):
                await asyncio.gather(agent.handle(first), agent.handle(last))
            agent.respond.assert_awaited_once()
            kwargs = agent.respond.await_args.kwargs
            assert [row[2] for row in kwargs['burst']] == [first.content, last.content]
            assert agent.respond.await_args.args[1] == last.content
            assert not agent.bursts.rows
        finally:
            await fri.close()
    asyncio.run(scenario())
