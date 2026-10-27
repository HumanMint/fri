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
from fri.fri_chat import Chat, ChatResult
from fri.fri_errors import ControllerUnavailable
from fri.fri_mentions import Invitation

MARKER = 'lavender-bicycle-private'


def model(fri, requests, *, attention=False, failures=0):
    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) <= failures:  # The model is down.
            return httpx.Response(503)
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


class Waits:
    """Attention's slot and sign-in, either of which can hold its batch back: `during` holds the named one
    while a change lands, then lets attention go on."""

    def __init__(self, agent, fri):
        self.held, self.waiting, self.release = None, asyncio.Event(), asyncio.Event()
        signin, waits = fri.chat.headers, self

        class Slot(asyncio.Semaphore):
            async def acquire(self):
                if waits.held == 'slot':
                    await waits.wait()
                return await super().acquire()

        async def headers():
            if waits.held == 'sign-in':
                await waits.wait()
            return await signin()

        agent.slots, fri.chat.headers = Slot(1), headers

    async def wait(self):
        self.held = None
        self.waiting.set()
        await self.release.wait()

    async def during(self, held, attend, change):
        self.held = held
        self.waiting.clear()
        self.release.clear()
        task = asyncio.create_task(attend())
        await asyncio.wait_for(self.waiting.wait(), 2)
        if asyncio.iscoroutine(changed := change()):  # A Discord event's handler, say.
            await changed
        self.release.set()
        await asyncio.wait_for(task, 2)


@pytest.mark.parametrize('wait', ['slot', 'sign-in'])
def test_attention_never_sends_a_question_deleted_while_it_waited(wait):
    # fri#50: the question is deleted after attention picked it up, before its model request.
    async def scenario():
        channel = SimpleNamespace(id=456)
        requests = []
        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, get_channel=lambda _: channel)
        model(fri, requests, attention=True)
        agent = Agent(fri)
        agent.record = AsyncMock()
        agent.profile = AsyncMock(return_value={'enabled': True, 'revision': 0})
        cache(agent, channel, SimpleNamespace(id=100, author=SimpleNamespace(id=11), content=MARKER+' anyone?'))
        await Waits(agent, fri).during(wait, agent.attend, lambda: agent.unsend(456, [100]))
        assert not requests
    asyncio.run(scenario())


