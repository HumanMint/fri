"""A long heartbeat stage that keeps finishing work isn't stuck; one that stops is, and a model call can't hide it."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from gene.api import create_app

from compat_tests.test_api import headers
from compat_tests.test_fri_chat import response_event
from compat_tests.test_fri_heartbeat import heartbeat as heartbeat  # noqa: F401
from compat_tests.test_fri_learning import HistoryChannel
from compat_tests.test_fri_learning import learning as learning  # noqa: F401
from compat_tests.test_fri_memes import attachment, build
from fri.fri_agent import Agent
from fri.fri_agent_model import ATTENTION, analyze
from fri.fri_chat import Chat
from fri.fri_watch import STALL, Watch


def clocks(monkeypatch):
    """One fake monotonic clock for the tick's progress marks and the watch that reads them."""
    clock = [1000.0]
    fake = SimpleNamespace(time=time.time, monotonic=lambda: clock[0])
    monkeypatch.setattr('fri.fri_agent.time', fake)
    monkeypatch.setattr('fri.fri_watch.time', fake)
    return clock


def watched(agent):
    """What the owner's watch makes of the heartbeat now: 'ok' or 'stalled'."""
    return Watch(SimpleNamespace(agent=agent)).stalled()[0]


def answering(content):
    return lambda request: httpx.Response(200, text='data: '+json.dumps(response_event(content=content))+'\n\n')


QUIET = json.dumps({'action': 'quiet', 'source_id': '', 'reason': ''})


def test_a_model_call_that_only_keeps_its_stream_alive_ends_at_its_deadline(monkeypatch):
    monkeypatch.setattr('fri.fri_agent_model.DEADLINE', 0.3, raising=False)

    class KeepAlive(httpx.AsyncByteStream):  # SSE comments only: never an event, never an idle read timeout.
        async def __aiter__(self):
            while True:
                yield b': keepalive\n\n'
                await asyncio.sleep(0.02)

    def handler(request):
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=KeepAlive())
    chat = Chat(SimpleNamespace(access_token=lambda: 'plan-auth'), httpx.MockTransport(handler))

    async def scenario():
        return await asyncio.wait_for(analyze(chat, 'Decide.', {}, 'gpt-6-luna', 'low', ATTENTION), 5)
    assert asyncio.run(scenario()) == ({}, {}, 'interrupted')


def test_a_deadline_while_the_stream_closes_after_completing_is_interrupted_not_an_empty_completion(monkeypatch):
    """A completed answer whose stream then takes past DEADLINE to close must not read as a completed empty result:
    a member summary saved from that would be emptied."""
    monkeypatch.setattr('fri.fri_agent_model.DEADLINE', 0.1, raising=False)

    class SlowClose(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ('data: '+json.dumps(response_event(content=QUIET))+'\n\n').encode()

        async def aclose(self):
            await asyncio.sleep(0.5)

    def handler(request):
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=SlowClose())
    chat = Chat(SimpleNamespace(access_token=lambda: 'plan-auth'), httpx.MockTransport(handler))

    async def scenario():
        return await asyncio.wait_for(analyze(chat, 'Decide.', {}, 'gpt-6-luna', 'low', ATTENTION), 5)
    result, _, status = asyncio.run(scenario())
    assert (result, status) == ({}, 'interrupted')


def test_work_attention_stops_waiting_for_never_marks_the_ticks_progress(heartbeat):  # noqa: F811
    from fri.fri_agent_model import PROGRESS, progress
    agent, marks = heartbeat.agent, []

    async def scenario():
        PROGRESS.set(lambda: marks.append('tick'))
        progress()  # The tick's own step.

        async def work():
            progress()  # Bounded work, even while attention still waits for it.
            return 'done'
        task = await agent.bounded(work())
        assert task is not None and task.result() == 'done'
    asyncio.run(scenario())
    assert marks == ['tick']


