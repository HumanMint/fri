"""A controller that stops offering learning-withdrawals under a running Fri is noticed (fri #351).

Fri read learning-withdrawals at its start and kept it. Rolled back under Fri (a Gene-only rollback), the controller's
contract no longer offers it and its endpoint answers 404, but nothing read the contract again: #350 only read it while
the capability was missing. So each scheduled pass claimed a batch, reserving that much of the day's scan budget, and
then got the 404, sending nothing and keeping the capability. Now the 404 drops it, and the next pass reads the
contract again before it claims, through the withdrawal queue's throttled reader, so it waits as #350's does.
"""
import asyncio

from compat_tests.test_fri_learning import learning  # noqa: F401 (the fixture)
from compat_tests.test_fri_panel_and_learning_fences import batch
from fri.fri_errors import ControllerRejected


def test_a_controller_dropping_learning_withdrawals_under_fri_stops_claims(learning, monkeypatch):  # noqa: F811
    async def scenario():
        agent, store, (first, second), finishes, asked, signing = batch(learning, monkeypatch)
        assert 'learning-withdrawals' in agent.fri.capabilities  # As Fri read it at its start.
        api, paths, rolled = agent.fri.api, [], [True]

        async def controller(method, path, payload=None):
            paths.append(path)
            if rolled and path.endswith('/withdrawn'):
                raise ControllerRejected('{"detail":"Not Found"}', 404)
            result = await api(method, path, payload)
            if rolled and path == '/fri/contract':
                return {**result, 'capabilities': [c for c in result['capabilities'] if c != 'learning-withdrawals']}
            return result
        agent.fri.api = controller
        await agent.learn()
        await agent.learn()  # A scheduler's cadence later.
        assert paths.count('/agent/learning/claim') == 1 and asked == []
        assert 'learning-withdrawals' not in agent.fri.capabilities and agent.learning_waits_for_gene
        rolled.clear()  # Upgraded again: once the reader may read it again, the next pass runs.
        agent.saved_turns.reread_at = float('-inf')
        await agent.learn()
        assert paths.count('/agent/learning/claim') == 2 and not agent.learning_waits_for_gene
    asyncio.run(scenario())
