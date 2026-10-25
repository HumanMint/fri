import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import httpx
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app

from compat_tests.test_api import actor, headers
from compat_tests.test_fri_agent import finish, record
from compat_tests.test_fri_agent import setup_agent as setup_agent
from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_runtime import configured, event
from fri.fri_actions import CATCH_UP_CHARS, Actions
from fri.fri_agent import Agent
from fri.fri_chat import SCHEMA, Chat, ChatResult


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


def snowflake(age):
    return discord.utils.time_snowflake(dt.datetime.fromtimestamp(time.time()-age, dt.timezone.utc))


def chat_message(user, text, age):
    mid = snowflake(age)
    return SimpleNamespace(id=mid, author=SimpleNamespace(id=user, bot=user == 99, display_name=f'member {user}'),
        webhook_id=None, content=text, edited_at=None, jump_url=f'https://discord.com/channels/123/1/{mid}',
        created_at=discord.utils.snowflake_time(mid))


def catch_up_channel(channels, reads, rows):
    """Channel 1 holds rows of (user, text, age in seconds), read back like Discord's history API.

    Bot and empty rows look like Discord's. Edit or remove the returned messages to change the channel.
    """
    messages = [chat_message(*row) for row in rows]
    async def history(**kwargs):
        reads.append(kwargs)
        before = getattr(kwargs.get('before'), 'id', None)
        newest = sorted((m for m in messages if m.created_at > kwargs['after'] and (before is None or m.id < before)),
                        key=lambda m: m.id, reverse=True)
        for message in newest[:kwargs['limit']]:
            yield message
    channels[1].history = history
    return messages


def request(age=0, kind='message'):
    """The member's request, stamped with a real snowflake so history's before= bound applies."""
    item = event(channel=1, kind=kind, content='fri what did i miss')
    item.id = snowflake(age)
    return item


CATCH_UP = {'kind': 'read_recent', 'channel_id': '1', 'window': 'since_my_last_message'}


