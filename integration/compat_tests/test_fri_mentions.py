"""Real-controller invitation workflows, entirely synthetic and offline."""

import asyncio
import copy
import datetime as dt
import json
import logging
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import httpx
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.config import Settings
from gene.fri_agent_store import AgentStore

from compat_tests.test_api import headers
from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_continuity import arm, origin
from compat_tests.test_fri_runtime import configured
from fri.fri_actions import Actions
from fri.fri_addressing import explicit_mention
from fri.fri_agent import QUEUE_SECONDS, Agent
from fri.fri_chat import Chat, ChatResult
from fri.fri_errors import ControllerRejected, ControllerUnavailable
from fri.fri_mentions import CONTEXT_CHANGED, GLITCH, NOTICES, Invitation
from fri.fri_notices import ReplyInProgress


def environment(monkeypatch, tmp_path, *, channel_id=8):
    settings = Settings(tmp_path/'mentions.db', '123', '10', 'runtime-'+'x'*32,
        'worker-'+'x'*32, 'deploy-'+'x'*32, fri_token='fri-'+'x'*32,
        workshop_channel_id='456', agent_channels='1', web_search_enabled=True,
        test_guild_id='777', test_channel_id='7')
    client = TestClient(create_app(settings))
    store = client.app.state.store
    controller = AgentStore(store, settings)
    controller.update({'enabled': True}, 0, '10')
    fri = configured(monkeypatch)
    fri._connection.user = SimpleNamespace(id=99)
    agent = fri.agent = Agent(fri)
    agent.state = controller.state()
    agent.record = AsyncMock()
    calls, rows = [], {}
    permissions = discord.Permissions.all()
    guild = SimpleNamespace(id=123, me=fri.user)
    @asynccontextmanager
    async def typing():
        yield
    async def history(**kwargs):
        candidates = [m for m in rows.values() if m.id < kwargs['before'].id and
                      ('after' not in kwargs or discord.utils.snowflake_time(m.id) > kwargs['after'])]
        for item in sorted(candidates, key=lambda m: m.id, reverse=True)[:kwargs['limit']]:
            yield item
    channel = SimpleNamespace(id=channel_id, guild=guild, parent_id=None, nsfw=False,
        name='channel', permissions_for=lambda _: permissions, history=history, typing=typing)
    channel.fetch_message = AsyncMock(side_effect=lambda mid: rows[mid])
    guild.fetch_channel = AsyncMock(return_value=channel)
    guild.fetch_member = AsyncMock(side_effect=lambda user: SimpleNamespace(id=user, bot=user == 99))
    guild.fetch_roles = AsyncMock(return_value=[])
    fri.get_channel = lambda cid: channel if cid == channel_id else None
    fri.get_guild = lambda gid: guild if gid == 123 else None
    async def api(method, path, payload=None):
        calls.append((path, payload))
        response = client.request(method, path, headers=headers('fri'), json=payload)
        if not response.is_success:
            raise ControllerRejected(response.text)
        return response.json()
    fri.api = api
    fri.chat_once = AsyncMock(return_value=ChatResult(status='completed', reply='that meme is doing a lot of work'))
    def message(text='<@99> what do you think?', *, user=20, age=0, attachments=()):
        mid = discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=age))
        while mid in rows:
            mid += 1
        item = Mock(spec=discord.Message, id=mid, content=text, channel=channel, guild=guild,
            author=SimpleNamespace(id=user, display_name=f'member {user}', bot=False), webhook_id=None,
            mentions=[fri.user] if '<@99>' in text or '<@!99>' in text else [],
            reference=None, attachments=list(attachments), embeds=[], edited_at=None, reply=AsyncMock())
        rows[mid] = item
        return item
    return fri, agent, store, controller, channel, message, calls, rows


def mention_lines(caplog):
    return [line for line in caplog.messages if line.startswith('Mention ')]


def attachment(channel):
    return SimpleNamespace(url=f'https://cdn.discordapp.com/attachments/{channel}/1/meme.png',
        content_type='image/png', size=100, width=100, height=100, is_spoiler=lambda: False)


@pytest.mark.parametrize('text', ['fri what do you think?', 'hey fri', '@everyone', '<@123>',
    '`<@99>`', '```<@99>```', '```\n<@99>', '> <@99>', '>>> quote\n<@99>',
    '"<@99>"', '“<@99>”', "'<@99>'", r'\<@99>', 'https://example.com/<@99>'])
