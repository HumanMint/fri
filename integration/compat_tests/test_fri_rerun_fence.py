"""An edited question answered again (see Agent.rerun) is held to its first handling's privacy baseline (fri #248),
and a retry of its refetch to its turn's deadline (fri #259)."""

import asyncio
import inspect
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from compat_tests.test_fri_continuation_recovery import (
    EDITED,
    edited_while_recovering,
    gathered,
    server_error,
)
from compat_tests.test_fri_continuations import (
    ANSWER,
    DETAIL,
    QUESTION,
    Model,
    deleted,
    edited,
    finish,
    members,
    saved,
    until,
)
from compat_tests.test_fri_edit_reanswer import MARKER, edit, model
from compat_tests.test_fri_mentions import attachment, environment
from compat_tests.test_fri_timing import no_debounce
from fri import fri_agent
from fri.fri_agent import Agent
from fri.fri_chat import ChatResult
from fri.fri_errors import UNREACHABLE, ControllerUnavailable

ERASURES = ['local', 'gene', 'someone else']


def erasing(agent, store, erasure):
    """Their "forget me" through Fri, or their erasure through the panel, Gene or Red (their revision moves). Or
    another member's, which changes nothing for them."""
    def erase():
        if erasure == 'local':
            agent.forget(20, erased=True)
        elif erasure == 'gene':
            store.personal_control('20', 'disable')
        else:
            agent.forget(30, erased=True)
            store.personal_control('30', 'disable')
    return erase


def during_rerun_fetch(channel, during, *, fail=False):
    """The rerun's first fetch of the message runs `during` while Discord reads it out, then reads it (or, `fail`, a
    server error: the next try reads it). Returns the rerun's fetches."""
    fetch, made = channel.fetch_message.side_effect, []
    async def refetch(mid):
        if asyncio.current_task().get_coro().__name__ == 'rerun':
            made.append(mid)
            if len(made) == 1:
                during()
                if fail:
                    raise server_error()
        result = fetch(mid)
        return await result if inspect.isawaitable(result) else result
    channel.fetch_message.side_effect = refetch
    return made


