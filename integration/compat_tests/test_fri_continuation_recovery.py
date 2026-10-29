"""A question that gave way to a short line's burst (round-2 MA2), when that line goes: its recovery and notices."""

import asyncio

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