def test_a_model_call_after_long_reads_starts_its_own_clock_and_outside_calls_never_move_the_tick(
        heartbeat, monkeypatch):  # noqa: F811
    clock, agent, seen = clocks(monkeypatch), heartbeat.agent, []
    agent.record = AsyncMock()

    def slow(request):  # The call itself takes 500 s.
        clock[0] += 500
        seen.append(watched(agent))
        return answering(QUIET)(request)
    heartbeat.fri.chat = Chat(SimpleNamespace(access_token=lambda: 'plan-auth'), httpx.MockTransport(slow))

    async def attend():  # 500 s of reads with no mark of their own, then the call: 1000 s in all.
        clock[0] += 500
        assert (await agent.run('attention', 'Decide.', {}, ATTENTION))[2] == 'completed'
        seen.append(watched(agent))
    agent.attend = attend
    asyncio.run(agent.heartbeat.coro(agent))
    assert seen == ['ok', 'ok'] and agent.stages['attention']['error'] is None

    async def scenario():  # Stuck in memes, while a member's message gets a model call of its own.
        entered, release = asyncio.Event(), asyncio.Event()
        async def stuck():
            entered.set()
            await release.wait()
        agent.attend, agent.check_memes = AsyncMock(), stuck
        tick = asyncio.create_task(agent.heartbeat.coro(agent))
        await asyncio.wait_for(entered.wait(), 5)
        moved = agent.heartbeat_progress
        clock[0] += STALL
        await agent.run('attention', 'Decide.', {}, ATTENTION)
        assert agent.heartbeat_progress == moved and watched(agent) == 'stalled'
        agent.progressed()  # Outside the tick too: no mark.
        assert agent.heartbeat_progress == moved
        release.set()
        await asyncio.wait_for(tick, 5)
    asyncio.run(scenario())


def test_a_long_learning_batch_keeps_the_tick_moving_message_by_message(learning, monkeypatch):  # noqa: F811
    for name in ('fri.fri_workspace.deliver_one', 'fri.fri_social.deliver_one', 'fri.fri_fixes.check'):
        monkeypatch.setattr(name, AsyncMock())
    wall, _, settings, scheduler = learning
    scheduler.update({'history_batch': 12}, 1, 'owner')
    client = TestClient(create_app(settings))
    channel = HistoryChannel()
    for i in range(12):
        channel.add(wall[0]-200+i)
    clock, seen = clocks(monkeypatch), []

    async def api(method, path, payload=None):
        response = client.request(method, path, json=payload, headers=headers('fri'))
        assert response.status_code == 200, response.text
        return response.json()

    async def member(user):  # Each member read takes 100 s: twelve of them pass the stall threshold.
        clock[0] += 100
        seen.append(watched(agent))
        return SimpleNamespace(roles=[SimpleNamespace(id=90)])

    fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api, capabilities=frozenset(),
                          get_channel=lambda _: channel, get_user=lambda _: SimpleNamespace(send=AsyncMock()))
    agent = Agent(fri)
    agent.member = member
    agent.profile = AsyncMock(side_effect=lambda channel, user: scheduler.context(str(channel), str(user)))
    agent.run = AsyncMock(return_value=({'summary': 'Music chat.', 'notes': []}, {}, 'completed'))
    agent.attend, agent.send_reports = AsyncMock(), AsyncMock()
    asyncio.run(agent.heartbeat.coro(agent))
    assert agent.stages['learning']['error'] is None and len(agent.run.await_args.args[2]['messages']) == 12
    assert len(seen) == 12 and 12*100 > STALL and set(seen) == {'ok'}


def test_two_long_meme_inspections_keep_the_tick_moving(learning, tmp_path, monkeypatch):  # noqa: F811
    h = build(learning, tmp_path, monkeypatch)
    clock, seen = clocks(monkeypatch), []
    for mid in (100, 101):
        h.add(mid=mid, attachments=[attachment()])

    async def inspect(*args, **kwargs):  # Ten minutes each: twenty in all.
        clock[0] += 600
        seen.append(watched(h.agent))
        return {'reaction': '💀', 'confidence': 'low', 'observation': ''}, {}, 'completed'
    h.agent.run = AsyncMock(side_effect=inspect)
    h.agent.learn, h.agent.attend, h.agent.send_reports = AsyncMock(), AsyncMock(), AsyncMock()
    asyncio.run(Agent.heartbeat.coro(h.agent))
    assert h.agent.stages['memes']['error'] is None
    assert seen == ['ok', 'ok']


@pytest.mark.parametrize('where', ['a stage', 'a model call'])
def test_a_tick_that_stops_moving_is_still_stuck(heartbeat, monkeypatch, where):  # noqa: F811
    clock, agent = clocks(monkeypatch), heartbeat.agent
    agent.record = AsyncMock()

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def hung(*args):
            entered.set()
            await release.wait()
            return answering(QUIET)(None) if args else None
        if where == 'a stage':
            agent.check_memes = hung
        else:  # A request that never ends: its deadline is far off on this clock, so only the watch can tell.
            transport = httpx.MockTransport(hung)
            heartbeat.fri.chat = Chat(SimpleNamespace(access_token=lambda: 'plan-auth'), transport)
            agent.attend = lambda: agent.run('attention', 'Decide.', {}, ATTENTION)
        tick = asyncio.create_task(agent.heartbeat.coro(agent))
        await asyncio.wait_for(entered.wait(), 5)
        clock[0] += STALL
        assert watched(agent) == 'stalled'
        release.set()
        await asyncio.wait_for(tick, 5)
    asyncio.run(scenario())