@pytest.mark.parametrize('fetch', ['reads it', 'reads it after a failed try'])
@pytest.mark.parametrize('erasure', ERASURES)
def test_an_erasure_during_a_reruns_refetch_stops_it(monkeypatch, tmp_path, erasure, fetch):
    """fri #248: a message edited while Fri answers it is answered again as edited, after a refetch. They erase while
    that fetch is out, then it reads the edit (on its first try, or after one failed). It's held to the baseline its
    first handling took as it came in, so it goes unanswered, quietly: no model request, no reply, nothing saved, and
    nothing of it back in the live view. Another member's erasure changes nothing for it: answered once, as edited."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri whats teh best ' + MARKER)
        requests, copies = [], []
        async def respond(text, context, ready):
            await ready()
            requests.append(text)
            if not copies:
                copies.append(await edit(fri, rows, trigger, 'fri whats the best album'))
                return ChatResult(status='completed', reply='answer to the old words')
            return ChatResult(status='completed', reply='answer to the edit')
        model(fri, tmp_path, respond)
        made = during_rerun_fetch(channel, erasing(agent, store, erasure), fail=fetch != 'reads it')
        try:
            await agent.handle(trigger)
            await until(lambda: not agent.reruns, 10)
            assert len(made) >= (1 if fetch == 'reads it' else 2)
            trigger.reply.assert_not_awaited()  # Not the answer to the old words.
            outcomes = agent.ledger.snapshot()
            assert outcomes['in_flight'] == 0
            if erasure == 'someone else':
                assert requests == ['fri whats teh best ' + MARKER, 'fri whats the best album']
                copies[0].reply.assert_awaited_once()
                assert 'answer to the edit' in str(copies[0].reply.await_args)
                assert len(saved(calls)) == 1
                return
            assert requests == ['fri whats teh best ' + MARKER]  # The edit never reaches the model...
            copies[0].reply.assert_not_awaited()  # ...or gets a reply.
            assert not saved(calls)
            assert not any(row['id'] == str(trigger.id) for row in agent.recent[1])
            assert outcomes['quiet'].get('author privacy changed') == 1
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('erasure', ERASURES)
def test_an_erasure_before_a_reruns_refetch_stops_it(monkeypatch, tmp_path, erasure):
    """fri #248: they erase once the edit refused the first handling's answer, as its rerun is scheduled, before that
    rerun fetches it. Its own reads would hold that erasure as their baseline; the first handling's doesn't."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri whats teh best ' + MARKER)
        requests, copies = [], []
        async def respond(text, context, ready):
            await ready()
            requests.append(text)
            if not copies:
                copies.append(await edit(fri, rows, trigger, 'fri whats the best album'))
                return ChatResult(status='completed', reply='answer to the old words')
            return ChatResult(status='completed', reply='answer to the edit')
        model(fri, tmp_path, respond)
        schedule, erase = agent.answer_edits, erasing(agent, store, erasure)
        def answer_edits():
            if trigger.id in agent.reanswer and not agent.handling[trigger.id]:
                erase()  # Queued to be answered again: nothing handles it now, and its rerun hasn't started.
            schedule()
        agent.answer_edits = answer_edits
        try:
            await agent.handle(trigger)
            await until(lambda: not agent.reruns, 10)
            trigger.reply.assert_not_awaited()
            if erasure == 'someone else':
                assert len(requests) == 2
                copies[0].reply.assert_awaited_once()
                return
            assert len(requests) == 1
            copies[0].reply.assert_not_awaited()
            assert not saved(calls) and agent.ledger.snapshot()['in_flight'] == 0
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('erasure', ERASURES)
def test_an_erasure_during_a_recovered_questions_rerun_fetch_stops_it(monkeypatch, tmp_path, erasure):
    """fri #248 on the recovery path (fri #228's ordering): a question answered alone after its line was taken back,
    edited while that recovery read, is answered again from its rerun's fetch. They erase while that fetch is out."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        model = Model(fri, tmp_path, waits={1: 'before'})
        gathering = gathered(monkeypatch)
        tasks, release, fetched = [], None, None
        try:
            question, tasks, fetching, fetched, release = await edited_while_recovering(
                agent, model, message, gathering, channel, rows)
            await asyncio.wait_for(fetching.wait(), 5)
            erasing(agent, store, erasure)()
            fetched.set()
            if erasure == 'someone else':
                await until(lambda: len(gathering) == 3)  # Taken in again as it reads now: its own burst, just it.
                gathering[2].set()
            await until(lambda: not agent.reruns, 10)
            if erasure == 'someone else':
                assert [text for text, _ in model.requests] == [EDITED]
                question.reply.assert_awaited_once()
                assert question.reply.await_args.args[0] == ANSWER
                return
            assert not model.requests
            question.reply.assert_not_awaited()
            assert not saved(calls) and agent.ledger.snapshot()['in_flight'] == 0
        finally:
            model.hold[1].set()
            for event in (release, fetched, *gathering):
                if event:
                    event.set()
            await finish(fri, tasks)
    asyncio.run(scenario())


@pytest.mark.parametrize('erasure', ERASURES)
def test_an_erasure_during_the_rerun_fetch_of_a_question_edited_before_its_recovery_stops_it(
        monkeypatch, tmp_path, erasure):
    """fri #248 where the question, given way to a line that's then deleted, is edited before its recovery starts:
    it's answered again from a refetch (see Agent.unjoin), held to the baseline it came in with."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        no_debounce(monkeypatch)
        model = Model(fri, tmp_path, waits={1: 'before', 2: 'before'})
        question = message(QUESTION)
        tasks = [asyncio.create_task(agent.handle(question))]
        try:
            await asyncio.wait_for(model.reached[1].wait(), 5)
            detail = message(DETAIL)
            tasks.append(asyncio.create_task(agent.handle(detail)))
            await until(lambda: tasks[1].done() or detail.id in agent.replying)
            model.hold[1].set()
            await asyncio.wait_for(model.reached[2].wait(), 5)
            deleted(channel, rows, detail)
            during_rerun_fetch(channel, erasing(agent, store, erasure))
            agent.unsend(1, [detail.id])
            agent.edit(1, edited(rows, question, EDITED))
            model.hold[2].set()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            await until(lambda: not agent.reruns, 10)
            detail.reply.assert_not_awaited()
            if erasure == 'someone else':
                assert [text for text, _ in model.requests] == [EDITED]
                question.reply.assert_awaited_once()
                return
            assert not model.requests
            question.reply.assert_not_awaited()
            assert not saved(calls) and agent.ledger.snapshot()['in_flight'] == 0
        finally:
            for hold in model.hold.values():
                hold.set()
            await finish(fri, tasks)
    asyncio.run(scenario())


