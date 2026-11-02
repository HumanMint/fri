"""A turn's privacy fence at each boundary after an awaited read: its model requests and tool steps, and its last check
before sending, a rerun's memory listing included (fri #264, #265, #266)."""

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from compat_tests.test_fri_continuations import saved, until
from compat_tests.test_fri_edit_reanswer import MARKER, edit, model
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_rerun_fence import ERASURES, direct, erasing
from compat_tests.test_fri_timing import no_debounce
from fri.fri_chat import ChatResult


def place_of(fri, agent, rows, channel, message, place):
    """environment()'s member 20 in its channel, or in a DM with Fri: the channel, a message maker, its prefix."""
    if place == 'dm':
        return (*direct(fri, agent, rows), '')
    return channel, message, 'fri '


def rerunning():
    """Whether this runs as part of an edited question's rerun (see Agent.rerun)."""
    return asyncio.current_task().get_coro().__name__ == 'rerun'


@pytest.mark.parametrize('profiles', ['profiles off', 'profiles on'])
@pytest.mark.parametrize('place', ['server', 'dm'])
@pytest.mark.parametrize('erasure', ERASURES)
def test_a_reruns_memory_listing_is_held_to_its_baseline_after_its_read(monkeypatch, tmp_path, erasure, place,
                                                                         profiles):
    """fri #264: a question edited into "show my memory" is answered again as a listing, held to its first handling's
    baseline (fri #248). They erase once the listing's read has their note, before it returns: the listing went out,
    erased note and all. Checked again after that read, right before it's sent, nothing goes out. Another member's
    erasure changes nothing: the listing goes out, note included."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        if profiles == 'profiles on':
            fri.capabilities = fri.capabilities | {'member-profiles'}
        channel, message, prefix = place_of(fri, agent, rows, channel, message, place)
        store.save_chat('123', 'public:1' if place == 'server' else 'private:555', '20', 0, 'I like Minecraft',
                        'noted', 'model', 'high', notes=[{'category': 'games', 'evidence': 'I like Minecraft'}])
        trigger = message(prefix+'whats teh best ' + MARKER)
        requests, copies, read = [], [], []
        async def respond(text, context, ready):
            await ready()
            requests.append(text)
            copies.append(await edit(fri, rows, trigger, prefix+'show my memory'))
            return ChatResult(status='completed', reply='answer to the old words')
        model(fri, tmp_path, respond)
        api, erase = fri.api, erasing(agent, store, erasure)
        async def listing(method, path, payload=None):
            result = await api(method, path, payload)
            if path == '/chat/context' and rerunning() and not read:  # The listing's read: it has their note.
                read.append(result)
                erase()
            return result
        fri.api = listing
        try:
            await agent.handle(trigger)
            await until(lambda: not agent.reruns, 10)
            assert requests == [prefix+'whats teh best ' + MARKER] and read and 'Minecraft' in str(read[0])
            trigger.reply.assert_not_awaited()
            outcomes = agent.ledger.snapshot()
            assert outcomes['in_flight'] == 0
            if erasure == 'someone else':
                copies[0].reply.assert_awaited_once()
                assert 'Minecraft' in str(copies[0].reply.await_args)
                return
            copies[0].reply.assert_not_awaited()
            assert outcomes['quiet'].get('author privacy changed') == 1
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('place', ['server', 'dm'])
@pytest.mark.parametrize('turn', ['first handling', 'rerun'])
def test_a_forget_me_landing_before_a_model_request_keeps_the_question_from_it(monkeypatch, tmp_path, turn, place):
    """fri #265: their "forget me" comes in while a turn's last checks before its model request wait on Gene. Fri
    counts it at once, before Gene has it, so Gene's turn check passes and the question went to the model anyway
    (only the reply was held back after). The turn's fence carries that count beside the revision, checked after the
    request's last wait: nothing reaches the model, nothing goes out or is saved. On the rerun of an edited question
    (fri #248), and on a first handling."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        channel, message, prefix = place_of(fri, agent, rows, channel, message, place)
        trigger = message(prefix+'whats teh best ' + MARKER)
        requests, copies = [], []
        async def respond(text, context, ready):
            await ready()
            requests.append(text)
            if turn == 'rerun' and not copies:
                copies.append(await edit(fri, rows, trigger, prefix+'whats the best album'))
                return ChatResult(status='completed', reply='answer to the old words')
            return ChatResult(status='completed', reply='an answer')
        model(fri, tmp_path, respond)
        api, forgot = fri.api, []
        async def checked(method, path, payload=None):
            result = await api(method, path, payload)
            # The request's own check with Gene: the first one the turn makes (the rerun's, on a rerun).
            if path == '/chat/turn/check' and not forgot and (turn == 'first handling' or rerunning()):
                forgot.append(True)
                agent.forget(20, erased=True)  # As receive() takes in their "forget me", before Gene has it.
            return result
        fri.api = checked
        try:
            await agent.handle(trigger)
            await until(lambda: not agent.reruns, 10)
            assert forgot
            assert requests == ([] if turn == 'first handling' else [prefix+'whats teh best ' + MARKER])
            trigger.reply.assert_not_awaited()
            assert not any(made.reply.await_count for made in copies)
            assert not saved(calls) and agent.ledger.snapshot()['in_flight'] == 0
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('place', ['server', 'dm'])
def test_a_forget_me_landing_during_a_tool_steps_reads_stops_the_tool(monkeypatch, tmp_path, place):
    """fri #265 at a tool step: their "forget me" comes in while the step's own checks read Discord, after the model
    chose it. The step checks the turn's fence after those reads, before it acts: the tool never runs, and nothing
    goes out or is saved."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        channel, message, prefix = place_of(fri, agent, rows, channel, message, place)
        question = 'what is the current Python release?'
        trigger = message(prefix+question)
        found = {'kind': 'web_search', 'query': question, 'answer': 'public evidence',
                 'sources': ['https://www.python.org/downloads/'], 'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        lookup = AsyncMock(return_value=(found, {}, 'completed'))
        monkeypatch.setattr('fri.fri_actions.lookup', lookup)
        requests = []
        async def respond(text, context, ready):
            await ready()
            requests.append(sorted(context.get('actions', {})))
            if len(requests) == 1:
                return ChatResult(status='completed', action={'kind': 'web_search', 'query': question})
            return ChatResult(status='completed', reply='public evidence')
        model(fri, tmp_path, respond)
        from fri.fri_actions import Actions
        channels, forgot = Actions.channels, []
        async def reading(self, *args, **kwargs):
            result = await channels(self, *args, **kwargs)
            if requests and not forgot:  # The step's own access read, once the model chose it.
                forgot.append(True)
                agent.forget(20, erased=True)
            return result
        monkeypatch.setattr(Actions, 'channels', reading)
        try:
            await agent.handle(trigger)
            assert 'web_search' in requests[0] and forgot
            lookup.assert_not_awaited()
            assert len(requests) == 1
            trigger.reply.assert_not_awaited()
            assert not saved(calls) and agent.ledger.snapshot()['in_flight'] == 0
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('erasure', ERASURES)
def test_a_dm_reply_is_held_to_its_baseline_through_its_last_membership_lookup(monkeypatch, tmp_path, erasure):
    """fri #266: a DM reply passed Gene's last check, then they erased through Gene while Fri looked up, one last
    time, that they're still in the server. The reply went out, and its save was refused. After that lookup Fri
    checks their privacy with Gene again: nothing goes out or is saved. Their "forget me" here was caught already, and
    another member's erasure changes nothing."""
    async def scenario():
        fri, agent, store, _, _, _, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        channel, message = direct(fri, agent, rows)
        trigger = message('whats the best album')
        composed = []
        async def respond(text, context, ready):
            await ready()
            composed.append(True)
            return ChatResult(status='completed', reply='an answer')
        model(fri, tmp_path, respond)
        erase = erasing(agent, store, erasure)
        async def member(user, **kwargs):
            if composed and composed.pop():  # The lookup after the model: the reply's last before it goes out.
                erase()
            return SimpleNamespace(id=20)
        agent.member = AsyncMock(side_effect=member)
        try:
            await agent.handle(trigger)
            assert not composed  # That lookup ran.
            outcomes = agent.ledger.snapshot()
            assert outcomes['in_flight'] == 0
            if erasure == 'someone else':
                trigger.reply.assert_awaited_once()
                assert len(saved(calls)) == 1
                return
            trigger.reply.assert_not_awaited()
            assert not saved(calls) and outcomes['withheld'].get('author privacy changed') == 1  # Composed.
        finally:
            await fri.close()
    asyncio.run(scenario())