@pytest.mark.parametrize('when', ['before the retry', 'during its slot wait', 'during its sign-in'])
@pytest.mark.parametrize('change', ['deleted', 'edited', 'withdrawn', 'forgotten'])
def test_a_retried_attention_batch_is_rechecked_like_a_first_attempt(change, when):
    """A batch offered again after a failed model call carries nothing taken back since that call."""
    async def scenario():
        channel = SimpleNamespace(id=456)
        requests = []
        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, get_channel=lambda _: channel)
        model(fri, requests, attention=True, failures=1)
        agent = Agent(fri)
        agent.record = AsyncMock()
        agent.profile = AsyncMock(return_value={'enabled': True, 'revision': 0})
        question = SimpleNamespace(id=100, channel=channel, author=SimpleNamespace(id=11),
                                   content='anyone know why the build is broken?')
        aside = SimpleNamespace(id=101, channel=channel, author=SimpleNamespace(id=12), content=MARKER+' lol')
        cache(agent, channel, question)
        cache(agent, channel, aside)
        waits = Waits(agent, fri)
        await agent.attend()
        assert len(requests) == 1 and MARKER in json.dumps(requests[0])

        def withdraw():
            if change == 'deleted':
                agent.unsend(456, [101])
            elif change == 'edited':
                agent.edit(456, SimpleNamespace(id=101, content='never mind', author=SimpleNamespace(id=12),
                                                attachments=[]))
            elif change == 'withdrawn':  # Its own queue recheck found it changed.
                agent.withdraw(aside)
            else:
                agent.forget(12, erased=True)

        agent.last_attention[456] = 0  # The next heartbeat window.
        if when == 'before the retry':
            withdraw()
            await agent.attend()
        else:
            await waits.during('slot' if when == 'during its slot wait' else 'sign-in', agent.attend, withdraw)
            if change != 'edited':  # The row left the live view: held back like a first attempt, then offered again.
                assert len(requests) == 1
                agent.last_attention[456] = 0
                await agent.attend()
        assert len(requests) == 2
        retried = json.dumps(requests[1])
        assert MARKER not in retried and question.content in retried
        assert ('never mind' in retried) == (change == 'edited')
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
            with pytest.raises(UserFeedbackCheckFailure, match='edited or deleted|conversation changed'):  # Either last check.
                await fri.talk(123, 1, 30, later.content, 'public', event=later)
            assert not requests  # Refused before the model sees the old words.
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['edited', 'deleted'])
@pytest.mark.parametrize('consumer', ['direct', 'attention'])
def test_every_withdrawn_message_of_a_ping_burst_leaves_the_live_view(monkeypatch, tmp_path, change, consumer):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        first = message('fri first withdrawn caption')
        second = message('fri help with ' + MARKER)
        last = message('<@99> still there?')
        events = [first, second, last]
        for event in events:
            cache(agent, channel, event)
        burst = [(time.monotonic(), event, event.content) for event in events]
        invitation = await Invitation.create(agent, last)
        waiting = asyncio.Event()

        async def notice(*args, **kwargs):
            waiting.set()
            return SimpleNamespace(id=last.id + 1, content='hold on', delete=AsyncMock())

        last.reply.side_effect = notice
        fri.busy.add(20)
        task = asyncio.create_task(agent.respond(last, last.content, burst=burst,
            invitation=invitation, forgotten=0, revision=0))
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            if change == 'edited':
                for event in events[:2]:
                    current = copy.copy(event)
                    current.content = 'never mind'
                    current.edited_at = dt.datetime.now(dt.timezone.utc)
                    rows[event.id] = current
            else:
                original_fetch = channel.fetch_message.side_effect

                def deleted(mid):
                    if mid in (first.id, second.id):
                        raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
                    return original_fetch(mid)

                channel.fetch_message.side_effect = deleted
            fri.busy.discard(20)
            await asyncio.wait_for(task, 2)
            fri.chat_once.assert_not_awaited()
            live = {row['id'] for row in agent.recent[1]}
            assert str(first.id) not in live and str(second.id) not in live
            requests = []
            model(fri, requests, attention=consumer == 'attention')
            fri.chat_once = type(fri).chat_once.__get__(fri)
            if consumer == 'direct':
                later = message('fri what is up?', user=30)
                await fri.talk(123, 1, 30, later.content, 'public', event=later)
            else:
                await agent.attend()
            assert len(requests) == 1
            assert MARKER not in json.dumps(requests)
        finally:
            fri.busy.discard(20)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await fri.close()

    asyncio.run(scenario())


def test_a_transient_discord_error_does_not_withdraw_an_unchanged_message(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        earlier = message('fri what is the weather?')
        cache(agent, channel, earlier)
        original_fetch = channel.fetch_message.side_effect
        channel.fetch_message.side_effect = discord.HTTPException(
            SimpleNamespace(status=503, reason='Service Unavailable'), 'temporary failure')
        try:
            assert not await agent.unedited([earlier])
            assert str(earlier.id) not in agent.withdrawn  # Unconfirmed: unanswered, but not withdrawn.
            channel.fetch_message.side_effect = original_fetch
            assert await agent.still_addressed(earlier, answering=True)
            later = message('fri explain this?', user=30)
            later.reference = SimpleNamespace(message_id=earlier.id, channel_id=1)
            requests = []
            model(fri, requests)
            fri.chat_once = type(fri).chat_once.__get__(fri)
            await fri.talk(123, 1, 30, later.content, 'public', event=later)
            assert len(requests) == 1
        finally:
            await fri.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['edited', 'deleted'])