@pytest.mark.parametrize('erasure', ['none', 'local', 'gene'])
def test_a_memory_listing_edited_into_a_question_is_held_to_its_baseline_too(monkeypatch, tmp_path, erasure):
    """A "show my memory" isn't fenced by erasures (it shows what's kept now), but its rerun after an edit into a
    question is: its baseline, from as it came in, carries over. With nothing changed it's answered, as edited."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri show my memory')
        api, copies = fri.api, []
        async def listing(method, path, payload=None):
            result = await api(method, path, payload)
            if path == '/chat/context' and not copies:  # The listing's own read: edited while Fri lists it.
                copies.append(await edit(fri, rows, trigger, 'fri whats the best album'))
            return result
        fri.api = listing
        requests = []
        async def respond(text, context, ready):
            await ready()
            requests.append(text)
            return ChatResult(status='completed', reply='answer to the edit')
        model(fri, tmp_path, respond)
        if erasure != 'none':
            during_rerun_fetch(channel, erasing(agent, store, erasure))
        try:
            await agent.handle(trigger)
            await until(lambda: not agent.reruns, 10)
            assert copies
            if erasure == 'none':
                assert requests == ['fri whats the best album']
                copies[0].reply.assert_awaited_once()
                assert 'answer to the edit' in str(copies[0].reply.await_args)
            else:
                assert not requests
                copies[0].reply.assert_not_awaited()
            assert agent.ledger.snapshot()['in_flight'] == 0
        finally:
            await fri.close()
    asyncio.run(scenario())


def direct(fri, agent, rows):
    """environment()'s member 20, in a DM with Fri instead (DMs on)."""
    @asynccontextmanager
    async def typing():
        yield
    channel = SimpleNamespace(id=555, guild=None, typing=typing)
    channel.fetch_message = AsyncMock(side_effect=lambda mid: rows[mid])
    get_channel = fri.get_channel
    fri.get_channel = lambda cid: channel if cid == channel.id else get_channel(cid)
    agent.config['support_dms'] = True
    agent.member = AsyncMock(return_value=SimpleNamespace(id=20))
    def message(text):
        mid = max(rows, default=discord.utils.time_snowflake(discord.utils.utcnow()))+1
        item = Mock(spec=discord.Message, id=mid, content=text, channel=channel, guild=None,
                    author=SimpleNamespace(id=20, display_name='member 20', bot=False), webhook_id=None, mentions=[],
                    reference=None, attachments=[], embeds=[], edited_at=None, reply=AsyncMock())
        rows[mid] = item
        return item
    return channel, message


