"""A message withdrawn (edited, deleted, or its author forgotten) while Fri handles it leaves every model
request from then on: attention after its last reads, later turns after an outage, and a turn that
already copied the live view."""
import asyncio
import copy
import datetime as dt
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import httpx
import pytest

from compat_tests.test_fri_chat import response_event
from compat_tests.test_fri_mentions import environment
from fri.feedback import UserFeedbackCheckFailure
from fri.fri_agent import Agent
from fri.fri_chat import Chat
from fri.fri_errors import ControllerUnavailable
from fri.fri_mentions import Invitation

MARKER = 'lavender-bicycle-private'


def model(fri, requests, *, attention=False):
    def handler(request):
        requests.append(json.loads(request.content))
        result = ({'action': 'quiet', 'source_id': '', 'reason': ''} if attention else
                  {'reply': 'okay', 'request_draft': None})
        return httpx.Response(200, text='data: ' + json.dumps(response_event(content=json.dumps(result))) + '\n\n')

    fri.chat = Chat(SimpleNamespace(access_token=lambda: 'synthetic-review'), httpx.MockTransport(handler))
    fri.chat.headers = AsyncMock(return_value={'Authorization': 'Bearer synthetic-review'})


def cache(agent, channel, event):
    agent.recent[channel.id].append({'id': str(event.id), 'user_id': str(event.author.id),
        'name': 'member', 'text': event.content, 'at': time.time(), 'revision': 0, 'gene_error': False})


@pytest.mark.parametrize('operation', ['withdraw', 'forget'])
def test_attention_drops_a_row_withdrawn_during_its_last_profile_read(operation):
    async def scenario():
        channel = SimpleNamespace(id=456)
        requests = []
        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, get_channel=lambda _: channel)
        model(fri, requests, attention=True)
        agent = Agent(fri)
        agent.record = AsyncMock()
        event = SimpleNamespace(id=100, channel=channel, author=SimpleNamespace(id=11), content=MARKER+' help?')
        cache(agent, channel, event)
        waiting, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def profile(*_):
            nonlocal count
            count += 1
            before = {'enabled': True, 'revision': 0}
            if count == 2:  # Callback: live IDs have already been read, profile response is in flight.
                waiting.set()
                await release.wait()
            return before

        agent.profile = profile
        task = asyncio.create_task(agent.attend())
        await asyncio.wait_for(waiting.wait(), 2)
        if operation == 'withdraw':
            agent.withdraw(event)
        else:
            agent.forget(11, erased=True)
        assert not agent.recent[456]
        release.set()
        await asyncio.wait_for(task, 2)
        assert not requests
    asyncio.run(scenario())