def test_a_nearby_source_withdrawn_during_a_mention_reply_leaves_the_live_view(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        source = message('here is ' + MARKER, user=20)
        cache(agent, channel, source)
        ping = message('<@99> explain this?', user=30)
        ping.reference = SimpleNamespace(message_id=source.id, channel_id=1)
        invitation = await Invitation.create(agent, ping)

        async def initial_response(*args):
            assert str(source.id) in {r['source']['id'] for r in invitation.rows}
            if change == 'edited':
                current = copy.copy(source)
                current.content = 'never mind'
                current.edited_at = dt.datetime.now(dt.timezone.utc)
                rows[source.id] = current
            else:
                original_fetch = channel.fetch_message.side_effect

                def deleted(mid):
                    if mid == source.id:
                        raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
                    return original_fetch(mid)

                channel.fetch_message.side_effect = deleted
            return ChatResult(status='completed', reply='initial response')

        fri.chat_once.side_effect = initial_response
        try:
            await agent.respond(ping, 'explain this?', invitation=invitation, forgotten=0, revision=0)
            assert invitation.refusal == 'nearby message changed'
            assert not any(r['id'] == str(source.id) for r in agent.recent[1])
            requests = []
            model(fri, requests)
            fri.chat_once = type(fri).chat_once.__get__(fri)
            later = message('fri what is up?', user=40)
            await fri.talk(123, 1, 40, later.content, 'public', event=later)
            assert len(requests) == 1 and MARKER not in json.dumps(requests[0])
        finally:
            await fri.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('burst_input', [False, True])
@pytest.mark.parametrize('change', ['edited', 'deleted'])
def test_a_turn_whose_own_message_is_withdrawn_during_signin_is_refused(monkeypatch, tmp_path, burst_input, change):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        secret = message('fri help with ' + MARKER)
        latest = message('fri please explain it') if burst_input else secret
        events = [secret, latest] if burst_input else [secret]
        burst = [(time.monotonic(), e, e.content) for e in events] if burst_input else []
        for event in events:
            cache(agent, channel, event)
        other = message('<@99> explain this?', user=30)
        other.reference = SimpleNamespace(message_id=secret.id, channel_id=1)
        quoted = await Invitation.create(agent, other)
        await quoted.admit()
        await quoted.collect(include_recent=False)
        assert secret.id in quoted.events
        requests = []
        model(fri, requests)
        fri.chat_once = type(fri).chat_once.__get__(fri)

        async def headers():
            if change == 'edited':
                current = copy.copy(secret)
                current.content, current.edited_at = 'never mind', dt.datetime.now(dt.timezone.utc)
                rows[secret.id] = current
            else:
                fetch = channel.fetch_message.side_effect
                def deleted(mid):
                    if mid == secret.id:
                        raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
                    return fetch(mid)
                channel.fetch_message.side_effect = deleted
            # Another member's already-running mention notices the edit.
            assert not await quoted.current()
            assert quoted.refusal == 'nearby message changed'
            assert str(secret.id) in agent.withdrawn
            return {'Authorization': 'Bearer synthetic-review'}

        fri.chat.headers = headers
        try:
            with pytest.raises(UserFeedbackCheckFailure, match='edited or deleted|conversation changed'):  # Either last check.
                await fri.talk(123, 1, 20, latest.content, 'public', event=latest, burst=burst)
            assert not requests
        finally:
            await fri.close()

    asyncio.run(scenario())


def test_withdrawn_ids_are_kept_by_age_not_count(monkeypatch):
    agent = Agent(SimpleNamespace(get_channel=lambda _: None, guild_id=123))
    channel = SimpleNamespace(id=456)
    clock = [1000.0]
    monkeypatch.setattr('fri.fri_agent.time.time', lambda: clock[0])
    agent.withdraw(SimpleNamespace(id=1, channel=channel))
    for mid in range(2, 1502):  # A burst of other withdrawals doesn't push an in-flight one out.
        agent.withdraw(SimpleNamespace(id=mid, channel=channel))
    assert '1' in agent.withdrawn
    clock[0] += 3601  # An hour later no turn still holds a copy.
    agent.withdraw(SimpleNamespace(id=9999, channel=channel))
    assert list(agent.withdrawn) == ['9999']


@pytest.mark.parametrize('kind', ['burst', 'reply_target'])
@pytest.mark.parametrize('change', ['edited', 'deleted'])
@pytest.mark.parametrize('consumer', ['direct', 'attention'])
def test_a_change_seen_by_the_final_conversation_check_stays_out_of_later_requests(monkeypatch, tmp_path, kind, change, consumer):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        source = message('fri private caption ' + MARKER)
        latest = message('fri explain it please', user=20 if kind == 'burst' else 30)
        cache(agent, channel, source)
        burst = []
        if kind == 'burst':
            burst = [(0, item, item.content) for item in [source, latest]]
        else:
            latest.reference = SimpleNamespace(message_id=source.id, channel_id=1)

        requests = []
        model(fri, requests)
        fri.chat_once = type(fri).chat_once.__get__(fri)
        original_handler = fri.chat.transport.handler
        original_fetch = channel.fetch_message.side_effect

        def during_inference(request):
            if change == 'edited':
                current = copy.copy(source)
                current.content = 'never mind'
                current.edited_at = dt.datetime.now(dt.timezone.utc)
                rows[source.id] = current
            else:
                def deleted(mid):
                    if mid == source.id:
                        raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
                    return original_fetch(mid)
                channel.fetch_message.side_effect = deleted
            return original_handler(request)

        fri.chat.transport.handler = during_inference
        try:
            with pytest.raises(UserFeedbackCheckFailure, match='conversation changed'):
                await fri.talk(123, 1, latest.author.id, latest.content, 'public', event=latest, burst=burst)
            # Fri has fetched the changed source and refused the original answer.
            retained = any(row['id'] == str(source.id) for row in agent.recent[1])
            assert len(requests) == 1
            requests.clear()
            model(fri, requests, attention=consumer == 'attention')
            fri.chat_once = type(fri).chat_once.__get__(fri)
            later = message('fri what is up?', user=40)
            if consumer == 'direct':
                await fri.talk(123, 1, 40, later.content, 'public', event=later)
            else:
                cache(agent, channel, later)
                await agent.attend()
            assert len(requests) == 1
            assert MARKER not in json.dumps(requests), f'{kind}/{change}/{consumer}: changed row retained={retained}'
        finally:
            await fri.close()

    asyncio.run(scenario())


def test_a_delete_after_a_check_withdrew_the_row_still_stops_a_reply_in_flight(monkeypatch, tmp_path):
    """A check that sees a message gone drops it from the live view before Discord's delete event arrives.
    The delete must still be marked, so a reply already composed from a copy isn't delivered (see unsend)."""
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        source = message('here is ' + MARKER, user=20)
        cache(agent, channel, source)
        observer = message('<@99> explain that?', user=30)  # Another mention, already holding the source.
        observer.reference = SimpleNamespace(message_id=source.id, channel_id=1)
        invitation = await Invitation.create(agent, observer)
        await invitation.admit()
        await invitation.collect(include_recent=False)
        trigger = message('fri what was the caption?', user=40)
        fetch, requests = channel.fetch_message.side_effect, []

        async def handler(request):
            requests.append(json.loads(request.content))
            assert MARKER in json.dumps(requests[-1])
            def deleted(mid):
                if mid == source.id:
                    raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
                return fetch(mid)
            channel.fetch_message.side_effect = deleted
            assert not await invitation.current()  # The other mention's check withdraws it first.
            await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=source.id))
            result = {'reply': 'the caption was ' + MARKER, 'request_draft': None}
            return httpx.Response(200, text='data: ' + json.dumps(response_event(content=json.dumps(result))) + '\n\n')

        fri.chat = Chat(SimpleNamespace(access_token=lambda: 'synthetic-review'), httpx.MockTransport(handler))
        fri.chat.headers = AsyncMock(return_value={'Authorization': 'Bearer synthetic-review'})
        fri.chat_once = type(fri).chat_once.__get__(fri)
        try:
            await agent.respond(trigger, trigger.content, forgotten=0, revision=0)
            assert len(requests) == 1 and source.id in agent.unsent
            assert not any(MARKER in str(call) for call in trigger.reply.await_args_list)
            assert MARKER not in str(store.chat_context('123', 'public:1', '40')['turns'])
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['burst', 'reply_target'])
@pytest.mark.parametrize('change', ['edited', 'deleted'])
def test_a_change_the_final_check_saw_is_withdrawn_though_discord_then_fails(monkeypatch, tmp_path, kind, change):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        source = message('fri private caption ' + MARKER)
        latest = message('fri explain it please', user=20 if kind == 'burst' else 30)
        cache(agent, channel, source)
        burst = []
        if kind == 'burst':
            burst = [(0, item, item.content) for item in [source, latest]]
        else:
            latest.reference = SimpleNamespace(message_id=source.id, channel_id=1)
        requests = []
        model(fri, requests)
        fri.chat_once = type(fri).chat_once.__get__(fri)
        original_handler, original_fetch = fri.chat.transport.handler, channel.fetch_message.side_effect
        def during_inference(request):  # Once the final check has seen the change, Discord fails.
            seen = []
            def changed(mid):
                if mid != source.id:
                    return original_fetch(mid)
                seen.append(mid)
                if len(seen) > 1:
                    raise discord.HTTPException(SimpleNamespace(status=503, reason='Service Unavailable'), 'down')
                if change == 'deleted':
                    raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
                current = copy.copy(source)
                current.content, current.edited_at = 'never mind', dt.datetime.now(dt.timezone.utc)
                return current
            channel.fetch_message.side_effect = changed
            return original_handler(request)
        fri.chat.transport.handler = during_inference
        try:
            with pytest.raises(UserFeedbackCheckFailure, match='conversation changed'):
                await fri.talk(123, 1, latest.author.id, latest.content, 'public', event=latest, burst=burst)
            channel.fetch_message.side_effect = original_fetch
            requests.clear()
            later = message('fri what is up?', user=40)
            await fri.talk(123, 1, 40, later.content, 'public', event=later)
            assert len(requests) == 1 and MARKER not in json.dumps(requests)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['edited', 'deleted'])