@pytest.mark.parametrize('first', ['a question', 'a memory listing'])
@pytest.mark.parametrize('erasure', ['none', 'local', 'gene'])
def test_a_dm_edited_while_fri_answers_is_held_to_its_baseline(monkeypatch, tmp_path, erasure, first):
    """A DM's rerun too, its revision read even for a privacy control. A memory listing in a DM reads no revision
    as it comes in, so its rerun after an edit into a question has no baseline to be held to: it drops quietly,
    erasure or not, rather than take one now. A question's is answered with nothing changed."""
    async def scenario():
        fri, agent, store, _, _, _, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        channel, message = direct(fri, agent, rows)
        trigger = message('show my memory' if first == 'a memory listing' else 'whats teh best ' + MARKER)
        requests, copies = [], []
        api = fri.api
        async def listing(method, path, payload=None):
            result = await api(method, path, payload)
            if path == '/chat/context' and first == 'a memory listing' and not copies:
                copies.append(await edit(fri, rows, trigger, 'whats the best album'))
            return result
        fri.api = listing
        async def respond(text, context, ready):
            await ready()
            requests.append(text)
            if not copies:
                copies.append(await edit(fri, rows, trigger, 'whats the best album'))
                return ChatResult(status='completed', reply='answer to the old words')
            return ChatResult(status='completed', reply='answer to the edit')
        model(fri, tmp_path, respond)
        if erasure != 'none':
            during_rerun_fetch(channel, erasing(agent, store, erasure))
        try:
            await agent.handle(trigger)
            await until(lambda: not agent.reruns, 10)
            assert copies
            trigger.reply.assert_not_awaited()
            first_requests = [] if first == 'a memory listing' else ['whats teh best ' + MARKER]
            if erasure == 'none' and first == 'a question':
                assert requests == [*first_requests, 'whats the best album']
                copies[0].reply.assert_awaited_once()
                assert 'answer to the edit' in str(copies[0].reply.await_args)
            else:
                assert requests == first_requests
                copies[0].reply.assert_not_awaited()
                assert not saved(calls)
            outcomes = agent.ledger.snapshot()
            assert outcomes['in_flight'] == 0
            if first == 'a memory listing':
                assert outcomes['quiet'].get('no privacy baseline') == 1
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('place', ['server', 'dm'])
@pytest.mark.parametrize('erasure', ['none', 'local', 'gene'])
def test_a_question_edited_into_a_memory_listing_is_held_to_its_baseline(monkeypatch, tmp_path, erasure, place):
    """Edited into "show my memory", its rerun is a privacy control, which a first handling doesn't fence. A rerun is,
    whatever the edit made it: with nothing changed the listing goes out, after their erasure nothing does."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        prefix = 'fri ' if place == 'server' else ''
        if place == 'dm':
            channel, message = direct(fri, agent, rows)
        trigger = message(prefix+'whats teh best ' + MARKER)
        requests, copies = [], []
        async def respond(text, context, ready):
            await ready()
            requests.append(text)
            copies.append(await edit(fri, rows, trigger, prefix+'show my memory'))
            return ChatResult(status='completed', reply='answer to the old words')
        model(fri, tmp_path, respond)
        if erasure != 'none':
            during_rerun_fetch(channel, erasing(agent, store, erasure))
        try:
            await agent.handle(trigger)
            await until(lambda: not agent.reruns, 10)
            assert requests == [prefix+'whats teh best ' + MARKER]  # A listing makes no model request.
            trigger.reply.assert_not_awaited()
            assert copies[0].reply.await_count == (erasure == 'none')
            assert agent.ledger.snapshot()['in_flight'] == 0
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_an_owner_control_an_edit_made_is_held_to_the_reruns_baseline(monkeypatch, tmp_path):
    """The owner's settings request runs before any privacy read: their "forget me" during the rerun's fetch stops
    it there, as it would any other edit."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri whats teh best ' + MARKER, user=10)
        requests, copies = [], []
        async def respond(text, context, ready):
            await ready()
            requests.append(text)
            copies.append(await edit(fri, rows, trigger, 'fri show settings'))
            return ChatResult(status='completed', reply='answer to the old words')
        model(fri, tmp_path, respond)
        during_rerun_fetch(channel, lambda: agent.forget(10, erased=True))
        try:
            await agent.handle(trigger)
            await until(lambda: not agent.reruns, 10)
            assert len(requests) == 1
            trigger.reply.assert_not_awaited()
            copies[0].reply.assert_not_awaited()
            assert agent.ledger.snapshot()['in_flight'] == 0
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('erasure', ['none', 'chats cleared'])
def test_an_uncaptioned_post_an_edit_left_stays_out_of_the_live_view_after_an_erasure(monkeypatch, tmp_path, erasure):
    """Edited down to its picture, its rerun only puts the post back in the live view (see receive). Not once they
    cleared their chats through Gene during its fetch: their revision moved, though memory stays on."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri whats this ' + MARKER, attachments=[attachment(1)])
        requests, copies = [], []
        async def respond(text, context, ready):
            await ready()
            requests.append(text)
            copies.append(await edit(fri, rows, trigger, ''))
            return ChatResult(status='completed', reply='answer to the old words')
        model(fri, tmp_path, respond)
        if erasure != 'none':
            during_rerun_fetch(channel, lambda: store.clear_chats('20'))
        try:
            await agent.handle(trigger)
            await until(lambda: not agent.reruns, 10)
            assert len(requests) == 1 and copies
            kept = [row for row in agent.recent[1] if row['id'] == str(trigger.id)]
            assert [row['text'] for row in kept] == ([''] if erasure == 'none' else [])
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_reruns_outage_notice_is_fenced_even_for_a_memory_listing(monkeypatch, tmp_path):
    """A privacy control's outage notice isn't fenced as it first comes in. Edited into one, a rerun's is: their
    "forget me" while its profile read fails says nothing."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri whats teh best ' + MARKER)
        copies = []
        async def respond(text, context, ready):
            await ready()
            copies.append(await edit(fri, rows, trigger, 'fri show my memory'))
            return ChatResult(status='completed', reply='answer to the old words')
        model(fri, tmp_path, respond)
        profile = agent.profile
        async def down(*args, **kwargs):
            if asyncio.current_task().get_coro().__name__ == 'rerun':
                agent.forget(20, erased=True)
                raise ControllerUnavailable(UNREACHABLE)
            return await profile(*args, **kwargs)
        agent.profile = down
        try:
            await agent.handle(trigger)
            await until(lambda: not agent.reruns, 10)
            assert copies
            trigger.reply.assert_not_awaited()
            copies[0].reply.assert_not_awaited()
            assert agent.ledger.snapshot()['in_flight'] == 0
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_rerun_with_no_baseline_from_its_first_handling_drops_quietly(monkeypatch):
    """Nothing to hold it to, so it isn't taken in again as if it just came in: that would take a new baseline."""
    async def scenario():
        message_id = discord.utils.time_snowflake(discord.utils.utcnow())
        event = SimpleNamespace(id=message_id, edited_at=None, content='fri whats the best album',
                                author=SimpleNamespace(id=20, bot=False))
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=event))
        agent = Agent(SimpleNamespace(get_channel=lambda _: channel, user=SimpleNamespace(id=99)))
        agent.handle = AsyncMock()
        agent.requeued[message_id] = 20
        await agent.rerun(1, message_id)
        agent.handle.assert_not_awaited()
        assert not agent.requeued
    asyncio.run(scenario())


