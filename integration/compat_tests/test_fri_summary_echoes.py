"""fri #187: Fri's echo of its own public reply, kept in the channel's live view, doesn't outlive the reply's retirement.

Gene retires a saved reply that drew on the member's summary once that summary goes (gene #112): /chat/messages/sources
reports it as no longer verbatim. The echo of it in the live view is rechecked there before a later turn reuses it,
before every request, and before delivery. Against the real controller; an older one runs Fri as before."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from compat_tests.test_fri_member_profiles import GENERAL, model, run, world
from compat_tests.test_fri_member_summaries import CONTINUATION
from compat_tests.test_fri_summary_generations import deleted, summarized_from
from fri import feedback as commands
from fri import fri_agent
from fri.fri_chat import ChatResult

SOURCES = '/chat/messages/sources'


async def echoed(w, text='fri what were we on about', user=20):
    """Fri answers in #general through Agent.reply_once, which echoes a delivered public reply into the live view."""
    event = w.message(GENERAL, text, user)
    event.reply.side_effect = lambda reply, **_: SimpleNamespace(id=event.id+1, content=reply, edit=AsyncMock())
    await w.fri.agent.reply_once(event, text)
    return [row['text'] for row in w.fri.agent.messages(GENERAL) if row.get('fri')]


async def recalls(text, context):
    return ChatResult(status='completed', reply=CONTINUATION if context.get('member_summary') else 'nothing to recall')


def later(w, during=None, after=None):
    """Another member's turn in #general: what its requests carried from the live view."""
    carried = []

    async def respond(text, context, ready=None):
        if during:  # Once context is read, before the request's last checks.
            await during()
        await ready()
        carried.append([row['text'] for row in context.get('recent', [])])
        if after:  # While the model writes.
            await after()
        return ChatResult(status='completed', reply='sure')

    async def call(text, context):
        from fri.fri import Fri
        return await Fri.chat_once(w.fri, text, context)
    w.fri.chat = SimpleNamespace(respond=respond, headers=AsyncMock(return_value={'Authorization': 'Bearer test'}))
    w.fri.chat_once = AsyncMock(side_effect=call)
    return carried


def test_a_retired_summary_reply_leaves_the_channel_view(monkeypatch, tmp_path):
    """fri #187's repro: echo a summary-derived public reply, withdraw the summary's source, then a later turn."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await summarized_from(monkeypatch, w)
            model(w, recalls)
            assert await echoed(w) == [CONTINUATION]
            await deleted(w, events[0])
            carried = later(w)
            await w.say(GENERAL, 'fri what is going on', user=30)
            assert carried and CONTINUATION not in str(carried)
            assert CONTINUATION not in str(w.fri.agent.messages(GENERAL))  # Gone from the live view too.
        finally:
            await w.fri.close()
    run(scenario)


def test_a_summary_reply_retired_before_the_request_never_reaches_it(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await summarized_from(monkeypatch, w)
            model(w, recalls)
            await echoed(w)
            carried = later(w, during=lambda: deleted(w, events[0]))
            await w.say(GENERAL, 'fri what is going on', user=30)
            assert carried and CONTINUATION not in str(carried)
        finally:
            await w.fri.close()
    run(scenario)


def test_a_summary_reply_retired_while_the_model_writes_keeps_the_answer_unsaid(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await summarized_from(monkeypatch, w)
            model(w, recalls)
            await echoed(w)
            saved = len([path for path, _ in w.calls if path == '/chat/turn'])
            carried = later(w, after=lambda: deleted(w, events[0]))
            with pytest.raises(commands.UserFeedbackCheckFailure):
                await w.say(GENERAL, 'fri what is going on', user=30)
            assert CONTINUATION in str(carried)  # It was current when the request went.
            assert len([path for path, _ in w.calls if path == '/chat/turn']) == saved  # Nothing said or kept.
        finally:
            await w.fri.close()
    run(scenario)


def test_attention_drops_its_batch_when_a_summary_reply_in_it_is_retired(monkeypatch, tmp_path):
    """Attention's batch is rechecked after its waits (Agent.unwithdrawn), so a retired reply doesn't reach its model."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await summarized_from(monkeypatch, w)
            model(w, recalls)
            await echoed(w)
            records = await w.fri.agent.context_messages(GENERAL, raw_errors=True)
            assert CONTINUATION in str(records) and await w.fri.agent.unwithdrawn(GENERAL, records)
            await deleted(w, events[0])  # While attention waits for a slot.
            assert not await w.fri.agent.unwithdrawn(GENERAL, records)
        finally:
            await w.fri.close()
    run(scenario)


