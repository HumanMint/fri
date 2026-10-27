import asyncio
import copy
import datetime as dt
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_chat import CONTEXT, response_event
from compat_tests.test_fri_runtime import event
from fri.fri_actions import Actions
from fri.fri_chat import Chat, ChatResult
from fri.fri_history_search import MAX_DAYS, bounds, requested, snowflake, valid_action
from fri.fri_runner import run


def query(**changes):
    return {'kind': 'history_search', 'channel_id': '1', 'query': 'played', 'author': '',
            'after': '', 'before': '', 'has': '', 'cursor': '', **changes}


def setup(monkeypatch, tmp_path, *, public=True, text='find old messages about what we played'):
    fri, agent, channels, _ = harness(monkeypatch, tmp_path)
    actions = Actions(agent, 20, 1 if public else 999, public)
    actions.event = event(user=20, channel=actions.channel, guild=123 if public else None, content=text)
    actions.event.id = int(snowflake(time.time()-2))
    actions.event.created_at = dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=2)
    actions.event.webhook_id, actions.event.mentions = None, []
    actions.message = text
    source = actions.event
    rows, messages = [], {}
    for index in range(12):
        at = time.time()-(index+1)*86400
        mid = snowflake(at)
        row = {'id': mid, 'channel_id': '1', 'content': f'played game {index}',
               'author': {'id': '30', 'bot': False}, 'hit': True}
        rows.append(row)
        messages[int(mid)] = SimpleNamespace(id=int(mid), author=SimpleNamespace(
            id=30, bot=False, display_name='Dipper'), content=row['content'], webhook_id=None,
            created_at=dt.datetime.fromtimestamp(at, dt.timezone.utc),
            jump_url='https://discord.com/channels/123/1/'+mid)
    async def fetch(mid):
        return source if mid == source.id else messages[mid]
    for channel in channels.values():
        channel.guild.me = SimpleNamespace(id=99)
        channel.fetch_message = AsyncMock(side_effect=fetch)
    source.channel = channels[1] if public else SimpleNamespace(id=999, fetch_message=AsyncMock(return_value=source))
    async def http(route, *, params):
        if route.path.endswith('/members/search'):
            return [{'nick': 'Dipper', 'user': {'id': '30', 'username': 'dipper', 'bot': False}}]
        return {'messages': [[row] for row in rows if int(params['min_id']) <= int(row['id']) < int(params['max_id'])][:params['limit']]}
    fri.http.request = AsyncMock(side_effect=http)
    fri.api = AsyncMock(return_value={'actions': []})
    return fri, agent, actions, channels, rows, messages


def test_archive_intent_and_date_limits_are_bounded():
    for text in ('what was that game Dipper recommended last month?', 'search chat for watched',
                 'find messages before 2026-01-01', 'look through history for Minecraft',
                 'what did Dipper recommend?', 'what did I say about Minecraft?',
                 'find that game Dipper recommended last month', 'who recommended Minecraft?',
                 'what did we talk about?', 'what did Dipper mention in chat?',
                 'what was that song kev mentioned', 'whats that game jo recommended',
                 'what’s the link sam dropped', 'which was the meme you posted'):
        assert requested(text)
    for text in ('hey fri', 'play Sad Machine', 'do not search chat', '> find old messages',
                 'what do you recommend?', 'what should I say to a friend who is upset?',
                 'which game would you recommend?', 'what do I have to say?',
                 'find recommended movies', 'look up recommended games',
                 'what are the recommended settings for this game?',
                 'what is the recommended way to learn Python?', 'whats the most posted meme format',
                 'what is the most commonly recommended gaming laptop?',
                 'which is the most widely recommended Python tutorial?',
                 'what is the difference between shared and dedicated hosting?',
                 'what is the best dermatologist recommended sunscreen?',
                 'what is the average doctor recommended dose of vitamin D?',
                 'what was that'):
        assert not requested(text)
    assert valid_action(query())
    assert not valid_action(query(cursor='invented'))
    assert not valid_action(query(query='x'*81))
    now = time.time()
    assert bounds('', '', now) == (now-MAX_DAYS*86400, now)
    with pytest.raises(ValueError):
        bounds('2026-99-99', '', now)
    with pytest.raises(ValueError):
        bounds('2026-02-01', '2026-01-01', now)


@pytest.mark.parametrize('text', ['what do you recommend?',
    'what should I say to a friend who is upset?', 'which game would you recommend?',
    'find recommended movies', 'look up recommended games',
    'what are the recommended settings for this game?',
    'what is the recommended way to learn Python?'])
