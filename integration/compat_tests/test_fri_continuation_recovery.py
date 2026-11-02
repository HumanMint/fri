"""A question that gave way to a short line's burst (round-2 MA2), when that line goes: its recovery and notices."""

import asyncio
import time
from types import SimpleNamespace

import discord
import pytest

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
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_timing import no_debounce
from fri import fri_agent
from fri.fri_agent import REFETCH_BACKOFF, Agent

EDITED = 'fri whats a good starter modpack'


@pytest.mark.parametrize('when', ['before its recovery starts', 'once the turn for both is done'])
def test_a_question_edited_before_its_recovery_is_answered_again_as_edited(monkeypatch, tmp_path, when):
    async def scenario():
        fri, agent, _, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
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
            await asyncio.wait_for(model.reached[2].wait(), 5)  # It gave way; the turn for both holds before its request.
            if when == 'once the turn for both is done':
                recovery = agent.answer_alone
                async def late(token):  # Nothing handles it now, so only its recovery's own fetch shows the edit.
                    await until(lambda: not agent.handling[question.id])
                    agent.edit(1, edited(rows, question, EDITED))
                    await recovery(token)
                agent.answer_alone = late
            deleted(channel, rows, detail)
            agent.unsend(1, [detail.id])  # The question is to be answered alone after all...
            if when == 'before its recovery starts':
                agent.edit(1, edited(rows, question, EDITED))  # ...but they edit it first.
            model.hold[2].set()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            await until(lambda: not agent.reruns, 10)
            # As an edit after it gave way is: answered again as it reads now (see Agent.rerun), alone, once.
            assert [text for text, _ in model.requests] == [EDITED]
            question.reply.assert_awaited_once()
            assert question.reply.await_args.args[0] == ANSWER
            detail.reply.assert_not_awaited()
            [turn] = saved(calls)
            assert turn['source']['id'] == str(question.id) and not turn['burst']
        finally:
            for hold in model.hold.values():
                hold.set()
            await finish(fri, tasks)
    asyncio.run(scenario())


