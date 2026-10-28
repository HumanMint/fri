"""fri #187: Fri's echo of its own public reply, kept in the channel's live view, doesn't outlive the reply's retirement.

Gene retires a saved reply that drew on the member's summary once that summary goes (gene #112): /chat/messages/sources
reports it as no longer verbatim. The echo of it in the live view is rechecked there before a later turn reuses it,
before every request, and before delivery. Against the real controller; an older one runs Fri as before."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from compat_tests.test_fri_member_profiles import GENERAL, model, run, world
from compat_tests.test_fri_member_summaries import CONTINUATION
from compat_tests.test_fri_summary_generations import deleted, summarized_from
from fri import feedback as commands
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
    """A summary-derived reply whose summary is still there, and an ordinary one, which costs no extra request."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await summarized_from(monkeypatch, w)
            model(w, recalls)
            await echoed(w)
            before = len(w.calls)
            assert await echoed(w, 'fri hi', user=30) == [CONTINUATION, 'nothing to recall']
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