def test_an_outage_notices_last_fetch_withdraws_a_change_the_loop_missed(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        event = message('fri help with '+MARKER)
        cache(agent, channel, event)
        original_fetch, seen = channel.fetch_message.side_effect, []
        def later_changed(mid):  # As it was for the outage loop, changed by the notice's own fetch.
            seen.append(mid)
            if mid != event.id or len(seen) == 1:
                return original_fetch(mid)
            if change == 'deleted':
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
            current = copy.copy(event)
            current.content, current.edited_at = 'never mind', dt.datetime.now(dt.timezone.utc)
            return current
        channel.fetch_message.side_effect = later_changed
        try:
            await agent.outage(event, [event], since=(0, 0))
            event.reply.assert_not_awaited()
            assert len(seen) == 2
            channel.fetch_message.side_effect = original_fetch
            requests = []
            model(fri, requests)
            fri.chat_once = type(fri).chat_once.__get__(fri)
            later = message('fri can you help me with this?', user=30)
            await fri.talk(123, 1, 30, later.content, 'public', event=later)
            assert len(requests) == 1 and MARKER not in json.dumps(requests)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_changed_reply_target_still_withdraws_the_changed_burst_messages(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        target = message('the plan is tuesday', user=30)
        first = message('fri about this '+MARKER)
        latest = message('fri does that work')
        latest.reference = SimpleNamespace(message_id=target.id, channel_id=1)
        for item in (target, first, latest):
            cache(agent, channel, item)
        burst = [(0, item, item.content) for item in [first, latest]]
        requests = []
        model(fri, requests)
        fri.chat_once = type(fri).chat_once.__get__(fri)
        original_handler = fri.chat.transport.handler
        def during_inference(request):  # Both the target and the earlier burst message are edited.
            for item in (target, first):
                current = copy.copy(item)
                current.content, current.edited_at = 'never mind', dt.datetime.now(dt.timezone.utc)
                rows[item.id] = current
            return original_handler(request)
        fri.chat.transport.handler = during_inference
        try:
            with pytest.raises(UserFeedbackCheckFailure, match='conversation changed'):
                await fri.talk(123, 1, 20, latest.content, 'public', event=latest, burst=burst)
            live = {row['id'] for row in agent.recent[1]}
            assert str(target.id) not in live and str(first.id) not in live
            requests.clear()
            later = message('fri what is up?', user=40)
            await fri.talk(123, 1, 40, later.content, 'public', event=later)
            assert len(requests) == 1 and MARKER not in json.dumps(requests)
        finally:
            await fri.close()
    asyncio.run(scenario())
