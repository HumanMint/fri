"""What happened to the messages addressed to Fri, counted for its health file and "fri health": counts only."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_edit_reanswer import edit
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_unsend import delete, heard, model, no_debounce
from fri import feedback as commands
from fri.fri import CONVERSATION_CHANGED
from fri.fri_agent import QUEUE_SECONDS
from fri.fri_chat import ChatResult
from fri.fri_errors import UNREACHABLE, ControllerUnavailable
from fri.fri_ledger import REASONS, UNRESOLVED, Ledger, describe

EMPTY = {'told_why': {}, 'quiet': {}, 'withheld': {}, 'outage': 0, 'cooldown': 0}


def settled(outcomes):
    """The outcome buckets alone, without the clock, the kinds and the latency."""
    return {key: outcomes[key] for key in ('answered', *EMPTY)}


def test_answered_pings_names_and_dms_count_with_their_latency(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        agent.config['support_dms'] = True
        agent.member = AsyncMock(side_effect=lambda user, **_: SimpleNamespace(id=user))
        ping, named = message('<@99> what do you think?', user=20), message('fri what do you think?', user=21)
        dm = message('what do you think?', user=22)
        dm.guild = None
        chatter = message('just chatting', user=23)  # Not to Fri: not counted.
        try:
            for event in (ping, named, dm, chatter):
                await agent.handle(event)
            for event in (ping, named, dm):
                event.reply.assert_awaited_once()
            outcomes = agent.ledger.snapshot()
            assert outcomes['addressed'] == {'ping': 1, 'name': 1, 'dm': 1} and outcomes['in_flight'] == 0
            assert settled(outcomes) == {'answered': 3, **EMPTY}
            latency = outcomes['latency']
            assert latency['samples'] == 3 and 0 <= latency['p50'] <= latency['p90'] < 5
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['told why', 'quiet', 'notice', 'low battery', 'cooldown notice', 'outage'])
def test_each_refusal_is_counted_once_by_its_reason(monkeypatch, tmp_path, case):
    async def scenario():
        fri, agent, _, controller, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        event = message('fri you there?' if case in ('quiet', 'notice') else '<@99> you there?',
                        age=QUEUE_SECONDS-0.2 if case == 'told why' else 0)
        event.reply.return_value = SimpleNamespace(id=event.id+1, delete=AsyncMock(), content='')
        expected = {'answered': 0, **EMPTY}
        if case == 'told why':
            fri.busy.add(20)  # An earlier reply that doesn't finish in time.
            expected['told_why'] = {'earlier reply still running': 1}
        elif case == 'quiet':
            agent.config['enabled'] = False
            expected['quiet'] = {'fri disabled': 1}
        elif case == 'notice':  # The conversation changed before the model answered: told so.
            fri.chat_once.side_effect = commands.UserFeedbackCheckFailure(CONVERSATION_CHANGED)
            expected['told_why'] = {'conversation changed': 1}
        elif case == 'low battery':
            for _ in range(30):  # Spend the allowance; its one cooldown notice has already shown.
                controller.admit('20', 'public')
            expected['cooldown'] = 1
        elif case == 'cooldown notice':
            for _ in range(controller.state()['config']['public_turns']):  # This ping's admission is refused.
                controller.admit('20', 'public')
            expected['cooldown'] = 1
        else:
            agent.profile = AsyncMock(side_effect=ControllerUnavailable(UNREACHABLE))
            expected['outage'] = 1
        try:
            if case == 'outage':
                with pytest.raises(ControllerUnavailable):
                    await agent.handle(event)
            else:
                await agent.handle(event)
            assert settled(agent.ledger.snapshot()) == expected
            assert agent.ledger.snapshot()['latency']['samples'] == 0  # Only answers are timed.
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['nearby message deleted', 'chose quiet'])
def test_a_composed_reply_that_never_goes_out_is_withheld_but_a_chosen_quiet_is_not(monkeypatch, tmp_path, case):
    async def scenario():
        fri, agent, _, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        other = message('my plans for the weekend', user=21)
        # A ping is always answered in words (see force_text), so the quiet choice is a name's.
        trigger = message('<@99> what did they just say' if case == 'nearby message deleted' else 'fri what did they say')
        async def respond(context, ready):
            await ready()
            if case == 'nearby message deleted':  # Deleted while the model wrote its answer.
                await delete(fri, rows, other)
                return ChatResult(status='completed', reply='they said weekend plans')
            return ChatResult(status='completed', response_kind='quiet')
        model(fri, tmp_path, respond)
        try:
            await heard(agent, other)
            await asyncio.wait_for(agent.handle(trigger), 10)
            outcomes = settled(agent.ledger.snapshot())
            if case == 'nearby message deleted':
                assert outcomes == {'answered': 0, **EMPTY, 'withheld': {'nearby message deleted': 1}}
            else:
                assert outcomes == {'answered': 0, **EMPTY, 'quiet': {'chose quiet': 1}}
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_outcomes_reach_the_health_file_and_fri_health_without_ids_names_or_words(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        fri.data = tmp_path
        answered, refused = message('<@99> zebra question', user=20), message('fri zebra again', user=21)
        try:
            await agent.handle(answered)
            agent.config['enabled'] = False
            await agent.handle(refused)
            await fri.health.coro(fri)
            health = json.loads((tmp_path/'fri-health.json').read_text())
            outcomes = health['outcomes']
            assert outcomes['addressed'] == {'ping': 1, 'name': 1} and outcomes['answered'] == 1
            assert outcomes['quiet'] == {'fri disabled': 1} and outcomes['since'] <= health['at']
            text = json.dumps(outcomes)
            for private in (answered.id, refused.id, '"20"', '"21"', 'member 2', 'zebra'):
                assert str(private) not in text
            assert set(outcomes) == {'since', 'addressed', 'in_flight', 'answered', 'told_why', 'quiet', 'withheld',
                                     'outage', 'cooldown', 'latency'}
            line = next(line for line in (await fri.watcher.report()).splitlines() if line.startswith('- addressed'))
            assert line.startswith('- addressed to me since <t:') and ':R>: 2 (1 ping, 1 by name), 1 answered (p50 ' in line
            assert line.endswith(', 1 quiet (fri disabled)')
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_the_ledger_caps_reasons_and_ranks_latency(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('fri.fri_ledger.time', SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))
    ledger = Ledger()
    ids = iter(range(1, 10**6))
    def one(kind='dm'):
        i = next(ids)
        ledger.track(SimpleNamespace(id=i), kind)
        return i
    for seconds in range(1, 11):  # Ten answers, 1 s to 10 s after they came in.
        i = one('ping')
        clock[0] += seconds
        ledger.answered([i])
    for n in range(REASONS+2):
        i = one('name')
        ledger.settle(i, f'gate {n}')
    i = one()
    ledger.miss(i, 'first reason')
    ledger.settle(i, 'later reason')
    outcomes = ledger.snapshot()
    assert outcomes['addressed'] == {'ping': 10, 'name': REASONS+2, 'dm': 1} and outcomes['in_flight'] == 0
    assert outcomes['latency'] == {'p50': 5.0, 'p90': 9.0, 'samples': 10}
    assert len(outcomes['quiet']) == REASONS+1 and outcomes['quiet']['other'] == 3  # Named ones, then "other".
    assert 'first reason' not in outcomes['quiet']  # Past the cap too.
    line = describe(outcomes)
    assert line.startswith('- addressed to me since <t:1000:R>: 53 (10 pings, 42 by name, 1 dm), 10 answered (p50 5s, p90 9s)')
    assert line.endswith(f'{REASONS+3} quiet (other ×3, gate 0, gate 1, …)')


def test_a_reply_to_fri_found_only_by_fetching_its_reference_counts_from_its_arrival(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        clock = [5000.0]
        monkeypatch.setattr('fri.fri_ledger.time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
        mine, theirs = message('the second one is better'), message('nah the first', user=21)
        mine.author = SimpleNamespace(id=99, display_name='Fri', bot=True)  # Fri's own earlier answer.
        reply, chatter = message('wait why', user=20), message('lol', user=22)
        # Discord left the replied-to message out, so only Fri's fetch of it shows who it answers.
        reply.reference = SimpleNamespace(message_id=mine.id, resolved=None)
        chatter.reference = SimpleNamespace(message_id=theirs.id, resolved=None)  # A reply to someone else.
        fetch, slow = channel.fetch_message.side_effect, []
        def fetched(mid):
            if mid == mine.id and not slow:  # The reference fetch takes 30 s; the latency still counts from arrival.
                slow.append(mid)
                clock[0] += 30
            return fetch(mid)
        channel.fetch_message.side_effect = fetched
        try:
            for event in (reply, chatter):
                await agent.handle(event)
            reply.reply.assert_awaited_once()
            chatter.reply.assert_not_awaited()
            outcomes = agent.ledger.snapshot()
            assert outcomes['addressed'] == {'name': 1} and outcomes['in_flight'] == 0
            assert settled(outcomes) == {'answered': 1, **EMPTY}
            assert outcomes['latency'] == {'p50': 30.0, 'p90': 30.0, 'samples': 1}
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['name dropped', 'gone before the rerun'])
def test_an_edit_while_fri_composes_still_settles_its_message_once(monkeypatch, tmp_path, case):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri whats teh best album')
        edited = []
        async def respond(context, ready):
            await ready()
            if not edited:  # Edited while the model wrote its answer: it's answered again as it reads now.
                edited.append(await edit(fri, rows, trigger, 'whats the best album' if case == 'name dropped'
                                         else 'fri whats the best album'))
            return ChatResult(status='completed', reply='that one')
        model(fri, tmp_path, respond)
        try:
            await agent.handle(trigger)
            if case == 'gone before the rerun':
                channel.fetch_message.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='Not Found'),
                                                                     'gone')
            await asyncio.gather(*agent.reruns)
            assert not trigger.reply.await_count and not edited[0].reply.await_count
            outcomes = agent.ledger.snapshot()
            assert outcomes['addressed'] == {'name': 1} and outcomes['in_flight'] == 0
            reason = 'not a visible user mention' if case == 'name dropped' else 'message deleted'
            assert settled(outcomes) == {'answered': 0, **EMPTY, 'quiet': {reason: 1}}
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_rerun_cancelled_while_it_fetches_counts_as_cancelled_once(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri whats teh best album')
        edited, fetching = [], asyncio.Event()
        async def respond(context, ready):
            await ready()
            if not edited:
                edited.append(await edit(fri, rows, trigger, 'fri whats the best album'))
            return ChatResult(status='completed', reply='that one')
        model(fri, tmp_path, respond)

        async def hang(*_):  # The rerun's fetch of the edited message never returns.
            fetching.set()
            await asyncio.Event().wait()
        try:
            await agent.handle(trigger)
            channel.fetch_message.side_effect = hang
            await asyncio.wait_for(fetching.wait(), 5)
            for task in list(agent.reruns):
                task.cancel()
            await asyncio.gather(*agent.reruns, return_exceptions=True)
            outcomes = agent.ledger.snapshot()
            assert outcomes['in_flight'] == 0
            assert settled(outcomes) == {'answered': 0, **EMPTY, 'quiet': {'CancelledError': 1}}
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_an_edit_answered_again_counts_its_latency_from_the_first_arrival(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        clock = [5000.0]
        monkeypatch.setattr('fri.fri_ledger.time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
        trigger = message('fri whats teh best album')
        edited = []
        async def respond(context, ready):
            await ready()
            if not edited:  # A 100 s first try, refused for the edit, then a 10 s answer to the edit.
                clock[0] += 100
                edited.append(await edit(fri, rows, trigger, 'fri whats the best album'))
            else:
                clock[0] += 10
            return ChatResult(status='completed', reply='that one')
        model(fri, tmp_path, respond)
        try:
            await agent.handle(trigger)
            await asyncio.gather(*agent.reruns)
            assert edited[0].reply.await_count == 1
            outcomes = agent.ledger.snapshot()
            assert outcomes['addressed'] == {'name': 1} and settled(outcomes) == {'answered': 1, **EMPTY}
            assert outcomes['latency'] == {'p50': 110.0, 'p90': 110.0, 'samples': 1}
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_the_ledger_keeps_an_edit_answered_again_as_it_first_came_in_and_counts_unconfirmed_replies_never(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('fri.fri_ledger.time', SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))
    ledger = Ledger()
    first, second, third, reply, chatter = (SimpleNamespace(id=i) for i in range(1, 6))
    for message in (first, second, third):
        ledger.track(message, 'name')
        ledger.miss(message.id, 'message edited')
        ledger.park(message.id)  # To be answered again as it reads now.
    parked = ledger.parked(third.id)
    assert ledger.snapshot()['in_flight'] == 3 and not ledger.snapshot()['addressed']
    clock[0] += 40
    ledger.track(first, None)  # Its rerun: no longer addressed, still counted as it came in.
    ledger.answered([first.id])
    ledger.track(second, 'ping')
    ledger.abandon(second.id, ledger.parked(second.id), 'should not count')  # Its rerun's handling took it.
    ledger.settle(second.id, 'not a visible user mention')
    ledger.abandon(third.id, parked, 'message deleted')  # Its rerun ended before handling it again.
    ledger.abandon(third.id, parked, 'message deleted')  # Once.
    ledger.track(reply, UNRESOLVED)
    ledger.track(chatter, UNRESOLVED)
    assert ledger.snapshot()['in_flight'] == 0  # Not known to be to Fri yet.
    ledger.confirm(reply.id)  # Its reference fetch found Fri's message.
    ledger.answered([reply.id, chatter.id])
    outcomes = ledger.snapshot()
    assert outcomes['addressed'] == {'name': 4} and outcomes['in_flight'] == 0 and outcomes['answered'] == 2
    assert outcomes['quiet'] == {'not a visible user mention': 1, 'message deleted': 1}
    assert outcomes['latency'] == {'p50': 0.0, 'p90': 40.0, 'samples': 2}