def test_current_replies_stay_in_view(monkeypatch, tmp_path):
    """A summary-derived reply whose summary is still there, and an ordinary one, which costs no extra request. The
    ordinary one comes first: a reply whose request carried a summary-derived one depends on it too (fri #193)."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await summarized_from(monkeypatch, w)
            model(w, recalls)
            before = len(w.calls)
            assert await echoed(w, 'fri hi', user=30) == ['nothing to recall']
            assert await echoed(w) == ['nothing to recall', CONTINUATION]
            carried = later(w)
            await w.say(GENERAL, 'fri what is going on', user=31)
            assert CONTINUATION in str(carried) and 'nothing to recall' in str(carried)
            asked = [payload['message_ids'] for path, payload in w.calls[before:] if path == SOURCES]
            assert asked and all(len(ids) == 1 for ids in asked)  # Only the summary-derived one is rechecked.
        finally:
            await w.fri.close()
    run(scenario)


def test_an_older_controller_runs_as_before(monkeypatch, tmp_path):
    """Without summary generations Fri can't tell which replies drew on the summary: it rechecks none, as before."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        w.fri.capabilities -= {'summary-generations'}
        try:
            await summarized_from(monkeypatch, w)
            model(w, recalls)
            before = len(w.calls)
            assert await echoed(w) == [CONTINUATION]
            carried = later(w)
            await w.say(GENERAL, 'fri what is going on', user=30)
            assert CONTINUATION in str(carried)
            assert SOURCES not in [path for path, _ in w.calls[before:]]
        finally:
            await w.fri.close()
    run(scenario)


def lookups(w, since=0):
    return [payload['message_ids'] for path, payload in w.calls[since:] if path == SOURCES]


def stalled(w, stuck):
    """The sources lookup gets no answer while stuck() holds; everything else answers as before."""
    api = w.fri.api

    async def call(method, path, payload=None):
        if path == SOURCES and stuck():
            w.calls.append((path, payload))
            await asyncio.sleep(3600)
        return await api(method, path, payload)
    w.fri.api = call


def test_an_ordinary_turn_rechecks_a_marked_echo_before_its_request_and_before_delivery_only(monkeypatch, tmp_path):
    """fri #193 item 3: reading the turn's context doesn't ask as well, since the request's own check follows it."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await summarized_from(monkeypatch, w)
            model(w, recalls)
            await echoed(w)
            before = len(w.calls)
            carried = later(w)
            await w.say(GENERAL, 'fri what is going on', user=30)
            assert CONTINUATION in str(carried)
            assert len(lookups(w, before)) == 2
        finally:
            await w.fri.close()
    run(scenario)


def test_a_recheck_that_gets_no_answer_costs_the_turn_one_short_wait(monkeypatch, tmp_path):
    """fri #193 item 3: the controller doesn't answer the lookup. The turn waits a few seconds once, not the API's 20
    at every check. The echo it couldn't confirm stays out of the request, so the reply, which never carried it, goes
    out and is saved."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        monkeypatch.setattr(fri_agent, 'RECHECK_TIMEOUT', 0.05, raising=False)
        try:
            await summarized_from(monkeypatch, w)
            model(w, recalls)
            await echoed(w)
            before = len(w.calls)
            stalled(w, lambda: True)
            carried = later(w)
            result, saved, _ = await asyncio.wait_for(w.say(GENERAL, 'fri what is going on', user=30), 5)
            assert carried and CONTINUATION not in str(carried)
            assert result.reply == 'sure' and saved
            assert len(lookups(w, before)) == 1
        finally:
            await w.fri.close()
    run(scenario)