def test_a_question_deleted_before_its_recovery_is_not_answered_again(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
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
            agent.unsend(1, [detail.id])
            deleted(channel, rows, question)  # Gone, not edited: nothing is answered again.
            agent.unsend(1, [question.id])
            model.hold[2].set()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            await until(lambda: not agent.reruns, 10)
            assert not model.requests and not saved(calls) and not agent.reanswer
            question.reply.assert_not_awaited()
            detail.reply.assert_not_awaited()
        finally:
            for hold in model.hold.values():
                hold.set()
            await finish(fri, tasks)
    asyncio.run(scenario())


def test_a_line_deleted_after_the_turn_for_both_committed_found_only_by_refetch_is_told_on_the_question(
        monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        no_debounce(monkeypatch)
        model = Model(fri, tmp_path, waits={1: 'before', 2: 'after'})
        question = message(QUESTION)
        tasks = [asyncio.create_task(agent.handle(question))]
        try:
            await asyncio.wait_for(model.reached[1].wait(), 5)
            detail = message(DETAIL)
            tasks.append(asyncio.create_task(agent.handle(detail)))
            await until(lambda: tasks[1].done() or detail.id in agent.replying)
            model.hold[1].set()
            await asyncio.wait_for(model.reached[2].wait(), 5)  # The turn for both, its request out.
            deleted(channel, rows, detail)  # Discord's delete event never comes: only a refetch shows it.
            model.hold[2].set()
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
            assert not saved(calls)  # Nothing it made of the line is kept or said...
            detail.reply.assert_not_awaited()
            question.reply.assert_awaited_once()  # ...and the notice goes on the question, which still stands.
            assert 'changed' in question.reply.await_args.args[0]
            assert detail.id in agent.unsent  # As its delete event would have marked it.
        finally:
            for hold in model.hold.values():
                hold.set()
            await finish(fri, tasks)
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['edited', 'deleted'])
def test_a_line_taken_back_while_its_burst_gathers_leaves_nothing_pending(monkeypatch, tmp_path, change):
    """fri #199: the question gave way to the burst the line reopened, and the line goes while that burst is still
    gathering. Its recovery answers the question, and nothing stays pending for the member, so ambient attention
    comes back after the usual cooldown."""
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        model = Model(fri, tmp_path, waits={1: 'before'})
        question = message(QUESTION)
        tasks = [asyncio.create_task(agent.handle(question))]
        try:
            await asyncio.wait_for(model.reached[1].wait(), 5)
            detail = message(DETAIL)
            tasks.append(asyncio.create_task(agent.handle(detail)))
            await until(lambda: tasks[1].done() or agent.bursts.group(detail) is not None)  # Admitted, gathering.
            model.hold[1].set()
            await asyncio.wait_for(tasks[0], 5)  # The question gave way to the burst the line reopened.
            assert agent.pending[(channel.id, detail.author.id)] == detail.id and not tasks[1].done()
            if change == 'deleted':
                deleted(channel, rows, detail)
                agent.unsend(1, [detail.id])
            else:
                agent.edit(1, edited(rows, detail))
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            await until(lambda: not agent.reruns, 10)
            question.reply.assert_awaited_once()  # Answered alone after all...
            assert question.reply.await_args.args[0] == ANSWER
            assert (channel.id, detail.author.id) not in agent.pending  # ...and nothing names the line now.
            agent.last_spoke.clear()  # The usual cooldown, over.
            assert agent.ambient_ready(channel.id, detail.author.id)
        finally:
            model.hold[1].set()
            await finish(fri, tasks)
    asyncio.run(scenario())


def test_an_earlier_line_of_a_burst_leaves_the_newer_ones_pending_mark(monkeypatch, tmp_path):
    """fri #199's counterpart: a burst's earlier message, answered for by a later one, lets go of only its own mark."""
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        model = Model(fri, tmp_path)
        real_sleep, gathering = asyncio.sleep, []
        async def held(delay, *args, **kwargs):  # Each burst's wait for its rest ends when the test says.
            if delay != 1.5:
                return await real_sleep(0)
            gathering.append(asyncio.Event())
            await gathering[-1].wait()
        monkeypatch.setattr('fri.fri_agent.asyncio.sleep', held)
        first, second = message(QUESTION), message(DETAIL)
        tasks = [asyncio.create_task(agent.handle(first))]
        try:
            await until(lambda: len(gathering) == 1)
            tasks.append(asyncio.create_task(agent.handle(second)))
            await until(lambda: len(gathering) == 2)
            gathering[0].set()
            await asyncio.wait_for(tasks[0], 5)  # The first defers to the second...
            assert agent.pending[(channel.id, second.author.id)] == second.id  # ...whose mark stays.
            gathering[1].set()
            await asyncio.wait_for(tasks[1], 5)
            assert [text for text, _ in model.requests] == [DETAIL] and not agent.pending
        finally:
            for event in gathering:
                event.set()
            await finish(fri, tasks)
    asyncio.run(scenario())


def gathered(monkeypatch):
    """Each burst's wait for its rest, held until the test sets its event (in the order they began)."""
    real_sleep, gathering = asyncio.sleep, []
    async def held(delay, *args, **kwargs):
        if delay != 1.5:
            return await real_sleep(0)
        gathering.append(asyncio.Event())
        await gathering[-1].wait()
    monkeypatch.setattr('fri.fri_agent.asyncio.sleep', held)
    return gathering


async def gave_way(agent, model, message, gathering):
    """The question, composing, gives way to the burst its line reopened, which is still gathering."""
    question = message(QUESTION)
    tasks = [asyncio.create_task(agent.handle(question))]
    await until(lambda: len(gathering) == 1)
    gathering[0].set()  # The question's own burst: just it.
    await asyncio.wait_for(model.reached[1].wait(), 5)
    detail = message(DETAIL)
    tasks.append(asyncio.create_task(agent.handle(detail)))
    await until(lambda: len(gathering) == 2)  # The line's reopened burst, gathering.
    model.hold[1].set()
    await asyncio.wait_for(tasks[0], 5)
    return question, detail, tasks


def test_attention_waits_for_a_question_answered_alone_once_its_line_lets_go(monkeypatch, tmp_path):
    """fri #210 (1): the line is edited while its burst gathers, and the question's recovery waits on its privacy read
    when the line's handler lets go of its pending mark. The question is still to be answered: ambient attention
    doesn't answer an earlier message of theirs before it, though nothing of theirs is busy or pending."""
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        model = Model(fri, tmp_path, waits={1: 'before'})
        gathering = gathered(monkeypatch)
        reading, release, erasure = asyncio.Event(), asyncio.Event(), agent.erasure
        async def read(user, *since):  # The recovery's privacy read waits until the test says.
            if asyncio.current_task() in agent.reruns:
                reading.set()
                await release.wait()
            return await erasure(user, *since)
        agent.erasure = read
        before = channel.history
        async def history(**kwargs):  # Attention's last look reads what came after its pick, too.
            if 'before' in kwargs:
                async for item in before(**kwargs):
                    yield item
                return
            for item in sorted((m for m in rows.values() if m.id > kwargs['after'].id), key=lambda m: m.id):
                yield item
        channel.history = history
        earlier = message('anyone know if the server is down?')  # Not said to Fri.
        tasks = []
        try:
            question, detail, tasks = await gave_way(agent, model, message, gathering)
            agent.edit(1, edited(rows, detail))  # The question is to be answered alone after all.
            await asyncio.wait_for(reading.wait(), 5)
            gathering[1].set()
            await asyncio.wait_for(tasks[1], 5)  # The line's handler is done: its pending mark went with it.
            assert (channel.id, detail.author.id) not in agent.pending and 20 not in fri.busy
            assert not agent.ambient_ready(channel.id, 20)  # Its recovery hasn't answered them yet.
            await agent.ambient_reply(channel, {'id': str(earlier.id)})
            earlier.reply.assert_not_awaited()
            release.set()
            await until(lambda: not agent.reruns, 10)
            question.reply.assert_awaited_once()  # Answered alone, and nothing else answered first.
            assert [text for text, _ in model.requests] == [QUESTION]
            agent.last_spoke.clear()
            assert agent.ambient_ready(channel.id, 20)  # Once it's done, after the usual cooldown.
        finally:
            model.hold[1].set()
            release.set()
            for event in gathering:
                event.set()
            await finish(fri, tasks)
    asyncio.run(scenario())


def test_a_line_whose_handler_is_cancelled_after_its_burst_was_voided_leaves_nothing_pending(monkeypatch, tmp_path):
    """fri #210 (2): an edit voids the line's gathering burst, then its handler is cancelled during that wait. Its
    pending mark still goes, as when it ends normally (fri #199), so attention comes back once the question is
    answered alone."""
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        model = Model(fri, tmp_path, waits={1: 'before'})
        gathering = gathered(monkeypatch)
        tasks = []
        try:
            question, detail, tasks = await gave_way(agent, model, message, gathering)
            agent.edit(1, edited(rows, detail))
            tasks[1].cancel()  # Still in its burst's wait.
            await asyncio.gather(tasks[1], return_exceptions=True)
            assert tasks[1].cancelled()
            await until(lambda: not agent.reruns, 10)
            question.reply.assert_awaited_once()
            assert question.reply.await_args.args[0] == ANSWER
            assert (channel.id, detail.author.id) not in agent.pending
            agent.last_spoke.clear()
            assert agent.ambient_ready(channel.id, 20)
        finally:
            model.hold[1].set()
            for event in gathering:
                event.set()
            await finish(fri, tasks)
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['edited', 'deleted'])
def test_a_recovery_cancelled_before_it_starts_gives_back_the_questions_place(monkeypatch, tmp_path, change):
    """fri #210 (3): the line is taken back and the question's recovery, scheduled, is cancelled before its first
    step, so its own cleanup never runs. Its place in their queue and its count in the ledger still go: their next
    question is answered, not told it took too long."""
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        model = Model(fri, tmp_path, waits={1: 'before'})
        gathering = gathered(monkeypatch)
        monkeypatch.setattr('fri.fri_agent.QUEUE_SECONDS', 3)  # How long the next one would wait behind it.
        tasks = []
        try:
            question, detail, tasks = await gave_way(agent, model, message, gathering)
            if change == 'deleted':
                deleted(channel, rows, detail)
                agent.unsend(1, [detail.id])
            else:
                agent.edit(1, edited(rows, detail))
            [recovery] = agent.reruns
            recovery.cancel()  # Nothing awaited since it was scheduled: it never starts.
            await asyncio.gather(recovery, return_exceptions=True)
            gathering[1].set()
            await asyncio.wait_for(tasks[1], 5)
            await until(lambda: not agent.reruns)
            assert 20 not in agent.turns.arrived  # Nothing holds their queue...
            assert agent.ledger.snapshot()['in_flight'] == 0  # ...or stays counted as being handled.
            question.reply.assert_not_awaited()
            nxt = message('fri whats a good shader pack')
            tasks.append(asyncio.create_task(agent.handle(nxt)))
            await until(lambda: len(gathering) == 3)
            gathering[2].set()
            await asyncio.wait_for(tasks[2], 10)
            nxt.reply.assert_awaited_once()
            assert nxt.reply.await_args.args[0] == ANSWER  # Not "took me too long".
        finally:
            model.hold[1].set()
            for event in gathering:
                event.set()
            await finish(fri, tasks)
    asyncio.run(scenario())