@pytest.mark.parametrize('held', [1, 2], ids=['first try again', 'second try again'])
def test_a_reruns_try_again_is_bounded_by_its_turns_deadline(monkeypatch, held):
    """fri #259: a try again whose fetch Discord holds open ends at the turn's deadline, settled quietly as a failed
    fetch, so attention isn't kept off and the question isn't counted in flight past it."""
    async def scenario():
        clock, fetches = [time.time()], []
        message_id = discord.utils.time_snowflake(discord.utils.utcnow())
        async def fetch(_):
            fetches.append(clock[0])
            if len(fetches) > held:
                await asyncio.Event().wait()  # Held open.
            raise server_error()
        async def sleep(delay):
            clock[0] += delay
        monkeypatch.setattr(fri_agent, 'time', SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic))
        monkeypatch.setattr(fri_agent.asyncio, 'sleep', sleep)
        monkeypatch.setattr(fri_agent, 'QUEUE_SECONDS', sum(fri_agent.REFETCH_BACKOFF[:held])+0.3)
        agent = Agent(SimpleNamespace(get_channel=lambda _: SimpleNamespace(fetch_message=fetch)))
        agent.requeued[message_id] = 20
        agent.ledger.track(SimpleNamespace(id=message_id, channel=SimpleNamespace(id=1)), 'name')
        agent.ledger.park(message_id)
        await asyncio.wait_for(agent.rerun(1, message_id), 5)
        assert len(fetches) == held+1
        assert not agent.requeued  # Attention can come back...
        outcomes = agent.ledger.snapshot()
        assert outcomes['in_flight'] == 0  # ...and it isn't counted as being handled.
        assert outcomes['quiet'].get('fetch failed: TimeoutError') == 1
    asyncio.run(scenario())