def test_outside_noninvitations_do_nothing(monkeypatch, tmp_path, caplog, text):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, channel, message, calls, _ = environment(monkeypatch, tmp_path)
        event = message(text)
        event.mentions = [fri.user]  # Even a parsed/forged token must pass the visible-token gate.
        assert not explicit_mention(event, 99)
        channel.history = Mock(side_effect=AssertionError('ambient history read'))
        try:
            await agent.handle(event)
            assert not calls and not agent.recent and not agent.active
            fri.chat_once.assert_not_awaited()
            event.reply.assert_not_awaited()
            assert f'Mention 8/{event.id} not answered: not a visible user mention' in caplog.messages
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('configured_channel', [False, True])
def test_fris_role_pill_is_not_an_invitation_but_says_why(monkeypatch, tmp_path, caplog, configured_channel):
    # Picking Fri's integration role from autocomplete looks like a ping but isn't a user mention.
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, channel, message, calls, _ = environment(
            monkeypatch, tmp_path, channel_id=1 if configured_channel else 8)
        event = message('<@&555> you there?')
        event.role_mentions = [SimpleNamespace(id=555, tags=SimpleNamespace(bot_id=99))]
        try:
            await agent.handle(event)
            fri.chat_once.assert_not_awaited()
            event.reply.assert_not_awaited()
            assert not any(path == '/chat/mentions/admit' for path, _ in calls)
            assert f'Mention {channel.id}/{event.id} not answered: not a visible user mention' in caplog.messages
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('configured_channel', [False, True])
@pytest.mark.parametrize('model_kind', ['reply', 'react', 'quiet'])
def test_ping_after_another_members_meme_gets_text_without_ambient_expansion(monkeypatch, tmp_path, configured_channel, model_kind):
    async def scenario():
        fri, agent, store, _, channel, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1 if configured_channel else 8)
        meme = message('public meme caption', user=30, age=15*60, attachments=[attachment(channel.id)])
        event = message('<@99> what do you think of that meme?')
        final = ChatResult(status='completed', response_kind=model_kind,
            reaction='👍' if model_kind == 'react' else None,
            reply='that meme is doing a lot of work' if model_kind == 'reply' else '')
        if configured_channel:
            async def respond(_, context):
                if not context.get('action_result'):
                    assert not context['invited_context'] and 'work_save' in context['actions']
                    return ChatResult(status='completed', action={'kind': 'read_recent', 'channel_id': str(channel.id)})
                assert context['images'] and 'work_save' not in context['actions']
                assert 'image_search' not in context['actions']
                return final
            fri.chat_once.side_effect = respond
        else:
            fri.chat_once.return_value = final
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            event.reply.assert_awaited_once()
            assert event.reply.await_args.args[0]
            context = fri.chat_once.await_args.args[1]
            assert context['force_text_reply'] and not context['acknowledgments_enabled']
            assert context['invited_context'][0]['source']['user_id'] == '30'
            assert context['image_event'].id == meme.id and context['images']
            saved = store.chat_context('123', f'public:{channel.id}', '20')
            assert not saved['personal_memory']['notes']
            assert 'public meme caption' not in str(saved)
            assert 'doing a lot' not in str(saved)
            assert sum(path == '/chat/mentions/admit' for path, _ in calls) == 1
            assert not any(path == '/agent/admit' for path, _ in calls)
            assert agent.state['channels'] == ['1']
            if not configured_channel:
                assert set(context['actions']) == {'web_search'}
                assert not agent.recent and not agent.active and not agent.bursts.rows
                assert not any(path.startswith(('/controls', '/workspace', '/social')) for path, _ in calls)
                # Only for the privacy revision the ping came in under, then to check it before sending.
                assert sum(path == '/agent/context' for path, _ in calls) == 2
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['old', 'bot', 'webhook', 'foreign_guild', 'foreign_source', 'permission', 'disabled'])
def test_invalid_invitation_fails_before_model(monkeypatch, tmp_path, caplog, failure):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, channel, message, calls, rows = environment(monkeypatch, tmp_path)
        event = message(age=301 if failure == 'old' else 0)
        if failure == 'bot':
            event.author.bot = True
        elif failure == 'webhook':
            event.webhook_id = 999
        elif failure == 'foreign_guild':
            event.guild = SimpleNamespace(id=777)
        elif failure == 'foreign_source':
            changed = copy.copy(event)
            changed.author = SimpleNamespace(id=30, display_name='forged', bot=False)
            rows[event.id] = changed
        elif failure == 'permission':
            channel.permissions_for = lambda _: discord.Permissions.none()
        elif failure == 'disabled':
            agent.config['enabled'] = False
        try:
            await agent.handle(event)
            # Nothing is admitted. A notice reads only the author's privacy revision, before and after its
            # checks, to see if they erase meanwhile.
            reads = {'old': 2, 'foreign_source': 1}.get(failure, 0)
            assert [path for path, _ in calls] == ['/agent/context']*reads
            fri.chat_once.assert_not_awaited()
            # A member's dropped mention says why in the log, by message ID only. Bots and webhooks aren't members.
            reason = {'old': 'stale event', 'bot': None, 'webhook': None,
                      'foreign_guild': 'outside the main server', 'foreign_source': 'message changed',
                      'permission': 'missing permissions', 'disabled': 'fri disabled'}[failure]
            logged = [line for line in caplog.messages if line.startswith('Mention ')]
            if failure == 'old':  # A ping that's still there hears why, never what it said.
                assert event.reply.await_args.args == (NOTICES['stale event'],)
                assert logged == [f'Mention {channel.id}/{event.id} not answered, told why: stale event']
            else:  # Another author's message, or access Fri no longer has, gets nothing.
                event.reply.assert_not_awaited()
                assert logged == ([f'Mention {channel.id}/{event.id} not answered: {reason}'] if reason else [])
            assert not any('what do you think' in line for line in caplog.messages)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_disabled_fri_logs_a_mention_in_its_own_channel(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        agent.config['enabled'] = False
        event = message('<@99> you there?')
        try:
            await agent.handle(event)
            assert not calls
            event.reply.assert_not_awaited()
            assert f'Mention 1/{event.id} not answered: fri disabled' in caplog.messages
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('configured_channel', [False, True])
def test_a_discord_refusal_names_the_mention_it_dropped(monkeypatch, tmp_path, caplog, configured_channel):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(
            monkeypatch, tmp_path, channel_id=1 if configured_channel else 8)
        @asynccontextmanager
        async def typing():
            raise discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'Missing Permissions')
            yield
        channel.typing = typing
        event = message('<@99> you there?')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await fri.on_message(event)
            event.reply.assert_not_awaited()
            assert f'Mention {channel.id}/{event.id} not answered: Forbidden' in caplog.messages
            assert not any('Missing Permissions' in line or 'you there' in line for line in caplog.messages)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_withdrawn_image_share_names_the_mention_it_dropped(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        event = message('<@99> you there?')
        monkeypatch.setattr('fri.fri_image_share.send_reply', AsyncMock(return_value=None))
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert not any(path == '/chat/turn' for path, _ in calls)
            assert f'Mention 1/{event.id} not answered: image share withdrawn' in caplog.messages
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_an_erasure_during_the_burst_wait_names_the_mention(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        event = message('<@99> you there?')
        async def erased(_):
            agent.forget(20)  # "Forget me", the panel, or Gene/Red erasure, during the debounce.
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=erased):
                await agent.handle(event)
            fri.chat_once.assert_not_awaited()
            event.reply.assert_not_awaited()
            assert mention_lines(caplog) == [f'Mention 1/{event.id} not answered: author privacy changed']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('when', ['state read', 'check'])
def test_an_erasure_during_the_owner_settings_check_names_the_mention(monkeypatch, tmp_path, caplog, when):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        event = message('<@99> you sound like an outdated model', user=10)
        api = fri.api
        async def erasing(method, path, payload=None):
            if when == 'state read' and path == '/agent/state':
                agent.forget(10, erased=True)
            return await api(method, path, payload)
        fri.api = erasing
        async def analyze(*args, **kwargs):
            if when == 'check':
                agent.forget(10, erased=True)
            return {'answer': 'other'}, {}, 'completed'
        monkeypatch.setattr('fri.fri_agent.analyze', analyze)
        monkeypatch.setattr('fri.fri_chat.Chat.headers', AsyncMock(return_value={'Authorization': 'Bearer test'}))
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            fri.chat_once.assert_not_awaited()
            event.reply.assert_not_awaited()
            assert mention_lines(caplog) == [f'Mention 1/{event.id} not answered: author privacy changed']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('reset', ['memory check', 'stale cleanup'])
def test_a_cache_reset_while_an_owner_settings_request_waits_keeps_its_preview(monkeypatch, tmp_path, reset):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        event = message('<@99> set heartbeat to 10 minutes', user=10)
        monkeypatch.setattr('fri.fri_chat.Chat.headers', AsyncMock(return_value={'Authorization': 'Bearer test'}))
        checking, resume = asyncio.Event(), asyncio.Event()
        async def analyze(chat, instruction, data, model, effort, schema, **kwargs):
            if 'answer' in schema['properties']:
                checking.set()
                await resume.wait()
                return {'answer': 'settings_change'}, {}, 'completed'
            return {'explanation': '', 'changes': [{'key': 'heartbeat_seconds', 'json_value': '600'}]}, {}, 'completed'
        monkeypatch.setattr('fri.fri_agent.analyze', analyze)
        task = asyncio.create_task(agent.handle(event))
        try:
            await asyncio.wait_for(checking.wait(), 1)
            if reset == 'memory check':  # Read-only, but it clears Fri's cached context for the owner.
                with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                    await agent.handle(message('<@99> what do you remember about me?', user=10))
            else:  # Attention drops a row cached before an older erasure: nothing new was erased.
                agent.recent[1].append({'id': '5', 'user_id': '10', 'name': 'owner', 'text': 'old', 'at': time.time(),
                                        'revision': -1, 'gene_error': False})
                await agent.context_messages(1)
            assert not agent.forgotten.get(10) and not agent.erased.get(10)  # Neither erased anything.
            resume.set()
            await task
            assert event.reply.await_args.kwargs['view'].changes == {'heartbeat_seconds': 600}
        finally:
            resume.set()
            await fri.close()
    asyncio.run(scenario())


def test_only_forget_me_counts_as_an_erasure_for_waiting_settings_requests(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(message('<@99> what do you remember about me?', user=20))
                assert not agent.forgotten.get(20) and not agent.erased.get(20)
                await agent.handle(message('<@99> forget me', user=20))
            assert agent.erased.get(20)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('reactions', [True, False])
def test_a_cooldown_burst_shows_a_low_battery_for_the_mention_it_answers_for(monkeypatch, tmp_path, caplog, reactions):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, controller, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        for _ in range(30):  # Spend the allowance; its one cooldown notice has already shown.
            controller.admit('20', 'public')
        if not reactions:
            channel.permissions_for = lambda _: discord.Permissions(view_channel=True, send_messages=True,
                                                                   read_message_history=True)
        first, second = message('<@99> how is it going?', age=1), message('you around?')
        try:
            task = asyncio.create_task(agent.handle(first))
            while not agent.bursts.has(1, 20):
                await asyncio.sleep(0.01)
            await agent.handle(second)  # Answers for both, without the mention's invitation.
            await task
            first.reply.assert_not_awaited()
            second.reply.assert_not_awaited()  # Its one text notice already showed.
            first.add_reaction.assert_not_awaited()
            if reactions:  # A low battery on the newest message, instead of silence.
                second.add_reaction.assert_awaited_once_with('🪫')
                assert mention_lines(caplog) == [f'Mention 1/{first.id} not answered, told why: social cooldown']
            else:
                second.add_reaction.assert_not_awaited()
                assert mention_lines(caplog) == [f'Mention 1/{first.id} not answered: social cooldown']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('when', ['before', 'during'])
def test_a_mention_behind_a_running_reply_is_answered_after_it(monkeypatch, tmp_path, caplog, when):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, calls, _ = environment(monkeypatch, tmp_path)
        event = message('<@99> you there?')
        notice = SimpleNamespace(delete=AsyncMock())
        replies = []
        async def reply(text, **kwargs):
            replies.append(text)
            if text == str(ReplyInProgress()):
                fri.busy.discard(20)  # The earlier reply finishes after this mention starts waiting.
                return notice
            return SimpleNamespace(id=event.id+1, content=text)
        event.reply.side_effect = reply
        if when == 'before':
            fri.busy.add(20)  # An earlier reply to the same member (here, its panel) is still running.
        else:  # The panel reply starts after this mention's turn began, right before it talks.
            talk, raced = fri.talk, []
            async def racing(*args, **kwargs):
                if not raced:
                    raced.append(True)
                    fri.busy.add(20)
                    asyncio.get_running_loop().call_later(0.05, fri.busy.discard, 20)
                    raise ReplyInProgress()
                return await talk(*args, **kwargs)
            fri.talk = racing
        try:
            await agent.handle(event)
            assert replies == [str(ReplyInProgress()), 'that meme is doing a lot of work']
            assert any(path == '/chat/turn' for path, _ in calls)
            notice.delete.assert_awaited_once()  # The waiting notice goes once the answer lands.
            assert not mention_lines(caplog)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_message_waiting_behind_a_stuck_reply_hears_why(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        event = message('<@99> you there?', age=QUEUE_SECONDS-0.2)
        event.reply.return_value = SimpleNamespace(delete=AsyncMock())
        fri.busy.add(20)  # An earlier reply that doesn't finish in time.
        try:
            await agent.handle(event)
            fri.chat_once.assert_not_awaited()
            assert [item.args for item in event.reply.await_args_list] == [
                (str(ReplyInProgress()),), (NOTICES['stale event'],)]
            assert mention_lines(caplog) == [
                f'Mention 8/{event.id} not answered, told why: earlier reply still running']
            assert 20 not in agent.turns.holders and 20 not in agent.turns.arrived
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_messages_that_come_in_mid_reply_are_answered_in_order(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        first, second, third = (message(f'fri question {n}') for n in (1, 2, 3))
        started, finish, answered = asyncio.Event(), asyncio.Event(), []
        async def answer(text, context):
            if not started.is_set():
                started.set()
                await finish.wait()
            return ChatResult(status='completed', reply='answer to '+text)
        fri.chat_once.side_effect = answer
        for item in (first, second, third):
            item.reply.side_effect = lambda text, item=item, **_: (answered.append((item.id, text)) or
                                                                  SimpleNamespace(id=item.id+1, content=text,
                                                                                  delete=AsyncMock()))
        pause = asyncio.sleep  # The burst debounce is skipped below; this test's own waits aren't.
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                tasks = [asyncio.create_task(agent.handle(first))]
                await started.wait()
                for item in (second, third):
                    tasks.append(asyncio.create_task(agent.handle(item)))
                    while not any(row[0] == item.id for row in answered):
                        await pause(0.01)  # Its waiting notice.
                finish.set()
                await asyncio.gather(*tasks)
            texts = [(mid, text) for mid, text in answered if text.startswith('answer to')]
            assert texts == [(first.id, 'answer to fri question 1'), (second.id, 'answer to fri question 2'),
                             (third.id, 'answer to fri question 3')]
        finally:
            finish.set()
            await fri.close()
    asyncio.run(scenario())


def test_a_message_waiting_its_turn_is_dropped_if_its_author_erases(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        event = message('<@99> you there?')
        async def reply(text, **kwargs):
            store.personal_control('20', 'disable')  # Erased through Gene or Red while it waits.
            fri.busy.discard(20)
            return SimpleNamespace(delete=AsyncMock())
        event.reply.side_effect = reply
        fri.busy.add(20)
        try:
            await agent.handle(event)
            fri.chat_once.assert_not_awaited()
            assert [item.args for item in event.reply.await_args_list] == [(str(ReplyInProgress()),)]
            assert mention_lines(caplog) == [f'Mention 8/{event.id} not answered: author privacy changed']
            assert 20 not in agent.turns.holders and 20 not in agent.turns.arrived
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('erase', [False, True])
def test_a_reply_that_starts_mid_turn_keeps_the_deadline_and_erasure_checks(monkeypatch, tmp_path, caplog, erase):
    # A panel reply can take the member's busy slot during the turn's own typing request.
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        event = message('<@99> you there?', age=0 if erase else QUEUE_SECONDS-0.3)
        event.reply.return_value = SimpleNamespace(delete=AsyncMock())
        talk = fri.talk
        async def racing(*args, **kwargs):
            if 20 not in fri.busy and not fri.chat_once.await_count:
                fri.busy.add(20)  # The panel reply, which never finishes in time...
                if erase:  # ...or finishes after the member erased through Gene or Red.
                    store.personal_control('20', 'disable')
                    asyncio.get_running_loop().call_later(0.05, fri.busy.discard, 20)
                raise ReplyInProgress()
            return await talk(*args, **kwargs)
        fri.talk = racing
        try:
            await agent.handle(event)
            fri.chat_once.assert_not_awaited()
            texts = [item.args[0] for item in event.reply.await_args_list]
            if erase:  # Not even the waiting notice, once they erased.
                assert texts == []
                assert mention_lines(caplog) == [f'Mention 8/{event.id} not answered: author privacy changed']
            else:
                assert texts == [str(ReplyInProgress()), NOTICES['stale event']]
                assert mention_lines(caplog) == [
                    f'Mention 8/{event.id} not answered, told why: earlier reply still running']
            assert 20 not in agent.turns.holders
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('channel_id', [1, 8])
def test_no_notice_goes_out_after_the_author_erases_through_gene(monkeypatch, tmp_path, caplog, channel_id):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=channel_id)
        event = message()
        async def answer(*_):
            store.personal_control('20', 'disable')  # Not through Fri, so no forget() here.
            return ChatResult(status='completed', reply='a reply built before the erasure')
        fri.chat_once.side_effect = answer
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            event.reply.assert_not_awaited()
            assert not any(path == '/chat/turn' for path, _ in calls)
            assert len(mention_lines(caplog)) == 1
            assert ' not answered: reply withheld: ' in mention_lines(caplog)[0]
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_an_erasure_while_waiting_out_the_queue_sends_no_notice(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        event = message('<@99> you there?', age=QUEUE_SECONDS-0.3)
        async def reply(text, **kwargs):
            store.personal_control('20', 'disable')  # While it waits behind a reply that never ends.
            return SimpleNamespace(delete=AsyncMock())
        event.reply.side_effect = reply
        fri.busy.add(20)
        try:
            await agent.handle(event)
            assert [item.args for item in event.reply.await_args_list] == [(str(ReplyInProgress()),)]
            assert mention_lines(caplog) == [f'Mention 8/{event.id} not answered: author privacy changed']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_slow_check_cannot_let_a_later_message_go_first(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        first, second, third = (message(f'fri question {n}') for n in (1, 2, 3))
        started, finish, slow, answered = asyncio.Event(), asyncio.Event(), asyncio.Event(), []
        async def answer(text, context):
            if not started.is_set():
                started.set()
                await finish.wait()
            return ChatResult(status='completed', reply='answer to '+text)
        fri.chat_once.side_effect = answer
        profile, profiles = agent.profile, []
        async def lagging(channel, user):
            profiles.append(channel)
            if profiles.count(456) == 1 and channel == 456:  # The second message's queue check lags.
                await slow.wait()
            return await profile(channel, user)
        agent.profile = lagging
        for item in (first, second, third):
            item.reply.side_effect = lambda text, item=item, **_: (answered.append((item.id, text)) or
                                                                  SimpleNamespace(id=item.id+1, content=text,
                                                                                  delete=AsyncMock()))
        pause = asyncio.sleep
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                tasks = [asyncio.create_task(agent.handle(first))]
                await started.wait()
                tasks.append(asyncio.create_task(agent.handle(second)))
                while 456 not in profiles:
                    await pause(0.01)
                tasks.append(asyncio.create_task(agent.handle(third)))
                while profiles.count(1) < 3:
                    await pause(0.01)  # The third came in too.
                finish.set()
                await pause(0.05)
                slow.set()
                await asyncio.gather(*tasks)
            texts = [mid for mid, text in answered if text.startswith('answer to')]
            assert texts == [first.id, second.id, third.id]
        finally:
            finish.set()
            slow.set()
            await fri.close()
    asyncio.run(scenario())


def test_turns_follow_send_order_even_while_an_earlier_message_is_held_up():
    from fri.fri_turns import Turns
    fri = SimpleNamespace(busy=set())
    turns = Turns(fri)
    turns.arrive(20, 2)  # E.g. waiting for Fri's previous reply to be saved, in another channel.
    turns.arrive(20, 3)
    assert turns.ready(20, 2) and not turns.ready(20, 3)
    turns.take(20, 2)
    assert not turns.ready(20, 2)
    turns.release(20)
    turns.leave(20, 2)
    assert turns.ready(20, 3) and turns.ready(30, 1)
    fri.busy.add(20)
    assert not turns.ready(20, 3)


def test_a_ping_removed_before_its_waiting_notice_gets_none(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, rows = environment(monkeypatch, tmp_path)
        event = message('<@99> you there?')
        fri.busy.add(20)
        profile = agent.profile
        async def unpinged(channel, user):
            edited = copy.copy(event)
            edited.content, edited.mentions, edited.edited_at = 'you there?', [], dt.datetime.now(dt.timezone.utc)
            rows[event.id] = edited
            return await profile(channel, user)
        agent.profile = unpinged
        try:
            await agent.handle(event)
            event.reply.assert_not_awaited()
            fri.chat_once.assert_not_awaited()
            assert mention_lines(caplog) == [f'Mention 8/{event.id} not answered: reply withheld: message changed']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_controller_outage_entering_the_queue_still_says_so(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        event = message('<@99> you there?')
        fri.busy.add(20)
        agent.profile = AsyncMock(side_effect=ControllerUnavailable('The workshop controller is temporarily unavailable.'))
        try:
            await agent.handle(event)
            assert [item.args for item in event.reply.await_args_list] == [(GLITCH,)]
            assert mention_lines(caplog) == [f'Mention 8/{event.id} not answered, told why: controller unavailable']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('erasure', ['disable', 'clear'])
@pytest.mark.parametrize('channel_id', [1, 8])
def test_an_erasure_during_a_first_attempt_that_found_them_busy_drops_the_retry(
        monkeypatch, tmp_path, caplog, channel_id, erasure):
    # Gene erases once, while a panel reply that started first keeps this attempt waiting.
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=channel_id)
        event = message('<@99> remember the private marker?')
        talk = fri.talk
        async def racing(*args, **kwargs):
            if not racing.raced:
                racing.raced = True
                fri.busy.add(20)
                store.personal_control('20', 'disable') if erasure == 'disable' else store.clear_chats('20')
                asyncio.get_running_loop().call_later(0.05, fri.busy.discard, 20)
                raise ReplyInProgress()
            return await talk(*args, **kwargs)
        racing.raced = False
        fri.talk = racing
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            fri.chat_once.assert_not_awaited()
            assert not any(path == '/chat/turn' for path, _ in calls)
            assert 'private marker' not in json.dumps(store.chat_context('123', f'public:{channel_id}', '20'))
            assert all(item.args[0] == str(ReplyInProgress()) for item in event.reply.await_args_list)
            assert mention_lines(caplog) == [f'Mention {channel_id}/{event.id} not answered: author privacy changed']
            assert 20 not in agent.turns.holders
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_reply_that_ends_during_the_busy_recheck_still_rechecks_erasure(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        event = message('<@99> you there?')
        talk = fri.talk
        async def racing(*args, **kwargs):
            if not racing.raced:
                racing.raced = True
                fri.busy.add(20)
                agent.forget(20)  # E.g. their "forget me" in another channel.
                asyncio.get_running_loop().call_soon(fri.busy.discard, 20)  # Within the invitation recheck.
                raise ReplyInProgress()
            return await talk(*args, **kwargs)
        racing.raced = False
        fri.talk = racing
        try:
            await agent.handle(event)
            fri.chat_once.assert_not_awaited()
            event.reply.assert_not_awaited()
            assert mention_lines(caplog) == [f'Mention 8/{event.id} not answered: author privacy changed']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('erasure', ['local', 'gene'])
def test_an_erasure_during_the_waiting_notice_checks_sends_no_notice(monkeypatch, tmp_path, caplog, erasure):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        event = message('<@99> you there?')
        fri.busy.add(20)
        current = Invitation.current
        async def checking(self, **kwargs):
            result = await current(self, **kwargs)
            if kwargs.get('check_evidence', True):  # The waiting notice's own check, not the first one.
                agent.forget(20) if erasure == 'local' else store.personal_control('20', 'disable')
            return result
        monkeypatch.setattr(Invitation, 'current', checking)
        try:
            await agent.handle(event)
            event.reply.assert_not_awaited()
            fri.chat_once.assert_not_awaited()
            assert mention_lines(caplog) == [f'Mention 8/{event.id} not answered: author privacy changed']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('arrived', [False, True])
def test_an_erasure_during_a_notices_own_checks_silences_it(monkeypatch, tmp_path, caplog, arrived):
    # Already stale as it came in, so no baseline was read yet, or one that went stale while waiting.
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, _, channel, message, _, _ = environment(monkeypatch, tmp_path)
        event = message('<@99> you there?', age=QUEUE_SECONDS-0.3 if arrived else 301)
        if arrived:
            fri.busy.add(20)  # A reply that never ends in time.
            event.reply.return_value = SimpleNamespace(delete=AsyncMock())
        fetch = channel.guild.fetch_roles
        async def roles():
            if fetch.await_count == (2 if arrived else 0):  # The notice's own access check.
                store.personal_control('20', 'disable')
            return await fetch()
        channel.guild.fetch_roles = roles
        try:
            await agent.handle(event)
            fri.chat_once.assert_not_awaited()
            assert all(item.args[0] == str(ReplyInProgress()) for item in event.reply.await_args_list)
            assert len(mention_lines(caplog)) == 1 and ' not answered: ' in mention_lines(caplog)[0]
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_controller_outage_as_a_configured_ping_comes_in_says_so(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        event = message('<@99> you there?')
        agent.profile = AsyncMock(side_effect=ControllerUnavailable('The workshop controller is temporarily unavailable.'))
        try:
            await agent.handle(event)
            assert [item.args for item in event.reply.await_args_list] == [(GLITCH,)]
            fri.chat_once.assert_not_awaited()
            assert mention_lines(caplog) == [f'Mention 1/{event.id} not answered, told why: controller unavailable']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_slow_check_cannot_carry_a_waiting_message_past_its_deadline(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        event = message('<@99> you there?', age=QUEUE_SECONDS-0.2)
        fri.busy.add(20)
        profile = agent.profile
        async def slow(channel, user):
            if slow.calls == 1:  # The check before the waiting notice; the earlier reply ends meanwhile.
                await asyncio.sleep(0.35)
                fri.busy.discard(20)
            slow.calls += 1
            return await profile(channel, user)
        slow.calls = 0
        agent.profile = slow
        try:
            await agent.handle(event)
            fri.chat_once.assert_not_awaited()
            assert [item.args for item in event.reply.await_args_list] == [(NOTICES['stale event'],)]
            assert mention_lines(caplog) == [f'Mention 8/{event.id} not answered, told why: earlier reply still running']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('down', ['admit', 'everything'])
def test_a_controller_outage_at_admission_uses_the_glitch_line(monkeypatch, tmp_path, caplog, down):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        event = message('<@99> you there?')
        api = fri.api
        async def outage(method, path, payload=None):
            if path == '/chat/mentions/admit' or (down == 'everything' and outage.admitting):
                outage.admitting = True
                raise ControllerUnavailable('The workshop controller is temporarily unavailable.')
            return await api(method, path, payload)
        outage.admitting = False
        fri.api = outage
        try:
            await agent.handle(event)
            assert [item.args for item in event.reply.await_args_list] == [(GLITCH,)]
            fri.chat_once.assert_not_awaited()
            assert mention_lines(caplog) == [f'Mention 8/{event.id} not answered, told why: controller unavailable']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('channel_id', [1, 8])
def test_a_forget_during_the_arrival_read_still_counts_as_an_erasure(monkeypatch, tmp_path, caplog, channel_id):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=channel_id)
        event = message('<@99> you there?')
        fri.busy.add(20)  # A reply that never ends, so this one would wait.
        profile = agent.profile
        async def forgetting(channel, user):
            if not forgetting.done:
                forgetting.done = True
                agent.forget(20)  # "forget me" elsewhere, while this read is in flight.
            return await profile(channel, user)
        forgetting.done = False
        agent.profile = forgetting
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            event.reply.assert_not_awaited()
            fri.chat_once.assert_not_awaited()
            assert mention_lines(caplog) == [f'Mention {channel_id}/{event.id} not answered: author privacy changed']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('read', [1, 3])
def test_a_forget_while_a_revision_read_is_in_flight_still_counts(monkeypatch, tmp_path, caplog, read):
    # Read 1 is the waiting notice's check; read 3 is the stale notice's own last check.
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        event = message('<@99> you there?', age=QUEUE_SECONDS-0.3)
        event.reply.return_value = SimpleNamespace(delete=AsyncMock())
        fri.busy.add(20)
        revision = agent.revision
        async def reading(user):
            reading.calls += 1
            current = await revision(user)
            if reading.calls == read:
                agent.forget(20)  # Lands before the response does.
            return current
        reading.calls = 0
        agent.revision = reading
        try:
            await agent.handle(event)
            fri.chat_once.assert_not_awaited()
            assert [item.args[0] for item in event.reply.await_args_list] == ([] if read == 1 else [str(ReplyInProgress())])
            assert mention_lines(caplog) == [f'Mention 8/{event.id} not answered: ' +
                                             ('author privacy changed' if read == 1 else 'earlier reply still running')]
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('erased', [False, True])
@pytest.mark.parametrize('channel_id', [1, 8])
def test_a_controller_rejection_on_a_ping_gets_a_fixed_line_unless_they_erased(
        monkeypatch, tmp_path, caplog, channel_id, erased):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=channel_id)
        event = message('<@99> you there?')
        api = fri.api
        async def rejecting(method, path, payload=None):
            if path == '/chat/mentions/admit':
                if erased:
                    store.clear_chats('20')
                raise ControllerRejected('This Discord event cannot renew its mention invitation')
            return await api(method, path, payload)
        fri.api = rejecting
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            fri.chat_once.assert_not_awaited()
            texts = [item.args[0] for item in event.reply.await_args_list]
            if erased:
                assert texts == []
                assert mention_lines(caplog) == [
                    f'Mention {channel_id}/{event.id} not answered: check failed: ControllerRejected']
            else:
                assert texts == [CONTEXT_CHANGED]
                assert mention_lines(caplog) == [
                    f'Mention {channel_id}/{event.id} not answered, told why: check failed: ControllerRejected']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('channel_id', [1, 8])
def test_no_low_battery_after_an_erasure_during_admission(monkeypatch, tmp_path, caplog, channel_id):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=channel_id)
        event = message('<@99> you there?')
        api = fri.api
        async def cooldown(method, path, payload=None):
            if path == '/chat/mentions/admit':
                store.clear_chats('20')  # While the cooldown answer is on its way.
                return {'allowed': False, 'notice': False, 'until': time.time()+600}
            return await api(method, path, payload)
        fri.api = cooldown
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            event.add_reaction.assert_not_awaited()
            event.reply.assert_not_awaited()
            assert mention_lines(caplog) == [f'Mention {channel_id}/{event.id} not answered: author privacy changed']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_rejected_revision_read_is_not_an_outage(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        event = message('<@99> you there?')
        agent.profile = AsyncMock(side_effect=ControllerRejected('Channel is outside Fri scope'))
        try:
            await agent.handle(event)
            event.reply.assert_not_awaited()  # Its revision can't be read, so no word at all.
            fri.chat_once.assert_not_awaited()
            assert mention_lines(caplog) == [f'Mention 8/{event.id} not answered: check failed: ControllerRejected']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('when', ['before notice', 'after waking'])
def test_a_controller_that_goes_down_while_a_ping_waits_says_so(monkeypatch, tmp_path, caplog, when):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        event = message('<@99> you there?')
        event.reply.return_value = SimpleNamespace(delete=AsyncMock())
        fri.busy.add(20)
        if when == 'after waking':
            asyncio.get_running_loop().call_later(0.05, fri.busy.discard, 20)
        revision = agent.revision
        async def down(user):
            down.calls += 1
            if when == 'before notice' or down.calls > 1:
                raise ControllerUnavailable('The workshop controller is temporarily unavailable.')
            return await revision(user)
        down.calls = 0
        agent.revision = down
        try:
            await agent.handle(event)
            fri.chat_once.assert_not_awaited()
            texts = [item.args[0] for item in event.reply.await_args_list]
            assert texts == ([GLITCH] if when == 'before notice' else [str(ReplyInProgress()), GLITCH])
            assert mention_lines(caplog) == [f'Mention 8/{event.id} not answered, told why: controller unavailable']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('queued', [False, True])
@pytest.mark.parametrize('channel_id', [1, 8])
def test_an_erasure_during_the_typing_call_stops_the_reply(monkeypatch, tmp_path, caplog, channel_id, queued):
    # Before Fri reads the context its reply is fenced against, queued or not.
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, _, channel, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=channel_id)
        event = message('<@99> remember the private marker?')
        event.reply.return_value = SimpleNamespace(delete=AsyncMock())
        @asynccontextmanager
        async def typing():
            if not typing.erased:
                typing.erased = True
                store.clear_chats('20')
            yield
        typing.erased = False
        channel.typing = typing
        if queued:
            fri.busy.add(20)
            asyncio.get_running_loop().call_later(0.05, fri.busy.discard, 20)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            fri.chat_once.assert_not_awaited()
            assert not any(path == '/chat/turn' for path, _ in calls)
            assert all(item.args[0] == str(ReplyInProgress()) for item in event.reply.await_args_list)
            assert mention_lines(caplog) == [f'Mention {channel_id}/{event.id} not answered: author privacy changed']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['erase', 'unping', 'access'])
def test_the_waiting_notice_checks_run_once_it_can_post(monkeypatch, tmp_path, caplog, change):
    # Another notice to the same member holds the way; the ping changes meanwhile.
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path)
        event = message('<@99> you there?')
        fri.busy.add(20)
        send, sending = fri.waiting_notices.send, asyncio.Event()
        async def queued(item, eligible=None):
            sending.set()
            return await send(item, eligible)
        fri.waiting_notices.send = queued
        lock = fri.waiting_notices.locks[20]
        await lock.acquire()
        try:
            task = asyncio.create_task(agent.handle(event))
            await sending.wait()
            await asyncio.sleep(0.01)
            if change == 'erase':
                store.personal_control('20', 'disable')
            elif change == 'unping':
                edited = copy.copy(event)
                edited.content, edited.mentions = 'you there?', []
                rows[event.id] = edited
            else:
                channel.permissions_for = lambda _: discord.Permissions.none()
            lock.release()
            await asyncio.wait_for(task, 5)
            event.reply.assert_not_awaited()
            fri.chat_once.assert_not_awaited()
            assert len(mention_lines(caplog)) == 1 and ' not answered: ' in mention_lines(caplog)[0]
        finally:
            if lock.locked():
                lock.release()
            await fri.close()
    asyncio.run(scenario())


def test_no_dm_low_battery_after_an_erasure_during_the_membership_check(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, _, _, _, _ = environment(monkeypatch, tmp_path)
        agent.config['support_dms'] = True
        since = (agent.forgotten.get(20, 0), await agent.revision(20))
        async def member(*_, **__):
            store.clear_chats('20')
            return SimpleNamespace(id=20)
        agent.member = member
        event = SimpleNamespace(id=1, guild=None, author=SimpleNamespace(id=20), add_reaction=AsyncMock())
        try:
            assert await agent.recharging(event, True, since) is False
            event.add_reaction.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_forget_me_ping_hears_about_an_outage(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        event = message('<@99> forget me')
        agent.profile = AsyncMock(side_effect=ControllerUnavailable('The workshop controller is temporarily unavailable.'))
        try:
            await agent.handle(event)
            assert [item.args for item in event.reply.await_args_list] == [(GLITCH,)]
            assert mention_lines(caplog) == [f'Mention 1/{event.id} not answered, told why: controller unavailable']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['outage', 'deadline'])
def test_a_ping_that_waited_for_a_save_still_gets_its_checks(monkeypatch, tmp_path, caplog, failure):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        event = message('<@99> you there?', age=QUEUE_SECONDS-0.2 if failure == 'deadline' else 0)
        saved = agent.recording[(1, 20)] = asyncio.Event()  # An earlier reply is still being saved.
        asyncio.get_running_loop().call_later(0.35, saved.set)
        if failure == 'outage':
            agent.revision = AsyncMock(side_effect=ControllerUnavailable('The workshop controller is temporarily unavailable.'))
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            fri.chat_once.assert_not_awaited()
            expected = GLITCH if failure == 'outage' else NOTICES['stale event']
            assert [item.args for item in event.reply.await_args_list] == [(expected,)]
            assert mention_lines(caplog) == [f'Mention 1/{event.id} not answered, told why: ' +
                                             ('controller unavailable' if failure == 'outage' else 'earlier reply still running')]
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_dm_outside_the_queue_keeps_its_controller_error(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, _, _, _ = environment(monkeypatch, tmp_path)
        agent.profile = AsyncMock(side_effect=ControllerUnavailable('The workshop controller is temporarily unavailable.'))
        mid = discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc))
        event = SimpleNamespace(id=mid, guild=None, author=SimpleNamespace(id=20, display_name='member', bot=False),
                                channel=SimpleNamespace(id=5), reply=AsyncMock())
        try:
            await agent.take_turn(event, 'hey', True)
            assert [item.args for item in event.reply.await_args_list] == [('the workshop controller is temporarily unavailable.',)]
            fri.chat_once.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['fri still there?', '<@99> still there?'])
def test_a_stale_cached_row_does_not_drop_a_message_sent_after_the_erasure(monkeypatch, tmp_path, caplog, text):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        earlier = message('fri remember the private marker', age=30)
        earlier.reply.return_value = SimpleNamespace(id=earlier.id+1, content='got it')
        event = message(text)
        event.reply.return_value = SimpleNamespace(id=event.id+1, content='still here')
        fri.chat_once.return_value = ChatResult(status='completed', reply='still here')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(earlier)
                assert agent.recent[1]  # Cached under the revision before the erasure...
                store.clear_chats('20')  # ...which they then clear through Gene, not Fri.
                await agent.handle(event)
            assert [item.args for item in event.reply.await_args_list] == [('still here',)]
            assert 'private marker' not in str(fri.chat_once.await_args.args[1])
            assert not mention_lines(caplog)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('channel_id,erasure', [(1, 'local'), (8, 'local'), (8, 'gene')])
def test_an_erasure_during_a_pings_last_checks_stops_the_reply(monkeypatch, tmp_path, caplog, channel_id, erasure):
    # The last Discord read after the controller's checks: the ping in a configured channel, where
    # Gene's grant fence catches its own erasures, and a nearby message outside one.
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=channel_id)
        nearby = message('another member nearby', user=30, age=60)
        event = message()
        last, seen = event if channel_id == 1 else nearby, []
        def fetch(mid):
            if mid == last.id and fri.chat_once.await_count:
                seen.append(mid)
                if len(seen) == 2:  # Fri's own last look, after talk's.
                    agent.forget(20) if erasure == 'local' else store.clear_chats('20')
            return rows[mid]
        channel.fetch_message.side_effect = fetch
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert len(seen) >= 2
            event.reply.assert_not_awaited()
            assert not any(path == '/chat/turn' for path, _ in calls)
            assert mention_lines(caplog) == [f'Mention {channel_id}/{event.id} not answered: author privacy changed']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_dm_erased_during_its_debounce_is_not_answered(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, _, message, calls, _ = environment(monkeypatch, tmp_path)
        agent.config['support_dms'] = True
        agent.member = AsyncMock(return_value=SimpleNamespace(id=20))
        event = message('remember the private marker?')
        event.guild = None
        async def debounce(_):
            store.clear_chats('20')  # Through Gene, while Fri waits to see if more is coming.
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=debounce):
                await agent.handle(event)
            fri.chat_once.assert_not_awaited()
            event.reply.assert_not_awaited()
            assert not any(path == '/chat/turn' for path, _ in calls)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_burst_does_not_carry_messages_from_before_an_erasure(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        first, second = message('<@99> remember the private marker'), message('fri anyway, hi')
        second.reply.return_value = SimpleNamespace(id=second.id+1, content='hi')
        fri.chat_once.return_value = ChatResult(status='completed', reply='hi')
        debounced, real_sleep = asyncio.Event(), asyncio.sleep
        async def debounce(_):
            await debounced.wait()
        async def grouped(event):
            while agent.bursts.group(event) is None:
                await real_sleep(0.01)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=debounce):
                tasks = [asyncio.create_task(agent.handle(first))]
                await asyncio.wait_for(grouped(first), 5)
                store.clear_chats('20')  # Through Gene, between the two.
                tasks.append(asyncio.create_task(agent.handle(second)))
                await asyncio.wait_for(grouped(second), 5)
                debounced.set()
                await asyncio.wait_for(asyncio.gather(*tasks), 5)
            fri.chat_once.assert_awaited_once()
            assert 'private marker' not in str(fri.chat_once.await_args)
            assert [item.args for item in second.reply.await_args_list] == [('hi',)]
            first.reply.assert_not_awaited()
            assert mention_lines(caplog) == [f'Mention 1/{first.id} not answered: author privacy changed']
        finally:
            debounced.set()
            await fri.close()
    asyncio.run(scenario())


def test_a_privacy_control_in_a_burst_is_carried_out_and_the_rest_answered(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        control, after = message('fri forget me'), message('<@99> lol ok')
        control.reply.return_value = SimpleNamespace(id=control.id+1, content='done')
        after.reply.return_value = SimpleNamespace(id=after.id+1, content='ok')
        fri.chat_once.return_value = ChatResult(status='completed', reply='ok')
        debounced, real_sleep = asyncio.Event(), asyncio.sleep
        async def debounce(_):
            await debounced.wait()
        async def grouped(event):
            while agent.bursts.group(event) is None:
                await real_sleep(0.01)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=debounce):
                tasks = [asyncio.create_task(agent.handle(control))]
                await asyncio.wait_for(grouped(control), 5)
                tasks.append(asyncio.create_task(agent.handle(after)))
                await asyncio.wait_for(grouped(after), 5)
                debounced.set()
                await asyncio.wait_for(asyncio.gather(*tasks), 5)
            assert control.reply.await_args.args[0].startswith('done. i erased our saved chats')
            assert not (await agent.profile(1, 20))['enabled']
            assert 'forget me' not in str(fri.chat_once.await_args_list)
        finally:
            debounced.set()
            await fri.close()
    asyncio.run(scenario())


def test_showing_their_memory_keeps_what_they_sent_before_waiting(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        waiting = message('<@99> you there?')
        waiting.reply.return_value = SimpleNamespace(id=waiting.id+1, content='here', delete=AsyncMock())
        fri.chat_once.return_value = ChatResult(status='completed', reply='here')
        fri.busy.add(20)  # A reply to them that's still going.
        real_sleep = asyncio.sleep
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                task = asyncio.create_task(agent.handle(waiting))
                while not waiting.reply.await_count:  # Told to hold on.
                    await real_sleep(0.01)
                show = message('fri what do you remember about me?')
                show.reply.return_value = SimpleNamespace(id=show.id+1, content='memory')
                await agent.handle(show)
                assert show.reply.await_args.args[0].startswith('Memory is on.')
                fri.busy.discard(20)
                await asyncio.wait_for(task, 5)
            assert [item.args[0] for item in waiting.reply.await_args_list] == [str(ReplyInProgress()), 'here']
            assert not mention_lines(caplog)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_stale_ping_redelivered_after_its_record_expires_is_told_once(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        event = message(age=301)
        try:
            await agent.handle(event)
            agent.delivered.clear()  # By age or count.
            await agent.handle(event)
            assert [item.args for item in event.reply.await_args_list] == [(NOTICES['stale event'],)]
            assert mention_lines(caplog) == [f'Mention 8/{event.id} not answered, told why: stale event']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('member', [True, False])
def test_a_dm_low_battery_needs_a_current_member(monkeypatch, tmp_path, member):
    async def scenario():
        fri, agent, _, _, _, _, _, _ = environment(monkeypatch, tmp_path)
        agent.config['support_dms'] = True
        agent.member = AsyncMock(return_value=SimpleNamespace(id=20) if member else None)
        event = SimpleNamespace(id=1, guild=None, author=SimpleNamespace(id=20), add_reaction=AsyncMock())
        try:
            assert await agent.recharging(event, True) is member
            agent.member.assert_awaited_once_with(20, fresh=True)
            assert event.add_reaction.await_count == int(member)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_mention_in_an_overlong_burst_says_why(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        first, rest = message('<@99> hey', age=1), [message(f'and {n}') for n in range(5)]
        try:
            tasks = []
            for event in (first, *rest[:-1]):
                tasks.append(asyncio.create_task(agent.handle(event)))
                while not agent.bursts.group(event):
                    await asyncio.sleep(0.01)
            await agent.handle(rest[-1])  # The sixth: the burst is dropped, and this one says so.
            await asyncio.gather(*tasks)
            first.reply.assert_not_awaited()
            assert 'at most five messages' in rest[-1].reply.await_args.args[0]
            fri.chat_once.assert_not_awaited()
            assert mention_lines(caplog) == [f'Mention 1/{first.id} not answered: burst too long']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_burst_that_fails_names_its_earlier_mention(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        @asynccontextmanager
        async def typing():
            raise discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'Missing Permissions')
            yield
        channel.typing = typing
        first, second = message('<@99> hey', age=1), message('you around?')
        try:
            task = asyncio.create_task(agent.handle(first))
            while not agent.bursts.has(1, 20):
                await asyncio.sleep(0.01)
            with pytest.raises(discord.Forbidden):
                await agent.handle(second)
            await task
            assert mention_lines(caplog) == [f'Mention 1/{first.id} not answered: Forbidden']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('cancelled', ['last', 'earlier'])
def test_a_cancelled_burst_wait_settles_every_ping_once(monkeypatch, tmp_path, caplog, cancelled):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        first, second = message('<@99> hey', age=1), message('<@99> you around?')
        try:
            tasks = []
            for event in (first, second):
                tasks.append(asyncio.create_task(agent.handle(event)))
                while not agent.bursts.group(event):
                    await asyncio.sleep(0.01)
            tasks[1 if cancelled == 'last' else 0].cancel()  # E.g. a shutdown, during the burst wait.
            await asyncio.gather(*tasks, return_exceptions=True)
            if cancelled == 'last':  # Nothing will answer the burst now.
                fri.chat_once.assert_not_awaited()
                assert mention_lines(caplog) == [f'Mention 1/{event.id} not answered: CancelledError'
                                                 for event in (first, second)]
            else:  # The last message still answers for the whole burst.
                second.reply.assert_awaited_once()
                assert mention_lines(caplog) == []
            assert agent.misses == {}
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('forgotten', [False, True])
def test_a_redelivered_ping_is_logged_once(monkeypatch, tmp_path, caplog, forgotten):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        agent.config['enabled'] = False
        event = message('<@99> you there?')
        try:
            await agent.handle(event)
            if forgotten:  # Even once the delivered record has let it go, by age or count.
                agent.delivered.clear()
            await agent.handle(event)  # Discord can deliver an event again, e.g. after a resume.
            assert mention_lines(caplog) == [f'Mention 1/{event.id} not answered: fri disabled']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('forgotten', [False, True])
@pytest.mark.parametrize('when', ['profile', 'burst', 'reply', 'cancelled'])
def test_a_redelivery_while_the_first_copy_is_handled_is_ignored(monkeypatch, tmp_path, caplog, when, forgotten):
    # Discord can deliver the same event twice. The first copy, or the burst holding it, answers.
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        event, later = message('<@99> hey', age=1), message('you around?')
        held, release, profile = asyncio.Event(), asyncio.Event(), agent.profile
        async def slow_profile(*args):
            if when == 'profile' and not held.is_set():
                held.set()
                await release.wait()
            return await profile(*args)
        agent.profile = slow_profile
        async def slow_reply(content, **kwargs):
            held.set()
            await release.wait()
            return SimpleNamespace(id=later.id+1, content=content)
        later.reply.side_effect = slow_reply
        tasks = [asyncio.create_task(agent.handle(event))]
        try:
            if when == 'profile':
                await asyncio.wait_for(held.wait(), 5)
            else:
                while not agent.bursts.group(event):
                    await asyncio.sleep(0.01)
            if when == 'reply':  # The burst's last message is replying for both.
                tasks.append(asyncio.create_task(agent.handle(later)))
                await asyncio.wait_for(held.wait(), 5)
            if forgotten:  # Pushed out of the delivered record (by age or count) while still in flight.
                agent.delivered.clear()
            await asyncio.wait_for(agent.handle(copy.copy(event)), 1)  # Ignored at once.
            if when == 'cancelled':
                tasks[0].cancel()
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            if when == 'cancelled':
                event.reply.assert_not_awaited()
                assert mention_lines(caplog) == [f'Mention 1/{event.id} not answered: CancelledError']
            else:
                (later if when == 'reply' else event).reply.assert_awaited_once()
                assert fri.chat_once.await_count == 1 and mention_lines(caplog) == []
                if when == 'reply':
                    event.reply.assert_not_awaited()
            assert agent.misses == {}
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


def test_a_burst_message_redelivered_after_its_answer_is_ignored(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        first, second = message('<@99> hey', age=1), message('you around?')
        try:
            await asyncio.gather(agent.handle(first), agent.handle(second))
            second.reply.assert_awaited_once()
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(copy.copy(first))  # Its own invitation is unspent, but it was answered.
            first.reply.assert_not_awaited()
            assert fri.chat_once.await_count == 1 and mention_lines(caplog) == []
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_an_answered_mention_logs_nothing(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        alone, first, second = message('<@99> hey', age=2), message('<@99> also', age=1), message('you around?')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(alone)
            alone.reply.assert_awaited_once()
            await asyncio.gather(agent.handle(first), agent.handle(second))  # One reply answers both.
            second.reply.assert_awaited_once()
            assert mention_lines(caplog) == [] and agent.misses == {}
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('configured_channel', [False, True])
def test_nicknamed_members_mention_matches_its_fetched_copy(monkeypatch, tmp_path, configured_channel):
    # The gateway event carries the author's server member, so it shows their nickname. A REST
    # fetch has no member data and Fri caches no members, so it shows their global name.
    async def scenario():
        fri, agent, _, _, channel, message, calls, rows = environment(
            monkeypatch, tmp_path, channel_id=1 if configured_channel else 8)
        event = message('<@99> you there?')
        event.author = SimpleNamespace(id=20, display_name='server nickname', bot=False)
        fetched = copy.copy(event)
        fetched.author = SimpleNamespace(id=20, display_name='global name', bot=False)
        rows[event.id] = fetched
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            event.reply.assert_awaited_once()
            assert event.reply.await_args.args[0] == 'that meme is doing a lot of work'
            assert sum(path == '/chat/mentions/admit' for path, _ in calls) == 1
            assert sum(path == '/chat/turn' for path, _ in calls) == 1
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_nicknamed_members_burst_matches_its_fetched_copies(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, _, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        first, second = message('fri yo', age=1), message('you around?')
        for event in (first, second):
            event.author = SimpleNamespace(id=20, display_name='server nickname', bot=False)
            fetched = copy.copy(event)
            fetched.author = SimpleNamespace(id=20, display_name='global name', bot=False)
            rows[event.id] = fetched
        try:
            await asyncio.gather(agent.handle(first), agent.handle(second))
            first.reply.assert_not_awaited()
            second.reply.assert_awaited_once()
            assert second.reply.await_args.args[0] == 'that meme is doing a lot of work'
            burst = fri.chat_once.await_args.args[1]['message_burst']
            assert [row['source']['display_name'] for row in burst] == ['server nickname']*2
            saved = store.chat_context('123', 'public:1', '20')['turns'][-1]
            assert [row['source']['id'] for row in saved['burst']] == [str(first.id), str(second.id)]
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['privacy', 'source_edit', 'permission', 'settings'])
def test_revocation_during_answer_never_sends_or_saves_context(monkeypatch, tmp_path, caplog, change):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, controller, channel, message, calls, rows = environment(monkeypatch, tmp_path)
        previous = message('another member private phrase', user=30, age=60)
        event = message()
        async def answer(*_):
            if change == 'privacy':
                store.personal_control('30', 'disable')
            elif change == 'source_edit':
                edited = copy.copy(previous)
                edited.content = 'changed'
                rows[previous.id] = edited
            elif change == 'permission':
                channel.permissions_for = lambda _: discord.Permissions.none()
            else:
                controller.update({'voice_notes': 'changed'}, 1, '10')
            return ChatResult(status='completed', reply='another member private phrase')
        fri.chat_once.side_effect = answer
        try:
            await agent.handle(event)
            assert not any(path == '/chat/turn' for path, _ in calls)
            reason = {'privacy': 'check failed: ControllerRejected', 'source_edit': 'nearby message changed',
                      'permission': 'missing permissions', 'settings': 'check failed: ControllerRejected'}[change]
            if change == 'permission':  # Fri can't answer there any more, so it doesn't.
                event.reply.assert_not_awaited()
                assert mention_lines(caplog) == [
                    f'Mention {channel.id}/{event.id} not answered: reply withheld: {reason}']
            else:  # A fixed notice, never the reply or what it drew on.
                assert [item.args for item in event.reply.await_args_list] == [(CONTEXT_CHANGED,)]
                assert mention_lines(caplog) == [
                    f'Mention {channel.id}/{event.id} not answered, told why: reply withheld: {reason}']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_outside_invitation_preserves_cooldown_and_blocks_all_nonweb_actions(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path)
        event = message()
        try:
            invitation = await Invitation.create(agent, event)
            await invitation.admit()
            actions = Actions(agent, 20, 8, True)
            actions.invitation, actions.event, actions.scope = invitation, event, invitation.granted_scope()
            actions.message = 'what do you think?'
            for kind in ('gene_command', 'gene_diagnose', 'prepare_post', 'preview_voice', 'read_recent', 'image_search', 'reminder_prepare'):
                assert 'blocked' in await actions.execute({'kind': kind})
            events = [message() for _ in range(20)]
            for item in events:
                await agent.handle(item)
            assert fri.chat_once.await_count < 20
            assert any(e.reply.await_args and 'recharge' in e.reply.await_args.args[0] for e in events)
            later = message()  # The notice shows once; later pings in the cooldown get a low battery.
            await agent.handle(later)
            later.reply.assert_not_awaited()
            later.add_reaction.assert_awaited_once_with('🪫')
            assert f'Mention 8/{later.id} not answered, told why: social cooldown' in caplog.messages
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('current_request', ['show a random sfw image featuring hatsune miku',
    'add a stopwatch command', 'save a draft called weekend plan', 'can you make a stopwatch command',
    'please add a command that flips a coin', 'can you please save a draft called weekend plan',
    'organize a game night tomorrow at 7pm'])
@pytest.mark.parametrize('has_surrounding', [False, True])
def test_configured_complete_requests_keep_existing_capabilities(monkeypatch, tmp_path, current_request, has_surrounding):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        if has_surrounding:
            message('an unrelated conversation', user=30, age=20)
        event = message('<@99> '+current_request)
        draft = 'Add a stopwatch command for the server.' if not current_request.startswith('show') else None
        fri.chat_once.return_value = ChatResult(status='completed', reply='here’s the draft' if draft else 'checking that', draft=draft)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            context = fri.chat_once.await_args.args[1]
            assert not context['invited_context']
            assert {'work_save', 'plan_prepare'} <= set(context['actions']) and context['requests_enabled']
            if current_request.startswith('show'):
                assert 'image_search' in context['actions']
            if draft:
                assert event.reply.await_args.kwargs['view'] is not None
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['optout', 'ambiguous', 'reference', 'old'])
def test_context_images_keep_privacy_age_reference_and_ambiguity_limits(monkeypatch, tmp_path, mode):
    async def scenario():
        fri, agent, store, _, channel, message, _, _ = environment(monkeypatch, tmp_path)
        meme = message('caption is not proof of image contents', user=30,
            age=3*86400 if mode == 'reference' else 31*60 if mode == 'old' else 60,
            attachments=[attachment(channel.id)])
        if mode == 'optout':
            store.personal_control('30', 'disable')
        elif mode == 'ambiguous':
            message('a second image', user=40, age=30, attachments=[attachment(channel.id)])
        event = message('<@99> you support the protests in france')
        if mode == 'reference':
            event.reference = SimpleNamespace(message_id=meme.id, channel_id=channel.id)
        loader = AsyncMock(return_value='data:image/png;base64,aGVsbG8=')
        monkeypatch.setattr('fri.fri_images.load_image', loader)
        try:
            await agent.handle(event)
            context = fri.chat_once.await_args.args[1]
            assert bool(context.get('images')) == (mode == 'reference')
            assert context['ambiguous_context_images'] == (mode == 'ambiguous')
            if mode == 'reference':
                assert context['image_provenance']['user_id'] == '30'
            elif mode != 'ambiguous':
                assert not context['invited_context']
            event.reply.assert_awaited_once()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_public_research_and_later_sources_work_without_exporting_surroundings(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, calls, _ = environment(monkeypatch, tmp_path)
        message('a private member detail must not become a search', user=30, age=60)
        question = 'what is the current Python release?'
        event = message('<@99> '+question)
        receipt = {'kind': 'web_search', 'query': question, 'answer': 'public evidence',
            'sources': ['https://www.python.org/downloads/'], 'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        lookup = AsyncMock(return_value=(receipt, {}, 'completed'))
        monkeypatch.setattr('fri.fri_actions.lookup', lookup)
        fri.chat_once.side_effect = [ChatResult(status='completed', action={'kind': 'web_search', 'query': question}),
                                    ChatResult(status='completed', reply='public evidence'),
                                    ChatResult(status='completed', action={'kind': 'web_sources', 'query': ''}),
                                    ChatResult(status='completed', reply='here’s the source')]
        try:
            await agent.handle(event)
            lookup.assert_awaited_once()
            assert lookup.await_args.args[1] == question
            assert sum(path == '/chat/research/start' for path, _ in calls) == 1
            sources = message('<@99> sources?')
            await agent.handle(sources)
            sources.reply.assert_awaited_once()
            assert 'https://www.python.org/downloads/' in sources.reply.await_args.args[0]
            assert 'web_sources' in fri.chat_once.await_args_list[2].args[1]['actions']
            assert lookup.await_count == 1
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('response_kind', ['react', 'quiet'])
def test_provider_response_has_text_only_schema_and_honest_fallback(response_kind):
    async def scenario():
        captured = []
        def transport(request):
            payload = json.loads(request.content)
            captured.append(payload)
            reply = {'response_kind': response_kind, 'reaction': '👍' if response_kind == 'react' else None,
                     'reply': '', 'request_draft': None, 'action': None, 'notes': [], 'allow_closing': False}
            return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(reply)))+'\n\n')
        chat = Chat(SimpleNamespace(access_token=lambda: 'fictional'), httpx.MockTransport(transport))
        result = await chat.respond('what do you think?', {**CONTEXT, 'force_text_reply': True})
        assert result.status == 'completed' and result.response_kind == 'reply' and result.reply
        schema = captured[0]['text']['format']['schema']
        assert schema['properties']['response_kind']['enum'] == ['reply']
        options = json.loads(captured[0]['input'][-1]['content'])['response_options']
        assert options == {'quiet': False, 'reactions': []}
    asyncio.run(scenario())


@pytest.mark.parametrize('channel_id', [1, 8])
def test_current_message_secrets_are_redacted_before_real_provider_request(monkeypatch, tmp_path, channel_id):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=channel_id)
        captured = []
        canary = 'sk-SYNTHETICREVIEWKEY123456789'
        def transport(request):
            captured.append(json.loads(request.content))
            reply = {'response_kind': 'reply', 'reaction': None, 'reply': 'check the key locally',
                     'request_draft': None, 'action': None, 'notes': [], 'allow_closing': False}
            return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(reply)))+'\n\n')
        chat = Chat(SimpleNamespace(access_token=lambda: 'fictional'), httpx.MockTransport(transport))
        fri.chat_once.side_effect = chat.respond
        event = message('<@99> why is '+canary+' rejected?')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert len(captured) == 1 and canary not in json.dumps(captured)
            assert '[REDACTED]' in json.dumps(captured)
            event.reply.assert_awaited_once()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['none', 'privacy', 'source', 'permission'])
def test_on_demand_meme_read_checks_sources_before_reply_and_prevents_persistence(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        meme = message('another member caption', user=30, age=60, attachments=[attachment(1)])
        event = message('<@99> what do you think of that meme?')
        contexts = []
        async def respond(_, context):
            contexts.append(context)
            if len(contexts) == 1:
                assert not context['invited_context'] and not context.get('images')
                assert 'work_save' in context['actions']
                return ChatResult(status='completed', action={'kind': 'read_recent', 'channel_id': '1'})
            assert context['images'] and context['image_provenance']['user_id'] == '30'
            assert not {'work_save', 'prepare_post', 'preview_voice', 'reminder_prepare', 'plan_prepare', 'image_search'} & set(context['actions'])
            if change == 'privacy':
                store.personal_control('30', 'disable')
            elif change == 'source':
                changed = copy.copy(meme)
                changed.attachments = []
                rows[meme.id] = changed
            elif change == 'permission':
                channel.permissions_for = lambda _: discord.Permissions.none()
            return ChatResult(status='completed', reply='checked that image', draft='Save a derived third-party feature request.')
        fri.chat_once.side_effect = respond
        monkeypatch.setattr('fri.fri_images.load_image', AsyncMock(return_value='data:image/png;base64,aGVsbG8='))
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            assert len(contexts) == 2
            if change == 'none':
                event.reply.assert_awaited_once()
                assert event.reply.await_args.kwargs['view'] is None
                assert 'another member caption' not in str(store.chat_context('123', 'public:1', '20'))
            else:  # Never the reply; a fixed notice only while Fri can still answer there.
                assert [item.args for item in event.reply.await_args_list] == (
                    [] if change == 'permission' else [(CONTEXT_CHANGED,)])
                assert not any(path == '/chat/turn' for path, _ in calls)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('prefix', ['fri', '<@99>'])
@pytest.mark.parametrize('utility,expected,user', [
    ('controls', 'music controls:', 20),
    ('what do you remember about me?', 'memory is on', 20),
    ('forget me', 'erased our saved chats', 20),
    ('show usage', 'nothing recorded', 10),
])
def test_configured_local_utilities_keep_addressing_parity_during_cooldown(monkeypatch, tmp_path, prefix, utility, expected, user):
    async def scenario():
        fri, _, _, controller, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        for _ in range(controller.state()['config']['public_turns']+1):
            controller.admit(str(user), 'public', '123')
        event = message(prefix+' '+utility, user=user)
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await fri.on_message(event)
            event.reply.assert_awaited_once()
            assert expected in event.reply.await_args.args[0].lower()
            fri.chat_once.assert_not_awaited()
            paths = [path for path, _ in calls]
            assert not {'/chat/mentions/admit', '/agent/admit'} & set(paths)
            assert ('/chat/operations/admit' in paths) == (utility == 'controls')
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('prefix', ['fri', '<@99>'])
@pytest.mark.parametrize('cancel', [False, True])
def test_configured_accepted_followups_keep_addressing_parity_during_cooldown(monkeypatch, tmp_path, prefix, cancel):
    async def scenario():
        fri, _, store, controller, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        question = 'what is the current Python release?'
        original = message(question, age=60)
        body = origin(goal='web', text=question)
        body['source'].update(id=str(original.id), display_name=original.author.display_name)
        arm(store, body, topic=question)
        for _ in range(controller.state()['config']['public_turns']+1):
            controller.admit('20', 'public', '123')
        event = message(prefix+(' never mind' if cancel else ' the stable one'))
        receipt = {'kind': 'web_search', 'query': question, 'answer': 'Public facts only.',
            'sources': ['https://www.python.org/downloads/'], 'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        lookup = AsyncMock(return_value=(receipt, {}, 'completed'))
        monkeypatch.setattr('fri.fri_actions.lookup', lookup)
        if not cancel:
            fri.chat_once.side_effect = [ChatResult(status='completed', action={'kind': 'web_search', 'query': question}),
                                        ChatResult(status='completed', reply='Public facts only.')]
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await fri.on_message(event)
            event.reply.assert_awaited_once()
            paths = [path for path, _ in calls]
            assert not {'/chat/mentions/admit', '/agent/admit'} & set(paths)
            assert store.chat_context('123', 'public:1', '20')['pending'] is None
            if cancel:
                assert 'dropped that pending' in event.reply.await_args.args[0]
                fri.chat_once.assert_not_awaited()
                lookup.assert_not_awaited()
            else:
                assert paths.count('/chat/operations/admit') == 1
                assert set(fri.chat_once.await_args_list[0].args[1]['actions']) == {'web_search'}
                lookup.assert_awaited_once()
                assert lookup.await_args.args[1] == question
                assert store.chat_context('123', 'public:1', '20')['web_research']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('channel_id,prefix', [(1, 'fri'), (1, '<@99>'), (8, '<@99>')])
def test_new_chat_stays_socially_bounded_after_operational_bypass_fix(monkeypatch, tmp_path, channel_id, prefix):
    async def scenario():
        fri, _, _, controller, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=channel_id)
        for _ in range(controller.state()['config']['public_turns']+1):
            controller.admit('20', 'public', '123')
        event = message(prefix+' how is it going?')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await fri.on_message(event)
            fri.chat_once.assert_not_awaited()
            paths = [path for path, _ in calls]
            assert ('/chat/mentions/admit' if prefix == '<@99>' else '/agent/admit') in paths
            assert '/chat/operations/admit' not in paths
        finally:
            await fri.close()
    asyncio.run(scenario())
