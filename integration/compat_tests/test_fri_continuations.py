"""A short line a member adds while Fri composes (round-2 MA2): the turn it joins answers both, once."""

import asyncio
import copy
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_timing import no_debounce
from fri.fri_actions import Actions
from fri.fri_chat import ChatResult
from fri.fri_context import discord_source

QUESTION = 'fri whats a good starter mod'
DETAIL = 'for minecraft 1.21'
ANSWER = 'fabric api and sodium. obviously'


class Model:
    """Fri's real last checks before each request (where a turn commits), around a fake model. `waits` holds a
    request, by number from 1, 'before' its last checks or 'after' them (sent): `reached[n]` is set, then it
    waits for `hold[n]`."""

    def __init__(self, fri, tmp_path, *results, waits=None):
        fri.data = tmp_path
        fri.chat_once = type(fri).chat_once.__get__(fri)
        fri.chat = SimpleNamespace(respond=self.respond)
        self.results = list(results) or [{'reply': ANSWER}]
        self.waits, self.requests, self.calls = waits or {}, [], 0
        self.reached = {n: asyncio.Event() for n in self.waits}
        self.hold = {n: asyncio.Event() for n in self.waits}

    async def pause(self, n, when):
        if self.waits.get(n) == when:
            self.reached[n].set()
            await self.hold[n].wait()

    async def respond(self, text, context, *, ready=None):
        self.calls += 1
        n = self.calls
        await self.pause(n, 'before')
        await ready()
        self.requests.append((text, context))
        await self.pause(n, 'after')
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return ChatResult(status='completed', **result)


def members(message):
    """environment()'s messages, whose replies Discord takes as posts of their own."""
    def made(*args, **kwargs):
        event = message(*args, **kwargs)
        event.reply.side_effect = lambda text, **_: SimpleNamespace(id=event.id+7, content=text, delete=AsyncMock())
        return event
    return made


async def until(condition, timeout=5):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def saved(calls):
    return [payload for path, payload in calls if path == '/chat/turn']


def burst_of(context):
    return [row['text'] for row in context.get('message_burst', [])]


def deleted(channel, rows, event):
    """Discord has it deleted from now on."""
    rows.pop(event.id)
    fetch = channel.fetch_message.side_effect
    def gone(mid):
        if mid == event.id:
            raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
        return fetch(mid)
    channel.fetch_message.side_effect = gone


def edited(rows, event, text='for minecraft 1.20'):
    current = copy.copy(event)
    current.content, current.edited_at = text, dt.datetime.now(dt.timezone.utc)
    rows[event.id] = current
    return current


async def finish(fri, tasks):
    await asyncio.gather(*tasks, return_exceptions=True)
    await fri.close()