async def edited_while_recovering(agent, model, message, gathering, channel, rows, cancel_at_once=False):
    """fri #228's ordering, up to the edited question's rerun: the line is edited while its burst gathers, so the
    question is to be answered alone, and the question is edited while that recovery waits on its privacy read.
    Released, the recovery finds the edit, lets go of its place in their queue and schedules the rerun (see
    Agent.rerun), whose fetch waits until `fetched` is set. `cancel_at_once`: the rerun is cancelled as soon as it's
    scheduled, before its first step."""
    reading, release, fetching, fetched = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    erasure, fetch = agent.erasure, channel.fetch_message.side_effect

    def running(name):
        return asyncio.current_task().get_coro().__name__ == name

    async def read(user, *since):  # The recovery's privacy read waits until the test says.
        if running('answer_alone'):
            reading.set()
            await release.wait()
        return await erasure(user, *since)

    async def refetch(mid):  # And so does the rerun's fetch.
        if running('rerun'):
            fetching.set()
            await fetched.wait()
        return fetch(mid)
    agent.erasure, channel.fetch_message.side_effect = read, refetch
    if cancel_at_once:
        schedule = agent.answer_edits
        def answer_edits():
            before = set(agent.reruns)
            schedule()
            for task in agent.reruns - before:
                task.cancel()
        agent.answer_edits = answer_edits
    question, detail, tasks = await gave_way(agent, model, message, gathering)
    agent.edit(1, edited(rows, detail))  # The question is to be answered alone after all...
    await asyncio.wait_for(reading.wait(), 5)
    gathering[1].set()
    await asyncio.wait_for(tasks[1], 5)  # The line's handler is done: its pending mark went with it.
    agent.edit(1, edited(rows, question, EDITED))  # ...but they edit it while its recovery reads.
    release.set()
    return question, tasks, fetching, fetched, release


