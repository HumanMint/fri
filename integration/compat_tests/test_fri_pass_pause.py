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


def without_learning_withdrawals(agent):
    """A controller that doesn't say which messages it knows were withdrawn (before learning-withdrawals), as Fri
    learned at its start and as its contract says when read again. Returns the paths Fri asks for."""
    agent.fri.capabilities = agent.fri.capabilities - {'learning-withdrawals'}
    api, paths = agent.fri.api, []

    async def older(method, path, payload=None):
        paths.append(path)
        result = await api(method, path, payload)
        if path == '/fri/contract':
            return {**result, 'capabilities': [c for c in result['capabilities'] if c != 'learning-withdrawals']}
        return result

    agent.fri.api = older
    return paths


def test_a_scheduled_pass_asks_for_no_batch_where_gene_cant_say_what_was_withdrawn(learning, monkeypatch):  # noqa: F811
    """fri #346 item 2: on a controller without learning-withdrawals, each scheduled pass claimed its batch, reserving
    the day's allowance, read it and stopped at its request, so another claim was refused that day even once the
    controller could say. None is asked for while it can't."""
    async def scenario():
        agent, store, (first, second), finishes, asked, signing = batch(learning, monkeypatch, older=True)
        paths = without_learning_withdrawals(agent)
        await agent.learn()
        await agent.learn()
        assert '/agent/learning/claim' not in paths and finishes == [] and asked == [] and not agent.passes

    asyncio.run(scenario())


def test_learning_resumes_once_the_controller_says_what_was_withdrawn(learning, monkeypatch):  # noqa: F811
    """fri #346 item 3: Fri started against a controller without learning-withdrawals, which was then upgraded. Nothing
    read its contract again, so learning never resumed. While paused for it, a pass reads the contract again first,
    no more than the withdrawal queue's own reread allows (REREAD_SECONDS), and once it's offered, runs."""
    async def scenario():
        agent, store, (first, second), finishes, asked, signing = batch(learning, monkeypatch, older=True)
        if 'learning-withdrawals' not in (await agent.fri.api('GET', '/fri/contract'))['capabilities']:
            pytest.skip('needs a controller offering learning-withdrawals')
        api, paths = agent.fri.api, []

        async def calls(method, path, payload=None):
            paths.append(path)
            return await api(method, path, payload)

        agent.fri.api = calls
        await agent.learn()
        assert paths[:2] == ['/fri/contract', '/agent/learning/claim']
        assert [[record['id'] for record in data['messages']] for data in asked] == [[first, second]]

    asyncio.run(scenario())


def test_fri_health_says_learning_waits_for_the_controller(learning, monkeypatch, tmp_path):  # noqa: F811
    """fri #346 item 2: the pause said nothing in fri health. It names it once a pass was skipped for it."""
    from fri.fri_watch import Watch

    async def scenario():
        agent, *_ = batch(learning, monkeypatch, older=True)
        without_learning_withdrawals(agent)
        fri = agent.fri
        fri.data = tmp_path
        fri.agent, fri.guild_id, fri.owner_id = agent, 123, 10
        fri.get_guild, fri.is_ready = (lambda _: None), (lambda: True)
        fri.owner_reviews = fri.health = None
        fri.watching = None
        watch = Watch(fri)
        watch.probe = AsyncMock(return_value='unreachable')
        assert 'learning-withdrawals' not in await watch.report()
        await agent.learn()
        assert any('learning' in line and 'learning-withdrawals' in line for line in (await watch.report()).splitlines())

    asyncio.run(scenario())


def test_the_owners_learning_run_is_refused_with_why_where_gene_cant_say(monkeypatch, tmp_path):
    """fri #346 item 2: their own run on such a controller claimed a batch and ended in "ValueError: learning pass could
    not complete". It's refused before it asks, saying why, after the contract is read again."""
    from compat_tests.test_fri_actions import harness
    from fri.fri_actions import Actions

    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        overview = {'history_channels': [], 'reports': [], 'summaries': [], 'historical_notes': []}
        fri.api = AsyncMock(side_effect=lambda method, path, payload=None: overview if path.endswith('/overview') else {})
        fri.capabilities = fri.capabilities - {'learning-withdrawals'}
        try:
            result = await Actions(agent, 10, 999, False).execute({'kind': 'run_learning', 'channel_id': ''})
            assert 'learning-withdrawals' in result.get('blocked', '')
            assert not [c for c in fri.api.await_args_list if c.args[1] == '/agent/learning/request']
            assert [c for c in fri.api.await_args_list if c.args[1] == '/fri/contract']  # Read again first.
        finally:
            await fri.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('extra', [{}, []], ids=['an object', 'a list'])
def test_a_contract_read_again_with_a_capability_that_isnt_a_name_keeps_the_owners_run_refused(monkeypatch, tmp_path,
                                                                                               extra):
    """fri #350's Sol round 1: a contract read again that lists an object or a list among its capabilities passed
    compatible(), and building Fri's capabilities from it raised TypeError, out of the owner's run. A capability that
    isn't a name makes it a contract Fri doesn't run with: what Fri read before is kept, and the run is refused saying
    why."""
    from compat_tests.test_fri_actions import harness
    from fri.fri_actions import Actions
    from fri.fri_protocol import contract

    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        overview = {'history_channels': [], 'reports': [], 'summaries': [], 'historical_notes': []}
        said = {**contract(), 'capabilities': [c for c in contract()['capabilities'] if c != 'learning-withdrawals']
                + [extra]}

        def answer(method, path, payload=None):
            return overview if path.endswith('/overview') else said if path == '/fri/contract' else {}

        fri.api = AsyncMock(side_effect=answer)
        fri.capabilities = before = fri.capabilities - {'learning-withdrawals'}
        try:
            result = await Actions(agent, 10, 999, False).execute({'kind': 'run_learning', 'channel_id': ''})
            assert 'learning-withdrawals' in result.get('blocked', '')
            assert [c for c in fri.api.await_args_list if c.args[1] == '/fri/contract']  # Read again first.
            assert fri.capabilities == before
        finally:
            await fri.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('extra', [{}, []], ids=['an object', 'a list'])
def test_a_scheduled_pass_asks_for_no_batch_on_a_contract_with_a_capability_that_isnt_a_name(learning, monkeypatch,  # noqa: F811
                                                                                             extra):
    """The same contract read again before a scheduled pass: no TypeError, no batch asked for, Fri's capabilities as
    they were."""
    async def scenario():
        agent, store, (first, second), finishes, asked, signing = batch(learning, monkeypatch, older=True)
        paths = without_learning_withdrawals(agent)
        api = agent.fri.api

        async def malformed(method, path, payload=None):
            result = await api(method, path, payload)
            return {**result, 'capabilities': [*result['capabilities'], extra]} if path == '/fri/contract' else result

        agent.fri.api = malformed
        before = agent.fri.capabilities
        await agent.learn()
        assert '/fri/contract' in paths and '/agent/learning/claim' not in paths
        assert finishes == [] and asked == [] and agent.fri.capabilities == before

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
    from compat_tests.test_fri_exploration import archive, offered
    clock, store, settings, scheduler = archive(learning)
    client = TestClient(create_app(settings))
    offered(client)  # A pass asks for no batch from a controller without learning-withdrawals (fri #346).

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