def test_a_short_line_sent_while_fri_composes_is_answered_with_the_question_once(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        no_debounce(monkeypatch)
        model = Model(fri, tmp_path, waits={1: 'before'})
        question = message(QUESTION)
        tasks = [asyncio.create_task(agent.handle(question))]
        try:
            await asyncio.wait_for(model.reached[1].wait(), 5)  # Composing, its request not built yet.
            detail = message(DETAIL)  # No "fri" in it.
            tasks.append(asyncio.create_task(agent.handle(detail)))
            await until(lambda: tasks[1].done() or detail.id in agent.replying)
            model.hold[1].set()
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
            assert len(model.requests) == 1  # One model request...
            text, context = model.requests[0]
            assert text == DETAIL and burst_of(context) == [QUESTION, DETAIL]  # ...that reads both lines...
            detail.reply.assert_awaited_once()  # ...and one reply, to the newest line, as for any burst.
            assert detail.reply.await_args.args[0] == ANSWER
            question.reply.assert_not_awaited()
            [turn] = saved(calls)
            assert [row['source']['id'] for row in turn['burst']] == [str(question.id), str(detail.id)]
            assert sum(path == '/agent/admit' for path, _ in calls) == 1  # One exchange, one allowance.
            assert context['fence']['actions'].reopened  # A reopened burst completes no request.
        finally:
            await finish(fri, tasks)
    asyncio.run(scenario())


def test_a_short_line_after_the_request_went_out_is_answered_on_its_own_once_the_reply_is_saved(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        no_debounce(monkeypatch)
        model = Model(fri, tmp_path, {'reply': ANSWER}, {'response_kind': 'quiet', 'reply': ''}, waits={1: 'after'})
        question = message(QUESTION)
        tasks = [asyncio.create_task(agent.handle(question))]
        try:
            await asyncio.wait_for(model.reached[1].wait(), 5)  # Its request went out without the line.
            detail = message(DETAIL)
            tasks.append(asyncio.create_task(agent.handle(detail)))
            await until(lambda: tasks[1].done() or detail.id in agent.replying)
            model.hold[1].set()
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
            assert len(model.requests) == 2
            (first, before), (second, after) = model.requests
            assert first == QUESTION and burst_of(before) == []
            assert DETAIL not in [row['text'] for row in before['recent']]
            # Answered on its own after the first reply was delivered and saved, and allowed to stay quiet.
            assert second == DETAIL and burst_of(after) == []
            assert not after['force_text_reply'] and after['acknowledgments_enabled']
            assert [turn['response_kind'] for turn in saved(calls)] == ['reply', 'quiet']
            question.reply.assert_awaited_once()
            detail.reply.assert_not_awaited()  # Quiet, and no "still on your last one" notice while it waited.
        finally:
            await finish(fri, tasks)
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['late', 'another member', 'long', 'ping', 'dm', 'someone spoke between',
                                  'reply to someone', 'gene control', 'usage report'])
def test_other_lines_while_fri_composes_go_as_before(monkeypatch, tmp_path, case):
    async def scenario():
        fri, agent, _, _, channel, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        no_debounce(monkeypatch)
        model = Model(fri, tmp_path, waits={1: 'before'})
        reached, held = model.reached[1], model.hold[1]
        if case in ('gene control', 'usage report'):  # Their own paths, which take no line while in flight.
            api = fri.api
            async def checking(method, path, payload=None):
                if path in ('/controls/catalog', '/agent/usage'):
                    reached.set()
                    await held.wait()
                return await api(method, path, payload)
            fri.api = checking
        first = {'gene control': 'fri skip track', 'usage report': 'fri usage today'}.get(case, QUESTION)
        author = 10 if case == 'usage report' else 20  # The owner.
        question = message(first, user=author, age=6 if case == 'late' else 0)
        if case == 'dm':
            agent.config['support_dms'] = True
            question.guild = None
        tasks = [asyncio.create_task(agent.handle(question))]
        try:
            await asyncio.wait_for(reached.wait(), 5)
            if case == 'someone spoke between':
                tasks.append(asyncio.create_task(agent.handle(message('lol create mod', user=30))))
                await until(lambda: tasks[-1].done())
            text = {'long': 'for minecraft 1.21 ' + 'and also '*40, 'ping': '<@99> for minecraft 1.21'}.get(case, DETAIL)
            detail = message(text, user=30 if case == 'another member' else author)
            if case == 'dm':
                detail.guild = None
            if case == 'reply to someone':
                other = message('anyone got a modpack', user=30, age=60)
                detail.reference = SimpleNamespace(message_id=other.id, channel_id=1, resolved=None)
            tasks.append(asyncio.create_task(agent.handle(detail)))
            await until(lambda: tasks[-1].done() or detail.id in agent.replying)
            held.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            assert all(burst_of(context) == [] for _, context in model.requests)  # Never one turn for both.
            if case in ('ping', 'dm'):  # Spoken to Fri: its own turn after the first, as before.
                assert [text for text, _ in model.requests] == [QUESTION, DETAIL]
                assert question.reply.await_args.args[0] == detail.reply.await_args.args[0] == ANSWER
            else:
                detail.reply.assert_not_awaited()
                assert len(model.requests) == (case not in ('gene control', 'usage report'))
                assert not any(path == '/chat/turn' and payload['source']['id'] == str(detail.id)
                               for path, payload in calls)
        finally:
            held.set()
            await finish(fri, tasks)
    asyncio.run(scenario())

def test_a_line_from_before_an_erasure_does_not_join(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        no_debounce(monkeypatch)
        model = Model(fri, tmp_path, waits={1: 'before'})
        question = message(QUESTION)
        tasks = [asyncio.create_task(agent.handle(question))]
        try:
            await asyncio.wait_for(model.reached[1].wait(), 5)
            agent.forget(20)  # A privacy control elsewhere: what they said before it goes unanswered.
            detail = message(DETAIL)
            tasks.append(asyncio.create_task(agent.handle(detail)))
            await until(lambda: tasks[1].done() or detail.id in agent.replying)
            model.hold[1].set()
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
            detail.reply.assert_not_awaited()
            question.reply.assert_not_awaited()
            assert not saved(calls) and all(burst_of(context) == [] for _, context in model.requests)
        finally:
            model.hold[1].set()
            await finish(fri, tasks)
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['deleted', 'edited'])
def test_a_line_taken_back_before_the_turn_commits_leaves_the_question_answered_alone(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, _, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        model = Model(fri, tmp_path, waits={1: 'before'})
        question = message(QUESTION)
        tasks = [asyncio.create_task(agent.handle(question))]  # The usual 1.5 s wait for the rest of a burst.
        try:
            await asyncio.wait_for(model.reached[1].wait(), 5)
            detail = message(DETAIL)
            tasks.append(asyncio.create_task(agent.handle(detail)))
            await until(lambda: tasks[1].done() or agent.bursts.group(detail) is not None)  # Gathering.
            if change == 'deleted':
                deleted(channel, rows, detail)
                agent.unsend(1, [detail.id])
            else:
                agent.edit(1, edited(rows, detail))
            model.hold[1].set()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            detail.reply.assert_not_awaited()
            question.reply.assert_awaited_once()
            assert question.reply.await_args.args[0] == ANSWER or change == 'edited'
            assert all(DETAIL not in burst_of(context) for _, context in model.requests)
            assert not any(DETAIL in [row['text'] for row in context['recent']] for _, context in model.requests)
            assert all(turn['source']['id'] == str(question.id) for turn in saved(calls))
        finally:
            model.hold[1].set()
            await finish(fri, tasks)
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['deleted', 'edited'])
def test_a_line_taken_back_while_its_turn_composes_is_neither_answered_nor_saved(monkeypatch, tmp_path, change):
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
            if change == 'deleted':
                deleted(channel, rows, detail)
                agent.unsend(1, [detail.id])
            else:
                agent.edit(1, edited(rows, detail))
            model.hold[2].set()
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
            assert len(model.requests) == 1 and burst_of(model.requests[0][1]) == [QUESTION, DETAIL]
            assert not saved(calls)  # Nothing it made of the line is kept...
            assert all(call.args[0] != ANSWER for call in detail.reply.await_args_list)  # ...or said.
            question.reply.assert_not_awaited()
            assert str(detail.id) in agent.withdrawn or detail.id in agent.unsent
        finally:
            for hold in model.hold.values():
                hold.set()
            await finish(fri, tasks)
    asyncio.run(scenario())


def test_a_question_edited_after_a_line_joined_drops_the_line_and_is_answered_as_edited(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        model = Model(fri, tmp_path, waits={1: 'before'})
        question = message(QUESTION)
        tasks = [asyncio.create_task(agent.handle(question))]
        try:
            await asyncio.wait_for(model.reached[1].wait(), 5)
            detail = message(DETAIL)
            tasks.append(asyncio.create_task(agent.handle(detail)))
            await until(lambda: tasks[1].done() or agent.bursts.group(detail) is not None)
            agent.edit(1, edited(rows, question, 'fri whats a good starter modpack'))  # The line was let in for that.
            assert agent.bursts.group(detail) is None
            model.hold[1].set()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            await until(lambda: not agent.reruns, 10)  # Answered again as it reads now (see Agent.rerun).
            detail.reply.assert_not_awaited()
            assert all(DETAIL not in burst_of(context) for _, context in model.requests)
            assert [text for text, _ in model.requests][-1] == 'fri whats a good starter modpack'
        finally:
            model.hold[1].set()
            await finish(fri, tasks)
    asyncio.run(scenario())


def test_only_one_line_is_answered_after_the_turn_it_followed(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        model = Model(fri, tmp_path, {'reply': ANSWER}, {'response_kind': 'quiet', 'reply': ''}, waits={1: 'after'})
        question = message(QUESTION)
        tasks = [asyncio.create_task(agent.handle(question))]
        try:
            await asyncio.wait_for(model.reached[1].wait(), 5)
            detail = message(DETAIL)
            tasks.append(asyncio.create_task(agent.handle(detail)))
            await asyncio.sleep(1.7)  # Its burst taken: a later line can't join it.
            again = message('or 1.20 idk')
            tasks.append(asyncio.create_task(agent.handle(again)))
            await until(lambda: tasks[2].done())  # Not let in: one follow-up already.
            model.hold[1].set()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            assert [text for text, _ in model.requests] == [QUESTION, DETAIL]
            again.reply.assert_not_awaited()
        finally:
            model.hold[1].set()
            await finish(fri, tasks)
    asyncio.run(scenario())


def test_a_reopened_burst_completes_no_request(monkeypatch, tmp_path):
    fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
    play, song = message('fri play'), message('never gonna give you up')
    actions = Actions(agent, 20, 1, True)
    actions.event, actions.revision = song, 0
    actions.conversation = {'message_burst': [{'source': discord_source(event).model_dump(), 'text': event.content}
                                              for event in (play, song)]}
    action = {'action': 'play', 'argument': song.content}
    assert actions.burst_permission(action)  # An ordinary burst: the narrow split play/volume handoff.
    actions.reopened = True  # A line let in while Fri composed: no new authority from it.
    assert actions.burst_permission(action) is None



def test_a_question_that_waited_out_its_turn_after_a_line_joined_says_nothing_of_its_own(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, calls, _ = environment(monkeypatch, tmp_path, channel_id=1)
        message = members(message)
        no_debounce(monkeypatch)
        monkeypatch.setattr('fri.fri_agent.QUEUE_SECONDS', 3)
        model = Model(fri, tmp_path)
        agent.turns.take(20, 1)  # Another reply of theirs (a panel's, say) holds their turn.
        question = message(QUESTION, age=2.5)  # Its queue deadline passes while it waits; the line's doesn't.
        tasks = [asyncio.create_task(agent.handle(question))]
        try:
            await until(lambda: question.id in agent.replying)
            detail = message(DETAIL)
            tasks.append(asyncio.create_task(agent.handle(detail)))
            await until(lambda: tasks[1].done() or detail.id in agent.replying)
            await asyncio.wait_for(tasks[0], 5)  # Past its deadline, after the line joined: no stale notice.
            question.reply.assert_not_awaited()
            agent.turns.release(20)
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
            [(text, context)] = model.requests
            assert text == DETAIL and burst_of(context) == [QUESTION, DETAIL]
            detail.reply.assert_awaited_once()
            assert len(saved(calls)) == 1
        finally:
            await finish(fri, tasks)
    asyncio.run(scenario())