def ambient_history(channel, rows):
    """Attention's last look reads what came after its pick, too."""
    before = channel.history
    async def history(**kwargs):
        if 'before' in kwargs:
            async for item in before(**kwargs):
                yield item
            return
        for item in sorted((m for m in rows.values() if m.id > kwargs['after'].id), key=lambda m: m.id):
            yield item
    channel.history = history


def test_attention_waits_for_an_edited_questions_rerun_until_it_is_handled(monkeypatch, tmp_path):
    """fri #228: the edited question's recovery is over and its rerun waits on its fetch. Nothing of theirs is busy,
    pending or in their queue, but the question is still to be answered as edited: ambient attention doesn't answer
    an earlier message of theirs before it. The question is answered once, as edited."""
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        model = Model(fri, tmp_path, waits={1: 'before'})
        gathering = gathered(monkeypatch)
        ambient_history(channel, rows)
        earlier = message('anyone know if the server is down?')  # Not said to Fri.
        tasks, release, fetched = [], None, None
        try:
            question, tasks, fetching, fetched, release = await edited_while_recovering(
                agent, model, message, gathering, channel, rows)
            await asyncio.wait_for(fetching.wait(), 5)
            assert 20 not in agent.turns.arrived and 20 not in fri.busy  # Its reservation has ended...
            assert (channel.id, 20) not in agent.pending and not agent.handling[question.id]
            assert not agent.ambient_ready(channel.id, 20)  # ...but it hasn't been answered yet.
            await agent.ambient_reply(channel, {'id': str(earlier.id)})
            earlier.reply.assert_not_awaited()
            fetched.set()
            await until(lambda: len(gathering) == 3)  # Taken in again as it reads now: its own burst, just it.
            gathering[2].set()
            await until(lambda: not agent.reruns, 10)
            question.reply.assert_awaited_once()  # Answered as edited, and nothing else answered first.
            assert [text for text, _ in model.requests] == [EDITED]
            agent.last_spoke.clear()
            assert agent.ambient_ready(channel.id, 20)  # Once it's done, after the usual cooldown.
        finally:
            model.hold[1].set()
            for event in (release, fetched, *gathering):
                if event:
                    event.set()
            await finish(fri, tasks)
    asyncio.run(scenario())