def test_a_recheck_that_gets_no_answer_after_the_request_withholds_the_reply_after_one_short_wait(monkeypatch, tmp_path):
    """The request carried the echo, confirmed then, and the controller stops answering while the model writes. The
    echo can't be confirmed, so the answer stays unsaid, as for one retired then, after a few seconds, not 20."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        monkeypatch.setattr(fri_agent, 'RECHECK_TIMEOUT', 0.05, raising=False)
        try:
            await summarized_from(monkeypatch, w)
            model(w, recalls)
            await echoed(w)
            saved = len([path for path, _ in w.calls if path == '/chat/turn'])
            stuck = []
            stalled(w, lambda: bool(stuck))

            async def outage():
                stuck.append(True)
            carried = later(w, after=outage)
            with pytest.raises(commands.UserFeedbackCheckFailure):
                await asyncio.wait_for(w.say(GENERAL, 'fri what is going on', user=30), 5)
            assert CONTINUATION in str(carried)
            assert len([path for path, _ in w.calls if path == '/chat/turn']) == saved
        finally:
            await w.fri.close()
    run(scenario)


def test_a_turns_rechecks_reuse_what_it_learned(monkeypatch, tmp_path):
    """Within one turn's record: an echo Gene retired isn't asked about again, and once a lookup gets no answer, or the
    turn's time for them is spent, nothing is asked: every marked echo counts as retired. A new turn asks again."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        monkeypatch.setattr(fri_agent, 'RECHECK_TIMEOUT', 0.05)
        agent = w.fri.agent
        try:
            events = await summarized_from(monkeypatch, w)
            model(w, recalls)
            await echoed(w)
            rows = agent.messages(GENERAL)
            marked = {row['id'] for row in rows if row.get('fri')}
            stuck = [True]
            stalled(w, lambda: bool(stuck))
            turn = fri_agent.Rechecks()
            assert await agent.retired(rows, channel=GENERAL, turn=turn) == marked  # No answer: unconfirmed.
            stuck.clear()
            before = len(w.calls)
            assert await agent.retired(rows, channel=GENERAL, turn=turn) == marked
            assert lookups(w, before) == []  # Not asked again in that turn.
            assert await agent.retired(rows, channel=GENERAL, turn=fri_agent.Rechecks()) == set()  # A new turn asks.
            assert len(lookups(w, before)) == 1
            await deleted(w, events[0])
            turn = fri_agent.Rechecks()
            assert await agent.retired(rows, channel=GENERAL, turn=turn) == marked
            before = len(w.calls)
            assert await agent.retired(rows, channel=GENERAL, turn=turn) == marked
            assert lookups(w, before) == []  # Retired for good.
            assert not [row for row in agent.messages(GENERAL) if row['id'] in marked]  # Out of the live view too.
            monkeypatch.setattr(fri_agent, 'RECHECK_BUDGET', 0)
            assert await agent.retired(rows, channel=GENERAL, turn=fri_agent.Rechecks()) == marked
            assert lookups(w, before) == []  # No time left for any.
        finally:
            await w.fri.close()
    run(scenario)


def test_a_turn_with_no_marked_echo_asks_nothing(monkeypatch, tmp_path):
    """An ordinary echo, from a turn that drew on no summary, is never rechecked."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            model(w, recalls)
            assert await echoed(w) == ['nothing to recall']
            carried = later(w)
            await w.say(GENERAL, 'fri what is going on', user=30)
            assert 'nothing to recall' in str(carried)
            assert lookups(w) == []
        finally:
            await w.fri.close()
    run(scenario)