def test_attention_rechecks_after_signin():
    async def scenario():
        channel = SimpleNamespace(id=456)
        requests = []
        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, get_channel=lambda _: channel)
        model(fri, requests, attention=True)
        agent = Agent(fri)
        agent.record = AsyncMock()
        agent.profile = AsyncMock(return_value={'enabled': True, 'revision': 0})
        event = SimpleNamespace(id=100, channel=channel, author=SimpleNamespace(id=11), content=MARKER+' help?')
        cache(agent, channel, event)
        waiting, release = asyncio.Event(), asyncio.Event()

        async def headers():
            waiting.set()
            await release.wait()
            return {'Authorization': 'Bearer synthetic-review'}

        fri.chat.headers = headers
        task = asyncio.create_task(agent.attend())
        await asyncio.wait_for(waiting.wait(), 2)
        agent.withdraw(event)
        release.set()
        await asyncio.wait_for(task, 2)
        assert not requests
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['edited', 'deleted'])
def test_an_outage_withdraws_the_changed_message_itself(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        event = message('fri help with '+MARKER)
        original_api = fri.api
        original_fetch = channel.fetch_message.side_effect

        async def unavailable(method, path, payload=None):
            if path == '/chat/context' and payload['user_id'] == '20':
                if change == 'edited':
                    current = copy.copy(event)
                    current.content, current.edited_at = 'never mind', dt.datetime.now(dt.timezone.utc)
                    rows[event.id] = current
                else:
                    def gone(mid):
                        if mid == event.id:
                            raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
                        return original_fetch(mid)
                    channel.fetch_message.side_effect = gone
                raise ControllerUnavailable('controller unavailable')
            return await original_api(method, path, payload)

        fri.api = unavailable
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            event.reply.assert_not_awaited()
            assert not any(row['id'] == str(event.id) for row in agent.recent[1])
            fri.api = original_api
            requests = []
            model(fri, requests)
            fri.chat_once = type(fri).chat_once.__get__(fri)
            later = message('fri can you help me with this?', user=30)
            await fri.talk(123, 1, 30, later.content, 'public', event=later)
            assert len(requests) == 1 and MARKER not in json.dumps(requests)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_an_outage_withdraws_every_changed_message_of_the_burst(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        first = message('fri first withdrawn '+MARKER)
        second = message('fri second withdrawn '+MARKER)
        last = message('fri still here?')
        events = [first, second, last]
        burst = [(time.monotonic(), item, item.content) for item in events]
        for item in events:
            cache(agent, channel, item)
        for item in events[:2]:
            current = copy.copy(item)
            current.content, current.edited_at = 'never mind', dt.datetime.now(dt.timezone.utc)
            rows[item.id] = current
        try:
            await agent.outage(last, events, burst=burst, since=(0, 0))
            live_ids = {row['id'] for row in agent.recent[1]}
            assert str(first.id) not in live_ids and str(second.id) not in live_ids and str(last.id) in live_ids
            requests = []
            model(fri, requests)
            fri.chat_once = type(fri).chat_once.__get__(fri)
            later = message('fri can you help me with this?', user=30)
            await fri.talk(123, 1, 30, later.content, 'public', event=later)
            assert len(requests) == 1 and MARKER not in json.dumps(requests)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_turn_that_copied_the_live_view_drops_a_row_withdrawn_during_signin(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        earlier = message('fri help with '+MARKER)
        cache(agent, channel, earlier)
        later = message('fri can you help me with this?', user=30)
        requests = []
        model(fri, requests)
        fri.chat_once = type(fri).chat_once.__get__(fri)

        async def headers():
            current = copy.copy(earlier)
            current.content, current.edited_at = 'never mind', dt.datetime.now(dt.timezone.utc)
            rows[earlier.id] = current
            assert not await agent.unedited([earlier])
            assert not agent.recent[1]
            return {'Authorization': 'Bearer synthetic-review'}

        fri.chat.headers = headers
        try:
            await fri.talk(123, 1, 30, later.content, 'public', event=later)
            assert len(requests) == 1 and MARKER not in json.dumps(requests)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_queued_message_edited_before_an_outage_leaves_the_live_view(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        event = message('fri help with '+MARKER)
        waiting = asyncio.Event()

        async def notice(text, **kwargs):
            waiting.set()
            return SimpleNamespace(id=event.id+1, content=text, delete=AsyncMock())

        event.reply.side_effect = notice
        fri.busy.add(20)
        original_revision = agent.revision
        task = None
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                task = asyncio.create_task(agent.handle(event))
                await asyncio.wait_for(waiting.wait(), 2)
                current = copy.copy(event)
                current.content, current.edited_at = 'never mind', dt.datetime.now(dt.timezone.utc)
                rows[event.id] = current
                agent.revision = AsyncMock(side_effect=ControllerUnavailable('controller unavailable'))
                fri.busy.discard(20)
                await asyncio.wait_for(task, 2)
            assert not any(row['id'] == str(event.id) for row in agent.recent[1])
            agent.revision = original_revision
            requests = []
            model(fri, requests)
            fri.chat_once = type(fri).chat_once.__get__(fri)
            later = message('fri can you help me with this?', user=30)
            await fri.talk(123, 1, 30, later.content, 'public', event=later)
            assert len(requests) == 1 and MARKER not in json.dumps(requests)
        finally:
            fri.busy.discard(20)
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


def test_all_changed_burst_rows_leave_while_fri_echo_and_unchanged_rows_stay():
    async def scenario():
        fri = SimpleNamespace(get_channel=lambda _: None, guild_id=123)
        agent = Agent(fri)
        channel = SimpleNamespace(id=456)
        events = [SimpleNamespace(id=mid, channel=channel, guild=None, content='original',
                  author=SimpleNamespace(id=11)) for mid in (100, 101, 102)]
        for event in events:
            cache(agent, channel, event)
        agent.recent[456].append({'id': '103', 'user_id': '99', 'fri': True,
            'provenance': [{'user_id': '11', 'revision': 0}], 'at': time.time(),
            'gene_error': False, 'text': 'hello'})
        current = {event.id: SimpleNamespace(content='original' if event.id == 102 else 'edited',
                   edited_at=None) for event in events}
        channel.fetch_message = AsyncMock(side_effect=lambda mid: current[mid])
        assert not await agent.unedited(events)
        assert [row['id'] for row in agent.recent[456]] == ['102', '103']
        agent.profile = AsyncMock(return_value={'enabled': True, 'revision': 0})
        # Channel absent from Discord's cache; Fri echo has no own revision.
        records = await agent.context_messages(456)
        assert await agent.unwithdrawn(456, records)
        agent.profile.return_value = {'enabled': False, 'revision': 1}
        assert not await agent.unwithdrawn(456, records)
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['edited', 'deleted'])
def test_queued_burst_ending_in_ping_purges_earlier_withdrawn_message(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, _, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        first = message('fri help with '+MARKER)
        last = message('<@99> still there?')
        burst = [(time.monotonic(), item, item.content) for item in [first, last]]
        for item in [first, last]:
            cache(agent, channel, item)
        invitation = await Invitation.create(agent, last)
        waiting = asyncio.Event()
        async def notice(*args, **kwargs):
            waiting.set()
            return SimpleNamespace(id=last.id+1, content='hold on', delete=AsyncMock())
        last.reply.side_effect = notice
        fri.busy.add(20)
        task = None
        try:
            task = asyncio.create_task(agent.respond(last, last.content, burst=burst, invitation=invitation,
                                                    forgotten=0, revision=0))
            await asyncio.wait_for(waiting.wait(), 2)
            if change == 'edited':
                edited = copy.copy(first)
                edited.content = 'never mind'
                edited.edited_at = dt.datetime.now(dt.timezone.utc)
                rows[first.id] = edited
            else:
                fetch = channel.fetch_message.side_effect
                def deleted(mid):
                    if mid == first.id:
                        raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
                    return fetch(mid)
                channel.fetch_message.side_effect = deleted
            fri.busy.discard(20)
            await asyncio.wait_for(task, 2)
            fri.chat_once.assert_not_awaited()
            retained = any(row['id'] == str(first.id) for row in agent.recent[1])
            requests = []
            model(fri, requests)
            fri.chat_once = type(fri).chat_once.__get__(fri)
            later = message('fri what is up?', user=30)
            await fri.talk(123, 1, 30, later.content, 'public', event=later)
            assert len(requests) == 1
            assert MARKER not in json.dumps(requests), f'withdrawn row retained={retained}; marker sent in later request'
        finally:
            fri.busy.discard(20)
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


def test_a_reply_to_a_message_withdrawn_during_signin_is_refused_before_the_request(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        earlier = message('here is '+MARKER, user=20)
        cache(agent, channel, earlier)
        later = message('fri explain this?', user=30)
        later.reference = SimpleNamespace(message_id=earlier.id, channel_id=1)
        requests = []
        model(fri, requests)
        fri.chat_once = type(fri).chat_once.__get__(fri)
        async def headers():
            edited = copy.copy(earlier)
            edited.content, edited.edited_at = 'never mind', dt.datetime.now(dt.timezone.utc)
            rows[earlier.id] = edited
            assert not await agent.unedited([earlier])
            assert not agent.recent[1]
            return {'Authorization': 'Bearer synthetic-review'}
        fri.chat.headers = headers
        try:
            with pytest.raises(UserFeedbackCheckFailure, match='edited or deleted'):
                await fri.talk(123, 1, 30, later.content, 'public', event=later)
            assert not requests  # Refused before the model sees the old words.
        finally:
            await fri.close()
    asyncio.run(scenario())