@pytest.mark.parametrize('when', ['before it starts', 'during its fetch'])
def test_an_edited_questions_rerun_cancelled_lets_attention_come_back(monkeypatch, tmp_path, when):
    """fri #228's counterpart: the rerun is cancelled (as teardown does) before its first step, so its own cleanup
    never runs, or while it fetches. Either way nothing of theirs is still waiting to be answered, so attention
    comes back after the usual cooldown."""
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        model = Model(fri, tmp_path, waits={1: 'before'})
        gathering = gathered(monkeypatch)
        tasks, release, fetched = [], None, None
        try:
            question, tasks, fetching, fetched, release = await edited_while_recovering(
                agent, model, message, gathering, channel, rows, cancel_at_once=when == 'before it starts')
            if when == 'during its fetch':
                await asyncio.wait_for(fetching.wait(), 5)
                [rerun] = agent.reruns
                rerun.cancel()
                await asyncio.gather(rerun, return_exceptions=True)
                assert rerun.cancelled()
            await until(lambda: not agent.reruns, 10)
            assert question.id not in agent.reanswer  # It was scheduled...
            assert fetching.is_set() == (when == 'during its fetch')  # ...and, cancelled at once, never fetched.
            question.reply.assert_not_awaited()
            assert agent.ledger.snapshot()['in_flight'] == 0
            agent.last_spoke.clear()
            assert agent.ambient_ready(channel.id, 20)
        finally:
            model.hold[1].set()
            for event in (release, fetched, *gathering):
                if event:
                    event.set()
            await finish(fri, tasks)
    asyncio.run(scenario())


def server_error():
    return discord.HTTPException(SimpleNamespace(status=500, reason='Internal Server Error'), 'synthetic')


def failing_rerun_fetch(channel, failures, error=server_error, during=None):
    """From now, the rerun's first `failures` fetches fail (`during(n)` runs as the n-th, from 1, fails); later ones
    read it. Returns the rerun's fetches, made so far."""
    fetch, made = channel.fetch_message.side_effect, []
    async def refetch(mid):
        if asyncio.current_task().get_coro().__name__ == 'rerun':
            made.append(mid)
            if len(made) <= failures:
                if during:
                    during(len(made))
                raise error()
        return await fetch(mid)
    channel.fetch_message.side_effect = refetch
    return made


def test_an_edited_questions_rerun_whose_fetch_keeps_failing_is_tried_again_then_settles_quietly(monkeypatch, tmp_path):
    """fri #243, the issue's ordering: the edited question's rerun can't fetch it, a server error each time. That's
    no sign it was deleted: it's tried again, a few times. Still unread, it's settled quietly as a failed fetch,
    not as deleted: Fri can't tell it still stands, or that its author hasn't erased since, so it says nothing."""
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        model = Model(fri, tmp_path, waits={1: 'before'})
        gathering = gathered(monkeypatch)
        tasks, release, fetched = [], None, None
        try:
            question, tasks, fetching, fetched, release = await edited_while_recovering(
                agent, model, message, gathering, channel, rows)
            fetched.set()
            made = failing_rerun_fetch(channel, 99)
            await until(lambda: not agent.reruns, 10)
            assert len(made) == 1+len(REFETCH_BACKOFF)  # Tried again after each backoff, not taken as deleted.
            assert not model.requests  # Nothing it can't read reaches the model...
            question.reply.assert_not_awaited()  # ...and nothing goes out.
            outcomes = agent.ledger.snapshot()
            assert outcomes['quiet'].get('fetch failed: HTTPException') == 1
            assert 'message deleted' not in outcomes['quiet'] and outcomes['in_flight'] == 0
            agent.last_spoke.clear()
            assert agent.ambient_ready(channel.id, 20)
        finally:
            model.hold[1].set()
            for event in (release, fetched, *gathering):
                if event:
                    event.set()
            await finish(fri, tasks)
    asyncio.run(scenario())