def test_catch_up_reads_what_others_said_since_the_speaker_left(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, reads = harness(monkeypatch, tmp_path)
        async def profile(channel, user):
            return {'enabled': user != 30, 'revision': 0}
        agent.profile.side_effect = profile
        messages = catch_up_channel(channels, reads, [
            (10, 'back', 60), (20, 'wb', 90), (99, 'now playing', 100), (20, 'ash found a modpack', 3600),
            (30, 'opted out', 3700), (40, '', 3800), (20, 'bo blamed lag', 4000), (10, 'gn', 7200),
            (20, 'said before they left', 8000)])
        actions = Actions(agent, 10, 1, True)
        offered = (await actions.available())['read_recent']
        assert 'since_my_last_message' in offered['windows']
        actions.event = request()
        receipt = await actions.execute(CATCH_UP)
        assert [row['text'] for row in receipt['messages']] == ['bo blamed lag', 'ash found a modpack', 'wb']
        assert all(set(row) == {'user_id', 'name', 'at', 'text'} for row in receipt['messages'])
        assert receipt['stopped_at'] == 'your_last_message' and receipt['window'] == 'since_my_last_message'
        assert receipt['your_last_message_at'] == messages[7].created_at.isoformat(timespec='minutes')
        assert receipt['scanned'] == 8 and not receipt['images_inspected']
        assert reads[0]['limit'] == 100 and reads[0]['before'].id == actions.event.id
        assert abs(time.time()-reads[0]['after'].timestamp()-12*3600) < 5
        assert set(actions.revisions) == {(1, 20)} and await actions.valid()
        agent.profile.side_effect = lambda channel, user: {'enabled': True, 'revision': int(user == 20)}
        assert not await actions.valid()
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('rows,stopped,texts', [
    # Never spoke here: everything in the window, up to the time floor.
    ([(20, 'one', 600), (30, 'two', 900)], 'time_limit', ['two', 'one']),
    # Their own messages from the last few minutes are them coming back, not leaving.
    ([(10, 'yo', 30), (20, 'sup', 60), (10, 'back', 120), (20, 'earlier', 1200)], 'time_limit', ['earlier', 'sup']),
    # Nobody but a bot answered their older message, so they didn't leave there either.
    ([(99, 'skipped', 850), (10, 'fri skip', 900), (20, 'earlier', 1200)], 'time_limit', ['earlier']),
    # A busy channel stops at the message cap.
    ([(20, f'line {i}', 60+i) for i in range(150)], 'message_limit', None),
])
def test_catch_up_without_a_departure_says_where_it_stopped(monkeypatch, tmp_path, rows, stopped, texts):
    async def scenario():
        fri, agent, channels, reads = harness(monkeypatch, tmp_path)
        catch_up_channel(channels, reads, rows)
        actions = Actions(agent, 10, 1, True)
        receipt = await actions.execute(CATCH_UP)
        assert receipt['stopped_at'] == stopped and receipt['your_last_message_at'] is None
        assert 'before' not in reads[0]  # No Discord message to anchor on, e.g. a panel turn.
        if texts is None:
            assert receipt['scanned'] == len(receipt['messages']) == 100
        else:
            assert [row['text'] for row in receipt['messages']] == texts
        await fri.close()
    asyncio.run(scenario())


def test_catch_up_window_hangs_off_the_request_not_a_slow_reply(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, reads = harness(monkeypatch, tmp_path)
        # "back" came 280 s before the request, which ran a minute late, so it's still them coming back.
        catch_up_channel(channels, reads, [(20, 'wb', 320), (10, 'back', 340), (20, 'missed event', 1800),
                                           (10, 'gn', 3600)])
        actions = Actions(agent, 10, 1, True)
        actions.event = request(age=60)
        receipt = await actions.execute(CATCH_UP)
        assert [row['text'] for row in receipt['messages']] == ['missed event', 'wb']
        assert receipt['stopped_at'] == 'your_last_message'
        assert abs(time.time()-60-reads[0]['after'].timestamp()-12*3600) < 5
        await fri.close()
    asyncio.run(scenario())


def test_catch_up_keeps_the_dedicated_channel_start_floor(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, reads = harness(monkeypatch, tmp_path)
        catch_up_channel(channels, reads, [(20, 'since fri started', 600)])
        channels[3].history = channels[1].history
        await Actions(agent, 10, 999, False).execute({**CATCH_UP, 'channel_id': '3'})
        assert abs(reads[0]['after'].timestamp()-agent.state['started']) < .001
        await fri.close()
    asyncio.run(scenario())


def test_configured_mention_catch_up_reads_the_channel_not_nearby_context(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, reads = harness(monkeypatch, tmp_path)
        messages = catch_up_channel(channels, reads, [(20, 'ash found a modpack', 600), (10, 'gn', 3600)])
        actions = Actions(agent, 10, 1, True)
        actions.invitation = SimpleNamespace(outside=False, require_current=AsyncMock(),
                                             current=AsyncMock(return_value=True))
        actions.invited_context = AsyncMock(return_value={'kind': 'read_recent', 'messages': []})
        offered = (await actions.available())['read_recent']
        assert offered['limit'] == 12 and 'since_my_last_message' in offered['windows']
        await actions.execute(RECENT)
        actions.invited_context.assert_awaited_once()
        assert not reads
        receipt = await actions.execute(CATCH_UP)
        actions.invited_context.assert_awaited_once()
        assert [row['text'] for row in receipt['messages']] == ['ash found a modpack']
        assert actions.private_evidence and await actions.valid()
        actions.invitation.current.return_value = False
        assert not await actions.valid()
        # The invitation still checks out, but the message the catch-up read was deleted.
        actions.invitation.current.return_value = True
        del messages[0]
        assert not await actions.valid()
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change,still_valid', [
    ('edit', False), ('delete', False),
    ('edit_unread', True),  # The speaker's own message where the read stopped was never shown.
    ('newer', True),  # Messages after the request never enter the read or its recheck.
])
def test_catch_up_withholds_the_reply_when_a_message_it_read_changes(monkeypatch, tmp_path, change, still_valid):
    async def scenario():
        fri, agent, channels, reads = harness(monkeypatch, tmp_path)
        messages = catch_up_channel(channels, reads, [(20, 'wb', 600), (30, 'bo blamed lag', 900), (10, 'gn', 3600)])
        actions = Actions(agent, 10, 1, True)
        actions.event = request(age=300)
        assert len((await actions.execute(CATCH_UP))['messages']) == 2
        assert await actions.valid()
        if change == 'edit':
            messages[1].content, messages[1].edited_at = 'nvm', dt.datetime.now(dt.timezone.utc)
        elif change == 'delete':
            del messages[0]
        elif change == 'edit_unread':
            messages[2].content, messages[2].edited_at = 'gn!!', dt.datetime.now(dt.timezone.utc)
        else:
            messages.append(chat_message(20, 'later', 10))
        assert await actions.valid() is still_valid
        assert all(read['before'].id == actions.event.id for read in reads)
        await fri.close()
    asyncio.run(scenario())


def test_panel_catch_up_stops_at_the_request(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, reads = harness(monkeypatch, tmp_path)
        catch_up_channel(channels, reads, [(20, 'after the request', 30), (20, 'before the request', 120)])
        actions = Actions(agent, 10, 999, False)
        actions.event = request(age=60, kind='interaction')
        receipt = await actions.execute(CATCH_UP)
        assert [row['text'] for row in receipt['messages']] == ['before the request']
        assert reads[0]['before'].id == actions.event.id
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['x' * 300, '😀' * 300], ids=['ascii', 'emoji'])
def test_catch_up_keeps_the_newest_rows_within_a_size_budget(monkeypatch, tmp_path, text):
    async def scenario():
        fri, agent, channels, reads = harness(monkeypatch, tmp_path)
        catch_up_channel(channels, reads, [(20, f'{i} {text}', 60+i) for i in range(100)])
        receipt = await Actions(agent, 10, 1, True).execute(CATCH_UP)
        rows = receipt['messages']
        assert receipt['stopped_at'] == 'size_limit' and 0 < len(rows) < 100
        assert rows[-1]['text'].startswith('0 ') and sum(len(json.dumps(row)) for row in rows) <= CATCH_UP_CHARS
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


RECENT = {'kind': 'read_recent', 'channel_id': '1', 'window': 'recent'}


@pytest.mark.parametrize('offered,reply,action,expected', [
    (True, '', RECENT, 'completed'),
    (True, '', {**RECENT, 'window': 'since_my_last_message'}, 'completed'),
    (False, '', RECENT, 'invalid_output'),
    (True, 'I already did it', RECENT, 'invalid_output'),
    (True, '', {'kind': 'read_recent', 'channel_id': '1'}, 'invalid_output'),
    (True, '', {**RECENT, 'window': 'everything'}, 'invalid_output'),
    (True, '', {'kind': 'shell', 'channel_id': '1'}, 'invalid_output'),
])
def test_action_parser_accepts_only_offered_complete_requests(offered, reply, action, expected):
    async def scenario():
        output = {'reply': reply, 'notes': [], 'request_draft': None, 'action': action}
        def handler(request):
            return httpx.Response(200, text='data: '+json.dumps(response_event('completed', json.dumps(output)))+'\n\n')
        chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler))
        result = await chat.respond('look around', {**CONTEXT, 'actions': {'read_recent': {}} if offered else {}})
        assert result.status == expected
    asyncio.run(scenario())


def test_read_recent_schema_always_names_a_window():
    branches = [branch for branch in SCHEMA['properties']['action']['anyOf']
                if 'read_recent' in branch.get('properties', {}).get('kind', {}).get('enum', [])]
    assert len(branches) == 1 and branches[0]['required'] == ['kind', 'channel_id', 'window']
    assert branches[0]['properties']['window']['enum'] == ['recent', 'since_my_last_message']


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
            await fri.record_turn(result)  # As after a successful Discord delivery.
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