def test_present_day_advice_does_not_offer_or_authorize_archive_search(monkeypatch, tmp_path, text):
    async def scenario():
        fri, _, actions, _, _, _ = setup(monkeypatch, tmp_path, text=text)
        try:
            assert 'history_search' not in await actions.available()
            assert 'blocked' in await actions.execute(query())
            fri.http.request.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_two_pages_keep_scoped_filters_without_skipping_unshown_hits(monkeypatch, tmp_path):
    async def scenario():
        fri, _, actions, _, rows, _ = setup(monkeypatch, tmp_path)
        first = await actions.execute(query())
        assert [r['id'] for r in first['messages']] == [r['id'] for r in rows[:5]]
        assert first['remaining_searches'] == 1 and first['complete_archive'] is False
        second = await actions.execute(query(cursor=first['next_cursor']))
        assert [r['id'] for r in second['messages']] == [r['id'] for r in rows[5:10]]
        assert not second['next_cursor'] and second['remaining_searches'] == 0
        assert 'blocked' in await actions.execute(query(query='watched'))
        calls = fri.http.request.call_args_list
        assert len(calls) == 2 and calls[0].kwargs['params']['channel_id'] == '1'
        assert calls[1].kwargs['params']['max_id'] == rows[4]['id']
        assert actions.private_evidence and await actions.valid()
        for kind in ('gene_command', 'work_save', 'prepare_post', 'reminder_prepare', 'plan_prepare'):
            assert 'blocked' in await actions.execute({'kind': kind})
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['member_optout', 'changed', 'requester_permission', 'bot_permission'])
def test_retrieved_evidence_is_revalidated_before_answer(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, actions, channels, _, messages = setup(monkeypatch, tmp_path)
        result = await actions.execute(query())
        assert len(result['messages']) == 5
        if change == 'member_optout':
            agent.profile.return_value = {'enabled': False, 'revision': 1}
        elif change == 'changed':
            messages[int(result['messages'][0]['id'])].content = 'edited while the model was answering'
        else:
            channels[1].permissions_for = lambda member: SimpleNamespace(
                view_channel=False if change == 'requester_permission' else member.id != 99,
                read_message_history=True)
        assert not await actions.valid()
        await fri.close()
    asyncio.run(scenario())


def test_public_cannot_search_another_channel_but_verified_dm_can(monkeypatch, tmp_path):
    async def scenario():
        fri, _, actions, _, _, _ = setup(monkeypatch, tmp_path)
        assert 'blocked' in await actions.execute(query(channel_id='2'))
        fri.http.request.assert_not_awaited()
        await fri.close()
        fri, _, actions, _, _, _ = setup(monkeypatch, tmp_path, public=False)
        assert 'messages' in await actions.execute(query(channel_id='1'))
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('invalid', ['channel', 'author', 'date', 'oversize', 'duplicate', 'indexing'])
def test_untrusted_search_responses_cannot_expand_scope(monkeypatch, tmp_path, invalid):
    async def scenario():
        fri, _, actions, _, rows, _ = setup(monkeypatch, tmp_path)
        data = copy.deepcopy(rows[:3])
        action = query()
        if invalid == 'channel':
            data[0]['channel_id'] = '2'
        elif invalid == 'author':
            action['author'] = '<@30>'
            actions.message = actions.event.content = 'find old messages from <@30>'
            data[0]['author']['id'] = '40'
        elif invalid == 'date':
            data[0]['id'] = snowflake(time.time()-400*86400)
        elif invalid == 'oversize':
            data = rows
        elif invalid == 'duplicate':
            data.append(data[0])
        response = {'messages': [[row] for row in data]}
        if invalid == 'indexing':
            response = {'code': 110000, 'retry_after': 1}
        fri.http.request = AsyncMock(return_value=response)
        result = await actions.execute(action)
        assert 'error' in result and not result.get('messages')
        await fri.close()
    asyncio.run(scenario())


def test_member_name_resolution_is_explicit_and_bot_optout_messages_are_skipped(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, actions, _, rows, _ = setup(monkeypatch, tmp_path,
            text='what game did Dipper recommend last week?')
        result = await actions.execute(query(author='Dipper'))
        assert result['author_id'] == '30'
        assert fri.http.request.call_args.kwargs['params']['author_id'] == '30'
        await fri.close()
        fri, agent, actions, _, rows, _ = setup(monkeypatch, tmp_path)
        rows[0]['author']['bot'] = True
        rows[1]['webhook_id'] = '123'
        agent.profile.return_value = {'enabled': False, 'revision': 1}
        assert (await actions.execute(query()))['messages'] == []
        await fri.close()
    asyncio.run(scenario())


def test_search_cursor_is_tied_to_exact_filters_and_lifetime(monkeypatch, tmp_path):
    async def scenario():
        fri, _, actions, _, _, _ = setup(monkeypatch, tmp_path)
        first = await actions.execute(query())
        assert 'blocked' in await actions.execute(query(query='watched', cursor=first['next_cursor']))
        actions.history.pages = 0
        assert 'blocked' in await actions.execute(query(cursor=first['next_cursor']))
        await fri.close()
    asyncio.run(scenario())


def test_history_action_schema_and_runner_keep_one_refinement_and_remove_persistence(monkeypatch, tmp_path):
    async def scenario():
        fri, _, actions, _, _, _ = setup(monkeypatch, tmp_path)
        offered = await actions.available()
        assert 'history_search' in offered
        assert 'plan_prepare' in offered
        from fri.fri_social import KINDS
        persistent = {'work_save', 'prepare_post', 'clarify'} | KINDS
        context = {**CONTEXT, 'actions': {**offered, **dict.fromkeys(persistent, {})}}
        calls = []
        async def chat(text, ctx):
            calls.append(ctx)
            assert not persistent & set(ctx['actions'])
            if len(calls) == 1:
                assert 'history_search' in ctx['actions']
                return ChatResult(status='completed', action=query(cursor=ctx['action_result']['next_cursor']))
            assert 'history_search' not in ctx['actions']
            return ChatResult(status='completed', reply='Dipper mentioned game 8.')
        fri.chat_once = chat
        result, receipts, _ = await run(fri, actions, actions.message, context, ChatResult(action=query()))
        assert result.reply and len(receipts) == 2 and await actions.valid()
        body = {'reply': '', 'notes': [], 'request_draft': None, 'action': query()}
        model = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(
            lambda request: httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(body)))+'\n\n')))
        parsed = await model.respond(actions.message, {**CONTEXT, 'actions': offered})
        assert parsed.status == 'completed' and parsed.action == query()
        await fri.close()
    asyncio.run(scenario())