@pytest.mark.parametrize('error', [server_error, TimeoutError, lambda: ConnectionResetError(54, 'reset by peer')],
                         ids=['server error', 'timeout', 'connection reset'])
def test_an_edited_questions_rerun_reads_it_once_a_failed_fetch_goes_through(monkeypatch, tmp_path, error):
    """fri #243: one failed fetch, then Discord shows it: the question is answered as edited, once, with no notice.
    A connection dropped under discord.py's own retries counts as failed too, not as the end of it (fri #249)."""
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        model = Model(fri, tmp_path, waits={1: 'before'})
        gathering = gathered(monkeypatch)
        tasks, release, fetched = [], None, None
        try:
            question, tasks, fetching, fetched, release = await edited_while_recovering(
                agent, model, message, gathering, channel, rows)
            fetched.set()
            made = failing_rerun_fetch(channel, 1, error)
            await until(lambda: len(gathering) == 3)  # Taken in again as it reads now: its own burst, just it.
            gathering[2].set()
            await until(lambda: not agent.reruns, 10)
            assert len(made) >= 2  # The one that failed, the one that read it, then any its turn's checks make.
            assert [text for text, _ in model.requests] == [EDITED]
            question.reply.assert_awaited_once()
            assert question.reply.await_args.args[0] == ANSWER
        finally:
            model.hold[1].set()
            for event in (release, fetched, *gathering):
                if event:
                    event.set()
            await finish(fri, tasks)
    asyncio.run(scenario())


@pytest.mark.parametrize('error, reason', [
    (lambda: discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone'), 'message deleted'),
    (lambda: discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'no'), 'missing permissions'),
], ids=['not found', 'forbidden'])
def test_an_edited_questions_rerun_told_it_is_gone_or_closed_stays_quiet(monkeypatch, tmp_path, error, reason):
    """Only Discord saying it isn't there settles it as deleted, at once; one Fri may no longer read is a closed gate,
    quiet too. Neither is tried again or told anything."""
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        model = Model(fri, tmp_path, waits={1: 'before'})
        gathering = gathered(monkeypatch)
        tasks, release, fetched = [], None, None
        try:
            question, tasks, fetching, fetched, release = await edited_while_recovering(
                agent, model, message, gathering, channel, rows)
            fetched.set()
            made = failing_rerun_fetch(channel, 99, error)
            await until(lambda: not agent.reruns, 10)
            assert len(made) == 1 and not model.requests
            question.reply.assert_not_awaited()
            outcomes = agent.ledger.snapshot()
            assert outcomes['quiet'].get(reason) and outcomes['in_flight'] == 0
        finally:
            model.hold[1].set()
            for event in (release, fetched, *gathering):
                if event:
                    event.set()
            await finish(fri, tasks)
    asyncio.run(scenario())


@pytest.mark.parametrize('deadline, waits, late, tries', [
    (60, [1, 2, 4], 0, 4), (5, [1, 2], 0, 3), (0, [], 0, 1), (5, [1], 9, 1)], ids=['room', 'short', 'none', 'woke late'])
def test_a_reruns_failed_fetch_is_tried_again_only_within_its_turns_deadline(monkeypatch, deadline, waits, late, tries):
    """The backoff never runs past the turn's deadline, QUEUE_SECONDS after the message was sent: a try that would
    start after it isn't made, even when a backoff wakes `late` seconds past its time."""
    async def scenario():
        clock, slept, fetches = [time.time()], [], []
        message_id = discord.utils.time_snowflake(discord.utils.utcnow())
        async def fetch(_):
            fetches.append(clock[0])
            raise server_error()
        async def sleep(delay):
            slept.append(delay)
            clock[0] += delay+late
        monkeypatch.setattr(fri_agent, 'time', SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic))
        monkeypatch.setattr(fri_agent.asyncio, 'sleep', sleep)
        monkeypatch.setattr(fri_agent, 'QUEUE_SECONDS', deadline)
        agent = Agent(SimpleNamespace(get_channel=lambda _: SimpleNamespace(fetch_message=fetch)))
        await agent.rerun(1, message_id)
        assert slept == waits and len(fetches) == tries
    asyncio.run(scenario())
