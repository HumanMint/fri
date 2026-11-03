"""While the withdrawal queue is closed, a scheduled learning or exploration pass asks for no batch, and fri health says
why; a pass, however it stops, leaves no watch behind (fri #335)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from gene.api import create_app

from compat_tests.test_api import headers
from compat_tests.test_fri_learning import learning  # noqa: F401 (the fixture)
from compat_tests.test_fri_panel_and_learning_fences import batch
from fri.fri_agent import Agent
from fri.fri_turn_withdrawal import CLOSED_RECORD, SavedTurns


def closed(agent, tmp_path):
    """The withdrawal queue's record unreadable: every saved context held back (see SavedTurns.fail_closed)."""
    agent.fri.data = tmp_path
    (tmp_path/'withdrawals.json').write_text('{"pending": {')
    agent.saved_turns = SavedTurns(agent)
    assert agent.saved_turns.closed()


def test_a_scheduled_pass_asks_for_no_batch_while_the_withdrawal_queue_is_closed(learning, monkeypatch,  # noqa: F811
                                                                                tmp_path):
    """fri #335: with the queue's record unreadable, each scheduled pass still claimed a batch, reserving more of the
    channel each time, read it and stopped at its first model request (see Agent.retracted), its cursor kept. Now none
    is asked for while it's closed; once it reopens, the next pass runs as before."""
    async def scenario():
        agent, store, (first, second), finishes, asked, signing = batch(learning, monkeypatch)
        api, paths = agent.fri.api, []

        async def calls(method, path, payload=None):
            paths.append(path)
            return await api(method, path, payload)

        agent.fri.api = calls
        closed(agent, tmp_path)
        await agent.learn()
        await agent.learn()
        assert '/agent/learning/claim' not in paths and finishes == [] and asked == [] and not agent.passes
        agent.saved_turns.closed_until = 0  # Over, as it is after its horizon or by hand (see docs/operations.md).
        (tmp_path/CLOSED_RECORD).unlink()
        await agent.learn()
        assert paths.count('/agent/learning/claim') == 1
        assert [record['id'] for record in finishes[-1]['records']] == [first, second] and not finishes[-1]['error']

    asyncio.run(scenario())


def test_fri_health_says_learning_waits_while_the_queue_is_closed(learning, monkeypatch, tmp_path):  # noqa: F811
    """The owner's fri health names the pause beside the held-back line, once a pass was skipped for it."""
    from fri.fri_watch import Watch

    async def scenario():
        agent, *_ = batch(learning, monkeypatch)
        closed(agent, tmp_path)
        fri = agent.fri
        fri.agent, fri.guild_id, fri.owner_id = agent, 123, 10
        fri.get_guild, fri.is_ready = (lambda _: None), (lambda: True)
        fri.owner_reviews = fri.health = None
        fri.watching = None
        watch = Watch(fri)
        watch.probe = AsyncMock(return_value='unreachable')
        before = await watch.report()
        assert 'learning' not in next(line for line in before.splitlines() if 'saved context' in line)
        await agent.learn()
        after = await watch.report()
        line = next(line for line in after.splitlines() if 'saved context' in line)
        assert 'learning passes wait until then' in line

    asyncio.run(scenario())


def test_a_learning_pass_cancelled_while_reading_leaves_no_watch(learning, monkeypatch):  # noqa: F811
    """fri #335: a pass stopped by anything but its own failures (a cancellation here) skipped its watch's removal, and
    the traceback kept it registered."""
    async def scenario():
        agent, *_ = batch(learning, monkeypatch)
        channel = agent.fri.get_channel(1)

        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError

        channel._state.http.logs_from = cancelled
        with pytest.raises(asyncio.CancelledError) as stopped:
            await agent.learn()
        assert stopped.value.__traceback__ is not None and not agent.passes

    asyncio.run(scenario())


def test_an_archive_search_cancelled_while_reading_the_roster_leaves_no_watch(learning):  # noqa: F811
    """The same for an exploration pass, stopped while it reads the role's members."""
    from compat_tests.test_fri_exploration import archive
    clock, store, settings, scheduler = archive(learning)
    client = TestClient(create_app(settings))

    async def api(method, path, payload=None):
        return client.request(method, path, json=payload, headers=headers('fri')).json()

    async def http(route, params):
        raise asyncio.CancelledError

    async def scenario():
        channel = SimpleNamespace(id=1, name='memba-chat', guild=SimpleNamespace(id=123, me=object()))
        runtime = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, api=api, get_channel=lambda _: channel,
                                  get_user=lambda _: SimpleNamespace(send=AsyncMock()),
                                  http=SimpleNamespace(request=http), chat=SimpleNamespace(headers=AsyncMock()))
        agent = Agent(runtime)
        agent.state = scheduler.state()
        agent.record = AsyncMock()
        with pytest.raises(asyncio.CancelledError) as stopped:
            await agent.learn()
        assert stopped.value.__traceback__ is not None and not agent.passes

    asyncio.run(scenario())


def test_the_owners_learning_run_is_refused_with_why_while_the_queue_is_closed(monkeypatch, tmp_path):
    """Their own run asked Gene for a batch that the pass then couldn't send. It's refused before it asks, saying why."""
    from compat_tests.test_fri_actions import harness
    from fri.fri_actions import Actions

    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        closed(agent, tmp_path)
        overview = {'history_channels': [], 'reports': [], 'summaries': [], 'historical_notes': []}
        fri.api = AsyncMock(side_effect=lambda method, path, payload=None: overview if path.endswith('/overview') else {})
        try:
            result = await Actions(agent, 10, 999, False).execute({'kind': 'run_learning', 'channel_id': ''})
            assert 'withdrawals record' in result.get('blocked', '')
            assert not [c for c in fri.api.await_args_list if c.args[1] == '/agent/learning/request']
        finally:
            await fri.close()

    asyncio.run(scenario())
